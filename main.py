"""YARG → WLED Stage Kit Bridge.

Listens for YARG UDP lighting packets, runs the Stage Kit cue engine,
renders 120 pixels, and sends them to WLED via DDP.
Automatically turns WLED on/off based on YARG activity.

The render loop runs on a dedicated thread (isolated from the asyncio
event loop) with adaptive perf_counter timing — inspired by LedFx's
Virtual.thread_function pattern.
"""

import asyncio
import signal
import sys
import threading
import time

from config import (
    YARG_LISTEN_HOST, YARG_LISTEN_PORT,
    WLED_HOST, WLED_DDP_PORT,
    LED_COUNT, TARGET_FPS, GLOBAL_BRIGHTNESS,
    STATUS_HOST, STATUS_PORT,
    IDLE_TIMEOUT,
)
from protocol.yarg_packet import parse_packet, CueByte
from protocol.ddp_sender import DDPSender
from protocol.wled_api import WLEDApi
from effects.cue_engine import CueEngine
from effects.mapper import LEDMapper
from status_server import StatusTracker, StatusServer
from settings import BridgeSettings


class YARGProtocol(asyncio.DatagramProtocol):
    """Receives YARG UDP packets and feeds the cue engine."""

    def __init__(self, engine: CueEngine, tracker: StatusTracker, wled_power: 'WLEDPowerManager'):
        self.engine = engine
        self.tracker = tracker
        self.wled_power = wled_power
        self._last_cue = -1
        self._last_strobe = -1

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        pkt = parse_packet(data)
        if pkt is None:
            return

        self.tracker.on_packet()
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


class WLEDPowerManager:
    """Manages WLED power state based on YARG activity."""

    def __init__(self, wled_api: WLEDApi, idle_timeout: int):
        self._api = wled_api
        self._idle_timeout = idle_timeout  # seconds, 0 = disabled
        self._last_activity = 0.0
        self._wled_on = False
        self._enabled = idle_timeout > 0

    def on_activity(self):
        """Called when a YARG packet is received."""
        self._last_activity = time.monotonic()
        if not self._wled_on:
            self._power_on()

    def on_test_activity(self):
        """Called when a test pattern is triggered from the web UI."""
        self.on_activity()

    def _power_on(self):
        if self._api.set_power(True):
            self._wled_on = True
            print("WLED: powered ON (YARG activity detected)")
        else:
            print("WLED: failed to power on via API")

    def _power_off(self):
        if self._api.set_power(False):
            self._wled_on = False
            print("WLED: powered OFF (idle timeout)")
        else:
            print("WLED: failed to power off via API")

    @property
    def is_on(self) -> bool:
        return self._wled_on

    def power_snapshot(self) -> dict:
        """Return current power state for the status page."""
        if not self._enabled:
            return {"enabled": False, "on": self._wled_on, "idle_seconds": 0, "timeout": 0, "remaining": 0}
        elapsed = time.monotonic() - self._last_activity if self._last_activity > 0 else 0.0
        remaining = max(0.0, self._idle_timeout - elapsed) if self._wled_on else 0.0
        return {
            "enabled": True,
            "on": self._wled_on,
            "idle_seconds": round(elapsed),
            "timeout": self._idle_timeout,
            "remaining": round(remaining),
        }

    def manual_power(self, on: bool):
        """Manual power toggle from the web UI."""
        if on:
            self._last_activity = time.monotonic()
            if not self._wled_on:
                self._power_on()
        else:
            if self._wled_on:
                self._power_off()

    async def watchdog_loop(self):
        """Background task that turns WLED off after idle timeout."""
        if not self._enabled:
            print(f"WLED power management: disabled (IDLE_TIMEOUT=0)")
            return

        print(f"WLED power management: enabled ({self._idle_timeout}s idle timeout)")

        while True:
            await asyncio.sleep(5)

            if not self._wled_on:
                continue

            elapsed = time.monotonic() - self._last_activity
            if elapsed >= self._idle_timeout:
                self._power_off()


