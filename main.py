"""YARG → WLED Stage Kit Bridge.

Listens for YARG UDP lighting packets, runs the Stage Kit cue engine,
renders 120 pixels, and sends them to WLED via DDP.
"""

import asyncio
import signal
import sys

from config import (
    YARG_LISTEN_HOST, YARG_LISTEN_PORT,
    WLED_HOST, WLED_DDP_PORT,
    LED_COUNT, TARGET_FPS, GLOBAL_BRIGHTNESS,
    STATUS_HOST, STATUS_PORT,
)
from protocol.yarg_packet import parse_packet, CueByte
from protocol.ddp_sender import DDPSender
from effects.cue_engine import CueEngine
from effects.mapper import LEDMapper
from status_server import StatusTracker, StatusServer


class YARGProtocol(asyncio.DatagramProtocol):
    """Receives YARG UDP packets and feeds the cue engine."""

    def __init__(self, engine: CueEngine, tracker: StatusTracker):
        self.engine = engine
        self.tracker = tracker
        self._last_cue = -1
        self._last_strobe = -1

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        pkt = parse_packet(data)
        if pkt is None:
            return

        self.tracker.on_packet()

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


async def render_loop(engine: CueEngine, mapper: LEDMapper, sender: DDPSender, tracker: StatusTracker):
    """Main render loop: reads cue engine state, maps to pixels, sends DDP."""
    interval = 1.0 / TARGET_FPS
    brightness = GLOBAL_BRIGHTNESS / 255.0

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

        sender.send_pixels(pixel_data)
        tracker.on_render(engine.zones, engine.strobe_rate, engine.bpm)
        await asyncio.sleep(interval)


async def main():
    print("=" * 60)
    print("  YARG → WLED Stage Kit Bridge")
    print(f"  Listening on {YARG_LISTEN_HOST}:{YARG_LISTEN_PORT}")
    print(f"  Sending DDP to {WLED_HOST}:{WLED_DDP_PORT}")
    print(f"  LED count: {LED_COUNT} | FPS: {TARGET_FPS}")
    print(f"  Status page: http://{STATUS_HOST}:{STATUS_PORT}/")
    print("=" * 60)

    engine = CueEngine()
    mapper = LEDMapper(LED_COUNT)
    sender = DDPSender(WLED_HOST, WLED_DDP_PORT)
    tracker = StatusTracker()
    status_server = StatusServer(tracker, STATUS_HOST, STATUS_PORT)

    loop = asyncio.get_running_loop()

    # Start status web server
    await status_server.start()

    # Start UDP listener
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: YARGProtocol(engine, tracker),
        local_addr=(YARG_LISTEN_HOST, YARG_LISTEN_PORT),
    )

    # Start strobe background task
    strobe_task = asyncio.create_task(engine.run_strobe())

    # Start status broadcast task
    broadcast_task = asyncio.create_task(tracker.broadcast_loop())

    # Start render loop
    render_task = asyncio.create_task(render_loop(engine, mapper, sender, tracker))

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
    transport.close()
    sender.close()

    # Send all-black to WLED on exit
    try:
        cleanup_sender = DDPSender(WLED_HOST, WLED_DDP_PORT)
        cleanup_sender.send_pixels(b'\x00' * LED_COUNT * 3)
        cleanup_sender.close()
    except Exception:
        pass

    print("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
