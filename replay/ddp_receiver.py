"""Headless DDP receiver — a WLED controller's ears, without the WLED.

Reassembles DDP frames from UDP packets so tests can assert on what the bridge
actually put on the wire, and so a local replay can be watched without a
controller plugged in.

Preferred over running the WLED Simulator in CI: this is stdlib only, starts in
microseconds, and can assert on protocol details the simulator hides — the
sequence number cycling 1..15, and the PUSH flag landing on the last chunk of a
frame and no earlier.

    with DDPReceiver() as rx:
        sender = DDPSender("127.0.0.1", rx.port)
        sender.send_pixels(bytes(360))
        frame = rx.next_frame(timeout=1.0)
        assert frame.pixels == bytes(360)
"""

import socket
import struct
import threading
from dataclasses import dataclass, field

from protocol.ddp_sender import (
    DDP_FLAGS_PUSH,
    DDP_FLAGS_VER1,
    DDP_HEADER_LEN,
    DDP_PORT,
    DDP_TYPE_RGB24,
)


@dataclass
class DDPFrame:
    """One reassembled frame: every chunk that shared a sequence number."""
    seq: int
    pixels: bytes
    chunks: int = 1
    data_type: int = DDP_TYPE_RGB24
    offsets: list = field(default_factory=list)


class DDPReceiver:
    """Listens for DDP packets and reassembles them into frames.

    Binds port 0 by default, so tests get a free port and can run in parallel;
    read `.port` for the assigned one.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 max_frames: int = 4096):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self.host, self.port = self._sock.getsockname()
        self._frames: list[DDPFrame] = []
        self._max_frames = max_frames
        self._lock = threading.Lock()
        self._got_frame = threading.Condition(self._lock)
        self._partial = bytearray()
        self._partial_seq: int | None = None
        self._partial_chunks = 0
        self._partial_offsets: list[int] = []
        self._running = False
        self._thread: threading.Thread | None = None
        self.packets_received = 0
        self.bad_packets = 0

    # ── lifecycle ───────────────────────────────────────────────

    def start(self) -> "DDPReceiver":
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="ddp-rx",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._running = False
        try:
            # Unblock the recv by poking our own socket.
            self._sock.sendto(b"", (self.host, self.port))
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._sock.close()

    def __enter__(self) -> "DDPReceiver":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ── receive ─────────────────────────────────────────────────

    def _loop(self) -> None:
        self._sock.settimeout(0.2)
        while self._running:
            try:
                data, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                continue
            self._ingest(data)

    def _ingest(self, data: bytes) -> None:
        if len(data) < DDP_HEADER_LEN:
            self.bad_packets += 1
            return
        flags, seq, dtype, _dest, offset, length = struct.unpack(
            ">BBBB I H", data[:DDP_HEADER_LEN])
        payload = data[DDP_HEADER_LEN:DDP_HEADER_LEN + length]
        if len(payload) != length or not flags & DDP_FLAGS_VER1:
            self.bad_packets += 1
            return

        self.packets_received += 1

        with self._lock:
            # A new sequence number means the previous frame never got its PUSH
            # (a lost final chunk). Drop the partial rather than merging frames.
            if self._partial_seq is not None and seq != self._partial_seq:
                self._partial.clear()
                self._partial_chunks = 0
                self._partial_offsets = []

            self._partial_seq = seq
            if len(self._partial) < offset + length:
                self._partial.extend(bytes(offset + length - len(self._partial)))
            self._partial[offset:offset + length] = payload
            self._partial_chunks += 1
            self._partial_offsets.append(offset)

            if flags & DDP_FLAGS_PUSH:
                frame = DDPFrame(seq=seq, pixels=bytes(self._partial),
                                 chunks=self._partial_chunks, data_type=dtype,
                                 offsets=list(self._partial_offsets))
                self._frames.append(frame)
                if len(self._frames) > self._max_frames:
                    del self._frames[:len(self._frames) - self._max_frames]
                self._partial.clear()
                self._partial_seq = None
                self._partial_chunks = 0
                self._partial_offsets = []
                self._got_frame.notify_all()

    # ── inspection ──────────────────────────────────────────────

    @property
    def frames(self) -> list[DDPFrame]:
        with self._lock:
            return list(self._frames)

    @property
    def frame_count(self) -> int:
        with self._lock:
            return len(self._frames)

    def next_frame(self, timeout: float = 1.0, after: int = 0) -> DDPFrame | None:
        """Wait for frame number *after*+1 to arrive. None if it doesn't."""
        with self._got_frame:
            if not self._got_frame.wait_for(lambda: len(self._frames) > after,
                                            timeout=timeout):
                return None
            return self._frames[after]

    def wait_for_frames(self, count: int, timeout: float = 2.0) -> bool:
        with self._got_frame:
            return self._got_frame.wait_for(lambda: len(self._frames) >= count,
                                            timeout=timeout)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()


def main() -> None:
    """Print a summary line per received frame — a poor man's strip monitor."""
    import argparse

    ap = argparse.ArgumentParser(description="Headless DDP receiver")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DDP_PORT)
    args = ap.parse_args()

    rx = DDPReceiver(args.host, args.port)
    print(f"Listening for DDP on {rx.host}:{rx.port} — Ctrl+C to stop")
    seen = 0
    try:
        with rx:
            while True:
                frame = rx.next_frame(timeout=5.0, after=seen)
                if frame is None:
                    continue
                seen += 1
                px = frame.pixels
                n = len(px) // 3
                lit = sum(1 for i in range(n) if px[i * 3:i * 3 + 3] != b"\0\0\0")
                avg = sum(px) / len(px) if px else 0
                if seen % 20 == 1:  # ~2/s at 40 FPS
                    print(f"frame {seen:6d}  seq={frame.seq:2d}  "
                          f"chunks={frame.chunks}  pixels={n}  lit={lit:3d}  "
                          f"avg={avg:5.1f}")
    except KeyboardInterrupt:
        print(f"\nStopped. {seen} frames, {rx.packets_received} packets, "
              f"{rx.bad_packets} malformed.")


if __name__ == "__main__":
    main()
