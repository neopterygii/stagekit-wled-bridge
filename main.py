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

from config import (
    YARG_LISTEN_HOST, YARG_LISTEN_PORT,
    WLED_HOST, WLED_DDP_PORT,
    LED_COUNT, GLOBAL_BRIGHTNESS,
    STATUS_HOST, STATUS_PORT,
    IDLE_TIMEOUT,
    LOG_LEVEL,
)
from protocol.yarg_packet import parse_packet, CueByte, KNOWN_DATAGRAM_VERSIONS
from protocol.ddp_sender import DDPSender
from protocol.wled_api import WLEDApi
from effects.cue_engine import CueEngine
from effects.mapper import LEDMapper
from status_server import StatusTracker, StatusServer
from settings import BridgeSettings

log = logging.getLogger(__name__)


class YARGProtocol(asyncio.DatagramProtocol):
    """Receives YARG UDP packets and feeds the cue engine."""

    def __init__(self, engine: CueEngine, tracker: StatusTracker, wled_power: 'WLEDPowerManager'):
        self.engine = engine
        self.tracker = tracker
        self.wled_power = wled_power
        self._last_cue = -1
        self._last_strobe = -1
        self._last_camera_subject = -1
        self._warned_versions: set[int] = set()

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        pkt = parse_packet(data)
        if pkt is None:
            return

        self.tracker.on_packet()

        # Datagram-version guard. parse_packet reads a fixed offset layout;
        # that layout has been stable across every YARG version (append-only),
        # so an unknown version still parses but *might* have shifted a field
        # we read. Warn once per unseen version — this runs ~88x/s, so the set
        # keeps it to a single line — and keep rendering rather than going dark.
        ver = pkt.datagram_version
        if ver not in KNOWN_DATAGRAM_VERSIONS and ver not in self._warned_versions:
            self._warned_versions.add(ver)
            log.warning(
                "YARG datagram version %d is unrecognised (known: %s) — parsing "
                "lighting fields at their v1-v4 offsets, which may be wrong if "
                "the layout changed; re-check DataStreamController.cs",
                ver, sorted(KNOWN_DATAGRAM_VERSIONS),
            )
        self.wled_power.on_activity()

        # Update BPM
        if pkt.bpm > 0:
            self.engine.bpm = pkt.bpm

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

        # Drum notes (for cues that listen for drums)
        self.engine.on_drum(pkt.drum_notes)

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

        # Camera cut (v3) — parse-only for now: surface the subject on the
        # status page on change; not yet wired into lighting.
        if pkt.camera_cut_subject != self._last_camera_subject:
            self.tracker.on_camera_cut(pkt.camera_cut_subject, pkt.camera_cut_priority)
            self._last_camera_subject = pkt.camera_cut_subject

        # Surface scene + auto-gen track flag for the status page.
        self.tracker.on_scene(pkt.scene)
        self.tracker.on_auto_gen(pkt.auto_gen)
        self.tracker.on_paused(pkt.paused == 2)
        self.tracker.on_star_power(pkt.sp_active, pkt.sp_charge, pkt.sp_active_count)


