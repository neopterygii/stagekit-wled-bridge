"""Dashboard test patterns must end on their own and yield to YARG.

A test pattern drives the same engine YARG does and counts as activity for the
idle timeout. Before 2026-11-16 it ran until someone pressed Stop — a pattern
left going in a closed tab lit the room indefinitely — and it could be started
over a live song, where its synthetic beats and cue mixed with YARG's.

Run: python -m pytest tests/test_test_patterns.py -v
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import status_server  # noqa: E402
from main import YARGProtocol  # noqa: E402
from protocol.yarg_packet import CueByte, KeyframeByte, StrobeSpeed  # noqa: E402
from status_server import (TEST_PATTERN_MAX_S, TEST_PATTERNS, StatusServer,  # noqa: E402
                           StatusTracker)

PATTERN = next(iter(TEST_PATTERNS))


class _Engine:
    def __init__(self):
        self.bpm = 0.0
        self.cues = []
        self.strobes = []
        self.beats = []
        self.keyframes = []

    def on_cue(self, value):
        self.cues.append(value)

    def on_strobe(self, value):
        self.strobes.append(value)

    def on_beat(self, value):
        self.beats.append(value)

    def on_keyframe(self, value):
        self.keyframes.append(value)

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Power:
    def __init__(self):
        self.ticks = 0

    def on_test_activity(self):
        self.ticks += 1

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _server(**kwargs):
    tracker = StatusTracker()
    engine = _Engine()
    clock = _Clock()
    server = StatusServer(tracker, engine=engine, clock=clock, **kwargs)
    return server, tracker, engine, clock


def _yarg_connects(tracker):
    tracker._last_packet_time = time.monotonic()
    assert tracker.connected


async def _spin(n=3):
    for _ in range(n):
        await asyncio.sleep(0)


def _fast_beats(monkeypatch):
    """Make the beat loop's sleep return at once, so a test steps it by hand."""
    real = asyncio.sleep

    async def sleep(_delay):
        await real(0)

    monkeypatch.setattr(status_server.asyncio, "sleep", sleep)


# ── time limit ──────────────────────────────────────────────────────────


def test_a_pattern_ends_itself_after_the_limit(monkeypatch):
    async def run():
        _fast_beats(monkeypatch)
        server, tracker, engine, clock = _server()
        assert server._handle_test_action({"action": "pattern", "pattern": PATTERN})[0] == 200
        await _spin()
        assert tracker.test_active

        clock.now += TEST_PATTERN_MAX_S - 1
        await _spin()
        assert tracker.test_active

        clock.now += 1
        await _spin()
        assert not tracker.test_active
        assert tracker.test_pattern == ""
        assert tracker.test_ended == "timeout"
        assert tracker.snapshot()["test_ended"] == "timeout"
        assert engine.cues[-1] == CueByte.NO_CUE
        assert engine.strobes[-1] == StrobeSpeed.OFF
        assert server._beat_task is None

        # The loop has really stopped: no more beats arrive.
        before = len(engine.beats)
        await _spin(10)
        assert len(engine.beats) == before

    asyncio.run(run())


def test_choosing_a_pattern_again_restarts_the_allowance(monkeypatch):
    async def run():
        _fast_beats(monkeypatch)
        server, tracker, _engine, clock = _server()
        server._handle_test_action({"action": "pattern", "pattern": PATTERN})
        clock.now += TEST_PATTERN_MAX_S - 1
        server._handle_test_action({"action": "pattern", "pattern": PATTERN})
        clock.now += TEST_PATTERN_MAX_S - 1
        await _spin()
        assert tracker.test_active
        server._handle_test_action({"action": "stop"})

    asyncio.run(run())


def test_a_bpm_change_does_not_extend_the_allowance(monkeypatch):
    async def run():
        _fast_beats(monkeypatch)
        server, tracker, _engine, clock = _server()
        server._handle_test_action({"action": "pattern", "pattern": PATTERN})
        clock.now += TEST_PATTERN_MAX_S - 1
        assert server._handle_test_action({"action": "bpm", "bpm": 90})[0] == 200
        clock.now += 1
        await _spin()
        assert not tracker.test_active
        assert tracker.test_ended == "timeout"

    asyncio.run(run())


def test_a_manual_stop_records_no_reason(monkeypatch):
    async def run():
        _fast_beats(monkeypatch)
        server, tracker, engine, _clock = _server()
        server._handle_test_action({"action": "pattern", "pattern": PATTERN})
        await _spin()
        assert server._handle_test_action({"action": "stop"}) == (200, "Stopped")
        assert not tracker.test_active
        assert tracker.test_ended == ""
        assert engine.cues[-1] == CueByte.NO_CUE

    asyncio.run(run())


