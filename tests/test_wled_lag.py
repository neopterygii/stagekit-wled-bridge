"""Tests for the bridge-to-strip lag probe.

The probe's whole job is to produce a number that is either trustworthy or
loudly not, so the tests drive synthetic series with a known lag and check both
that it is recovered and that flat or too-short input is refused rather than
answered with noise.

Run: python -m pytest tests/test_wled_lag.py -v
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import wled_lag  # noqa: E402


def _wave(duration=60.0, rate=15.0, lag=0.0, freq=2.0, scale=1.0, offset=0.0):
    """A sampled sine — stands in for the pulse of a beat-driven cue."""
    points, t = [], 0.0
    step = 1.0 / rate
    while t < duration:
        points.append((t, offset + scale * math.sin(2 * math.pi * freq * (t - lag))))
        t += step
    return points


@pytest.mark.parametrize("true_lag", [0.0, 0.08, 0.25, -0.12])
def test_recovers_a_known_lag(true_lag):
    bridge = _wave(freq=0.7)
    # Device sees the same animation, delayed, dimmer, and offset — exactly the
    # brightness scaling and downsample the real pair differs by.
    device = _wave(freq=0.7, lag=true_lag, scale=0.5, offset=100.0)
    result = wled_lag.estimate_lag(bridge, device, max_lag=1.0)
    assert result["lag_ms"] == pytest.approx(true_lag * 1000, abs=25)
    assert result["correlation"] > 0.9


def test_normalising_cancels_scale_and_offset():
    a = wled_lag._normalise([(0, 1.0), (1, 2.0), (2, 3.0)])
    b = wled_lag._normalise([(0, 110.0), (1, 120.0), (2, 130.0)])
    assert [round(v, 6) for _, v in a] == [round(v, 6) for _, v in b]


def test_a_flat_series_is_refused_rather_than_guessed_at():
    flat = [(i / 15, 500.0) for i in range(900)]
    with pytest.raises(wled_lag.LagError, match="perfectly flat"):
        wled_lag.estimate_lag(_wave(), flat, max_lag=1.0)


def test_too_few_samples_is_refused():
    with pytest.raises(wled_lag.LagError, match="not enough samples"):
        wled_lag.estimate_lag(_wave(duration=1.0), _wave(duration=1.0))


def test_too_short_a_window_for_the_lag_search_is_refused():
    with pytest.raises(wled_lag.LagError, match="too short"):
        wled_lag.estimate_lag(_wave(duration=8.0), _wave(duration=8.0), max_lag=2.0)


def test_describe_flags_an_untrustworthy_correlation():
    weak = {"lag_ms": 40.0, "correlation": 0.2, "bridge_samples": 100,
            "device_samples": 100, "window_s": 30.0}
    text = "\n".join(wled_lag.describe(weak))
    assert "do not trust this number" in text

    strong = dict(weak, correlation=0.95)
    text = "\n".join(wled_lag.describe(strong))
    assert "do not trust" not in text
    assert "positive = the strip is behind" in text


def test_device_brightness_sums_hex_triples(monkeypatch):
    monkeypatch.setattr(wled_lag, "_get",
                        lambda url, timeout=4.0: ({"leds": ["FF0000", "000102"]}, 1.0))
    total, t = wled_lag.device_brightness("h")
    assert total == 255 + (0 + 1 + 2)
    assert t == 1.0


def test_bridge_brightness_sums_the_preview_strip(monkeypatch):
    monkeypatch.setattr(wled_lag, "_get",
                        lambda url, timeout=4.0: ({"preview": {"strip": [1, 2, 3, 4]}}, 2.0))
    assert wled_lag.bridge_brightness("h") == (10.0, 2.0)


def test_missing_payloads_raise_lag_error(monkeypatch):
    monkeypatch.setattr(wled_lag, "_get", lambda url, timeout=4.0: ({}, 0.0))
    with pytest.raises(wled_lag.LagError, match="preview.strip"):
        wled_lag.bridge_brightness("h")
    with pytest.raises(wled_lag.LagError, match="JSONLIVE"):
        wled_lag.device_brightness("h")


def test_cli_reports_failure_without_a_traceback(capsys, monkeypatch):
    monkeypatch.setattr(wled_lag, "collect",
                        lambda *a, **k: (_ for _ in ()).throw(wled_lag.LagError("nope")))
    assert wled_lag.main(["probe", "--seconds", "1"]) == 1
    assert "lag probe failed: nope" in capsys.readouterr().err


# --- step response ----------------------------------------------------------
#
# The step probe exists because cross-correlation aliases against a periodic
# stimulus. These cover the parts that silently gave wrong answers when it was
# first written: a malformed packet (cue at the wrong offset) and detection
# against a stale reference.

def test_step_packet_puts_the_cue_where_the_bridge_reads_it():
    from protocol.yarg_packet import parse_packet
    pkt = wled_lag._yarg_packet(12)
    parsed = parse_packet(pkt)
    assert parsed is not None, "bridge could not parse the step packet"
    assert parsed.lighting_cue == 12


def test_step_packet_reads_as_in_game_not_menu():
    # buf[6] (scene) and buf[7] (paused) must be right or the bridge treats the
    # datagram as out-of-game and never drives a cue.
    from protocol.yarg_packet import parse_packet
    parsed = parse_packet(wled_lag._yarg_packet(8))
    assert parsed.scene == 1
    assert not parsed.paused


def test_step_probe_times_each_transition(monkeypatch):
    # Device brightness flips 200 ms after each cue change.
    state = {"cue": 8, "changed_at": 0.0}
    clock = {"t": 0.0}

    def fake_sleep(seconds):
        clock["t"] += seconds

    def fake_monotonic():
        return clock["t"]

    def fake_device(host):
        elapsed = clock["t"] - state["changed_at"]
        bright = state["cue"] == 12 and elapsed >= 0.2
        return (900.0 if bright else 0.0), clock["t"]

    class FakeSock:
        def sendto(self, data, addr):
            from protocol.yarg_packet import parse_packet
            cue = parse_packet(data).lighting_cue
            if cue != state["cue"]:
                state["cue"] = cue
                state["changed_at"] = clock["t"]

        def close(self):
            pass

    monkeypatch.setattr(wled_lag.time, "sleep", fake_sleep)
    monkeypatch.setattr(wled_lag.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(wled_lag, "device_brightness", fake_device)
    monkeypatch.setattr(wled_lag.socket, "socket", lambda *a, **k: FakeSock())

    results = wled_lag.step_probe("w", "b", 36107, cycles=2, quiet=True)
    assert len(results) == 2
    lit = [r for r in results if r["cue"] == 12 and r["latency_ms"] is not None]
    assert lit, "the dark->bright transition should have been detected"
    assert lit[0]["latency_ms"] == pytest.approx(200, abs=60)


def test_step_probe_uses_fresh_dwells_so_a_period_cannot_masquerade(monkeypatch):
    # The whole point of the step test: dwell times must vary, or a periodic
    # artefact could line up with them.
    monkeypatch.setattr(wled_lag, "device_brightness", lambda h: (0.0, 0.0))
    monkeypatch.setattr(wled_lag.time, "sleep", lambda s: None)
    monkeypatch.setattr(wled_lag.socket, "socket",
                        lambda *a, **k: type("S", (), {"sendto": lambda *_: None,
                                                       "close": lambda _: None})())
    results = wled_lag.step_probe("w", "b", 36107, cycles=6, quiet=True)
    dwells = {r["dwell_s"] for r in results}
    assert len(dwells) > 1, "dwell times were constant — the probe can alias"


def test_summarise_steps_reports_nothing_seen_rather_than_inventing_a_median():
    text = "\n".join(wled_lag.summarise_steps(
        [{"step": i, "latency_ms": None} for i in range(4)]))
    assert "never visibly reacted" in text


def test_summarise_steps_calls_out_a_visibly_late_transition():
    steps = [{"step": 0, "latency_ms": 120.0}, {"step": 1, "latency_ms": 900.0}]
    text = "\n".join(wled_lag.summarise_steps(steps))
    assert "visible hesitation" in text
    assert "n=2/2" in text