class WLEDPowerManager:
    """Manages WLED power state based on YARG activity."""

    def __init__(self, wled_api: WLEDApi, idle_timeout: int):
        self._api = wled_api
        self._idle_timeout = idle_timeout  # seconds, 0 = disabled
        self._last_activity = 0.0
        self._wled_on = False
        self._enabled = idle_timeout > 0
        self._power_on_pending = False
        self._power_off_pending = False
        self._dark_since = 0.0
        self._dark_warned = False

    def _check_dark_while_active(self):
        """Warn when YARG is feeding us but the strip is still dark.

        This state means power-on is failing silently — the render thread
        gates DDP on _wled_on, so nothing reaches the strip while the
        status page happily reports YARG as connected.
        """
        now = time.monotonic()
        recent_activity = self._last_activity > 0 and (now - self._last_activity) < 10.0
        if not recent_activity or self._wled_on:
            self._dark_since = 0.0
            self._dark_warned = False
            return
        if self._dark_since == 0.0:
            self._dark_since = now
        elif not self._dark_warned and (now - self._dark_since) >= 30.0:
            self._dark_warned = True
            log.warning(
                "Receiving YARG packets but WLED has stayed off for %ds — "
                "power-on is failing, no DDP is being sent (WLED reachable: %s)",
                round(now - self._dark_since), self._api.reachable,
            )

    def on_activity(self):
        """Called when a YARG packet is received."""
        # Hot path — this runs ~88x/second per YARG packet. Update timestamp
        # unconditionally (cheap), but only mark a power transition when the
        # WLED is actually off.
        self._last_activity = time.monotonic()
        if not self._wled_on and not self._power_on_pending:
            self._power_on_pending = True

    def on_test_activity(self):
        """Called when a test pattern is triggered from the web UI."""
        self.on_activity()

    def _power_on(self):
        if self._api.set_power(True):
            self._wled_on = True
            log.info("WLED: powered ON")
        else:
            log.warning("WLED: failed to power on via API")

    def _power_off(self):
        if self._api.set_power(False):
            self._wled_on = False
            log.info("WLED: powered OFF (idle)")
        else:
            log.warning("WLED: failed to power off via API")

    @property
    def is_on(self) -> bool:
        return self._wled_on

    def power_snapshot(self) -> dict:
        """Return current power state for the status page."""
        wifi = self._api.wifi_info
        if not self._enabled:
            return {"enabled": False, "on": self._wled_on, "reachable": self._api.reachable, "idle_seconds": 0, "timeout": 0, "remaining": 0, "wifi": wifi}
        elapsed = time.monotonic() - self._last_activity if self._last_activity > 0 else 0.0
        remaining = max(0.0, self._idle_timeout - elapsed) if self._wled_on else 0.0
        return {
            "enabled": True,
            "on": self._wled_on,
            "reachable": self._api.reachable,
            "idle_seconds": round(elapsed),
            "timeout": self._idle_timeout,
            "remaining": round(remaining),
            "wifi": wifi,
        }

    def manual_power(self, on: bool):
        """Manual power toggle from the web UI (deferred to watchdog thread)."""
        if on:
            self._last_activity = time.monotonic()
            if not self._wled_on:
                self._power_on_pending = True
        else:
            if self._wled_on:
                self._power_off_pending = True

    async def watchdog_loop(self):
        """Background task that turns WLED off after idle timeout and checks reachability.

        Never allowed to die. This task owns the only path that sets
        _wled_on, and the render thread gates all DDP output on it — so if
        this task raises, the strip goes dark permanently while YARG still
        reads as connected, with nothing in the log to say why. Any
        exception is logged and the loop resumes.
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
        # Initial reachability check (non-blocking)
        await asyncio.to_thread(self._api.is_on)
        await asyncio.to_thread(self._api.fetch_wifi_info)

        if not self._enabled:
            log.info("WLED power management: disabled (IDLE_TIMEOUT=0)")
            # Still check reachability and handle manual power periodically
            wifi_counter = 0
            while True:
                await asyncio.sleep(5)
                if self._power_on_pending:
                    self._power_on_pending = False
                    await asyncio.to_thread(self._power_on)
                if self._power_off_pending:
                    self._power_off_pending = False
                    await asyncio.to_thread(self._power_off)
                self._check_dark_while_active()
                wifi_counter += 1
                if wifi_counter >= 6:
                    wifi_counter = 0
                    await asyncio.to_thread(self._api.is_on)
                    await asyncio.to_thread(self._api.fetch_wifi_info)
            return

        log.info("WLED power management: enabled (%ds idle timeout)", self._idle_timeout)

        check_counter = 0
        while True:
            await asyncio.sleep(5)

            # Handle deferred power-on from on_activity() or manual_power()
            if self._power_on_pending:
                self._power_on_pending = False
                await asyncio.to_thread(self._power_on)

            if self._power_off_pending:
                self._power_off_pending = False
                await asyncio.to_thread(self._power_off)

            self._check_dark_while_active()

            check_counter += 1

            # Reachability + WiFi check every ~30s (6 × 5s)
            if check_counter >= 6:
                check_counter = 0
                if not self._wled_on:
                    await asyncio.to_thread(self._api.is_on)
                await asyncio.to_thread(self._api.fetch_wifi_info)

            if not self._wled_on:
                continue

            elapsed = time.monotonic() - self._last_activity
            if elapsed >= self._idle_timeout:
                await asyncio.to_thread(self._power_off)


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

        # Cue cross-fade state. When the engine signals a cue change, we
        # snapshot the previously sent frame into _fade_from and linearly
        # blend new frames against it for FADE_DURATION seconds. Removes
        # the one-frame all-black blink between cues.
        self._fade_from = bytearray(LED_COUNT * 3)
        self._fade_buf = bytearray(LED_COUNT * 3)
        self._last_sent = bytearray(LED_COUNT * 3)
        self._fade_until = 0.0
        self._last_cue_change_at = 0.0
        self._FADE_DURATION = 0.25  # seconds

    def run(self):
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

            # Cache zone colours — only re-read when palette changes
            palette_name = self._settings.palette_name
            if palette_name != self._cached_palette:
                self._cached_colors = self._settings.zone_colors
                self._cached_palette = palette_name

            # Advance time-based patterns (zone bitmasks computed from
            # wall-clock time — immune to asyncio event-loop congestion)
            self._engine.tick(time.monotonic())

            # Get effects (consumes and clears transient flags)
            effects = self._engine.get_effects()
            # Mapper scales frame-count effects (sparkle life) by FPS so
            # they hold their wall-clock duration at any render rate.
            effects["fps"] = fps

            # Brightness baked into mapper output (Phase 2)
            brightness = self._settings.brightness / 255.0

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
            )

            # Apply strobe (replace with pre-allocated black)
            if not self._engine.get_strobe_visible():
                pixel_data = self._black

            # Cue cross-fade: snapshot the last sent frame on cue change,
            # then blend incoming frames against it for FADE_DURATION.
            # Skipped when strobe is suppressing the frame to black, so
            # we don't fade *through* the strobe blackout.
            now = time.monotonic()
            cue_change_at = effects.get("cue_change_at") or 0.0
            if cue_change_at > self._last_cue_change_at:
                self._fade_from[:] = self._last_sent
                self._fade_until = cue_change_at + self._FADE_DURATION
                self._last_cue_change_at = cue_change_at

            if pixel_data is not self._black and now < self._fade_until:
                t = 1.0 - (self._fade_until - now) / self._FADE_DURATION
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

            # Send DDP every frame when WLED is on (no dedup — WiFi
            # can drop UDP packets, so always resend like LedFx does)
            ddp_sent = False
            if self._wled_power.is_on:
                self._sender.send_pixels(pixel_data)
                ddp_sent = True

            self._tracker.on_render(self._engine.zones,
                                    self._engine.strobe_rate,
                                    self._engine.bpm,
                                    ddp_sent=ddp_sent)
            self._frames_rendered += 1

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

    def stop(self):
        self._active = False

    def render_stats(self) -> dict:
        """Rolling timing stats for diagnostics / status page."""
        fps = self._settings.fps
        work = self._work_times
        gaps = self._frame_gaps
        if not work:
            return {"fps": fps, "rendered": 0, "skipped": 0, "stalls": 0,
                    "work_ms_avg": 0.0, "work_ms_max": 0.0,
                    "gap_ms_avg": 0.0, "gap_ms_max": 0.0,
                    "target_ms": round(1000.0 / fps, 1),
                    "ddp": self._sender.stats()}
        return {
            "fps": fps,
            "rendered": self._frames_rendered,
            "skipped": self._frames_skipped,
            "stalls": self._stall_count,
            "work_ms_avg": round(sum(work) / len(work), 2),
            "work_ms_max": round(max(work), 2),
            "gap_ms_avg": round(sum(gaps) / len(gaps), 2),
            "gap_ms_max": round(max(gaps), 2),
            "target_ms": round(1000.0 / fps, 1),
            "ddp": self._sender.stats(),
        }


async def main():
    idle_mins = IDLE_TIMEOUT // 60 if IDLE_TIMEOUT else 0
    log.info("YARG → WLED Stage Kit Bridge")
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
    wled_power = WLEDPowerManager(wled_api, IDLE_TIMEOUT)
    tracker = StatusTracker()
    settings = BridgeSettings()
    status_server = StatusServer(tracker, STATUS_HOST, STATUS_PORT, engine=engine,
                                 wled_power=wled_power, settings=settings)

    loop = asyncio.get_running_loop()

    # Start status web server
    await status_server.start()

    # Start UDP listener
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: YARGProtocol(engine, tracker, wled_power),
        local_addr=(YARG_LISTEN_HOST, YARG_LISTEN_PORT),
    )

    # Start status broadcast task
    broadcast_task = asyncio.create_task(tracker.broadcast_loop(wled_power=wled_power, settings=settings))

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
    transport.close()

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
