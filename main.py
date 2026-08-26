"""YARG → WLED Stage Kit Bridge.

Listens for YARG UDP lighting packets, runs the Stage Kit cue engine,
renders pixels, and sends them to WLED via DDP.
Automatically turns WLED on/off based on YARG activity.

The render loop runs on a dedicated thread (isolated from the asyncio
event loop) with adaptive perf_counter timing — inspired by LedFx's
Virtual.thread_function pattern.
"""

import asyncio
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable

from config import (
    YARG_LISTEN_HOST, YARG_LISTEN_PORT,
    WLED_HOST, WLED_DDP_PORT,
    LED_COUNT,
    STATUS_HOST, STATUS_PORT,
    IDLE_TIMEOUT,
    CAPTURE_DIR,
    LOG_LEVEL,
)
from protocol.yarg_packet import (
    parse_packet, CueByte, StrobeSpeed, KNOWN_DATAGRAM_VERSIONS,
)
from protocol.ddp_sender import DDPSender
from protocol.wled_api import WLEDApi
from effects.cue_engine import CueEngine
from effects.mapper import LEDMapper, MAPPED_REGION, PREVIEW_CELLS
from effects.palette_map import build_ring
from status_server import StatusTracker, StatusServer
from settings import BridgeSettings, DEFAULT_IDLE
from replay.controller import CaptureController
from version import VERSION_LABEL

log = logging.getLogger(__name__)


class YARGProtocol(asyncio.DatagramProtocol):
    """Receives YARG UDP packets and feeds the cue engine."""

    def __init__(self, engine: CueEngine, tracker: StatusTracker,
                 wled_power: 'WLEDPowerManager', capture=None):
        self.engine = engine
        self.tracker = tracker
        self.wled_power = wled_power
        # Optional CaptureController. When a recording is active its recorder
        # buffers datagrams for replay; see replay/controller.py.
        self.capture = capture
        self._last_cue = -1
        self._last_strobe = -1
        self._last_camera_subject = -1
        self._last_venue_size = -1
        self._last_song_section = -1
        self._warned_versions: set[int] = set()

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        # Record before parsing, so a capture holds exactly what arrived —
        # malformed and truncated datagrams included. Those are the ones worth
        # replaying against the parser later. Costs an attribute load and a
        # None check on the ~90/s path when nothing is recording; when something
        # is, a deque append. No encoding or disk I/O happens here.
        if self.capture is not None:
            recorder = self.capture.recorder
            if recorder is not None:
                recorder.record(data)

        pkt = parse_packet(data)
        if pkt is None:
            return

        self.tracker.on_packet()

        # Datagram-version guard. parse_packet knows one layout per version
        # range; an unknown version parses at the newest known offsets, but
        # *might* have shifted a field we read — as v5 did, inserting a fog
        # timer at offset 37 and silently turning the beat byte into a
        # permanently-true bonus flag (solid white strip). Warn once per unseen
        # version — this runs ~88x/s, so the set keeps it to a single line —
        # and keep rendering rather than going dark.
        ver = pkt.datagram_version
        if ver not in KNOWN_DATAGRAM_VERSIONS and ver not in self._warned_versions:
            self._warned_versions.add(ver)
            log.warning(
                "YARG datagram version %d is unrecognised (known: %s) — parsing "
                "lighting fields at the newest known layout's offsets, which may "
                "be wrong if the layout changed again; re-check "
                "DataStreamController.cs",
                ver, sorted(KNOWN_DATAGRAM_VERSIONS),
            )
        self.wled_power.on_activity()

        # Update BPM
        if pkt.bpm > 0:
            self.engine.bpm = pkt.bpm

        # Venue size is cue-launch context, so update it before dispatching a
        # cue from the same packet. Pattern transforms happen only at launch.
        if pkt.venue_size != self._last_venue_size:
            self.engine.on_venue_size(pkt.venue_size)
            self._last_venue_size = pkt.venue_size

        # Lighting cue change
        if pkt.lighting_cue != self._last_cue:
            self.engine.on_cue(pkt.lighting_cue)
            self.tracker.on_cue(pkt.lighting_cue)
            self._last_cue = pkt.lighting_cue

        # Strobe state change
        if pkt.strobe_state != self._last_strobe:
            self.engine.on_strobe(pkt.strobe_state)
            self._last_strobe = pkt.strobe_state

        # Beat events
        self.engine.on_beat(pkt.beat)
        self.tracker.on_beat(pkt.beat)

        # Keyframe events
        self.engine.on_keyframe(pkt.keyframe)

        # Note-hold accents (Phase 4) — rising-edge per-instrument hits
        # (guitar/bass/drums/keys) light a brief accent in each instrument's
        # slice of the strip.
        self.engine.on_notes(pkt.guitar_notes, pkt.bass_notes,
                             pkt.drum_notes, pkt.keys_notes)

        # Vocal ribbon (Phase 4) — lead + harmony MIDI pitches drive a
        # colour-by-pitch blob per sounding voice.
        self.engine.on_vocals(pkt.vocal_note, pkt.harmony0_note,
                              pkt.harmony1_note, pkt.harmony2_note)

        # Performer highlight (Phase 4) — spotlight + singalong performer
        # bitmasks bias the wash toward the featured performers' colours.
        self.engine.on_performers(pkt.spotlight, pkt.singalong)

        # Post-processing (Phase 4) — venue film grade; the mapper applies the
        # colour-tint grades as a global palette modifier.
        self.engine.on_post_processing(pkt.post_processing)

        # Fog/haze (Phase 6) — lifts the post-process blur toward a soft
        # blur-glow floor while the venue haze is up.
        self.engine.on_fog(pkt.fog_state)

        # Song section (offset 13) — slow palette/energy bias per section.
        # YARG only changes the byte on a Verse/Chorus lighting event, so this
        # is change-gated; the engine eases the new bias in over SECTION_EASE.
        if pkt.song_section != self._last_song_section:
            self.engine.on_song_section(pkt.song_section)
            self._last_song_section = pkt.song_section

        # Bonus FX flag — one-frame celebration burst on the strip.
        if pkt.bonus_effect:
            self.engine.on_bonus()

        # Pause state — freezes pattern motion + dims output.
        # YARG: 0 = AtMenu, 1 = Unpaused, 2 = Paused.
        self.engine.on_paused(pkt.paused == 2)

        # Star power (v4) — sustained "tasteful surge" overlay. Fed every frame
        # since amount drains continuously while a player's overdrive is active.
        self.engine.on_star_power(
            pkt.sp_active, pkt.sp_amount, pkt.sp_charge, pkt.sp_active_count)

        # Camera cut (v3, Phase 5) — on a subject change, bias the wash toward
        # the on-camera player (region + hue) and, on a directed cut, fire a
        # brief accent. Also surfaced on the status page.
        if pkt.camera_cut_subject != self._last_camera_subject:
            self.engine.on_camera_cut(pkt.camera_cut_subject, pkt.camera_cut_priority)
            self.tracker.on_camera_cut(pkt.camera_cut_subject, pkt.camera_cut_priority)
            self._last_camera_subject = pkt.camera_cut_subject

        # Surface scene + auto-gen track flag for the status page.
        self.tracker.on_scene(pkt.scene)
        self.tracker.on_auto_gen(pkt.auto_gen)
        self.tracker.on_venue_size(pkt.venue_size)
        self.tracker.on_song_section(pkt.song_section)
        self.tracker.on_paused(pkt.paused == 2)
        self.tracker.on_star_power(pkt.sp_active, pkt.sp_charge, pkt.sp_active_count)

    def release_latched_state(self):
        """Forget the cue latched from a sender that has gone away.

        Everything above is change-gated — the engine is told about a cue only
        when the byte differs from the last one — so nothing off the packet path
        ever clears it. That is right on the timescale the gating exists for:
        YARG streams at ~88 Hz, and blacking out on a dropped datagram would be
        far worse than briefly holding the last cue. It is wrong once the sender
        has been gone long enough that the bridge has stopped driving the strip
        altogether, which is when the watchdog calls this: past that point the
        held cue is not resilience, it is a stale frame the dashboard has to
        apologise for and an engine full of patterns anchored to a `_clock()`
        reading from the previous session.

        **Both halves matter.** Resetting the engine without resetting the
        change detectors below is worse than doing nothing: with `_last_cue`
        still holding DEFAULT and the engine released to NO_CUE, the next song
        opening on DEFAULT — the most common opening cue there is — compares
        equal, never reaches `engine.on_cue()`, and plays to a dark strip until
        some *other* cue happens along. So every detector goes back to the -1 it
        was constructed with, which no real byte can equal, and the first packet
        of the next stream re-sends the whole set.

        Called from the watchdog's worker thread, not the event loop. That is
        the same concurrency the render thread already sees from
        `datagram_received` — `on_cue()` rebinds its pattern lists rather than
        mutating them, so a reader sees the old set or the new one, never a
        half-cleared one.
        """
        self._last_cue = -1
        self._last_strobe = -1
        self._last_camera_subject = -1
        self._last_venue_size = -1
        self._last_song_section = -1
        self.engine.on_cue(CueByte.NO_CUE)
        self.engine.on_strobe(StrobeSpeed.OFF)
        self.tracker.on_cue(CueByte.NO_CUE)


