"""Read what a WLED controller is displaying, on any firmware the rig has run.

The lag and frame-match tools need one thing from the device: the contents of
the live buffer that DDP writes into. WLED has exposed that two ways, and which
one exists depends on the build:

- **`/json/live`** (HTTP, polled) — 120 hex triples, already scaled by the
  strip brightness. This is what WLED 0.14.4 served. WLED 16 compiles it in
  *only* when websockets are disabled, so on a stock 16.x build it is a 501.
- **The websocket live peek** — connect to `/ws`, send `{"lv": true}`, and the
  device pushes a binary frame `'L', version, R,G,B, ...` every >=40 ms
  (`WS_LIVE_INTERVAL` in `wled00/ws.cpp`). Version 2 is a 2D matrix and carries
  width/height in two more header bytes.

`open_live()` tries the peek first and falls back to `/json/live`, and both
readers present the same interface, so the tools do not care which they got.
But the two sources differ in ways a measurement must account for:

1. **Timestamps.** A peek frame is stamped when it arrives; an HTTP reading is
   stamped at the midpoint of its round trip. The systematic offsets therefore
   differ, and absolute lags measured through one are not comparable with the
   other. Deltas within one source still are.
2. **Cadence.** The peek is a push at <=25 Hz, so a change waits up to one
   interval (~20 ms on average) before it can be seen. `cadence_ms()` reports
   the observed interval so a reader can allow for it.
3. **Scale.** Peek values are the raw buffer, *before* brightness; `/json/live`
   values are after it. `prescaled` says which, for anything comparing content.
4. **Off means black.** The peek reports zeros whenever WLED's global
   brightness is 0 (JSON `on: false`), even while DDP is visibly lighting the
   strip. The reader tracks the power state from the JSON the device pushes on
   every state change, and refuses to return a frame while it is off rather
   than hand back a black frame that looks like data.
5. **One viewer.** WLED keeps a single live-view client id; whoever sent
   `{"lv": true}` last gets the frames. Opening the live preview in the WLED
   web UI mid-probe silently takes the feed. The reader notices frames
   stopping and says so, rather than returning the last frame forever.

Either source reads the buffer at packet arrival, not the LEDs: the device
latches it within one >15 ms show-throttle window plus ~1 ms of SPI, and
neither source can see that part.

Standard library only, so the tools keep running from the bridge's venv.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import statistics
import struct
import threading
import time
import urllib.error
import urllib.request

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class PeekError(Exception):
    """The device's live view could not be read, and the reason why."""


def parse_live_frame(payload: bytes) -> bytes | None:
    """The RGB bytes of a binary live-peek frame, or None if it isn't one."""
    if len(payload) < 2 or payload[0] != ord("L"):
        return None
    offset = 4 if payload[1] == 2 else 2   # v2 = 2D matrix: + width, height
    data = payload[offset:]
    return data[: len(data) - len(data) % 3]


def rgb_triples(data: bytes) -> list[tuple[int, int, int]]:
    return [tuple(data[i:i + 3]) for i in range(0, len(data), 3)]


def _is_off(state: dict) -> bool | None:
    if "on" not in state and "bri" not in state:
        return None
    return not state.get("on", True) or state.get("bri", 1) == 0


