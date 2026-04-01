"""YARG → WLED Stage Kit Bridge.

Listens for YARG UDP lighting packets, runs the Stage Kit cue engine,
renders 120 pixels, and sends them to WLED via DDP.
Automatically turns WLED on/off based on YARG activity.
"""

import asyncio
import signal
import sys
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


async def render_loop(engine: CueEngine, mapper: LEDMapper, sender: DDPSender, tracker: StatusTracker):
    """Main render loop: reads cue engine state, maps to pixels, sends DDP."""
    interval = 1.0 / TARGET_FPS
    brightness = GLOBAL_BRIGHTNESS / 255.0
    last_pixel_data = b''
    last_send_time = 0.0
    # WLED max timeout is 65000ms; resend well within that to prevent timeout
    KEEPALIVE_INTERVAL = 50.0

    print(f"Render loop started: {TARGET_FPS} FPS, {LED_COUNT} LEDs → {WLED_HOST}:{WLED_DDP_PORT}")

    while True:
        # Get current pixel data from zone bitmasks
        pixel_data = mapper.render(engine.zones)

        # Apply strobe
        if not engine.get_strobe_visible():
            pixel_data = b'\x00' * len(pixel_data)

        # Apply global brightness
        if brightness < 1.0:
            pixel_data = bytes(int(b * brightness) for b in pixel_data)

        # Send if frame changed OR keepalive interval elapsed
        now = time.monotonic()
        if pixel_data != last_pixel_data or (now - last_send_time) >= KEEPALIVE_INTERVAL:
            sender.send_pixels(pixel_data)
            last_pixel_data = pixel_data
            last_send_time = now

        tracker.on_render(engine.zones, engine.strobe_rate, engine.bpm)
        await asyncio.sleep(interval)


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
    status_server = StatusServer(tracker, STATUS_HOST, STATUS_PORT, engine=engine,
                                 wled_power=wled_power)

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
    broadcast_task = asyncio.create_task(tracker.broadcast_loop(wled_power=wled_power))

    # Start render loop
    render_task = asyncio.create_task(render_loop(engine, mapper, sender, tracker))

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
    render_task.cancel()
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
