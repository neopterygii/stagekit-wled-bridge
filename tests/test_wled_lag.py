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
from tools.wled_peek import PeekError  # noqa: E402


class FakeView:
    """Stands in for a tools.wled_peek reader. *read* returns (value, t)."""

    source = "/json/live"
    prescaled = True
    pushed = False

    def __init__(self, read, history=None):
        self.read = read
        self._history = history or []

    def brightness(self):
        return self.read()

    def latest(self):
        value, t = self.read()
        return b"", t

    def history(self, since=None):
        return [p for p in self._history if since is None or p[0] >= since]

    def cadence_ms(self):
        return None

    def close(self):
        pass


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


def test_bridge_brightness_sums_the_preview_strip(monkeypatch):
    monkeypatch.setattr(wled_lag, "_get",
                        lambda url, timeout=4.0: ({"preview": {"strip": [1, 2, 3, 4]}}, 2.0))
    assert wled_lag.bridge_brightness("h") == (10.0, 2.0)


def test_missing_bridge_preview_raises_lag_error(monkeypatch):
    monkeypatch.setattr(wled_lag, "_get", lambda url, timeout=4.0: ({}, 0.0))
    with pytest.raises(wled_lag.LagError, match="preview.strip"):
        wled_lag.bridge_brightness("h")


def test_cli_reports_failure_without_a_traceback(capsys, monkeypatch):
    monkeypatch.setattr(wled_lag, "open_live", lambda host, prefer="auto": FakeView(lambda: (0.0, 0.0)))
    monkeypatch.setattr(wled_lag, "collect",
                        lambda *a, **k: (_ for _ in ()).throw(wled_lag.LagError("nope")))
    assert wled_lag.main(["probe", "--seconds", "1"]) == 1
    assert "lag probe failed: nope" in capsys.readouterr().err


def test_cli_reports_an_unreadable_device_up_front(capsys, monkeypatch):
    def no_view(host, prefer="auto"):
        raise PeekError("no live view available from WLED: /json/live returned HTTP 501")
    monkeypatch.setattr(wled_lag, "open_live", no_view)
    assert wled_lag.main(["step", "--direct"]) == 1
    assert "HTTP 501" in capsys.readouterr().err