def test_starting_a_pattern_clears_the_last_reason(monkeypatch):
    async def run():
        _fast_beats(monkeypatch)
        server, tracker, _engine, clock = _server()
        server._handle_test_action({"action": "pattern", "pattern": PATTERN})
        clock.now += TEST_PATTERN_MAX_S
        await _spin()
        assert tracker.test_ended == "timeout"
        server._handle_test_action({"action": "pattern", "pattern": PATTERN})
        assert tracker.test_ended == ""
        server._handle_test_action({"action": "stop"})

    asyncio.run(run())


# ── YARG has the engine ─────────────────────────────────────────────────


def test_test_controls_are_refused_while_yarg_is_connected():
    server, tracker, engine, _clock = _server()
    _yarg_connects(tracker)
    for body in ({"action": "pattern", "pattern": PATTERN},
                 {"action": "strobe", "level": "fast"},
                 {"action": "bpm", "bpm": 150}):
        status, message = server._handle_test_action(body)
        assert status == 409
        assert "YARG" in message
    assert not tracker.test_active
    assert engine.cues == [] and engine.strobes == [] and engine.bpm == 0.0
    assert server._beat_task is None


def test_stop_is_allowed_while_yarg_is_connected():
    server, tracker, _engine, _clock = _server()
    _yarg_connects(tracker)
    assert server._handle_test_action({"action": "stop"})[0] == 200


def test_a_running_pattern_ends_when_yarg_connects(monkeypatch):
    async def run():
        _fast_beats(monkeypatch)
        calls = []
        server, tracker, engine, _clock = _server(
            on_yarg_takeover=lambda: calls.append("takeover"))
        server._handle_test_action({"action": "pattern", "pattern": PATTERN})
        await _spin()
        cues_before = list(engine.cues)

        _yarg_connects(tracker)
        await _spin()
        assert not tracker.test_active
        assert tracker.test_ended == "yarg"
        assert calls == ["takeover"]
        # The takeover callback owns the engine reset; the server adds none.
        assert engine.cues == cues_before

        before = len(engine.beats)
        await _spin(10)
        assert len(engine.beats) == before

    asyncio.run(run())


def test_takeover_lets_yargs_cue_through_even_if_it_was_already_sent(monkeypatch):
    """The real handover: YARG's first datagrams land before the test notices."""
    async def run():
        _fast_beats(monkeypatch)
        tracker = StatusTracker()
        engine = _Engine()
        protocol = YARGProtocol(engine, tracker, _Power())
        server = StatusServer(tracker, engine=engine,
                              on_yarg_takeover=protocol.release_latched_state)
        server._handle_test_action({"action": "pattern", "pattern": PATTERN})
        await _spin()

        # YARG's cue reached the engine and the change detector before the
        # beat loop's next check.
        protocol._last_cue = CueByte.DEFAULT
        engine.cues.append(CueByte.DEFAULT)
        _yarg_connects(tracker)
        await _spin()

        assert not tracker.test_active
        assert engine.cues[-1] == CueByte.NO_CUE
        # The detector no longer matches, so YARG's next datagram re-sends it.
        assert protocol._last_cue == -1

    asyncio.run(run())


def test_without_a_takeover_callback_the_engine_is_still_cleared(monkeypatch):
    async def run():
        _fast_beats(monkeypatch)
        server, tracker, engine, _clock = _server()
        server._handle_test_action({"action": "pattern", "pattern": PATTERN})
        await _spin()
        _yarg_connects(tracker)
        await _spin()
        assert tracker.test_ended == "yarg"
        assert engine.cues[-1] == CueByte.NO_CUE

    asyncio.run(run())


# ── keyframes ───────────────────────────────────────────────────────────


def test_keyframes_fire_without_a_power_manager(monkeypatch):
    async def run():
        _fast_beats(monkeypatch)
        server, _tracker, engine, _clock = _server()
        assert server.wled_power is None
        server._handle_test_action({"action": "pattern", "pattern": PATTERN})
        await _spin(12)
        server._handle_test_action({"action": "stop"})
        assert engine.keyframes and set(engine.keyframes) == {KeyframeByte.NEXT}

    asyncio.run(run())


def test_each_beat_counts_as_activity_for_the_power_manager(monkeypatch):
    async def run():
        _fast_beats(monkeypatch)
        power = _Power()
        server, _tracker, engine, _clock = _server(wled_power=power)
        server._handle_test_action({"action": "pattern", "pattern": PATTERN})
        await _spin(8)
        server._handle_test_action({"action": "stop"})
        assert power.ticks >= len(engine.beats) > 0

    asyncio.run(run())


# ── dashboard ───────────────────────────────────────────────────────────


def test_the_dashboard_shows_a_refusal_and_an_automatic_stop():
    html = status_server.STATUS_HTML
    assert 'id="test-note"' in html
    assert "d.test_ended" in html
    assert "j.message" in html
