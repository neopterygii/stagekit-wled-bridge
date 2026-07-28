"""Runtime capture control for the live bridge.

Owns the active `DatagramRecorder`, if any. Shared by two callers that both run
on the asyncio event loop, so no locking is needed between them:

- `YARGProtocol.datagram_received` reads `.recorder` on every datagram (~90/s)
  and, when one is active, hands it the bytes. One attribute load and a `None`
  check when idle.
- `StatusServer` starts and stops recording from `POST /api/capture`.

The file write does *not* happen on the UDP callback. `flush_loop()` drains the
recorder's buffer on a timer through `asyncio.to_thread`, keeping disk I/O off
the event loop entirely.
"""

import asyncio
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from replay.capture import DEFAULT_MAX_BYTES, DEFAULT_MAX_PACKETS, DatagramRecorder

log = logging.getLogger(__name__)

FLUSH_INTERVAL = 0.5  # seconds between buffer drains

_SAFE_NOTE = re.compile(r"[^A-Za-z0-9 ._+-]")


def _slug(note: str, limit: int = 40) -> str:
    """A filename-safe fragment of an operator's note."""
    cleaned = _SAFE_NOTE.sub("", note).strip().replace(" ", "-")
    return cleaned[:limit].strip("-")


class CaptureController:
    """Starts, stops and flushes datagram recordings."""

    def __init__(self, directory, max_packets: int = DEFAULT_MAX_PACKETS,
                 max_bytes: int = DEFAULT_MAX_BYTES):
        self.directory = Path(directory)
        self.recorder: DatagramRecorder | None = None
        self._max_packets = max_packets
        self._max_bytes = max_bytes
        self._started_at = 0.0
        self.last_file = ""
        self.last_error = ""

    # ── control ─────────────────────────────────────────────────

    def start(self, note: str = "") -> tuple[bool, str]:
        """Begin recording. Returns (ok, message)."""
        if self.recorder is not None:
            return False, f"already recording to {self.recorder.path.name}"

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = _slug(note)
        name = f"{stamp}-{slug}.jsonl" if slug else f"{stamp}.jsonl"
        path = self.directory / name

        try:
            self.recorder = DatagramRecorder(
                path, note=note, max_packets=self._max_packets,
                max_bytes=self._max_bytes)
        except OSError as exc:
            self.last_error = str(exc)
            log.warning("Capture: cannot start recording to %s: %s", path, exc)
            return False, f"cannot write {path}: {exc}"

        self._started_at = time.monotonic()
        self.last_error = ""
        log.info("Capture: recording YARG datagrams to %s", path)
        return True, name

    def stop(self) -> tuple[bool, str]:
        """End recording and flush. Returns (ok, message)."""
        rec = self.recorder
        if rec is None:
            return False, "not recording"
        self.recorder = None  # hot path stops seeing it immediately
        try:
            rec.close()
        except OSError as exc:
            self.last_error = str(exc)
            log.warning("Capture: error closing %s: %s", rec.path, exc)
            return False, f"error closing {rec.path.name}: {exc}"
        self.last_file = rec.path.name
        log.info("Capture: wrote %d datagrams to %s", rec.packets, rec.path)
        return True, f"{rec.path.name} ({rec.packets} datagrams)"

    # ── background flush ────────────────────────────────────────

    async def flush_loop(self, interval: float = FLUSH_INTERVAL) -> None:
        """Drain the recorder's buffer to disk on a timer.

        Never allowed to die: a capture failing is not a reason to take the
        lighting down, so an unexpected error is logged and the loop continues
        with recording stopped.
        """
        while True:
            try:
                await asyncio.sleep(interval)
                rec = self.recorder
                if rec is None:
                    continue
                await asyncio.to_thread(rec.flush)
                # A recorder that hit its cap stops itself; close the file so the
                # capture is complete on disk rather than missing its tail.
                if not rec.recording:
                    log.info("Capture: %s reached its %s — stopping",
                             rec.path.name, rec.limit_hit or "limit")
                    self.stop()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Capture: flush failed — stopping recording")
                self.last_error = "flush failed"
                try:
                    self.stop()
                except Exception:
                    log.exception("Capture: stop after flush failure also failed")

    def shutdown(self) -> None:
        """Close any open capture on bridge shutdown."""
        if self.recorder is not None:
            self.stop()

    # ── status ──────────────────────────────────────────────────

    def snapshot(self) -> dict:
        rec = self.recorder
        if rec is None:
            return {"recording": False, "file": "", "packets": 0, "bytes": 0,
                    "seconds": 0, "last_file": self.last_file,
                    "error": self.last_error, "dir": str(self.directory)}
        snap = rec.snapshot()
        snap["seconds"] = round(time.monotonic() - self._started_at, 1)
        snap["last_file"] = self.last_file
        snap["error"] = self.last_error
        snap["dir"] = str(self.directory)
        return snap

    def list_captures(self, limit: int = 20) -> list[dict]:
        """Newest captures on disk, for the dashboard."""
        try:
            files = sorted(self.directory.glob("*.jsonl"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        out = []
        for p in files[:limit]:
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"name": p.name, "bytes": st.st_size,
                        "modified": int(st.st_mtime)})
        return out