class LivePeek:
    """Websocket live-peek reader. Use as a context manager, or open()/close()."""

    source = "websocket peek"
    prescaled = False
    pushed = True

    def __init__(self, host: str, port: int = 80, *, timeout: float = 3.0,
                 stale_after: float = 1.5, clock=time.monotonic,
                 history_limit: int = 100_000):
        self.host, self.port = host, port
        self.timeout = timeout
        self.stale_after = stale_after
        self.clock = clock
        self.history_limit = history_limit
        self._sock: socket.socket | None = None
        self._buf = b""
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._latest: tuple[bytes, float] | None = None
        self._history: list[tuple[float, float]] = []
        self._first_frame = threading.Event()
        self._closed_reason: str | None = None
        self._thread: threading.Thread | None = None
        self.off: bool | None = None       # None until the device reports state
        self.frames = 0

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> "LivePeek":
        try:
            self._sock = socket.create_connection((self.host, self.port),
                                                  timeout=self.timeout)
            self._handshake()
        except OSError as exc:
            self._drop()
            raise PeekError(f"could not open ws://{self.host}:{self.port}/ws: {exc}") from exc
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(target=self._run, name="wled-peek", daemon=True)
        self._thread.start()
        self._send(OP_TEXT, json.dumps({"lv": True}).encode())
        if not self._first_frame.wait(self.timeout):
            reason = self._closed_reason or "connected, but no live frames arrived"
            self.close()
            raise PeekError(f"websocket live peek: {reason}")
        return self

    def close(self) -> None:
        if self._sock is not None and self._closed_reason is None:
            try:
                self._send(OP_TEXT, json.dumps({"lv": False}).encode())
                self._send(OP_CLOSE, struct.pack(">H", 1000))
            except OSError:
                pass
        self._closed_reason = self._closed_reason or "closed"
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._drop()

    def __enter__(self) -> "LivePeek":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- reads --------------------------------------------------------------

    def latest(self) -> tuple[bytes, float]:
        """Newest frame's RGB bytes and arrival time, or PeekError saying why not."""
        with self._lock:
            latest, off, closed = self._latest, self.off, self._closed_reason
        if closed is not None:
            raise PeekError(f"websocket live peek {closed}")
        if latest is None:
            raise PeekError("no live frame received yet")
        if off:
            raise PeekError("WLED is off (on:false or bri 0) — the websocket peek "
                            "reads black while off, even if DDP is lighting the strip")
        age = self.clock() - latest[1]
        if age > self.stale_after:
            raise PeekError(
                f"no live frame for {age:.1f}s — the connection stalled, or another "
                f"client took WLED's single live-view slot (is the WLED UI's live "
                f"preview open?)")
        return latest

    def brightness(self) -> tuple[float, float]:
        data, t = self.latest()
        return float(sum(data)), t

    def leds(self) -> list[tuple[int, int, int]]:
        return rgb_triples(self.latest()[0])

    def history(self, since: float | None = None) -> list[tuple[float, float]]:
        """Every frame's (arrival time, total brightness) since *since*."""
        with self._lock:
            pts = list(self._history)
        return pts if since is None else [p for p in pts if p[0] >= since]

    def cadence_ms(self) -> float | None:
        pts = self.history()
        gaps = [b[0] - a[0] for a, b in zip(pts, pts[1:])]
        return statistics.median(gaps) * 1000 if gaps else None

    # -- protocol -----------------------------------------------------------

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        self._sock.sendall(
            (f"GET /ws HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
             f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during websocket handshake")
            raw += chunk
            if len(raw) > 16384:
                raise ConnectionError("oversized handshake response")
        head, self._buf = raw.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise ConnectionError(f"websocket upgrade refused: {lines[0]!r}")
        expect = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        headers = {k.strip().lower(): v.strip()
                   for k, _, v in (ln.partition(":") for ln in lines[1:])}
        if headers.get("sec-websocket-accept") != expect:
            raise ConnectionError("websocket accept key mismatch")

    def _send(self, opcode: int, payload: bytes) -> None:
        """Client frames must be masked (RFC 6455 5.3)."""
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            if self._sock is None:
                raise OSError("socket closed")
            self._sock.sendall(header + mask + masked)

    def _read_frame(self) -> tuple[bool, int, bytes] | None:
        """One frame from the buffer, or None if it hasn't fully arrived."""
        buf = self._buf
        if len(buf) < 2:
            return None
        fin, opcode = bool(buf[0] & 0x80), buf[0] & 0x0F
        masked, n, pos = bool(buf[1] & 0x80), buf[1] & 0x7F, 2
        if n == 126:
            if len(buf) < 4:
                return None
            n, pos = struct.unpack("!H", buf[2:4])[0], 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n, pos = struct.unpack("!Q", buf[2:10])[0], 10
        mask = b""
        if masked:                 # servers shouldn't, but don't choke if one does
            if len(buf) < pos + 4:
                return None
            mask, pos = buf[pos:pos + 4], pos + 4
        if len(buf) < pos + n:
            return None
        payload = buf[pos:pos + n]
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._buf = buf[pos + n:]
        return fin, opcode, payload

    def _run(self) -> None:
        message, message_op = b"", None
        while self._closed_reason is None:
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                self._closed_reason = self._closed_reason or f"connection lost: {exc}"
                break
            if not chunk:
                self._closed_reason = self._closed_reason or "connection closed by the device"
                break
            self._buf += chunk
            while (frame := self._read_frame()) is not None:
                fin, opcode, payload = frame
                if opcode == OP_PING:
                    try:
                        self._send(OP_PONG, payload)
                    except OSError:
                        pass
                    continue
                if opcode == OP_CLOSE:
                    self._closed_reason = "closed by the device"
                    break
                if opcode in (OP_TEXT, OP_BINARY):
                    message, message_op = payload, opcode
                elif opcode == OP_CONT:
                    message += payload
                else:
                    continue
                if fin and message_op is not None:
                    self._dispatch(message_op, message)
                    message, message_op = b"", None
        self._first_frame.set()    # unblock open() if we died before a frame

    def _dispatch(self, opcode: int, payload: bytes) -> None:
        t = self.clock()
        if opcode == OP_BINARY:
            data = parse_live_frame(payload)
            if data is None:
                return
            with self._lock:
                self._latest = (data, t)
                self._history.append((t, float(sum(data))))
                if len(self._history) > self.history_limit:
                    del self._history[: len(self._history) - self.history_limit]
                self.frames += 1
            self._first_frame.set()
            return
        # Text: WLED pushes {"state": {...}, "info": {...}} on connect and on
        # every state change — which is how we know when it is switched off.
        try:
            doc = json.loads(payload)
        except ValueError:
            return                 # "pong" and other non-JSON chatter
        if isinstance(doc, dict):
            state = doc.get("state", doc)
            off = _is_off(state) if isinstance(state, dict) else None
            if off is not None:
                with self._lock:
                    self.off = off


class HttpLive:
    """`/json/live` reader for firmware that still serves it (0.14.x, or 16.x
    built with websockets disabled). Polled; each reading stamped at the
    midpoint of its round trip."""

    source = "/json/live"
    prescaled = True
    pushed = False

    def __init__(self, host: str, *, timeout: float = 4.0, clock=time.monotonic):
        self.host, self.timeout, self.clock = host, timeout, clock

    def open(self) -> "HttpLive":
        self.latest()
        return self

    def close(self) -> None:
        pass

    def __enter__(self) -> "HttpLive":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self) -> tuple[dict, float]:
        start = self.clock()
        with urllib.request.urlopen(f"http://{self.host}/json/live",
                                    timeout=self.timeout) as resp:
            body = resp.read()
        return json.loads(body), (start + self.clock()) / 2

    def latest(self) -> tuple[bytes, float]:
        try:
            doc, t = self._get()
        except urllib.error.HTTPError as exc:
            hint = (" — WLED 16+ compiles /json/live out when websockets are "
                    "enabled; use the websocket peek") if exc.code == 501 else ""
            raise PeekError(f"/json/live returned HTTP {exc.code}{hint}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PeekError(f"/json/live read failed: {exc}") from exc
        leds = doc.get("leds") or []
        if not leds:
            raise PeekError("/json/live carried no leds")
        out = bytearray()
        for hexrgb in leds:
            v = int(hexrgb, 16)
            out += bytes(((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF))
        return bytes(out), t

    def brightness(self) -> tuple[float, float]:
        data, t = self.latest()
        return float(sum(data)), t

    def leds(self) -> list[tuple[int, int, int]]:
        return rgb_triples(self.latest()[0])

    def history(self, since: float | None = None) -> list[tuple[float, float]]:
        return []                  # polled, not pushed: callers sample it themselves

    def cadence_ms(self) -> float | None:
        return None


LIVE_SOURCES = ("auto", "ws", "http")


def open_live(host: str, prefer: str = "auto", **kwargs):
    """An open live-view reader: the websocket peek if the device offers one,
    else `/json/live`. PeekError lists why each source failed."""
    if prefer not in LIVE_SOURCES:
        raise ValueError(f"prefer must be one of {LIVE_SOURCES}")
    failures = []
    if prefer in ("auto", "ws"):
        try:
            return LivePeek(host, **kwargs).open()
        except PeekError as exc:
            failures.append(str(exc))
    if prefer in ("auto", "http"):
        try:
            return HttpLive(host).open()
        except PeekError as exc:
            failures.append(str(exc))
    raise PeekError("no live view available from WLED: " + "; ".join(failures))


# -- device state the probes check before trusting a reading ------------------

def realtime_source(host: str, timeout: float = 4.0) -> tuple[bool, str, str]:
    """(in realtime?, mode e.g. "DDP", sender IP) from `/json/info`.

    `lip` names whoever last wrote the realtime buffer — the direct way to tell
    that a second sender (YALCY's WLED output, a stray test) is fighting the
    bridge, which no amount of timing or content comparison can attribute.
    """
    with urllib.request.urlopen(f"http://{host}/json/info", timeout=timeout) as resp:
        info = json.load(resp)
    return bool(info.get("live")), str(info.get("lm") or ""), str(info.get("lip") or "")


def get_power(host: str, timeout: float = 4.0) -> bool:
    with urllib.request.urlopen(f"http://{host}/json/state", timeout=timeout) as resp:
        return bool(json.load(resp).get("on"))


def set_power(host: str, on: bool, timeout: float = 4.0) -> None:
    req = urllib.request.Request(
        f"http://{host}/json/state", data=json.dumps({"on": on}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()
