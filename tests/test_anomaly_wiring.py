"""The bridge side of anomaly detection: render ring, status-loop hook, evidence, API.

The detectors themselves are covered in test_anomalies.py; this checks the plumbing that feeds
them inside the bridge, and that none of it can take the dashboard stream down.
"""

import asyncio
import json
import sys
from array import array
from collections import deque
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
from main import LEVEL_RING, RenderThread, _read_pids, wire_anomalies
from replay.capture import read_capture
from status_server import StatusServer, StatusTracker
from tests.test_anomalies import snap


class FakeRender:
    """Stands in for RenderThread: the two methods the hook reads, backed by the real code."""

    frame_levels_since = RenderThread.frame_levels_since
    last_frame = RenderThread.last_frame

    def __init__(self):
        self._levels = array('f', bytes(4 * LEVEL_RING))
        self._levels_n = 0
        self._last_sent = bytearray(360)

    def send(self, level):
        self._levels[self._levels_n % LEVEL_RING] = level
        self._levels_n += 1
        self._last_sent[:] = bytes([int(level)]) * 360


def test_level_ring_returns_new_frames_in_order_and_survives_wrap():
    r = FakeRender()
    for v in range(5):
        r.send(v)
    n, levels = r.frame_levels_since(0)
    assert (n, levels) == (5, [0, 1, 2, 3, 4])
    assert r.frame_levels_since(n) == (5, [])
    for v in range(1000):
        r.send(v % 200)
    n2, levels = r.frame_levels_since(n)
    assert n2 == 1005 and len(levels) == LEVEL_RING - 1  # a slow reader gets the newest, not junk
    assert levels[-1] == 999 % 200


def test_read_pids(tmp_path):
    (tmp_path / "pids.current").write_text("17\n")
    (tmp_path / "pids.max").write_text("2048\n")
    assert _read_pids(str(tmp_path)) == (17, 2048)
    (tmp_path / "pids.max").write_text("max\n")
    assert _read_pids(str(tmp_path)) is None
    assert _read_pids(str(tmp_path / "missing")) is None


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


def _wired(tmp_path, monkeypatch, clock=None):
    monkeypatch.setattr(main, "_bridge_ip_toward", lambda host: "192.168.0.230")
    monkeypatch.setattr(main, "_read_pids", lambda: None)
    tracker = StatusTracker()
    render = FakeRender()
    protocol = SimpleNamespace(recent=deque((i * 0.011, bytes([i % 256]) * 20) for i in range(300)))
    wled_api = SimpleNamespace(device_info=None)
    loop = asyncio.new_event_loop()
    log = wire_anomalies(tracker, render, protocol, wled_api, str(tmp_path / "captures"),
                         str(tmp_path / "anomalies"), loop, clock=clock or Clock())
    return tracker, render, log, loop


def test_hook_detects_from_frames_and_writes_replayable_evidence(tmp_path, monkeypatch):
    clock = Clock()
    tracker, render, log, loop = _wired(tmp_path, monkeypatch, clock)
    try:
        assert log.since(0)[0]["kind"] == "bridge_start"
        for i in range(60):  # 6 s of a lit cue whose frames are black
            clock.t += 0.1
            for _ in range(6):
                render.send(0.0)
            tracker.on_snapshot(snap(i / 10, cue="CHORUS", live=False) | {"output_live": True})
        loop.run_until_complete(asyncio.sleep(0.2))  # let the executor write
    finally:
        loop.close()
    ev = next(e for e in log.since(0) if e["kind"] == "black_strip")
    path = tmp_path / "captures" / ev["evidence_file"]
    header, packets = read_capture(path)
    assert header.extra["anomaly"]["kind"] == "black_strip"
    assert len(packets) == 300 and packets[0].t == 0.0
    statuses = path.with_suffix(".status.jsonl").read_text().splitlines()
    assert statuses and "settings" not in json.loads(statuses[-1])["snap"]
    day_log = list((tmp_path / "anomalies").glob("*.jsonl"))
    assert day_log and "black_strip" in day_log[0].read_text()


def test_broadcast_loop_survives_a_failing_hook():
    tracker = StatusTracker()

    def boom(data):
        raise RuntimeError("detector bug")

    tracker.on_snapshot = boom

    async def go():
        q = tracker.subscribe()
        task = asyncio.create_task(tracker.broadcast_loop())
        got = [await asyncio.wait_for(q.get(), 1.0) for _ in range(3)]
        task.cancel()
        return got

    got = asyncio.run(go())
    assert len(got) == 3 and tracker._snapshot_hook_errors >= 3


def test_api_anomalies_route(tmp_path, monkeypatch):
    tracker, render, log, loop = _wired(tmp_path, monkeypatch)
    loop.close()
    server = StatusServer(tracker)
    status, body = server._anomalies_response("/api/anomalies?since=0")
    data = json.loads(body)
    assert status == 200 and data["events"][0]["kind"] == "bridge_start" and data["seq"] == 1
    status, body = server._anomalies_response("/api/anomalies?since=1")
    assert json.loads(body)["events"] == []
    assert server._anomalies_response("/api/anomalies?since=x")[0] == 400
    assert "anomalies" in tracker.snapshot()

    tracker.anomalies = None  # detection switched off: the watcher must learn to run its own
    assert StatusServer(tracker)._anomalies_response("/api/anomalies")[0] == 404
    assert "anomalies" not in tracker.snapshot()


def test_beats_received_counts_only_real_beats():
    tracker = StatusTracker()
    for b in (0, 1, 0, 0, 2, 0):
        tracker.on_beat(b)
    assert tracker.snapshot()["beats_received"] == 2