class DeviceMode:
    """How the bridge is currently driving the strip it owns.

    Plain strings rather than an Enum: this value is read from the render
    thread and serialised straight into the status JSON, and both want a string.
    """

    OFF = "off"     # powered down — nothing rendered, nothing sent
    IDLE = "idle"   # powered and owned, running the WLED-native idle look
    LIVE = "live"   # baseline asserted, DDP frames flowing


class WLEDPowerManager:
    """Owns the strip: its power, its mode, and the state it is left in.

    The bridge treats every strip it drives as owned, which means there is no
    moment when the strip's appearance is undefined. Three modes cover the
    whole lifetime, and every one of them is a *defined* state:

      LIVE   YARG (or a test pattern) is feeding us. The device baseline is
             asserted and the render thread's DDP frames own every pixel.
      IDLE   The stream has been quiet for longer than the operator's grace
             period. DDP stops and WLED runs the idle look on its own, so
             "the bridge is not sending" has a defined appearance instead of
             leaving whatever the last cue painted frozen on the strip for the
             next half hour.
      OFF    Quiet past the idle timeout. Powered down.

    The mode is stored as two booleans rather than one string because they
    answer different questions and can disagree: `_wled_on` is a belief about
    the *device's* power, which the world can change behind our back (a reboot,
    the WLED app, a wall switch), while `_output_live` is a fact about *our own*
    behaviour that nothing else can touch. `mode` composes them.

    Ownership is asserted on transitions, not continuously — the WLED app stays
    usable between songs, and only a mode change stomps what it did.
    """

    def __init__(self, wled_api: WLEDApi, idle_timeout: int,
                 settings=None, led_count: int = LED_COUNT):
        self._api = wled_api
        self._idle_timeout = idle_timeout  # seconds, 0 = disabled
        self._settings = settings
        self._led_count = led_count
        self._last_activity = 0.0
        self._wled_on = False
        self._output_live = False
        self._enabled = idle_timeout > 0
        self._power_on_pending = False
        self._power_off_pending = False
        # Set when a mode transition owes the device an assertion the watchdog
        # must make on a worker thread. _adopt_device_state() runs on the event
        # loop and must not block on a 3 s HTTP call, so it queues instead.
        self._idle_push_pending = False
        # A manual power-on from the dashboard. Distinct from _power_on_pending,
        # which means "go live": pressing Turn On with no song playing should
        # give the strip its defined idle look, not resurrect whichever cue the
        # last song happened to end on.
        self._claim_pending = False
        self._dark_since = 0.0
        self._dark_warned = False
        # Lets on_activity() cut the watchdog's tick short. Without it a song
        # starting one second after a tick waits out the remaining four before
        # the strip powers on, which reads on the rig as the bridge being slow
        # to notice YARG.
        self._wake = asyncio.Event()
        # Called once each time the manager stops driving the strip, to let the
        # cue path drop what it latched from the departed sender. Set after
        # construction because the protocol that owns that state takes this
        # manager as a constructor argument — the dependency only runs one way
        # at build time, and back the other way at runtime.
        self.on_release: Callable[[], None] | None = None

    # ── mode ────────────────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        if not self._wled_on:
            return DeviceMode.OFF
        return DeviceMode.LIVE if self._output_live else DeviceMode.IDLE

    @property
    def output_enabled(self) -> bool:
        """True when the render thread should be sending DDP.

        The render thread's gate. Distinct from `is_on`: an idle strip is
        powered and owned, but its pixels belong to WLED, not to us — sending
        frames at it would fight the idle look.
        """
        return self._output_live

    def _idle_spec(self) -> dict:
        return self._settings.idle if self._settings is not None else dict(DEFAULT_IDLE)

    def _grace_seconds(self) -> int:
        if self._settings is not None:
            return self._settings.idle_grace_seconds
        return int(DEFAULT_IDLE["grace_seconds"])

    def _quiet_for(self) -> float:
        """Seconds since the last activity, or inf if there has never been any.

        An owned strip that has never seen a packet still gets a defined look;
        treating "never" as zero elapsed would leave it holding NO_CUE forever.
        """
        if self._last_activity == 0.0:
            return float("inf")
        return time.monotonic() - self._last_activity

    def _adopt_device_state(self, device_on: bool | None):
        """Reconcile _wled_on with what the device actually reports.

        The bridge is not the only thing that can power this strip — a reboot,
        the WLED UI, a button press or a power blip all change the device
        without telling us. (This firmware boots with `def.on: true`, so a power
        blip alone is enough.) Every probe result is therefore adopted, in
        *both* directions; believing a stale value has a distinct failure mode
        either way:

          - believed OFF, device ON  → the idle timer never runs, so the strip
            stays lit indefinitely at ~1.6 A;
          - believed ON, device OFF  → on_activity() never queues a power-on, so
            a song plays to a dark strip, and _check_dark_while_active() cannot
            see it because it returns early while we believe we are driving it.

        Adopting ON claims the strip: it enters IDLE, which queues the idle look
        over whatever it was showing, and grants it a full idle timeout rather
        than switching it off on the next tick. Powering the strip on from the
        WLED app therefore hands it to the bridge rather than to the app — that
        is what ownership means here, and it is the only place the bridge
        overrides a deliberate action taken elsewhere.

        Sync and non-blocking: it runs on the event loop, so the HTTP push it
        implies is queued for the watchdog rather than made here.
        """
        # is_on() returns None when the request failed. Unreachable is not an
        # answer about power — keep the last belief rather than inventing one.
        if device_on is None:
            return

        # A queued command is newer than this answer. The probe blocks for up
        # to 3 s on HTTP, and on_activity()/manual_power() can fire during that
        # window, so adopting here would silently discard the operator's (or
        # YARG's) intent. _process_pending_power() settles it on the next tick.
        if self._power_on_pending or self._power_off_pending or self._claim_pending:
            return

        if device_on == self._wled_on:
            return

        if device_on:
            # Seed the idle timer *only* on this transition. The probe repeats
            # every 30 s; refreshing _last_activity on every adoption would
            # push the deadline out forever and the strip would never idle off
            # at all — a worse version of the bug being fixed here. Reaching
            # this line means the belief actually changed, so it runs once per
            # divergence, not once per probe.
            self._last_activity = time.monotonic()
            self._wled_on = True
            self._output_live = False
            self._idle_push_pending = True
            log.info("WLED: adopted device state ON (powered on externally) — "
                     "claiming it, idle look queued, idle timer restarted")
        else:
            self._wled_on = False
            self._output_live = False
            self._idle_push_pending = False
            log.info("WLED: adopted device state OFF (powered off externally)")

    def _check_dark_while_active(self):
        """Warn when YARG is feeding us but our frames are not reaching the strip.

        This state means a transition into LIVE is failing silently — the render
        thread gates DDP on `output_enabled`, so nothing reaches the strip while
        the status page happily reports YARG as connected.
        """
        now = time.monotonic()
        recent_activity = self._last_activity > 0 and (now - self._last_activity) < 10.0
        if not recent_activity or self._output_live:
            self._dark_since = 0.0
            self._dark_warned = False
            return
        if self._dark_since == 0.0:
            self._dark_since = now
        elif not self._dark_warned and (now - self._dark_since) >= 30.0:
            self._dark_warned = True
            log.warning(
                "Receiving YARG packets but the bridge has not been sending for "
                "%ds — the transition to live output is failing, so the strip is "
                "showing its idle look or nothing at all (mode: %s, WLED "
                "reachable: %s)",
                round(now - self._dark_since), self.mode, self._api.reachable,
            )

    def on_activity(self):
        """Called when a YARG packet is received."""
        # Hot path — this runs ~88x/second per YARG packet. Update timestamp
        # unconditionally (cheap), but only mark a transition when we are not
        # already driving the strip.
        self._last_activity = time.monotonic()
        if not self._output_live and not self._power_on_pending:
            self._power_on_pending = True
            # Cheap when already set, and only reached on the transition edge.
            self._wake.set()

    def on_test_activity(self):
        """Called on each beat of a dashboard test pattern.

        A test pattern is activity in exactly the sense the grace period means:
        it is a live stream of frames the operator is watching. Ticking it per
        beat rather than once at start is what stops the grace period from
        pulling the strip into the idle look 15 s into a test.
        """
        self.on_activity()

    # ── transitions ─────────────────────────────────────────────────────

    def _enter_live(self):
        """Power up if needed and assert the baseline, then open the DDP gate.

        Ordered so frames never precede the baseline: `_output_live` is set last,
        and the render thread reads it as its gate. A strip that is already on
        still gets the baseline — it may have been powered on externally, or
        left in a state the WLED app changed while we were idle.
        """
        if not self._wled_on:
            if not self._api.set_power(True):
                log.warning("WLED: failed to power on via API")
                return
            self._wled_on = True
            log.info("WLED: powered ON")
        if not self._api.assert_live_baseline(self._led_count):
            # Sending anyway would put frames on a device whose master
            # brightness and segment geometry are unknown — the look would be
            # wrong in a way that is very hard to read off the strip. Leave the
            # gate shut; _check_dark_while_active() reports it if it persists.
            log.warning("WLED: failed to assert the live baseline — not sending")
            return
        self._idle_push_pending = False
        self._output_live = True
        log.info("WLED: live (baseline asserted, DDP flowing)")

    def _close_output_gate(self):
        """Stop driving the strip, and release what the cue path latched.

        The single falling edge of `_output_live`, shared by both exits (idle
        and power-off) so the release fires exactly once however the strip is
        left. `_enter_idle()` delegating to `_power_off()` for the "skip the
        idle stage" spec therefore does not double-fire: the first call already
        cleared the flag, so the second sees `was_live` False.

        Guarded rather than unconditional because a release is only meaningful
        if there was something to release. `_power_off()` also runs on a strip
        that has been sitting idle for half an hour, and firing there would mean
        re-releasing already-released state on every such transition.

        The callback is a courtesy to the cue path, not part of owning the
        device: it must never be able to leave the strip powered on because the
        cue engine raised. Hence the try/except — this runs on the watchdog's
        worker thread, where an escape would surface as the whole `to_thread`
        call failing and the mode transition being abandoned half-done.
        """
        was_live = self._output_live
        self._output_live = False
        self._idle_push_pending = False
        if was_live and self.on_release is not None:
            try:
                self.on_release()
            except Exception:
                log.exception("cue release failed — the strip is still being "
                              "left correctly, but the last cue stays latched")

    def _enter_idle(self):
        """Close the DDP gate and hand the strip a look to run on its own.

        The gate closes first: the idle look must not be pushed while we are
        still sending, or the two fight until the device's realtime timeout
        lapses. Even so the look does not *appear* until that timeout
        (~2.5 s here) — WLED holds the last DDP frame until then.
        """
        self._close_output_gate()
        spec = self._idle_spec()
        if spec.get("mode") == "off":
            # The operator chose to skip the idle stage entirely.
            self._power_off()
            return
        if self._api.apply_idle_look(spec, self._led_count):
            log.info("WLED: idle (%s look handed to the device)", spec.get("mode"))
        else:
            log.warning("WLED: failed to push the idle look — the strip keeps "
                        "the last frame until its realtime timeout lapses")

    def _power_off(self):
        self._close_output_gate()
        if self._api.set_power(False, transition_ms=700):
            self._wled_on = False
            log.info("WLED: powered OFF (idle)")
        else:
            log.warning("WLED: failed to power off via API")

    def _claim(self):
        """Power the strip up and hand it the idle look — a manual Turn On."""
        self._claim_pending = False
        if not self._wled_on:
            if not self._api.set_power(True):
                log.warning("WLED: failed to power on via API")
                return
            self._wled_on = True
            log.info("WLED: powered ON (manual)")
        self._push_idle_look()

    def _push_idle_look(self):
        """Apply a queued idle-look assertion (from an external or manual
        power-on)."""
        self._idle_push_pending = False
        spec = self._idle_spec()
        if spec.get("mode") == "off":
            return
        if not self._api.apply_idle_look(spec, self._led_count):
            log.warning("WLED: failed to push the idle look onto an adopted strip")

    @property
    def is_on(self) -> bool:
        return self._wled_on

    def capabilities(self) -> dict:
        """The firmware's own effect and palette names, for the idle-look editor.

        Served from its own endpoint rather than folded into the status
        snapshot: ~220 effects and ~72 palettes is a few KB that would
        otherwise ride along on every SSE frame, and neither list changes
        without a reflash.
        """
        return {
            "effects": list(self._api.effects),
            "palettes": list(self._api.palettes),
            "realtime_timeout_ms": self._api.realtime_timeout_ms,
            "reachable": self._api.reachable,
        }

    def power_snapshot(self) -> dict:
        """Return current power state for the status page."""
        wifi = self._api.wifi_info
        spec = self._idle_spec()
        base = {
            "on": self._wled_on,
            "mode": self.mode,
            "output_live": self._output_live,
            "reachable": self._api.reachable,
            "grace": self._grace_seconds(),
            "idle_mode": spec.get("mode"),
            "realtime_timeout_ms": self._api.realtime_timeout_ms,
            "wifi": wifi,
        }
        if not self._enabled:
            return {**base, "enabled": False, "idle_seconds": 0,
                    "timeout": 0, "remaining": 0}
        elapsed = time.monotonic() - self._last_activity if self._last_activity > 0 else 0.0
        remaining = max(0.0, self._idle_timeout - elapsed) if self._wled_on else 0.0
        return {
            **base,
            "enabled": True,
            "idle_seconds": round(elapsed),
            "timeout": self._idle_timeout,
            "remaining": round(remaining),
        }

    def manual_power(self, on: bool):
        """Manual power toggle from the web UI (deferred to watchdog thread).

        Turning on *claims* the strip rather than driving it: it powers up and
        takes the idle look. If YARG is in fact streaming, on_activity() fires
        ~88 times a second and pulls it to LIVE within a frame or two, so the
        operator never has to think about which of the two they wanted.

        The idle timer is seeded either way, so a strip switched on by hand
        still gets a full timeout before it powers itself back off.
        """
        if on:
            self._power_off_pending = False
            self._last_activity = time.monotonic()
            if not self._wled_on:
                self._claim_pending = True
                self._wake.set()
        else:
            self._power_on_pending = False
            self._claim_pending = False
            if self._wled_on:
                self._power_off_pending = True
                self._wake.set()

    # ── watchdog ────────────────────────────────────────────────────────

    async def _wait_tick(self, seconds: float):
        """Sleep until the next watchdog tick, or until something wakes us.

        A separate method so tests can shorten the loop without patching
        asyncio.sleep globally.
        """
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def watchdog_loop(self):
        """Background task that owns every transition of the strip's mode.

        Never allowed to die. This task owns the only path that sets
        `_wled_on` and `_output_live`, and the render thread gates all DDP
        output on the latter — so if this task raises, the strip goes dark
        permanently while YARG still reads as connected, with nothing in the
        log to say why. Any exception is logged and the loop resumes.
        """
        while True:
            try:
                await self._watchdog_run()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("WLED watchdog crashed — restarting in 5s")
                await asyncio.sleep(5)

    async def _watchdog_run(self):
        # Capability lists first: the dashboard's idle-look editor offers the
        # firmware's own effects and palettes, and the realtime timeout it
        # reports is what decides how long an idle look takes to appear.
        await asyncio.to_thread(self._api.fetch_capabilities)

        # Initial reachability check (non-blocking)
        self._adopt_device_state(await asyncio.to_thread(self._api.is_on))
        await asyncio.to_thread(self._api.fetch_wifi_info)

        # Do not make a live sender wait for the first watchdog tick. Activity
        # may have arrived while the initial HTTP probes were in flight.
        await self._process_pending_power()

        if self._enabled:
            log.info("WLED power management: enabled (%ds idle timeout, %ds grace)",
                     self._idle_timeout, self._grace_seconds())
        else:
            log.info("WLED power management: disabled (IDLE_TIMEOUT=0) — the "
                     "strip is still owned and still falls back to its idle "
                     "look, it just never powers off")

        check_counter = 0
        while True:
            await self._wait_tick(5)

            # Handle deferred commands from on_activity() or manual_power().
            await self._process_pending_power()

            self._check_dark_while_active()

            check_counter += 1

            # Reachability + WiFi check every ~30s (6 × 5s). The probe is
            # unconditional: gating it on `not self._wled_on` meant the
            # believed-ON/device-OFF divergence was never even looked for.
            if check_counter >= 6:
                check_counter = 0
                self._adopt_device_state(await asyncio.to_thread(self._api.is_on))
                await asyncio.to_thread(self._api.fetch_wifi_info)
                # Adoption may have queued a look for a strip someone else
                # powered on. Apply it here rather than on the next tick.
                await self._process_pending_power()

            if not self._wled_on:
                continue

            quiet = self._quiet_for()

            # LIVE → IDLE. Runs whether or not power management is enabled:
            # handing the strip a defined look is ownership, not power saving.
            if self._output_live and quiet >= self._grace_seconds():
                log.info("WLED: no activity for %ds — falling back to the idle look",
                         round(quiet) if quiet != float("inf") else -1)
                await asyncio.to_thread(self._enter_idle)

            # → OFF, on the operator's idle timeout.
            if self._enabled and quiet >= self._idle_timeout:
                await asyncio.to_thread(self._power_off)

    async def _process_pending_power(self):
        """Apply deferred commands, with the most recent command winning."""
        if self._power_off_pending:
            self._power_off_pending = False
            self._power_on_pending = False
            self._claim_pending = False
            await asyncio.to_thread(self._power_off)
        elif self._power_on_pending:
            self._power_on_pending = False
            self._claim_pending = False
            await asyncio.to_thread(self._enter_live)
        elif self._claim_pending:
            await asyncio.to_thread(self._claim)
        elif self._idle_push_pending:
            await asyncio.to_thread(self._push_idle_look)


