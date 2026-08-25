"""Tests for the two state bugs: the bridge cached a belief about the world,
nothing refreshed it, and the dashboard reported the belief as fact.

Covers WLED power-state adoption (main.py) and held-cue/unsent-output labelling
(status_server.py).
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import WLEDPowerManager
from protocol.yarg_packet import CueByte
from status_server import CONNECTED_TIMEOUT, StatusServer, StatusTracker


class _Api:
    """WLEDApi stand-in whose is_on() answer is scriptable.

    `answers` is consumed one per call; the last value repeats once exhausted,
    so a test can set a steady state without counting watchdog ticks.
    """

    def __init__(self, answers=(True,)):
        self._answers = list(answers)
        self.reachable = True
        self.wifi_info = {}
        self.effects = ["Solid", "Blink", "Breathe"]
        self.palettes = ["Default", "Party"]
        self.realtime_timeout_ms = 2500
        self.power_calls = []
        self.baseline_calls = 0
        self.idle_calls = []
        self.is_on_calls = 0

    def is_on(self):
        self.is_on_calls += 1
        if len(self._answers) > 1:
            return self._answers.pop(0)
        return self._answers[0]

    def fetch_wifi_info(self):
        return {}

    def set_power(self, on, transition_ms=None):
        self.power_calls.append(on)
        return True

    def assert_live_baseline(self, led_count):
        self.baseline_calls += 1
        return True

    def apply_idle_look(self, spec, led_count, transition_ms=700):
        self.idle_calls.append(dict(spec))
        return True

    def fetch_capabilities(self):
        return True


# ── WLED power state adoption ───────────────────────────────────────────


def test_adopts_on_when_device_powered_up_externally():
    """A reboot or the WLED UI leaves the device on; the bridge must notice."""
    manager = WLEDPowerManager(_Api(), idle_timeout=1800)
    assert not manager.is_on

    manager._adopt_device_state(True)

    assert manager.is_on
    # Seeded, so the idle timer starts now rather than measuring from 0.0.
    assert manager._last_activity > 0


def test_adopting_on_grants_a_full_idle_timeout_not_one_tick():
    manager = WLEDPowerManager(_Api(), idle_timeout=1800)
    manager._adopt_device_state(True)

    elapsed = time.monotonic() - manager._last_activity
    assert elapsed < manager._idle_timeout


def test_repeated_on_adoption_does_not_refresh_the_idle_timer():
    """The trap: the probe repeats every 30 s.

    Seeding _last_activity on every adoption rather than only on the OFF->ON
    transition would push the idle deadline out forever, and the strip would
    never power off at all — a worse version of the bug being fixed.
    """
    manager = WLEDPowerManager(_Api(), idle_timeout=1800)
    manager._adopt_device_state(True)
    seeded_at = manager._last_activity

    # Pretend the timer has nearly expired, then let more probes land.
    manager._last_activity = seeded_at - 1799
    for _ in range(5):
        manager._adopt_device_state(True)

    assert manager._last_activity == seeded_at - 1799
    assert (time.monotonic() - manager._last_activity) >= 1799


def test_adopts_off_when_device_powered_down_externally():
    manager = WLEDPowerManager(_Api(), idle_timeout=1800)
    manager._wled_on = True

    manager._adopt_device_state(False)

    assert not manager.is_on
    # An OFF adoption must not touch the idle timer.
    assert manager._last_activity == 0.0


def test_adopting_off_lets_activity_queue_a_power_on():
    """The believed-ON/device-OFF case: a song must not play to a dark strip."""
    manager = WLEDPowerManager(_Api(), idle_timeout=1800)
    manager._wled_on = True
    manager._output_live = True

    # Before adoption, on_activity() sees that we are already driving the strip
    # and queues nothing. Note the gate is `_output_live`, not `_wled_on`: a
    # strip that is powered but showing its idle look still needs a transition
    # into live output when a song starts.
    manager.on_activity()
    assert not manager._power_on_pending

    manager._adopt_device_state(False)
    manager.on_activity()
    assert manager._power_on_pending


def test_adopting_off_lets_the_dark_while_active_warning_fire():
    """_check_dark_while_active() returns early while the bridge believes it is
    driving the strip, so it could not see the very case it was written for
    until OFF is adopted."""
    manager = WLEDPowerManager(_Api(), idle_timeout=1800)
    manager._wled_on = True
    manager._output_live = True
    manager._last_activity = time.monotonic()

    manager._check_dark_while_active()
    assert manager._dark_since == 0.0  # blind while the belief says ON

    manager._adopt_device_state(False)
    manager._check_dark_while_active()
    assert manager._dark_since > 0.0


def test_unreachable_device_does_not_change_the_belief():
    """is_on() returns None on a failed request — that is not an answer."""
    for believed in (True, False):
        manager = WLEDPowerManager(_Api(), idle_timeout=1800)
        manager._wled_on = believed
        manager._adopt_device_state(None)
        assert manager.is_on is believed


def test_pending_command_outranks_a_stale_probe_answer():
    """The probe blocks up to 3 s; a command queued during it is newer."""
    manager = WLEDPowerManager(_Api(), idle_timeout=1800)
    manager._wled_on = True
    manager._power_off_pending = True
    manager._adopt_device_state(True)
    assert manager._power_off_pending
    assert manager.is_on

    manager = WLEDPowerManager(_Api(), idle_timeout=1800)
    manager.on_activity()
    assert manager._power_on_pending
    manager._adopt_device_state(False)
    assert manager._power_on_pending
    assert not manager.is_on


def test_watchdog_adopts_device_state_at_startup():
    api = _Api(answers=[True])
    manager = WLEDPowerManager(api, idle_timeout=1800)
    assert _run_fast_watchdog(manager, lambda: manager.is_on)
    # Adopted, not commanded.
    assert api.power_calls == []


def _run_fast_watchdog(manager, predicate, timeout=2.0):
    """Drive _watchdog_run() until predicate() holds, with its tick shortened.

    The watchdog waits 5 s per tick and probes every 6th, so the wait has to go
    for the test to be quick. It patches `manager._wait_tick` rather than
    `asyncio.sleep` globally — the wait is an `asyncio.wait_for` on the wake
    event now, which a patched sleep would not shorten, and stubbing one
    instance beats reaching into the stdlib. The predicate is the *outcome*
    rather than the probe count: the probe runs on a worker thread, so counting
    calls would race the adoption that follows.
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


