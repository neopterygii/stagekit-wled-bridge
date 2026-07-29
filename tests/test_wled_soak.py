"""Tests for the long-run WLED/bridge soak sampler.

The tool exists to answer one question — is the device drifting while the host
stays flat — so the tests cover the parts that would quietly give a wrong
answer: duration parsing, sampling both sides through a stubbed HTTP layer,
surviving an unreachable endpoint mid-run, and the analysis that flags counter
resets, trends, and the realtime latch.

Run: python -m pytest tests/test_wled_soak.py -v
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import wled_soak  # noqa: E402


INFO = {
    "uptime": 44085,
    "freeheap": 181156,
    "leds": {"fps": 0, "pwr": 220},
    "live": True,
    "lm": "DDP",
    "lip": "192.168.0.230",
    "wifi": {"rssi": -33, "signal": 100, "channel": 6},
    "ws": 1,
    "ndc": 2,
}

STATUS = {
    "connected": False,
    "paused": False,
    "cue": "NO_CUE",
    "packets_received": 75017,
    "packets_per_sec": 0.0,
    "ddp_frames_sent": 61114,
    "render": {
        "rendered": 2652023,
        "skipped": 0,
        "stalls": 1,
        "work_ms_max": 0.74,
        "gap_ms_avg": 16.67,
        "gap_ms_max": 17.45,
        "ddp": {"send_us_avg": 18.1, "send_us_max": 42.4, "send_errors": 0},
    },
}


@pytest.fixture
def stub_http(monkeypatch):
    """Route _get_json through a dict of url-substring -> payload-or-error."""
    routes = {}

    def fake_get(url, timeout=5.0):
        for needle, payload in routes.items():
            if needle in url:
                if isinstance(payload, Exception):
                    return None, str(payload)
                return payload, None
        return None, "URLError: no route"

    monkeypatch.setattr(wled_soak, "_get_json", fake_get)
    return routes


# --- duration parsing -------------------------------------------------------

@pytest.mark.parametrize("text,seconds", [
    ("90", 90.0), ("30s", 30.0), ("15m", 900.0), ("4h", 14400.0), ("1.5h", 5400.0),
])
def test_parse_duration(text, seconds):
    assert wled_soak.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "  ", "abc", "4d"])
def test_parse_duration_rejects_junk(text):
    with pytest.raises(ValueError):
        wled_soak.parse_duration(text)


# --- sampling ---------------------------------------------------------------

def test_sample_wled_pulls_the_fields_that_could_move(stub_http):
    stub_http["/json/info"] = INFO
    got = wled_soak.sample_wled("192.168.0.53")
    assert got["freeheap"] == 181156
    assert got["fps"] == 0
    assert got["rssi"] == -33
    assert got["live"] is True and got["lm"] == "DDP"
    assert "error" not in got


def test_sample_bridge_flattens_nested_render_stats(stub_http):
    stub_http["/api/status"] = STATUS
    got = wled_soak.sample_bridge("192.168.0.230:36180")
    assert got["ddp_frames_sent"] == 61114
    assert got["render_gap_ms_max"] == 17.45
    assert got["ddp_send_us_max"] == 42.4
    assert got["send_errors"] == 0


def test_samples_carry_the_http_round_trip(stub_http):
    # An ESP32 serves HTTP from the same loop as the strip, so a slow reply is
    # a signal about the device, not just about the network.
    stub_http["/json/info"] = INFO
    stub_http["/api/status"] = STATUS
    monkey = wled_soak._get_json

    def timed(url, timeout=5.0):
        wled_soak._last_http_ms = 12.5
        data, err = monkey(url, timeout)
        wled_soak._last_http_ms = 12.5
        return data, err

    wled_soak._get_json = timed
    try:
        assert wled_soak.sample_wled("h")["http_ms"] == 12.5
        assert wled_soak.sample_bridge("h")["http_ms"] == 12.5
    finally:
        wled_soak._get_json = monkey


def test_an_unreachable_side_records_a_reason_rather_than_raising(stub_http):
    stub_http["/json/info"] = OSError("host unreachable")
    assert "error" in wled_soak.sample_wled("192.168.0.53")
    assert "error" in wled_soak.sample_bridge("nope:1")


def test_log_writes_one_flushed_jsonl_row_per_sample(tmp_path, stub_http):
    stub_http["/json/info"] = INFO
    stub_http["/api/status"] = STATUS
    out = tmp_path / "soak.jsonl"

    written = wled_soak.run_log("w", "b", str(out), interval=0.01,
                                duration=0.0, quiet=True)

    assert written == 1
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["wled"]["freeheap"] == 181156
    assert rows[0]["bridge"]["ddp_frames_sent"] == 61114
    assert "wall" in rows[0] and rows[0]["t"] == pytest.approx(0.0, abs=1.0)


def test_log_appends_so_a_resumed_soak_keeps_earlier_samples(tmp_path, stub_http):
    stub_http["/json/info"] = INFO
    stub_http["/api/status"] = STATUS
    out = tmp_path / "soak.jsonl"

    wled_soak.run_log("w", "b", str(out), 0.01, 0.0, quiet=True)
    wled_soak.run_log("w", "b", str(out), 0.01, 0.0, quiet=True)

    assert len(out.read_text().strip().splitlines()) == 2


def test_log_rejects_a_non_positive_interval(tmp_path, stub_http):
    with pytest.raises(ValueError):
        wled_soak.run_log("w", "b", str(tmp_path / "s.jsonl"), 0, None, quiet=True)


# --- analysis ---------------------------------------------------------------

def _write(tmp_path, rows):
    path = tmp_path / "soak.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


def _row(t, *, heap=180000, frames=1000, uptime=1000, live=False, **bridge):
    return {
        "t": t,
        "wall": "2026-07-28T09:00:00+00:00",
        "wled": {"freeheap": heap, "fps": 60, "rssi": -33, "uptime": uptime,
                 "live": live, "lm": "DDP" if live else ""},
        "bridge": dict({"ddp_frames_sent": frames, "rendered": frames,
                        "stalls": 0, "skipped": 0, "send_errors": 0,
                        "render_gap_ms_max": 17.0, "ddp_send_us_max": 40.0},
                       **bridge),
    }


def test_read_soak_rejects_empty_and_malformed_logs(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    with pytest.raises(wled_soak.SoakError):
        wled_soak.read_soak(str(empty))

    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json}\n")
    with pytest.raises(wled_soak.SoakError):
        wled_soak.read_soak(str(bad))

    with pytest.raises(wled_soak.SoakError):
        wled_soak.read_soak(str(tmp_path / "missing.jsonl"))


def test_analyse_reports_a_falling_heap_as_a_negative_slope(tmp_path):
    rows = [_row(t * 3600, heap=180000 - t * 5000, uptime=1000 + t * 3600,
                 frames=1000 + t * 216000) for t in range(4)]
    report = "\n".join(wled_soak.analyse(_write(tmp_path, rows)))
    assert "free heap" in report
    assert "-5000.0/h" in report


def test_analyse_flags_a_counter_reset_because_it_voids_deltas(tmp_path):
    rows = [_row(0, uptime=5000, frames=9000), _row(60, uptime=12, frames=30)]
    report = "\n".join(wled_soak.analyse(_write(tmp_path, rows)))
    assert "counter went backwards" in report
    assert "wled.uptime" in report


def test_analyse_flags_the_realtime_latch(tmp_path):
    # Device says live, bridge sent not one frame between the two samples.
    rows = [_row(0, live=True, frames=61114), _row(60, live=True, frames=61114)]
    report = "\n".join(wled_soak.analyse(_write(tmp_path, rows)))
    assert "realtime latch" in report


def test_analyse_stays_quiet_about_the_latch_while_frames_flow(tmp_path):
    rows = [_row(0, live=True, frames=1000), _row(60, live=True, frames=4600)]
    report = "\n".join(wled_soak.analyse(_write(tmp_path, rows)))
    assert "realtime latch" not in report
    assert "60.0/s" in report  # 3600 frames over 60 s


def test_analyse_calls_out_errors_and_stalls_gained_mid_run(tmp_path):
    rows = [_row(0, send_errors=0, stalls=1), _row(60, send_errors=3, stalls=4)]
    report = "\n".join(wled_soak.analyse(_write(tmp_path, rows)))
    assert "DDP send errors gained during the run: 3" in report
    assert "render stalls gained during the run: 3" in report


def test_analyse_counts_unreachable_samples(tmp_path):
    rows = [_row(0), {"t": 60, "wall": "x", "wled": {"error": "boom"},
                      "bridge": {"error": "boom"}}]
    report = "\n".join(wled_soak.analyse(_write(tmp_path, rows)))
    assert "wled unreachable in 1/2" in report
    assert "bridge unreachable in 1/2" in report


def test_analyse_survives_a_run_where_one_side_was_never_reachable(tmp_path):
    rows = [{"t": t, "wall": "x", "wled": {"error": "boom"},
             "bridge": {"ddp_frames_sent": 10, "rendered": 10}} for t in (0, 60)]
    report = "\n".join(wled_soak.analyse(_write(tmp_path, rows)))
    assert "free heap (bytes)" in report and "no data" in report


def test_cli_analyse_returns_nonzero_on_an_unreadable_log(tmp_path, capsys):
    assert wled_soak.main(["analyse", str(tmp_path / "nope.jsonl")]) == 1
    assert "cannot analyse" in capsys.readouterr().err


def test_cli_rejects_a_bad_duration(tmp_path, capsys):
    rc = wled_soak.main(["log", "--out", str(tmp_path / "s.jsonl"),
                         "--duration", "4 fortnights"])
    assert rc == 2
    assert "bad --duration" in capsys.readouterr().err


# --- transport vs firmware discriminator ------------------------------------

def _row2(t, *, http, tcp, rssi=-53, **kw):
    r = _row(t, **kw)
    r["wled"]["http_ms"] = http
    r["wled"]["tcp_ms"] = tcp
    r["wled"]["rssi"] = rssi
    return r


def test_tcp_rtt_returns_none_when_the_connect_fails():
    # Port 1 on localhost: refused fast, no network dependency.
    assert wled_soak.tcp_rtt_ms("127.0.0.1", port=1, timeout=0.5) is None


def test_network_implicated_when_tcp_rises_with_http(tmp_path):
    rows = [_row2(i * 30, http=40, tcp=8) for i in range(18)]
    rows += [_row2((18 + i) * 30, http=600, tcp=400) for i in range(2)]
    report = "\n".join(wled_soak.analyse(_write(tmp_path, rows)))
    assert "network path is implicated" in report
    assert "wired board would likely help" in report


def test_firmware_implicated_when_tcp_stays_flat(tmp_path):
    rows = [_row2(i * 30, http=40, tcp=8) for i in range(18)]
    rows += [_row2((18 + i) * 30, http=600, tcp=9) for i in range(2)]
    report = "\n".join(wled_soak.analyse(_write(tmp_path, rows)))
    assert "main loop is blocking" in report
    assert "would NOT fix this on its own" in report


def test_transport_section_always_warns_that_rssi_cannot_see_congestion(tmp_path):
    rows = [_row2(i * 30, http=40, tcp=8) for i in range(20)]
    report = "\n".join(wled_soak.analyse(_write(tmp_path, rows)))
    assert "cannot see channel congestion" in report


def test_transport_section_says_so_when_there_is_not_enough_data(tmp_path):
    rows = [_row2(i * 30, http=40, tcp=8) for i in range(4)]
    report = "\n".join(wled_soak.analyse(_write(tmp_path, rows)))
    assert "not enough paired TCP/HTTP samples" in report
