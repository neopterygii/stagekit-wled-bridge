"""Tests for strip ownership: the mode machine, and a preview that shows the
strip rather than the render buffer.

Two operator reports on 2026-08-25 drove this:

  1. the dashboard showed a moving strip while YARG was disconnected and WLED
     was off — the preview was a picture of the render buffer, which kept being
     composed at 60 fps with nowhere to go;
  2. what the strip did once the bridge stopped sending was undefined — whatever
     effect, colour and master brightness the WLED app, a preset or a reboot had
     last left on it.

Both are the same missing idea: the bridge owns the strips it drives, so every
state it can be in has to be one the bridge chose.

Run: python -m pytest tests/test_wled_ownership.py -v
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LED_COUNT  # noqa: E402
from effects.cue_engine import CueEngine  # noqa: E402
from effects.mapper import LEDMapper  # noqa: E402
from main import DeviceMode, RenderThread, WLEDPowerManager  # noqa: E402
from protocol.yarg_packet import CueByte  # noqa: E402
from settings import DEFAULT_IDLE, BridgeSettings  # noqa: E402
from status_server import StatusTracker  # noqa: E402

_NO_SETTINGS_FILE = "/proc/ownership-tests-have-no-settings/settings.json"


class _Api:
    """WLEDApi stand-in that records every assertion made against the device."""

    def __init__(self, on=False, baseline_ok=True, idle_ok=True):
        self.on = on
        self.reachable = True
        self.wifi_info = {}
        self.effects = ["Solid", "Blink", "Breathe"]
        self.palettes = ["Default", "Party"]
        self.realtime_timeout_ms = 2500
        self.baseline_ok = baseline_ok
        self.idle_ok = idle_ok
        self.power_calls = []
        self.baseline_calls = []
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
        self.baseline_calls.append(led_count)
        return self.baseline_ok

    def apply_idle_look(self, spec, led_count, transition_ms=700):
        self.idle_calls.append(dict(spec))
        return self.idle_ok


def _settings(**idle):
    s = BridgeSettings(path=_NO_SETTINGS_FILE, warn_unwritable=False)
    if idle:
        s.update_idle(idle)
    return s


# ── modes ───────────────────────────────────────────────────────────────


def test_mode_composes_the_two_beliefs():
    """`mode` is derived, not stored: power and output answer different
    questions and the world can change one without the other."""
    m = WLEDPowerManager(_Api(), idle_timeout=1800)
    assert m.mode == DeviceMode.OFF

    m._wled_on = True
    assert m.mode == DeviceMode.IDLE  # powered and owned, but not driven

    m._output_live = True
    assert m.mode == DeviceMode.LIVE


def test_entering_live_asserts_the_baseline_before_opening_the_gate():
    """Frames must never precede the baseline, or they land on a device whose
    master brightness and segment geometry are unknown."""
    api = _Api(on=False)
    m = WLEDPowerManager(api, idle_timeout=1800, led_count=LED_COUNT)

    m._enter_live()

    assert api.power_calls == [True]
    assert api.baseline_calls == [LED_COUNT]
    assert m.output_enabled
    assert m.mode == DeviceMode.LIVE


def test_a_powered_strip_still_gets_the_baseline():
    """Powered is not owned. The WLED app may have changed anything while we
    were idle, so the transition into live output re-asserts regardless."""
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=1800)
    m._wled_on = True

    m._enter_live()

    assert api.power_calls == []      # already on — nothing to command
    assert len(api.baseline_calls) == 1
    assert m.output_enabled


def test_a_failed_baseline_keeps_the_gate_shut():
    """Sending anyway would paint a look that is wrong in a way nobody can read
    off the strip. Better to send nothing and let the dark-while-active warning
    report it."""
    api = _Api(on=True, baseline_ok=False)
    m = WLEDPowerManager(api, idle_timeout=1800)
    m._wled_on = True

    m._enter_live()

    assert not m.output_enabled
    assert m.mode == DeviceMode.IDLE


def test_entering_idle_closes_the_gate_before_pushing_the_look():
    """Pushing while still sending would leave the two fighting until the
    device's realtime timeout lapses."""
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=1800, settings=_settings())
    m._wled_on = True
    m._output_live = True

    m._enter_idle()

    assert not m.output_enabled
    assert m.mode == DeviceMode.IDLE
    assert m.is_on                      # idle is powered — it is a look, not a shutdown
    assert len(api.idle_calls) == 1
    assert api.idle_calls[0]["mode"] == "preset"