def test_watchdog_probe_is_no_longer_gated_on_the_cached_belief():
    """The periodic probe used to be skipped entirely while _wled_on was true,
    so the believed-ON/device-OFF divergence was never looked for.

    Startup adopts ON; the loop must then keep probing and adopt the later OFF.
    """
    api = _Api(answers=[True, False])
    manager = WLEDPowerManager(api, idle_timeout=1800)

    assert _run_fast_watchdog(
        manager, lambda: api.is_on_calls >= 2 and manager.is_on is False)


def test_disabled_power_management_still_adopts_device_state():
    """With IDLE_TIMEOUT=0 nothing powers the strip off, but the render thread
    still gates DDP on _wled_on, so the belief must stay accurate."""
    api = _Api(answers=[True, False])
    manager = WLEDPowerManager(api, idle_timeout=0)
    assert not manager._enabled

    assert _run_fast_watchdog(
        manager, lambda: api.is_on_calls >= 2 and manager.is_on is False)


# ── Held cue and unsent output ──────────────────────────────────────────


def test_cue_held_is_false_while_a_sender_is_live():
    t = StatusTracker()
    t.on_cue(CueByte.CHORUS)
    t.on_packet()
    assert t.connected
    assert not t.cue_held


def test_cue_held_is_true_once_the_sender_goes_away():
    t = StatusTracker()
    t.on_cue(CueByte.CHORUS)
    t.on_packet()
    # Back-date the last packet past CONNECTED_TIMEOUT.
    t._last_packet_time -= CONNECTED_TIMEOUT + 1
    assert not t.connected
    assert t.cue_held
    assert t.snapshot()["cue_held"] is True


def test_cue_held_is_false_during_a_test_pattern():
    """A dashboard-driven test pattern is live output, not a stale cue."""
    t = StatusTracker()
    t.on_cue(CueByte.CHORUS)
    t.test_active = True
    assert not t.cue_held


def test_no_cue_is_not_reported_as_held():
    t = StatusTracker()
    assert t.current_cue == CueByte.NO_CUE
    assert not t.connected
    assert not t.cue_held


def test_output_live_follows_whether_the_frame_was_sent():
    t = StatusTracker()
    assert not t.output_live  # nothing rendered yet

    t.on_render([0, 0, 0, 0], 0.0, 120.0, ddp_sent=True)
    assert t.output_live
    assert t.snapshot()["output_live"] is True

    # WLED powered off — the render thread keeps ticking, frames are not sent.
    t.on_render([0, 0, 0, 0], 0.0, 120.0, ddp_sent=False)
    assert not t.output_live
    assert t.snapshot()["output_live"] is False


def test_output_live_is_false_when_the_render_thread_died():
    """A dead render thread cannot update the flag, so the last value would
    otherwise be reported forever — the same staleness this fix is about."""
    t = StatusTracker()
    t.on_render([0, 0, 0, 0], 0.0, 120.0, ddp_sent=True)
    assert t.output_live

    t.render_thread = SimpleNamespace(
        failed=True,
        render_stats=lambda: {"fps": 40},
        preview_snapshot=lambda: None,
    )
    assert not t.output_live


def test_held_cue_and_unsent_output_are_independent():
    """Disconnected with WLED still on, and connected with WLED off, are
    different failures and must be labelled separately."""
    t = StatusTracker()
    t.on_cue(CueByte.CHORUS)
    t.on_packet()
    t.on_render([0, 0, 0, 0], 0.0, 120.0, ddp_sent=False)
    assert not t.cue_held and not t.output_live

    t = StatusTracker()
    t.on_cue(CueByte.CHORUS)
    t.on_packet()
    t._last_packet_time -= CONNECTED_TIMEOUT + 1
    t.on_render([0, 0, 0, 0], 0.0, 120.0, ddp_sent=True)
    assert t.cue_held and t.output_live


# ── /api/status reports the power and settings blocks ───────────────────


def test_api_status_includes_power_and_settings_blocks():
    """The blocks existed only on the SSE stream, which made the documented
    triage step (compare /api/status against the device) impossible."""
    tracker = StatusTracker()
    power = WLEDPowerManager(_Api(), idle_timeout=1800)
    settings = SimpleNamespace(snapshot=lambda: {"brightness": 200})
    server = StatusServer(tracker, wled_power=power, settings=settings)

    snap = tracker.snapshot(wled_power=server.wled_power, settings=server.settings)

    assert "wled_power" in snap
    assert snap["wled_power"]["timeout"] == 1800
    assert snap["settings"]["brightness"] == 200
