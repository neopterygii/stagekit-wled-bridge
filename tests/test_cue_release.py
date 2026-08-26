"""Tests for releasing the cue a departed sender left latched.

Operator report on 2026-08-25: hours after YARG had quit and the strip had
powered itself off, the dashboard still read `DEFAULT` with the blue zone lit
and the HELD pill up.

Holding a cue between packets is deliberate — YARG streams at ~88 Hz, and going
dark on one dropped datagram would be far worse than briefly showing a stale
one. But the hold was unbounded, because nothing off the packet path ever
cleared it. This wires the release to the moment the bridge already concedes
the sender is gone: the watchdog transition that stops it driving the strip.

The trap these tests exist to hold shut is the re-arm. Three separate layers
change-gate the cue — the protocol's `_last_cue`, the engine's `_current_cue`,
and the dashboard's tracker — and releasing some but not all of them is worse
than releasing none: a song opening on DEFAULT would then compare equal against
a stale `_last_cue`, never reach the engine, and play to a dark strip.

Run: python -m pytest tests/test_cue_release.py -v
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as bridge_main  # noqa: E402
from main import DeviceMode, WLEDPowerManager  # noqa: E402
from protocol.yarg_packet import CueByte, StrobeSpeed  # noqa: E402
from settings import BridgeSettings  # noqa: E402
from status_server import CONNECTED_TIMEOUT, StatusTracker  # noqa: E402

_NO_SETTINGS_FILE = "/proc/cue-release-tests-have-no-settings/settings.json"


class _Api:
    """WLEDApi stand-in — just enough for the mode transitions."""

    def __init__(self, on=False):
        self.on = on
        self.reachable = True
        self.wifi_info = {}
        self.effects = ["Solid", "Blink", "Breathe"]
        self.palettes = ["Default", "Party"]
        self.realtime_timeout_ms = 2500
        self.power_calls = []
        self.idle_calls = []

    def is_on(self):
        return self.on

    def fetch_wifi_info(self):
        return {}

    def fetch_capabilities(self):
        return True

    def set_power(self, on, transition_ms=None):
        self.power_calls.append(on)
        self.on = on
        return True

    def assert_live_baseline(self, led_count):
        return True

    def apply_idle_look(self, spec, led_count, transition_ms=700):
        self.idle_calls.append(dict(spec))
        return True


class _Engine:
    """Records the cue/strobe calls the protocol forwards."""

    bpm = 120.0

    def __init__(self):
        self.calls = []

    def on_cue(self, value):
        self.calls.append(("cue", value))

    def on_strobe(self, value):
        self.calls.append(("strobe", value))

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Sink:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _packet(cue=CueByte.DEFAULT, strobe=0):
    return SimpleNamespace(
        datagram_version=1, bpm=120.0,
        venue_size=0, lighting_cue=cue,
        strobe_state=strobe, beat=0, keyframe=0,
        guitar_notes=0, bass_notes=0, drum_notes=0, keys_notes=0,
        vocal_note=0.0, harmony0_note=0.0, harmony1_note=0.0,
        harmony2_note=0.0, spotlight=0, singalong=0, post_processing=0,
        fog_state=False, song_section=0, bonus_effect=False, paused=1,
        sp_active=False, sp_amount=0.0, sp_charge=0.0, sp_active_count=0,
        camera_cut_subject=0, camera_cut_priority=0, scene=0, auto_gen=False,
    )


def _settings(**idle):
    s = BridgeSettings(path=_NO_SETTINGS_FILE, warn_unwritable=False)
    if idle:
        s.update_idle(idle)
    return s


# ── what a release does ─────────────────────────────────────────────────


def test_release_clears_the_engine_and_the_dashboard():
    engine, tracker = _Engine(), StatusTracker()
    protocol = bridge_main.YARGProtocol(engine, tracker, _Sink())
    tracker.on_cue(CueByte.CHORUS)

    protocol.release_latched_state()

    assert engine.calls == [("cue", CueByte.NO_CUE), ("strobe", StrobeSpeed.OFF)]
    assert tracker.current_cue == CueByte.NO_CUE


def test_release_resets_every_change_detector():
    """All five go back to the -1 they were constructed with. No real byte can
    equal it, so the first packet of the next stream re-sends the whole set —
    the engine is rebuilt from the stream rather than from what survived."""
    protocol = bridge_main.YARGProtocol(_Engine(), _Sink(), _Sink())
    protocol._last_cue = CueByte.CHORUS
    protocol._last_strobe = 2
    protocol._last_camera_subject = 3
    protocol._last_venue_size = 1
    protocol._last_song_section = 5

    protocol.release_latched_state()

    assert protocol._last_cue == -1
    assert protocol._last_strobe == -1
    assert protocol._last_camera_subject == -1
    assert protocol._last_venue_size == -1
    assert protocol._last_song_section == -1


def test_the_same_cue_re_arms_after_a_release(monkeypatch):
    """The regression this whole change turns on.

    DEFAULT is the most common opening cue there is, so the realistic sequence
    is: a song ends on DEFAULT, the sender goes away, the watchdog releases, and
    the next song opens on DEFAULT again. If the release cleared the engine but
    left `_last_cue` holding DEFAULT, that third packet compares equal, never
    reaches `on_cue()`, and the song plays to a dark strip until some *other*
    cue happens along — a worse failure than the stale cue being fixed.
    """
    engine = _Engine()
    monkeypatch.setattr(bridge_main, "parse_packet", lambda _d: _packet())
    protocol = bridge_main.YARGProtocol(engine, _Sink(), _Sink())

    protocol.datagram_received(b"pkt", ("127.0.0.1", 36107))
    assert ("cue", CueByte.DEFAULT) in engine.calls

    # Same cue again while the stream is live: correctly gated out.
    engine.calls.clear()
    protocol.datagram_received(b"pkt", ("127.0.0.1", 36107))
    assert engine.calls == []

    protocol.release_latched_state()
    engine.calls.clear()

    # Sender returns on the same cue byte. It must reach the engine.
    protocol.datagram_received(b"pkt", ("127.0.0.1", 36107))
    assert ("cue", CueByte.DEFAULT) in engine.calls


# ── when the watchdog fires it ──────────────────────────────────────────


def test_going_idle_releases_the_latched_cue():
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=1800)
    released = []
    m.on_release = lambda: released.append(True)
    m._wled_on = True
    m._output_live = True

    m._enter_idle()

    assert released == [True]
    assert m.mode == DeviceMode.IDLE


def test_powering_off_an_already_idle_strip_does_not_release_again():
    """A release is only meaningful if there was something being driven. The
    idle→off transition half an hour later has nothing left to release, and
    firing there would re-release already-released state on every such hop."""
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=1800)
    released = []
    m.on_release = lambda: released.append(True)
    m._wled_on = True
    m._output_live = False  # already idle

    m._power_off()

    assert released == []
    assert m.mode == DeviceMode.OFF


def test_skipping_the_idle_stage_releases_exactly_once():
    """With the idle look set to "off", `_enter_idle` delegates to
    `_power_off`. Both close the gate, so a naive release in each would fire
    twice for one departure."""
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=1800, settings=_settings(mode="off"))
    released = []
    m.on_release = lambda: released.append(True)
    m._wled_on = True
    m._output_live = True

    m._enter_idle()

    assert released == [True]
    assert api.power_calls == [False]


def test_a_failing_release_still_leaves_the_strip_correctly():
    """The callback is a courtesy to the cue path, not part of owning the
    device. An exception escaping here would abandon the `to_thread` mode
    transition half-done and leave the strip powered on at full draw."""
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=1800)

    def boom():
        raise RuntimeError("cue engine exploded")

    m.on_release = boom
    m._wled_on = True
    m._output_live = True

    m._power_off()

    assert api.power_calls == [False]
    assert m.mode == DeviceMode.OFF


def test_no_callback_wired_is_not_an_error():
    """The manager is constructed before the protocol exists, and the replay
    and test harnesses never wire one at all."""
    m = WLEDPowerManager(_Api(on=True), idle_timeout=1800)
    m._wled_on = True
    m._output_live = True

    m._enter_idle()  # must not raise

    assert m.mode == DeviceMode.IDLE


# ── end to end: the dashboard stops apologising ─────────────────────────


def _run_fast_watchdog(manager, predicate, timeout=2.0):
    """Drive _watchdog_run() until predicate() holds, with its tick shortened.

    Same shape as the helper in test_status_truth.py: patches the instance's
    `_wait_tick` rather than `asyncio.sleep`, because the wait is an
    `asyncio.wait_for` on the wake event that a patched sleep would not shorten.
    """
    async def no_wait(_seconds):
        await asyncio.sleep(0)

    async def exercise():
        task = asyncio.create_task(manager._watchdog_run())
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            await asyncio.sleep(0)
        result = predicate()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return result

    manager._wait_tick = no_wait
    return asyncio.run(exercise())


def test_cue_held_clears_once_the_watchdog_gives_up_on_the_sender():
    """The operator-visible outcome, driven through the real watchdog loop:
    the sender goes quiet, the grace period lapses, and the dashboard stops
    reporting a cue it cannot honour."""
    engine, tracker = _Engine(), StatusTracker()
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=1800, settings=_settings())
    protocol = bridge_main.YARGProtocol(engine, tracker, m)
    m.on_release = protocol.release_latched_state

    # A song was playing and has just ended on CHORUS.
    tracker.on_cue(CueByte.CHORUS)
    tracker.on_packet()
    protocol._last_cue = CueByte.CHORUS
    m._wled_on = True
    m._output_live = True
    # Back-date both clocks past their respective timeouts: the sender is gone
    # as far as the dashboard is concerned, and quiet past the grace period as
    # far as the watchdog is concerned.
    tracker._last_packet_time -= CONNECTED_TIMEOUT + 1
    m._last_activity = time.monotonic() - (m._grace_seconds() + 1)

    assert tracker.cue_held  # the reported bug

    assert _run_fast_watchdog(m, lambda: not m._output_live)

    assert not tracker.cue_held
    assert tracker.current_cue == CueByte.NO_CUE
    assert protocol._last_cue == -1