def test_collect_reads_a_pushed_view_from_its_history(monkeypatch):
    # The peek records every frame itself; only the bridge is polled, and the
    # device series comes back from the view's history on the same clock.
    clock = {"t": 100.0}

    def fake_bridge(addr):
        clock["t"] += 0.05
        return 1.0, clock["t"]

    view = FakeView(lambda: (0.0, clock["t"]),
                    history=[(99.0, 5.0), (100.5, 6.0), (101.0, 7.0)])
    view.pushed = True
    monkeypatch.setattr(wled_lag.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(wled_lag, "bridge_brightness", fake_bridge)
    bridge_pts, device_pts = wled_lag.collect(view, "b", seconds=1.0)
    assert len(bridge_pts) >= 19
    assert device_pts == [(0.5, 6.0), (1.0, 7.0)]  # pre-start frame dropped


def test_collect_aborts_when_the_peek_dies(monkeypatch):
    def dead():
        raise PeekError("no live frame for 2.0s")
    view = FakeView(dead)
    view.pushed = True
    monkeypatch.setattr(wled_lag, "bridge_brightness", lambda a: (1.0, 0.0))
    with pytest.raises(PeekError, match="no live frame"):
        wled_lag.collect(view, "b", seconds=30.0)


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

    def fake_device():
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
    monkeypatch.setattr(wled_lag.socket, "socket", lambda *a, **k: FakeSock())

    results = wled_lag.step_probe(FakeView(fake_device), "b", 36107, cycles=2, quiet=True)
    assert len(results) == 2
    lit = [r for r in results if r["cue"] == 12 and r["latency_ms"] is not None]
    assert lit, "the dark->bright transition should have been detected"
    assert lit[0]["latency_ms"] == pytest.approx(200, abs=60)


def test_step_probe_uses_fresh_dwells_so_a_period_cannot_masquerade(monkeypatch):
    # The whole point of the step test: dwell times must vary, or a periodic
    # artefact could line up with them.
    clock = {"t": 0.0}

    def fake_sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(wled_lag.time, "sleep", fake_sleep)
    monkeypatch.setattr(wled_lag.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(wled_lag.socket, "socket",
                        lambda *a, **k: type("S", (), {"sendto": lambda *_: None,
                                                       "close": lambda _: None})())
    results = wled_lag.step_probe(FakeView(lambda: (0.0, clock["t"])), "b", 36107,
                                  cycles=6, quiet=True)
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


def test_summarise_steps_names_the_read_failure_instead_of_blaming_the_bridge():
    # The regression this port fixes: on WLED 16 every read failed, and the
    # summary said the strip "never visibly reacted".
    steps = [{"step": i, "latency_ms": None,
              "read_error": "WLED is off (on:false or bri 0)"} for i in range(4)]
    text = "\n".join(wled_lag.summarise_steps(steps))
    assert "could not be read" in text
    assert "WLED is off" in text
    assert "never visibly reacted" not in text


def test_summarise_steps_states_the_peek_cadence_allowance():
    steps = [{"step": 0, "latency_ms": 40.0}]
    text = "\n".join(wled_lag.summarise_steps(steps, source="websocket peek",
                                               cadence_ms=42.0))
    assert "~42 ms" in text and "~21 ms on average" in text


class _FakeSender:
    def __init__(self, host):
        pass

    def send_pixels(self, data):
        pass

    def close(self):
        pass


def _direct_fixture(monkeypatch, power_on, fail=False):
    calls = []
    monkeypatch.setattr(wled_lag, "get_power", lambda host: power_on)
    monkeypatch.setattr(wled_lag, "set_power", lambda host, on: calls.append(on))
    monkeypatch.setattr(wled_lag, "realtime_source", lambda host: (False, "", ""))
    monkeypatch.setattr(wled_lag.time, "sleep", lambda s: None)
    import protocol.ddp_sender as ddp
    monkeypatch.setattr(ddp, "DDPSender", _FakeSender)
    if fail:
        monkeypatch.setattr(wled_lag, "_direct_steps",
                            lambda *a: (_ for _ in ()).throw(PeekError("boom")))
    else:
        monkeypatch.setattr(wled_lag, "_direct_steps", lambda *a: [])
    view = FakeView(lambda: (0.0, 0.0))
    view.prescaled = False          # a peek: reads black while off
    return view, calls


def test_direct_switches_an_off_device_on_and_restores_it(monkeypatch):
    view, calls = _direct_fixture(monkeypatch, power_on=False)
    wled_lag.step_probe_direct("w", view, quiet=True)
    assert calls == [True, False]


def test_direct_restores_power_even_when_the_run_fails(monkeypatch):
    view, calls = _direct_fixture(monkeypatch, power_on=False, fail=True)
    with pytest.raises(PeekError):
        wled_lag.step_probe_direct("w", view, quiet=True)
    assert calls == [True, False]


def test_direct_leaves_an_already_on_device_alone(monkeypatch):
    view, calls = _direct_fixture(monkeypatch, power_on=True)
    wled_lag.step_probe_direct("w", view, quiet=True)
    assert calls == []


def test_direct_refuses_while_another_sender_drives_wled(monkeypatch):
    # The bridge or YALCY's WLED output already writing the buffer would race
    # every step; refuse and name who it is rather than measure the race.
    view, calls = _direct_fixture(monkeypatch, power_on=True)
    monkeypatch.setattr(wled_lag, "realtime_source",
                        lambda host: (True, "DDP", "192.168.0.15"))
    with pytest.raises(wled_lag.LagError, match="DDP from 192.168.0.15"):
        wled_lag.step_probe_direct("w", view, quiet=True)
    assert calls == []
