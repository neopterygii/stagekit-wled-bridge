"""Capture file format for YARG lighting datagrams.

One JSON object per line (JSONL). The first line is a header; every later line
is one received datagram:

    {"format": "yarg-capture", "version": 1, "started": "2026-07-27T20:00:00Z", ...}
    {"t": 0.0,     "d": "WUFSRwQB..."}
    {"t": 0.01124, "d": "WUFSRwQB..."}

`t` is seconds since the *first* recorded packet, so every capture starts at
0.0 and replay can begin immediately. `d` is the raw datagram, base64-encoded.

Why JSONL and not a binary format: a capture is small (a song is a few MB), and
being able to `head`, `grep`, `wc -l` and diff a fixture is worth more here than
the bytes saved. A truncated last line — the normal result of a container being
killed mid-capture — costs exactly one packet instead of the whole file.

Captures contain protocol data only. No audio, no chart content, nothing
encrypted, so they are safe to commit as test fixtures.
"""

import base64
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

CAPTURE_FORMAT = "yarg-capture"
CAPTURE_VERSION = 1

# Auto-stop limits for a live recording. YARG sends ~90 datagrams/second, and a
# line costs roughly 100 bytes, so these are about an hour of continuous play —
# far longer than any song. They exist so a capture left running can't quietly
# fill the appdata share.
DEFAULT_MAX_PACKETS = 300_000
DEFAULT_MAX_BYTES = 32 * 1024 * 1024


@dataclass
class CaptureHeader:
    """First line of a capture file."""
    format: str = CAPTURE_FORMAT
    version: int = CAPTURE_VERSION
    started: str = ""
    note: str = ""
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        d = {"format": self.format, "version": self.version,
             "started": self.started, "note": self.note}
        d.update(self.extra)
        return json.dumps(d, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict) -> "CaptureHeader":
        known = {"format", "version", "started", "note"}
        return cls(
            format=d.get("format", ""),
            version=int(d.get("version", 0)),
            started=d.get("started", ""),
            note=d.get("note", ""),
            extra={k: v for k, v in d.items() if k not in known},
        )


@dataclass
class CapturePacket:
    """One recorded datagram: offset in seconds from the capture start."""
    t: float
    data: bytes


class CaptureError(Exception):
    """Raised for a file that isn't a readable capture."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class CaptureWriter:
    """Writes a capture file. Each `add()` appends one line.

    Line-buffered rather than accumulating in memory: a capture that ends with
    the container being stopped should still hold everything up to that moment.
    """

    def __init__(self, path, note: str = "", extra: dict | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.header = CaptureHeader(started=_now_iso(), note=note,
                                    extra=extra or {})
        self._fh = self.path.open("w", encoding="utf-8")
        self._fh.write(self.header.to_json() + "\n")
        self.packets = 0
        self.bytes_written = len(self.header.to_json()) + 1

    def add(self, t: float, data: bytes) -> None:
        line = '{"t":%.6f,"d":"%s"}\n' % (t, base64.b64encode(data).decode("ascii"))
        self._fh.write(line)
        self.packets += 1
        self.bytes_written += len(line)

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> "CaptureWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def write_capture(path, packets, note: str = "", extra: dict | None = None) -> Path:
    """Write an iterable of (t, data) pairs — or CapturePackets — to *path*.

    Used to build test fixtures; `DatagramRecorder` is the live path.
    """
    with CaptureWriter(path, note=note, extra=extra) as w:
        for p in packets:
            if isinstance(p, CapturePacket):
                w.add(p.t, p.data)
            else:
                t, data = p
                w.add(t, data)
    return Path(path)


def iter_capture(path) -> tuple[CaptureHeader, Iterator[CapturePacket]]:
    """Stream a capture. Returns the header and a generator over its packets.

    A malformed or truncated final line is skipped rather than raising — that is
    what a capture interrupted mid-write looks like, and the packets before it
    are still good.
    """
    fh = Path(path).open("r", encoding="utf-8")
    first = fh.readline()
    if not first.strip():
        fh.close()
        raise CaptureError(f"{path}: empty file, no capture header")
    try:
        header = CaptureHeader.from_dict(json.loads(first))
    except json.JSONDecodeError as exc:
        fh.close()
        raise CaptureError(f"{path}: first line is not a JSON header: {exc}") from exc
    if header.format != CAPTURE_FORMAT:
        fh.close()
        raise CaptureError(
            f"{path}: not a {CAPTURE_FORMAT} file (format={header.format!r})")
    if header.version > CAPTURE_VERSION:
        fh.close()
        raise CaptureError(
            f"{path}: capture version {header.version} is newer than this "
            f"reader supports ({CAPTURE_VERSION})")

    def packets() -> Iterator[CapturePacket]:
        try:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    yield CapturePacket(float(d["t"]), base64.b64decode(d["d"]))
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue  # truncated tail or stray line
        finally:
            fh.close()

    return header, packets()


def read_capture(path) -> tuple[CaptureHeader, list[CapturePacket]]:
    """Read a whole capture into memory. Fine for fixtures and songs."""
    header, gen = iter_capture(path)
    return header, list(gen)


class DatagramRecorder:
    """Buffers received datagrams off the UDP hot path.

    `record()` runs in the UDP callback ~90 times a second, so it does nothing
    but append to a deque — no encoding, no file I/O, no lock. `flush()` drains
    the deque and writes; the bridge calls it from a background task.

    Recording auto-stops at `max_packets`/`max_bytes` so a capture left running
    can't fill the appdata share.
    """

    def __init__(self, path, note: str = "", extra: dict | None = None,
                 max_packets: int = DEFAULT_MAX_PACKETS,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 clock=time.monotonic):
        self._writer = CaptureWriter(path, note=note, extra=extra)
        self._pending: deque = deque()
        self._clock = clock
        self._t0: float | None = None
        self._max_packets = max_packets
        self._max_bytes = max_bytes
        self.recording = True
        self.limit_hit = ""
        self.packets = 0

    @property
    def path(self) -> Path:
        return self._writer.path

    def record(self, data: bytes) -> None:
        """Hot path. Append only — never encode, write, or lock here."""
        if not self.recording:
            return
        now = self._clock()
        if self._t0 is None:
            self._t0 = now
        self._pending.append((now - self._t0, data))
        self.packets += 1
        if self.packets >= self._max_packets:
            self.recording = False
            self.limit_hit = f"packet limit ({self._max_packets})"

    def flush(self) -> int:
        """Write buffered datagrams. Returns how many were written."""
        pending = self._pending
        written = 0
        while pending:
            try:
                t, data = pending.popleft()
            except IndexError:  # drained concurrently
                break
            self._writer.add(t, data)
            written += 1
        if written:
            self._writer.flush()
        if self.recording and self._writer.bytes_written >= self._max_bytes:
            self.recording = False
            self.limit_hit = f"size limit ({self._max_bytes} bytes)"
        return written

    def close(self) -> int:
        """Flush whatever is left and close the file."""
        self.recording = False
        written = self.flush()
        self._writer.close()
        return written

    def snapshot(self) -> dict:
        """Capture state for the status page."""
        return {
            "recording": self.recording,
            "file": self._writer.path.name,
            "packets": self.packets,
            "bytes": self._writer.bytes_written,
            "pending": len(self._pending),
            "limit_hit": self.limit_hit,
        }