def test_idle_mode_off_powers_down_at_the_grace_period():
    """The operator can opt out of the idle stage entirely."""
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=1800, settings=_settings(mode="off"))
    m._wled_on = True
    m._output_live = True

    m._enter_idle()

    assert api.idle_calls == []
    assert api.power_calls == [False]
    assert m.mode == DeviceMode.OFF


def test_idle_mode_black_is_still_a_defined_look():
    """Black is a choice, not an absence: the strip stays powered and owned."""
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=1800, settings=_settings(mode="black"))
    m._wled_on = True
    m._output_live = True

    m._enter_idle()

    assert m.is_on
    assert api.idle_calls[0]["mode"] == "black"


def test_activity_from_idle_queues_a_transition_to_live():
    """The gate is output, not power — an idle strip is powered but not driven,
    and a song starting has to reclaim it."""
    m = WLEDPowerManager(_Api(on=True), idle_timeout=1800)
    m._wled_on = True          # idle: powered, running the device's own look

    m.on_activity()

    assert m._power_on_pending
    assert m._wake.is_set()    # the watchdog's tick is cut short, not waited out


def test_adopting_an_external_power_on_claims_the_strip():
    """Ownership means a strip someone else powered up gets our idle look, not
    whatever preset the WLED app left on it."""
    m = WLEDPowerManager(_Api(on=True), idle_timeout=1800, settings=_settings())

    m._adopt_device_state(True)

    assert m.mode == DeviceMode.IDLE
    assert m._idle_push_pending
    # Queued, not sent: adoption runs on the event loop and must not block on
    # a 3 s HTTP call.
    assert not m._output_live


def test_adopting_a_power_off_clears_the_queued_look():
    m = WLEDPowerManager(_Api(), idle_timeout=1800, settings=_settings())
    m._wled_on = True
    m._idle_push_pending = True

    m._adopt_device_state(False)

    assert m.mode == DeviceMode.OFF
    assert not m._idle_push_pending


def test_never_seen_activity_reads_as_infinitely_quiet():
    """An owned strip that has never had a packet still gets a defined look;
    treating "never" as zero elapsed would hold NO_CUE on it forever."""
    m = WLEDPowerManager(_Api(), idle_timeout=1800)
    assert m._quiet_for() == float("inf")

    m._last_activity = time.monotonic()
    assert m._quiet_for() < 1.0


# ── the watchdog drives the transitions ─────────────────────────────────


def _run_watchdog(manager, predicate, timeout=2.0):
    """Drive _watchdog_run() until predicate() holds, with its tick shortened."""

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


def test_grace_expiry_falls_back_to_the_idle_look():
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=1800, settings=_settings(grace_seconds=0))
    m._wled_on = True
    m._output_live = True
    m._last_activity = time.monotonic()

    assert _run_watchdog(m, lambda: bool(api.idle_calls))
    assert not m.output_enabled
    assert m.is_on


def test_the_idle_look_applies_even_with_power_management_disabled():
    """IDLE_TIMEOUT=0 means the strip never powers off. It does not mean the
    strip is unowned — handing it a defined look is ownership, not power
    saving."""
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=0, settings=_settings(grace_seconds=0))
    m._wled_on = True
    m._output_live = True
    m._last_activity = time.monotonic()

    assert _run_watchdog(m, lambda: bool(api.idle_calls))
    assert m.is_on                      # never powered off
    assert False not in api.power_calls


def test_idle_timeout_still_powers_the_strip_off():
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=0, settings=_settings(grace_seconds=0))
    m._idle_timeout = 0
    m._enabled = True                   # timeout of 0s, enabled: off on the first tick
    m._wled_on = True
    m._output_live = True
    m._last_activity = time.monotonic()

    assert _run_watchdog(m, lambda: False in api.power_calls)
    assert m.mode == DeviceMode.OFF


def test_a_queued_idle_look_is_pushed_by_the_watchdog():
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=1800, settings=_settings())

    assert _run_watchdog(m, lambda: bool(api.idle_calls))
    # Adopted and claimed, never commanded.
    assert api.power_calls == []