class RenderThread(threading.Thread):
    """Dedicated render thread — completely isolated from the asyncio event loop.

    Reads engine state, runs the mapper, sends DDP packets, all on its own
    OS thread with time.sleep() + perf_counter adaptive timing.  This
    eliminates event-loop contention from SSE broadcasts, HTTP handlers,
    and beat-pattern coroutines.

    Time-based patterns and strobe are now computed deterministically on
    this thread via engine.tick() — immune to event-loop congestion.

    Communication with the asyncio world:
      - engine.zones / engine.get_effects() are read directly (list/dict
        copies are naturally atomic for CPython due to the GIL)
      - settings.brightness / settings.zone_colors go through BridgeSettings'
        own threading.Lock
      - wled_power.is_on is a simple bool read (GIL-safe)
      - tracker.on_render() writes to plain ints (GIL-safe)
    """

    def __init__(self, engine: CueEngine, mapper: LEDMapper, sender: DDPSender,
                 tracker: StatusTracker, settings: BridgeSettings,
                 wled_power: 'WLEDPowerManager'):
        super().__init__(name="render", daemon=True)
        self._engine = engine
        self._mapper = mapper
        self._sender = sender
        self._tracker = tracker
        self._settings = settings
        self._wled_power = wled_power
        self._active = False

        # Frame-skip tracking
        self._frames_rendered = 0
        self._frames_skipped = 0
        # Frames the loop deliberately did not compose because the bridge was
        # not driving the strip. Counted separately from `_frames_skipped`,
        # which means a *missed* frame — a stall. These are not misses.
        self._frames_idle = 0
        # Edge detector for the transition into live output, so the first live
        # frame does not cross-fade from a stage look left over from last song.
        self._was_live = False

        # Rolling stats (last N frames)
        self._STATS_WINDOW = 200
        self._work_times: list[float] = []
        self._frame_gaps: list[float] = []
        self._stall_count = 0

        # Strobe black frame (pre-allocated, never changes)
        self._black = b'\x00' * (LED_COUNT * 3)

        # Cached palette colours — refreshed when palette name changes
        self._cached_palette = ""
        self._cached_colors: dict = {}
        self._cached_ring = None

        # Cue cross-fade state. When the engine signals a cue change, we
        # snapshot the previously sent frame into _fade_from and linearly
        # blend new frames against it for the incoming cue's fade duration.
        # Removes the one-frame all-black blink between cues.
        #
        # The duration comes from settings.fade_seconds_for_cue() — the
        # operator's `cue_fade_ms` base, overridden per cue for the ones YARG
        # names "fast" or "slow" (settings.CUE_FADE_MS_OVERRIDES). It is
        # *latched* here at cue-change time rather than read every frame, so
        # dragging the dashboard slider mid-fade can't rescale a running fade
        # and make it jump.
        self._fade_from = bytearray(LED_COUNT * 3)
        self._fade_buf = bytearray(LED_COUNT * 3)
        self._last_sent = bytearray(LED_COUNT * 3)
        self._fade_until = 0.0
        self._fade_duration = 0.0
        self._last_cue_change_at = 0.0

        # Live dashboard preview (VISION Phase 7). Captured only while someone
        # is watching (tracker.has_subscribers) and throttled to ~11 Hz — the
        # render loop runs faster than the dashboard needs, so we don't pay the
        # downsample cost on every frame. The composed strip is downsampled from
        # the final sent frame (so strobe/cross-fade show); the per-layer rows
        # come from the mapper's layer_preview().
        self._PREVIEW_INTERVAL = 0.09  # seconds between preview captures
        self._last_preview_at = 0.0
        self._preview_strip: list = []
        self._preview_layers: dict | None = None
        # What the preview last described. The dashboard draws the strip from
        # this, so it has to say which of the three it is: our own frames, the
        # device's idle look, or nothing at all.
        self._preview_source = DeviceMode.OFF
        self._fatal_error: str | None = None

    def run(self):
        try:
            self._run_loop()
        except Exception as exc:
            self._fatal_error = f"{type(exc).__name__}: {exc}"
            self._active = False
            log.exception("Render thread crashed; health endpoint will report failure")
            # Best-effort fail dark instead of leaving the last stage look
            # frozen indefinitely. A sender failure is deliberately secondary.
            try:
                self._sender.send_pixels(self._black)
            except Exception:
                log.exception("Failed to send blackout after render crash")

    def _run_loop(self):
        self._active = True
        last_frame_time = time.perf_counter()
        fps = self._settings.fps
        interval = 1.0 / fps
        log.info("Render thread started: %d FPS, %d LEDs → %s:%d",
                 fps, LED_COUNT, WLED_HOST, WLED_DDP_PORT)

        next_frame = time.perf_counter()

        while self._active:
            # Re-read FPS from settings (lock-protected, cheap)
            new_fps = self._settings.fps
            if new_fps != fps:
                fps = new_fps
                interval = 1.0 / fps
                next_frame = time.perf_counter()

            next_frame += interval
            frame_start = time.perf_counter()

            # Frame-skip detection: if we're >2 frame periods behind,
            # drop this frame and reset the deadline
            drift = frame_start - (next_frame - interval)
            if drift > interval * 2.0:
                self._frames_skipped += 1
                next_frame = time.perf_counter() + interval
                time.sleep(0.001)
                continue

            self.render_frame(time.monotonic(), fps)

            # Rolling stats
            frame_end = time.perf_counter()
            work_ms = (frame_end - frame_start) * 1000.0
            gap_ms = (frame_start - last_frame_time) * 1000.0
            last_frame_time = frame_start

            idx = self._frames_rendered % self._STATS_WINDOW
            if len(self._work_times) >= self._STATS_WINDOW:
                self._work_times[idx] = work_ms
                self._frame_gaps[idx] = gap_ms
            else:
                self._work_times.append(work_ms)
                self._frame_gaps.append(gap_ms)

            if gap_ms > interval * 2000.0:
                self._stall_count += 1

            # Adaptive sleep: subtract elapsed work from target interval
            sleep_time = next_frame - time.perf_counter()
            if sleep_time < 0:
                next_frame = time.perf_counter()
                sleep_time = 0.001
            time.sleep(sleep_time)

        log.info("Render thread stopped")

    def render_frame(self, now: float, fps: int) -> bytes:
        """Render one frame, send it over DDP, and return the pixels that went out.

        The whole per-frame pipeline — settings sync, engine tick, mapper,
        strobe, cue cross-fade, DDP send, status and preview capture — with the
        clock passed in rather than read. `_run_loop` supplies
        `time.monotonic()` and owns scheduling; `replay/player.py` supplies a
        synthetic timeline so a recorded session renders identically on every
        run. Every time-dependent step here reads *now*, including
        `get_strobe_visible()`, which would otherwise sample the wall clock and
        make strobe frames unreproducible.

        The returned buffer is reused by later calls (it may be `_black` or the
        cross-fade scratch buffer) — copy it if you need to keep it.
        """
        # Cache zone colours — only re-read when palette changes
        palette_name = self._settings.palette_name
        if palette_name != self._cached_palette:
            self._cached_colors = self._settings.zone_colors
            # The colour family the reactivity layers are constrained to. Built
            # here rather than per frame: it depends only on the palette, and
            # this branch is already the "palette changed" one.
            self._cached_ring = build_ring(self._cached_colors,
                                           self._settings.palette_ramp)
            self._cached_palette = palette_name

        # Advance time-based patterns (zone bitmasks computed from the injected
        # clock — immune to asyncio event-loop congestion). This runs whether or
        # not we are sending: it is what keeps beat phase, cue counters and
        # strobe coherent, so resuming output picks up mid-song rather than
        # restarting every pattern from zero.
        self._engine.tick(now)

        # Are we driving the strip at all? While the bridge is idle the strip
        # is running WLED's own idle look and our frames would go nowhere, so
        # composing them is pure waste — and, until 2026-08-25, it was also the
        # source of a dashboard that showed a moving wash while the strip sat
        # dark. Everything below the gate is skipped, and the preview says why.
        live = self._wled_power.output_enabled
        if live and not self._was_live:
            # Entering live output. Zero the cross-fade source: the last frame
            # we sent may be an hour old, and fading the first cue of a new
            # song up from the last cue of the previous one is not a
            # transition anyone asked for.
            self._last_sent[:] = bytes(len(self._last_sent))
            self._fade_until = 0.0
            self._fade_duration = 0.0
        self._was_live = live
        if not live:
            self._frames_idle += 1
            self._preview_source = self._wled_power.mode
            self._preview_strip = []
            self._preview_layers = None
            self._tracker.on_render(self._engine.zones,
                                    self._engine.strobe_hz(),
                                    self._engine.bpm,
                                    ddp_sent=False)
            return self._black
        # Stamped on every live frame, not only on the ones that capture a
        # preview: capture is gated on someone watching the dashboard, and
        # leaving the source at its last value meant a live strip reported
        # itself as off whenever nobody had the page open.
        self._preview_source = DeviceMode.LIVE

        # Operator blur strength (dashboard slider) — fog stacks on top of
        # this in the engine. Synced each frame so a drag lands live.
        self._engine.set_blur_base(self._settings.blur_amount)

        # Venue sparkle + song-section intensity (dashboard sliders), plus
        # the discrete venue chase toggle. Synced each frame; chase-mask
        # changes take effect at the next cue launch.
        self._engine.set_venue_intensity(self._settings.venue_intensity)
        self._engine.set_venue_patterns_enabled(
            self._settings.effect_enabled("venue_patterns"))
        self._engine.set_section_intensity(self._settings.section_intensity)

        # Get effects (consumes and clears transient flags)
        effects = self._engine.get_effects()
        # Mapper scales frame-count effects (sparkle life) by FPS so
        # they hold their wall-clock duration at any render rate.
        effects["fps"] = fps
        # Apply the dashboard's per-effect on/off switches — suppresses the
        # render signal of any disabled toggle before the mapper sees it.
        self._settings.apply_effect_toggles(effects)

        # Brightness baked into mapper output (Phase 2)
        brightness = self._settings.brightness / 255.0

        # Live-preview gate: only capture when the dashboard is open and the
        # throttle interval has elapsed, so the mapper does the per-layer
        # downsample only when it will actually be shown.
        want_preview = (self._tracker.has_subscribers and
                        (now - self._last_preview_at) >= self._PREVIEW_INTERVAL)

        # Render pixels
        reverse = self._settings.direction == "reverse"
        pixel_data = self._mapper.render(
            self._engine.zones,
            zone_colors=self._cached_colors,
            effects=effects,
            brightness=brightness,
            reverse=reverse,
            zone_cell_levels=self._engine.zone_cell_levels,
            motion_sources=self._engine.motion_sources,
            preview=want_preview,
            palette_ring=self._cached_ring,
            palette_strictness=self._settings.palette_strictness,
        )

        # Apply strobe (replace with pre-allocated black)
        if not self._engine.get_strobe_visible(now):
            pixel_data = self._black

        # Cue cross-fade: snapshot the last sent frame on cue change, then
        # blend incoming frames against it for that cue's fade duration.
        # Skipped when strobe is suppressing the frame to black, so
        # we don't fade *through* the strobe blackout.
        cue_change_at = effects.get("cue_change_at") or 0.0
        if cue_change_at > self._last_cue_change_at:
            duration = self._settings.fade_seconds_for_cue(effects.get("cue"))
            self._fade_from[:] = self._last_sent
            self._fade_duration = duration
            # A zero-duration cue leaves _fade_until at 0.0, so the guard below
            # is false and the blend is skipped entirely — the cue snaps, and
            # there is no division by zero to guard against.
            self._fade_until = (cue_change_at + duration) if duration > 0.0 else 0.0
            self._last_cue_change_at = cue_change_at

        if pixel_data is not self._black and now < self._fade_until:
            t = 1.0 - (self._fade_until - now) / self._fade_duration
            if t < 0.0:
                t = 0.0
            inv_t = 1.0 - t
            fb = self._fade_buf
            ff = self._fade_from
            pd = pixel_data
            n = len(pd)
            for i in range(n):
                fb[i] = int(ff[i] * inv_t + pd[i] * t)
            pixel_data = fb

        # Save for next frame's potential cross-fade source. Skip the
        # strobe blank — a cue change mid-strobe should fade from the
        # last visible frame, not from black.
        if pixel_data is not self._black:
            n = len(pixel_data)
            self._last_sent[:n] = pixel_data

        # Send DDP every frame while we are driving the strip (no dedup —
        # WiFi can drop UDP packets, so always resend like LedFx does). The
        # gate was checked above; reaching here means it was open.
        self._sender.send_pixels(pixel_data)
        ddp_sent = True

        self._tracker.on_render(self._engine.zones,
                                self._engine.strobe_hz(),
                                self._engine.bpm,
                                ddp_sent=ddp_sent,
                                beat_phase=effects.get("beat_phase", 0.0),
                                bar_beat=effects.get("bar_beat", 0))
        self._frames_rendered += 1

        # Live preview: downsample the frame actually sent (captures strobe
        # blackout and cross-fade) and grab the mapper's per-layer rows.
        if want_preview:
            self._preview_strip = LEDMapper.downsample_rgb(
                pixel_data, MAPPED_REGION, PREVIEW_CELLS)
            self._preview_layers = self._mapper.layer_preview()
            self._last_preview_at = now

        return pixel_data

    def stop(self):
        self._active = False

    @property
    def failed(self) -> bool:
        return self._fatal_error is not None

    def preview_snapshot(self) -> dict | None:
        """What the strip is showing, for the dashboard.

        A picture of the *strip*, not of the render buffer. Those are the same
        thing only while the bridge is driving the strip; the rest of the time
        the honest answer is "we are not painting this". Returning the last
        composed frame then — which is what this did until 2026-08-25 — put a
        moving wash on the dashboard while the strip sat dark, and no amount of
        greying it out in CSS made that a true statement.

        `source` names which of the three it is, so an SSE consumer that is not
        the dashboard gets the same answer:

          "live"  these pixels went out over DDP;
          "idle"  the strip is running WLED's own idle look, which the bridge
                  does not render and therefore cannot preview;
          "off"   the strip is powered down.

        Returns None only before the first capture, when there is nothing to
        say at all.
        """
        if self._preview_source == DeviceMode.LIVE:
            if not self._preview_strip:
                return None
            return {
                "cells": PREVIEW_CELLS,
                "source": DeviceMode.LIVE,
                "strip": self._preview_strip,
                "layers": self._preview_layers["layers"] if self._preview_layers else [],
            }
        # Empty pixel lists rather than an omitted key: every consumer already
        # renders "no data" as black, so the strip goes dark on its own without
        # each of them needing to learn about `source` first.
        return {
            "cells": PREVIEW_CELLS,
            "source": self._preview_source,
            "strip": [],
            "layers": [],
        }

    def render_stats(self) -> dict:
        """Rolling timing stats for diagnostics / status page."""
        fps = self._settings.fps
        work = self._work_times
        gaps = self._frame_gaps
        if not work:
            return {"fps": fps, "alive": self.is_alive(),
                    "fatal_error": self._fatal_error,
                    "rendered": 0, "skipped": 0, "stalls": 0, "idle": self._frames_idle,
                    "work_ms_avg": 0.0, "work_ms_max": 0.0,
                    "gap_ms_avg": 0.0, "gap_ms_max": 0.0,
                    "target_ms": round(1000.0 / fps, 1),
                    "ddp": self._sender.stats()}
        return {
            "fps": fps,
            "alive": self.is_alive(),
            "fatal_error": self._fatal_error,
            "rendered": self._frames_rendered,
            "skipped": self._frames_skipped,
            "stalls": self._stall_count,
            # Frames deliberately not composed because the bridge was not
            # driving the strip. A climbing `rendered` while the strip is dark
            # is the symptom this number replaces.
            "idle": self._frames_idle,
            "work_ms_avg": round(sum(work) / len(work), 2),
            "work_ms_max": round(max(work), 2),
            "gap_ms_avg": round(sum(gaps) / len(gaps), 2),
            "gap_ms_max": round(max(gaps), 2),
            "target_ms": round(1000.0 / fps, 1),
            "ddp": self._sender.stats(),
        }


