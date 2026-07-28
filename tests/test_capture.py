"""Tests for the datagram capture format, recorder, controller and API.

Covers the write/read round-trip, the on-disk format golden, tolerance of the
truncated tail a killed container leaves behind, the recorder's hot-path
contract (buffer only, never write), the auto-stop caps that keep a forgotten
capture from filling the appdata share, and the /api/capture handler.

Run: python -m pytest tests/test_capture.py -v
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protocol.yarg_packet import CueByte, parse_packet  # noqa: E402
from replay.capture import (  # noqa: E402
    CAPTURE_FORMAT, CAPTURE_VERSION, CaptureError, CaptureWriter,
    DatagramRecorder, iter_capture, read_capture, write_capture,
)
from replay.controller import CaptureController, _slug  # noqa: E402
from status_server import StatusServer, StatusTracker  # noqa: E402
from test_sender import build_packet  # noqa: E402

FORMAT_GOLDEN = Path(__file__).parent / "fixtures" / "format_v1_sample.jsonl"


class FakeClock:
    """Advances only when told to, so recorded timestamps are exact."""

    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


# ── format round-trip ───────────────────────────────────────────

def test_write_then_read_round_trips_bytes_and_timing(tmp_path):
    packets = [(0.0, b"\x01\x02"), (0.125, b"\xff" * 47), (1.5, bytes(range(60)))]
    path = write_capture(tmp_path / "c.jsonl", packets, note="hello")

    header, read = read_capture(path)
    assert header.format == CAPTURE_FORMAT
    assert header.version == CAPTURE_VERSION
    assert header.note == "hello"
    assert header.started  # ISO timestamp recorded
    assert [(p.t, p.data) for p in read] == packets


def test_header_carries_extra_metadata(tmp_path):
    path = write_capture(tmp_path / "c.jsonl", [(0.0, b"x" * 44)],
                         extra={"fixture": "demo", "venue_source": "midi-venue"})
    header, _ = read_capture(path)
    assert header.extra["fixture"] == "demo"
    assert header.extra["venue_source"] == "midi-venue"


def test_first_packet_is_always_at_zero(tmp_path):
    """Replay can start immediately rather than waiting out a lead-in."""
    clock = FakeClock(start=12345.678)
    rec = DatagramRecorder(tmp_path / "c.jsonl", clock=clock)
    rec.record(b"a" * 44)
    clock.advance(0.25)
    rec.record(b"b" * 44)
    rec.close()

    _, packets = read_capture(tmp_path / "c.jsonl")
    assert [p.t for p in packets] == [0.0, 0.25]


def test_one_json_object_per_line(tmp_path):
    path = write_capture(tmp_path / "c.jsonl", [(0.0, b"x" * 44)] * 5)
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 6  # header + 5 packets
    for line in lines:
        json.loads(line)  # every line stands alone


# ── the committed format golden ─────────────────────────────────

def test_committed_golden_still_reads():
    """Pins the on-disk format so a change to it is a deliberate act.

    If this fails, the reader and the format have diverged — bump
    CAPTURE_VERSION and handle the old shape rather than editing the golden.
    """
    header, packets = read_capture(FORMAT_GOLDEN)
    assert header.format == CAPTURE_FORMAT
    assert header.version == 1
    assert header.extra.get("synthetic") is True
    assert len(packets) == 9
    assert packets[0].t == 0.0

    pkt = parse_packet(packets[0].data)
    assert pkt is not None
    assert pkt.lighting_cue == CueByte.WARM_AUTOMATIC
    assert pkt.bpm == pytest.approx(120.0)


# ── damaged and wrong files ─────────────────────────────────────

def test_truncated_final_line_costs_one_packet_not_the_file(tmp_path):
    """What a container killed mid-write leaves behind."""
    path = write_capture(tmp_path / "c.jsonl", [(0.0, b"a" * 44), (0.1, b"b" * 44)])
    with path.open("a") as fh:
        fh.write('{"t":0.2,"d":"aGFsZg')  # cut off mid-line, no newline

    _, packets = read_capture(path)
    assert [p.data for p in packets] == [b"a" * 44, b"b" * 44]


def test_blank_lines_are_skipped(tmp_path):
    path = write_capture(tmp_path / "c.jsonl", [(0.0, b"a" * 44)])
    with path.open("a") as fh:
        fh.write("\n\n")
    _, packets = read_capture(path)
    assert len(packets) == 1


def test_rejects_a_file_that_is_not_a_capture(tmp_path):
    p = tmp_path / "nope.jsonl"
    p.write_text('{"hello":"world"}\n')
    with pytest.raises(CaptureError, match="not a yarg-capture"):
        read_capture(p)


def test_rejects_non_json_first_line(tmp_path):
    p = tmp_path / "nope.jsonl"
    p.write_text("this is not json\n")
    with pytest.raises(CaptureError, match="not a JSON header"):
        read_capture(p)


def test_rejects_empty_file(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    with pytest.raises(CaptureError, match="empty file"):
        read_capture(p)


def test_refuses_a_newer_format_version(tmp_path):
    """Better to refuse than to silently misread a future layout."""
    p = tmp_path / "future.jsonl"
    p.write_text(json.dumps({"format": CAPTURE_FORMAT,
                             "version": CAPTURE_VERSION + 5}) + "\n")
    with pytest.raises(CaptureError, match="newer than this reader"):
        read_capture(p)


def test_iter_capture_closes_the_file_when_drained(tmp_path):
    path = write_capture(tmp_path / "c.jsonl", [(0.0, b"a" * 44)])
    _, gen = iter_capture(path)
    list(gen)
    # A leaked handle would keep the file open; on Linux we can only check the
    # generator is finished, which is when the finally: close runs.
    with pytest.raises(StopIteration):
        next(gen)


# ── the recorder's hot-path contract ────────────────────────────

def test_record_buffers_and_does_not_write(tmp_path):
    """record() runs in the UDP callback ~90x/s: no I/O allowed."""
    path = tmp_path / "c.jsonl"
    rec = DatagramRecorder(path, clock=FakeClock())
    header_size = path.stat().st_size

    for _ in range(50):
        rec.record(b"x" * 44)

    assert path.stat().st_size == header_size  # nothing written yet
    assert rec.snapshot()["pending"] == 50

    assert rec.flush() == 50
    assert path.stat().st_size > header_size
    assert rec.snapshot()["pending"] == 0
    rec.close()


def test_flush_is_incremental(tmp_path):
    rec = DatagramRecorder(tmp_path / "c.jsonl", clock=FakeClock())
    rec.record(b"a" * 44)
    assert rec.flush() == 1
    assert rec.flush() == 0  # nothing new
    rec.record(b"b" * 44)
    assert rec.flush() == 1
    rec.close()
    _, packets = read_capture(tmp_path / "c.jsonl")
    assert len(packets) == 2


def test_close_flushes_the_buffered_tail(tmp_path):
    rec = DatagramRecorder(tmp_path / "c.jsonl", clock=FakeClock())
    for _ in range(7):
        rec.record(b"z" * 44)
    assert rec.close() == 7
    _, packets = read_capture(tmp_path / "c.jsonl")
    assert len(packets) == 7


def test_packet_cap_stops_recording(tmp_path):
    rec = DatagramRecorder(tmp_path / "c.jsonl", max_packets=10,
                           clock=FakeClock())
    for _ in range(25):
        rec.record(b"x" * 44)
    assert rec.recording is False
    assert rec.packets == 10          # stopped accepting at the cap
    assert "packet limit" in rec.limit_hit
    rec.close()


def test_byte_cap_stops_recording(tmp_path):
    rec = DatagramRecorder(tmp_path / "c.jsonl", max_bytes=500,
                           clock=FakeClock())
    for _ in range(40):
        rec.record(b"x" * 44)
    rec.flush()
    assert rec.recording is False
    assert "size limit" in rec.limit_hit
    rec.close()


def test_stopped_recorder_ignores_further_packets(tmp_path):
    rec = DatagramRecorder(tmp_path / "c.jsonl", max_packets=2,
                           clock=FakeClock())
    for _ in range(10):
        rec.record(b"x" * 44)
    rec.close()
    _, packets = read_capture(tmp_path / "c.jsonl")
    assert len(packets) == 2


# ── the controller ──────────────────────────────────────────────

def test_start_then_stop_writes_a_named_capture(tmp_path):
    ctl = CaptureController(tmp_path)
    ok, name = ctl.start("Living room test")
    assert ok
    assert name.endswith(".jsonl")
    assert "Living-room-test" in name
    assert ctl.recorder is not None

    ctl.recorder.record(build_packet(cue=CueByte.CHORUS))
    ok, msg = ctl.stop()
    assert ok and "1 datagrams" in msg
    assert ctl.recorder is None

    _, packets = read_capture(tmp_path / name)
    assert len(packets) == 1


def test_starting_twice_is_refused(tmp_path):
    ctl = CaptureController(tmp_path)
    assert ctl.start("one")[0]
    ok, msg = ctl.start("two")
    assert not ok
    assert "already recording" in msg
    ctl.stop()


def test_stopping_when_idle_is_refused(tmp_path):
    ctl = CaptureController(tmp_path)
    ok, msg = ctl.stop()
    assert not ok
    assert msg == "not recording"


def test_start_reports_failure_for_an_unwritable_directory():
    ctl = CaptureController("/proc/definitely-not-writable/captures")
    ok, msg = ctl.start("x")
    assert not ok
    assert "cannot write" in msg
    assert ctl.recorder is None
    assert ctl.last_error


def test_note_is_sanitised_for_the_filename():
    """An operator's note becomes part of a path, so it can't carry separators."""
    assert _slug("Bohemian Rhapsody") == "Bohemian-Rhapsody"
    assert _slug("../../etc/passwd") == "....etcpasswd"   # no separators survive
    assert _slug("weird\x00chars\n!") == "weirdchars"
    assert "/" not in _slug("a/b/c")
    assert _slug("") == ""
    assert len(_slug("x" * 200)) <= 40


def test_snapshot_shape_idle_and_recording(tmp_path):
    ctl = CaptureController(tmp_path)
    idle = ctl.snapshot()
    assert idle["recording"] is False
    assert idle["packets"] == 0

    ctl.start("note")
    ctl.recorder.record(b"x" * 44)
    live = ctl.snapshot()
    assert live["recording"] is True
    assert live["packets"] == 1
    assert live["file"].endswith(".jsonl")
    ctl.stop()

    after = ctl.snapshot()
    assert after["recording"] is False
    assert after["last_file"].endswith(".jsonl")


def test_list_captures_is_newest_first(tmp_path):
    ctl = CaptureController(tmp_path)
    import os
    import time as _t
    for i, name in enumerate(["a.jsonl", "b.jsonl", "c.jsonl"]):
        p = tmp_path / name
        write_capture(p, [(0.0, b"x" * 44)])
        os.utime(p, (_t.time() + i, _t.time() + i))
    names = [c["name"] for c in ctl.list_captures()]
    assert names == ["c.jsonl", "b.jsonl", "a.jsonl"]
    assert all(c["bytes"] > 0 for c in ctl.list_captures())


def test_list_captures_survives_a_missing_directory(tmp_path):
    assert CaptureController(tmp_path / "nope").list_captures() == []


def test_flush_loop_drains_and_auto_stops_at_the_cap(tmp_path):
    """The background task writes, and closes the file when a cap is hit."""
    ctl = CaptureController(tmp_path, max_packets=5)
    ctl.start("capped")
    rec = ctl.recorder
    for _ in range(12):
        rec.record(b"x" * 44)

    async def exercise():
        task = asyncio.create_task(ctl.flush_loop(interval=0.01))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if ctl.recorder is None:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())
    assert ctl.recorder is None          # auto-stopped
    _, packets = read_capture(rec.path)
    assert len(packets) == 5


def test_shutdown_closes_an_open_capture(tmp_path):
    ctl = CaptureController(tmp_path)
    ctl.start("interrupted")
    path = ctl.recorder.path
    ctl.recorder.record(b"x" * 44)
    ctl.shutdown()
    assert ctl.recorder is None
    _, packets = read_capture(path)
    assert len(packets) == 1


# ── the HTTP API ────────────────────────────────────────────────

def _server(tmp_path):
    return StatusServer(StatusTracker(), capture=CaptureController(tmp_path))


def test_api_capture_start_and_stop(tmp_path):
    srv = _server(tmp_path)
    status, msg = srv._handle_capture_action({"action": "start", "note": "demo"})
    assert status == 200 and "Recording to" in msg
    status, msg = srv._handle_capture_action({"action": "stop"})
    assert status == 200 and "Saved" in msg


def test_api_capture_conflicts_return_409(tmp_path):
    srv = _server(tmp_path)
    srv._handle_capture_action({"action": "start"})
    assert srv._handle_capture_action({"action": "start"})[0] == 409
    srv._handle_capture_action({"action": "stop"})
    assert srv._handle_capture_action({"action": "stop"})[0] == 409


def test_api_capture_rejects_bad_input(tmp_path):
    srv = _server(tmp_path)
    assert srv._handle_capture_action({})[0] == 400
    assert srv._handle_capture_action({"action": "explode"})[0] == 400
    assert srv._handle_capture_action({"action": "start", "note": 5})[0] == 400
    assert srv._handle_capture_action(
        {"action": "start", "note": "x" * 201})[0] == 400


def test_api_capture_without_a_controller_is_a_500():
    srv = StatusServer(StatusTracker())
    assert srv._handle_capture_action({"action": "start"})[0] == 500


def test_status_snapshot_carries_capture_state(tmp_path):
    """The dashboard/operator can see a recording is running."""
    ctl = CaptureController(tmp_path)
    tracker = StatusTracker()
    snap = tracker.snapshot()
    snap["capture"] = ctl.snapshot()
    assert snap["capture"]["recording"] is False
    ctl.start("live")
    snap["capture"] = ctl.snapshot()
    assert snap["capture"]["recording"] is True
    ctl.stop()
