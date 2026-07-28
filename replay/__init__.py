"""Capture and replay of YARG lighting datagrams.

Records the datagrams the bridge receives, then replays them through the real
pipeline — `parse_packet` → `CueEngine` → `LEDMapper` → DDP — so a change can be
judged against a recorded performance instead of a guess.

Two replay paths share one file format:

- `replay.player` drives `RenderThread.render_frame` on a synthetic clock, in
  process, with no sockets. Deterministic, so tests can assert on frame bytes.
- `replay.cli` re-sends a capture over UDP at its original timing, for looking
  at the real strip.

`replay.ddp_receiver` is a headless DDP sink used by tests in place of a WLED
controller.

Captures hold protocol data only — no song audio and no chart content — so they
are safe to check in as fixtures.
"""

from replay.capture import (
    CAPTURE_FORMAT,
    CAPTURE_VERSION,
    CaptureHeader,
    CapturePacket,
    CaptureWriter,
    DatagramRecorder,
    iter_capture,
    read_capture,
    write_capture,
)

__all__ = [
    "CAPTURE_FORMAT",
    "CAPTURE_VERSION",
    "CaptureHeader",
    "CapturePacket",
    "CaptureWriter",
    "DatagramRecorder",
    "iter_capture",
    "read_capture",
    "write_capture",
]