class RenderThread(threading.Thread):
    """Dedicated render thread — completely isolated from the asyncio event loop.

    Reads engine state, runs the mapper, sends DDP packets, all on its own
    OS thread with time.sleep() + perf_counter adaptive timing.  This
    eliminates event-loop contention from SSE broadcasts, HTTP handlers,
    strobe toggles, and beat-pattern coroutines.

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

        # Timing
        self._interval = 1.0 / TARGET_FPS
        self._keepalive = 50.0  # seconds — well under WLED's 65s DDP timeout

        # Frame-skip tracking
        self._frames_rendered = 0
        self._frames_skipped = 0

        # Rolling jitter stats (last N frames)
        self._STATS_WINDOW = 200
        self._jitters: list[float] = []

        # Strobe black frame (pre-allocated, never changes)
        self._black = b'\x00' * (LED_COUNT * 3)

        # Cached palette colours — refreshed when palette name changes
        self._cached_palette = ""
        self._cached_colors: dict = {}

    def run(self):
        self._active = True
        last_pixel_data = b''
        last_send_time = 0.0
        interval = self._interval
        skip_threshold = interval * 2.0

        print(f"Render thread started: {TARGET_FPS} FPS, {LED_COUNT} LEDs → "
              f"{WLED_HOST}:{WLED_DDP_PORT}")

        next_frame = time.perf_counter()

        while self._active:
            next_frame += interval
            frame_start = time.perf_counter()

            # Frame-skip detection: if we're >2 frame periods behind,
            # drop this frame and reset the deadline
            drift = frame_start - (next_frame - interval)
            if drift > skip_threshold:
                self._frames_skipped += 1
                next_frame = time.perf_counter() + interval
                time.sleep(0.001)
                continue

            # Cache zone colours — only re-read when palette changes
            palette_name = self._settings.palette_name
            if palette_name != self._cached_palette:
                self._cached_colors = self._settings.zone_colors
                self._cached_palette = palette_name

            # Get effects (consumes and clears transient flags)
            effects = self._engine.get_effects()

            # Brightness baked into mapper output (Phase 2)
            brightness = self._settings.brightness / 255.0

            # Render pixels
            pixel_data = self._mapper.render(
                self._engine.zones,
                zone_colors=self._cached_colors,
                effects=effects,
                brightness=brightness,
            )

            # Apply strobe (replace with pre-allocated black)
            if not self._engine.get_strobe_visible():
                pixel_data = self._black

            # Send DDP only when WLED is powered on
            ddp_sent = False
            if self._wled_power.is_on:
                now = time.monotonic()
                if pixel_data != last_pixel_data or (now - last_send_time) >= self._keepalive:
                    self._sender.send_pixels(pixel_data)
                    last_pixel_data = pixel_data
                    last_send_time = now
                    ddp_sent = True
            else:
                last_pixel_data = b''

            self._tracker.on_render(self._engine.zones,
                                    self._engine.strobe_rate,
                                    self._engine.bpm,
                                    ddp_sent=ddp_sent)
            self._frames_rendered += 1

            # Rolling work-time stats
            work_ms = (time.perf_counter() - frame_start) * 1000.0
            if len(self._jitters) >= self._STATS_WINDOW:
                self._jitters[self._frames_rendered % self._STATS_WINDOW] = work_ms
            else:
                self._jitters.append(work_ms)

            # Adaptive sleep: subtract elapsed work from target interval
            sleep_time = next_frame - time.perf_counter()
            if sleep_time < 0:
                next_frame = time.perf_counter()
                sleep_time = 0.001
            time.sleep(sleep_time)

        print("Render thread stopped")

    def stop(self):
        self._active = False

    def render_stats(self) -> dict:
        """Rolling timing stats for diagnostics / status page."""
        jitters = self._jitters
        if not jitters:
            return {"fps": 0, "rendered": 0, "skipped": 0,
                    "work_ms_avg": 0.0, "work_ms_max": 0.0}
        return {
            "fps": TARGET_FPS,
            "rendered": self._frames_rendered,
            "skipped": self._frames_skipped,
            "work_ms_avg": round(sum(jitters) / len(jitters), 2),
            "work_ms_max": round(max(jitters), 2),
        }


async def main():
    idle_mins = IDLE_TIMEOUT // 60 if IDLE_TIMEOUT else 0
    print("=" * 60)
    print("  YARG → WLED Stage Kit Bridge")
    print(f"  Listening on {YARG_LISTEN_HOST}:{YARG_LISTEN_PORT}")
    print(f"  Sending DDP to {WLED_HOST}:{WLED_DDP_PORT}")
    print(f"  LED count: {LED_COUNT} | FPS: {TARGET_FPS}")
    print(f"  WLED idle timeout: {idle_mins}m" if IDLE_TIMEOUT else "  WLED idle timeout: disabled")
    print(f"  Status page: http://{STATUS_HOST}:{STATUS_PORT}/")
    print("=" * 60)

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

    # Start strobe background task
    strobe_task = asyncio.create_task(engine.run_strobe())

    # Start status broadcast task
    broadcast_task = asyncio.create_task(tracker.broadcast_loop(wled_power=wled_power, settings=settings))

    # Start render thread (Phase 3: isolated from asyncio event loop)
    render_thread = RenderThread(engine, mapper, sender, tracker, settings, wled_power)
    render_thread.start()

    # Start WLED idle watchdog
    watchdog_task = asyncio.create_task(wled_power.watchdog_loop())

    # Handle shutdown
    stop = asyncio.Event()

    def handle_signal():
        print("\nShutting down...")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    await stop.wait()

    # Cleanup
    render_thread.stop()
    render_thread.join(timeout=2.0)
    strobe_task.cancel()
    broadcast_task.cancel()
    watchdog_task.cancel()
    transport.close()
    sender.close()

    # Send all-black and turn off WLED on exit
    try:
        cleanup_sender = DDPSender(WLED_HOST, WLED_DDP_PORT)
        cleanup_sender.send_pixels(b'\x00' * LED_COUNT * 3)
        cleanup_sender.close()
        wled_api.set_power(False)
    except Exception:
        pass

    print("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