def test_manual_power_on_claims_the_strip_into_its_idle_look():
    """Turn On with no song playing must not resurrect whichever cue the last
    song ended on — the strip gets the look the operator defined for it."""
    api = _Api(on=False)
    m = WLEDPowerManager(api, idle_timeout=1800, settings=_settings())

    m.manual_power(True)
    assert m._claim_pending
    assert not m._power_on_pending          # a claim, not a go-live

    assert _run_watchdog(m, lambda: bool(api.idle_calls))
    assert api.power_calls == [True]
    assert m.mode == DeviceMode.IDLE
    assert api.baseline_calls == []          # nothing is being sent to baseline for


def test_activity_outranks_a_queued_claim():
    """If YARG is in fact streaming, on_activity() fires ~88x a second and the
    strip goes live rather than sitting on its idle look."""
    api = _Api(on=False)
    m = WLEDPowerManager(api, idle_timeout=1800, settings=_settings())
    m.manual_power(True)

    m.on_activity()

    assert _run_watchdog(m, lambda: m.mode == DeviceMode.LIVE)
    assert api.baseline_calls
    assert api.idle_calls == []


def test_manual_off_clears_a_queued_claim():
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=1800, settings=_settings())
    m._wled_on = True
    m.manual_power(True)
    m.manual_power(False)

    assert not m._claim_pending
    assert m._power_off_pending


def test_power_snapshot_reports_the_mode():
    api = _Api(on=True)
    m = WLEDPowerManager(api, idle_timeout=1800, settings=_settings())
    m._wled_on = True

    snap = m.power_snapshot()
    assert snap["on"] is True
    assert snap["mode"] == DeviceMode.IDLE
    assert snap["output_live"] is False
    assert snap["idle_mode"] == "preset"
    assert snap["grace"] == DEFAULT_IDLE["grace_seconds"]


# ── the preview shows the strip, not the render buffer ──────────────────


class _Sink:
    def __init__(self):
        self.frames_sent = 0

    def send_pixels(self, pixel_data):
        self.frames_sent += 1

    def stats(self):
        return {"send_us_avg": 0.0, "send_us_max": 0.0,
                "send_errors": 0, "frames_sent": self.frames_sent}


class _Power:
    def __init__(self, live):
        self.live = live

    @property
    def output_enabled(self):
        return self.live

    @property
    def is_on(self):
        return self.live

    @property
    def mode(self):
        return DeviceMode.LIVE if self.live else DeviceMode.IDLE


class _Clock:
    """Injected clock, so the engine's cue deadlines share the timeline these
    tests pass to render_frame().

    Not optional: CueEngine and LEDMapper default to time.monotonic(), and a
    cue stamped on the real clock against a frame rendered at t=1000.0 leaves
    every cross-fade permanently unfinished, which renders black forever. This
    is the same injection replay/player.py relies on.
    """

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _thread(power, tracker=None, clock=None):
    clock = clock or _Clock()
    engine = CueEngine(clock=clock)
    engine.bpm = 120.0
    tracker = tracker or StatusTracker()
    settings = BridgeSettings(path=_NO_SETTINGS_FILE, warn_unwritable=False)
    rt = RenderThread(engine, LEDMapper(LED_COUNT, clock=clock), _Sink(),
                      tracker, settings, power)
    return rt, engine, tracker


def test_an_unsent_frame_is_never_composed():
    """The render buffer had nowhere to go, so composing it was pure waste —
    and it was what the dashboard drew."""
    rt, engine, tracker = _thread(_Power(live=False))
    engine.on_cue(CueByte.CHORUS)

    out = rt.render_frame(1000.0, 40)

    assert out == bytes(LED_COUNT * 3)
    assert rt._sender.frames_sent == 0
    assert rt.render_stats()["idle"] == 1
    assert rt.render_stats()["rendered"] == 0


def test_the_preview_reports_its_source_and_blanks_when_not_live():
    rt, engine, _ = _thread(_Power(live=False))
    engine.on_cue(CueByte.CHORUS)
    rt.render_frame(1000.0, 40)

    snap = rt.preview_snapshot()
    assert snap["source"] == DeviceMode.IDLE
    assert snap["strip"] == []
    assert snap["layers"] == []