async def main():
    idle_mins = IDLE_TIMEOUT // 60 if IDLE_TIMEOUT else 0
    # Version first, and in the log as well as on the dashboard: `docker logs`
    # is the one place the operator can read it without the status port being
    # reachable, which is the case whenever a bad deploy is the thing in doubt.
    log.info("YARG → WLED Stage Kit Bridge %s", VERSION_LABEL)
    log.info("  Listening on %s:%d", YARG_LISTEN_HOST, YARG_LISTEN_PORT)
    log.info("  Sending DDP to %s:%d", WLED_HOST, WLED_DDP_PORT)
    log.info("  LED count: %d", LED_COUNT)
    if IDLE_TIMEOUT:
        log.info("  WLED idle timeout: %dm", idle_mins)
    else:
        log.info("  WLED idle timeout: disabled")
    log.info("  Status page: http://%s:%d/", STATUS_HOST, STATUS_PORT)

    engine = CueEngine()
    mapper = LEDMapper(LED_COUNT)
    sender = DDPSender(WLED_HOST, WLED_DDP_PORT)
    wled_api = WLEDApi(WLED_HOST)
    tracker = StatusTracker()
    settings = BridgeSettings()
    # The power manager reads the operator's idle look and grace period, so it
    # is built after settings rather than before.
    wled_power = WLEDPowerManager(wled_api, IDLE_TIMEOUT,
                                  settings=settings, led_count=LED_COUNT)
    capture = CaptureController(CAPTURE_DIR)
    status_server = StatusServer(tracker, STATUS_HOST, STATUS_PORT, engine=engine,
                                 wled_power=wled_power, settings=settings,
                                 capture=capture)

    loop = asyncio.get_running_loop()

    # Start status web server
    await status_server.start()

    # Start UDP listener
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: YARGProtocol(engine, tracker, wled_power, capture=capture),
        local_addr=(YARG_LISTEN_HOST, YARG_LISTEN_PORT),
    )

    # Close the loop between the two: the manager decides when the sender is
    # gone, the protocol owns the state latched from it. Wired here because the
    # protocol does not exist until create_datagram_endpoint has run.
    wled_power.on_release = protocol.release_latched_state

    # Start status broadcast task
    broadcast_task = asyncio.create_task(tracker.broadcast_loop(wled_power=wled_power, settings=settings))

    # Drain any active capture's buffer to disk off the event loop
    capture_task = asyncio.create_task(capture.flush_loop())

    # Start render thread (Phase 3: isolated from asyncio event loop)
    render_thread = RenderThread(engine, mapper, sender, tracker, settings, wled_power)
    render_thread.start()

    # Provide render thread reference for status snapshots
    tracker.render_thread = render_thread

    # Start WLED idle watchdog
    watchdog_task = asyncio.create_task(wled_power.watchdog_loop())

    # Handle shutdown
    stop = asyncio.Event()

    def handle_signal():
        log.info("Shutting down...")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    await stop.wait()

    # Cleanup — stop the render thread first so it can't race with the
    # final all-black DDP send below, then send black before closing the
    # socket and turning the strip off.
    render_thread.stop()
    render_thread.join(timeout=2.0)
    broadcast_task.cancel()
    watchdog_task.cancel()
    capture_task.cancel()
    transport.close()

    # Close any open capture so its buffered tail reaches disk.
    try:
        capture.shutdown()
    except Exception as e:
        log.debug("Capture shutdown failed: %s", e)

    try:
        sender.send_pixels(b'\x00' * LED_COUNT * 3)
    except Exception as e:
        log.debug("Final all-black send failed: %s", e)
    sender.close()

    try:
        wled_api.set_power(False)
    except Exception as e:
        log.debug("Final WLED power-off failed: %s", e)

    log.info("Goodbye.")


def _configure_logging():
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


if __name__ == "__main__":
    _configure_logging()
    asyncio.run(main())