def test_a_live_preview_still_carries_pixels():
    tracker = StatusTracker()
    tracker._sse_queues.append(object())   # a dashboard is watching
    rt, engine, _ = _thread(_Power(live=True), tracker)
    engine.on_cue(CueByte.CHORUS)
    rt.render_frame(1000.0, 40)

    snap = rt.preview_snapshot()
    assert snap["source"] == DeviceMode.LIVE
    assert len(snap["strip"]) > 0
    assert any(L["active"] for L in snap["layers"])


def test_a_live_strip_is_never_reported_as_off_just_because_nobody_is_watching():
    """Preview *capture* is gated on an open dashboard; the preview's *source*
    must not be. Stamping the source only inside the capture branch left a live
    strip reporting itself as off whenever the page was closed — the same class
    of untruth this whole change is about, pointing the other way."""
    rt, engine, _ = _thread(_Power(live=True))   # no SSE subscriber
    engine.on_cue(CueByte.CHORUS)
    rt.render_frame(1000.0, 40)

    assert rt._preview_source == DeviceMode.LIVE
    # Nothing was captured, so there is nothing to show — but "nothing to show"
    # is None, not a claim that the strip is dark.
    assert rt.preview_snapshot() is None


def test_output_stopping_blanks_a_preview_that_was_live():
    """The bug's exact shape: a lit preview must not survive the strip going
    dark. Nothing must be left frozen on the dashboard."""
    tracker = StatusTracker()
    tracker._sse_queues.append(object())
    power = _Power(live=True)
    rt, engine, _ = _thread(power, tracker)
    engine.on_cue(CueByte.CHORUS)
    rt.render_frame(1000.0, 40)
    assert len(rt.preview_snapshot()["strip"]) > 0

    power.live = False
    rt.render_frame(1000.1, 40)

    snap = rt.preview_snapshot()
    assert snap["source"] == DeviceMode.IDLE
    assert snap["strip"] == []


def test_resuming_output_does_not_cross_fade_from_the_last_song():
    """The frame in _last_sent may be an hour old. Fading the first cue of a
    new song up from the last cue of the previous one is not a transition
    anyone asked for."""
    clock = _Clock(1000.0)
    power = _Power(live=True)
    rt, engine, _ = _thread(power, None, clock)
    engine.on_cue(CueByte.CHORUS)
    for k in range(8):
        clock.t = 1000.0 + k * 0.025
        rt.render_frame(clock.t, 40)
    assert any(rt._last_sent)          # a lit frame is banked

    power.live = False
    clock.t = 1000.5
    rt.render_frame(clock.t, 40)
    power.live = True
    clock.t = 2000.0                   # an hour later, output resumes
    rt.render_frame(clock.t, 40)

    # The banked frame was cleared on the live edge, so nothing from the
    # previous song can bleed into the first frame of the next one.
    assert rt._fade_until == 0.0
    assert not any(rt._fade_from)


def test_the_tracker_sees_an_unsent_frame_as_unsent():
    rt, engine, tracker = _thread(_Power(live=False))
    engine.on_cue(CueByte.CHORUS)
    rt.render_frame(1000.0, 40)

    assert tracker.output_live is False
    assert tracker.ddp_frames_sent == 0


# ── idle-look settings ──────────────────────────────────────────────────


def test_idle_settings_are_bounds_checked_field_by_field():
    """One bad field must not revert the operator's good ones: the dashboard
    edits this block a field at a time."""
    s = _settings()
    out = s.update_idle({"mode": "nonsense", "brightness": 999, "speed": 12,
                         "color": [300, -5, 20]})

    assert out["mode"] == DEFAULT_IDLE["mode"]        # rejected, fell back
    assert out["brightness"] == 255                   # clamped
    assert out["speed"] == 12                         # accepted
    assert out["color"] == [255, 0, 20]               # clamped per channel


def test_idle_settings_survive_a_partial_update():
    s = _settings()
    s.update_idle({"effect": 38})
    out = s.update_idle({"brightness": 10})

    assert out["effect"] == 38
    assert out["brightness"] == 10
    assert out["palette"] == DEFAULT_IDLE["palette"]


def test_idle_block_is_published_in_the_settings_snapshot():
    snap = _settings().snapshot()
    assert snap["idle"]["mode"] in snap["idle_modes"]
