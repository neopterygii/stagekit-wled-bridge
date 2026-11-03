"""Tests for the WLED live-view reader the lag tools measure through.

The reader exists because WLED 16 dropped `/json/live` and the tools went on
printing "not seen" as if the strip had not reacted. So beyond decoding the
peek, these pin the failures it must report *loudly*: the device switched off
(the peek then reads black), the live-view slot taken by another client, the
connection dropping, and /json/live being compiled out.

The fake device speaks just enough RFC 6455 to drive the real client.

Run: python -m pytest tests/test_wled_peek.py -v
"""

import base64
import hashlib
import io
import json
import socket
import struct
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import wled_peek  # noqa: E402
from tools.wled_peek import HttpLive, LivePeek, PeekError, open_live  # noqa: E402


def _server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    first = (0x80 if fin else 0) | opcode
    n = len(payload)
    if n < 126:
        return bytes([first, n]) + payload
    return struct.pack("!BBH", first, 126, n) + payload


def _live(rgb: bytes, version: int = 1) -> bytes:
    head = b"L" + bytes([version]) + (b"\x0a\x0c" if version == 2 else b"")
    return head + rgb


class FakeWled(threading.Thread):
    """One-connection websocket device pushing live frames until told not to."""

    def __init__(self, rgb=bytes([10, 20, 30]) * 120, interval=0.01,
                 state=None, refuse=False):
        super().__init__(daemon=True)
        self.rgb, self.interval, self.refuse = rgb, interval, refuse
        self.state = state if state is not None else {"on": True, "bri": 128}
        self.pushing = threading.Event()
        self.pushing.set()
        self.stop = threading.Event()
        self.outbox: list[bytes] = []
        self.received: list[bytes] = []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.conn = None

    def _read_client_frame(self, conn):
        head = conn.recv(2)
        if len(head) < 2:
            return None
        n = head[1] & 0x7F
        if n == 126:
            n = struct.unpack("!H", conn.recv(2))[0]
        mask = conn.recv(4)
        data = b""
        while len(data) < n:
            data += conn.recv(n - len(data))
        return bytes(b ^ mask[i % 4] for i, b in enumerate(data))

    def run(self):
        conn, _ = self.srv.accept()
        self.conn = conn
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        if self.refuse:
            conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            conn.close()
            return
        key = [ln.split(b":", 1)[1].strip() for ln in req.split(b"\r\n")
               if ln.lower().startswith(b"sec-websocket-key")][0].decode()
        accept = base64.b64encode(
            hashlib.sha1((key + wled_peek.WS_GUID).encode()).digest()).decode()
        conn.sendall((f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        conn.sendall(_server_frame(0x1, json.dumps({"state": self.state,
                                                    "info": {}}).encode()))
        self.received.append(self._read_client_frame(conn))   # {"lv": true}
        try:
            while not self.stop.is_set():
                while self.outbox:
                    conn.sendall(self.outbox.pop(0))
                if self.pushing.is_set():
                    conn.sendall(_server_frame(0x2, _live(self.rgb)))
                time.sleep(self.interval)
        except OSError:
            pass

    def push_state(self, state: dict):
        self.outbox.append(_server_frame(0x1, json.dumps({"state": state}).encode()))

    def hang_up(self):
        self.stop.set()
        time.sleep(0.05)
        self.conn.close()

    def close(self):
        self.stop.set()
        try:
            self.srv.close()
            if self.conn:
                self.conn.close()
        except OSError:
            pass


@pytest.fixture
def device():
    dev = FakeWled()
    dev.start()
    yield dev
    dev.close()


def _wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- frame decoding ---------------------------------------------------------

def test_parses_a_strip_frame():
    assert wled_peek.parse_live_frame(_live(bytes([1, 2, 3, 4, 5, 6]))) == bytes([1, 2, 3, 4, 5, 6])


def test_a_matrix_frame_skips_its_width_and_height_bytes():
    assert wled_peek.parse_live_frame(_live(bytes([7, 8, 9]), version=2)) == bytes([7, 8, 9])


def test_non_live_binary_is_ignored_and_a_partial_triple_dropped():
    assert wled_peek.parse_live_frame(b"X\x01\x01\x02\x03") is None
    assert wled_peek.parse_live_frame(_live(bytes([1, 2, 3, 4]))) == bytes([1, 2, 3])


# --- websocket peek against a fake device -----------------------------------

def test_reads_pushed_frames_and_subscribes(device):
    with LivePeek("127.0.0.1", device.port, timeout=2.0) as peek:
        total, t = peek.brightness()
        assert total == (10 + 20 + 30) * 120
        assert peek.leds()[0] == (10, 20, 30)
        assert _wait_for(lambda: len(peek.history()) >= 5)
        assert peek.cadence_ms() == pytest.approx(10, abs=15)
    assert json.loads(device.received[0]) == {"lv": True}


def test_refuses_to_read_while_wled_is_off(device):
    # The peek reports zeros while off even if DDP is lighting the strip, so a
    # frame here would look like data and be wrong.
    with LivePeek("127.0.0.1", device.port, timeout=2.0) as peek:
        peek.brightness()
        device.push_state({"on": False, "bri": 128})
        assert _wait_for(lambda: peek.off is True)
        with pytest.raises(PeekError, match="WLED is off"):
            peek.brightness()
        device.push_state({"on": True, "bri": 128})
        assert _wait_for(lambda: peek.off is False)
        peek.brightness()


def test_says_when_the_feed_stops_rather_than_serving_the_last_frame(device):
    with LivePeek("127.0.0.1", device.port, timeout=2.0, stale_after=0.15) as peek:
        peek.brightness()
        device.pushing.clear()
        time.sleep(0.3)
        with pytest.raises(PeekError, match="single live-view slot"):
            peek.brightness()


def test_reports_the_device_hanging_up(device):
    with LivePeek("127.0.0.1", device.port, timeout=2.0) as peek:
        peek.brightness()
        device.hang_up()
        assert _wait_for(lambda: peek._closed_reason is not None)
        with pytest.raises(PeekError, match="closed|lost"):
            peek.brightness()


def test_a_refused_upgrade_is_a_peek_error():
    dev = FakeWled(refuse=True)
    dev.start()
    try:
        with pytest.raises(PeekError, match="upgrade refused"):
            LivePeek("127.0.0.1", dev.port, timeout=2.0).open()
    finally:
        dev.close()


def test_open_fails_if_no_frames_ever_arrive():
    dev = FakeWled()
    dev.pushing.clear()
    dev.start()
    try:
        with pytest.raises(PeekError, match="no live frames"):
            LivePeek("127.0.0.1", dev.port, timeout=0.3).open()
    finally:
        dev.close()


def test_a_fragmented_state_message_is_reassembled():
    peek = LivePeek("h")
    peek._buf = (_server_frame(0x1, b'{"state": {"on": ', fin=False)
                 + _server_frame(0x0, b'false}}'))
    frames = []
    while (f := peek._read_frame()) is not None:
        frames.append(f)
    assert [op for _, op, _ in frames] == [0x1, 0x0]
    peek._dispatch(0x1, frames[0][2] + frames[1][2])
    assert peek.off is True


# --- /json/live fallback ----------------------------------------------------

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_http_live_decodes_hex_triples(monkeypatch):
    body = json.dumps({"leds": ["FF0000", "000102"]}).encode()
    monkeypatch.setattr(wled_peek.urllib.request, "urlopen",
                        lambda url, timeout=0: _Resp(body))
    live = HttpLive("h")
    total, _ = live.brightness()
    assert total == 255 + 0 + 1 + 2
    assert live.leds() == [(255, 0, 0), (0, 1, 2)]
    assert live.prescaled and not live.pushed


def test_http_live_explains_a_501(monkeypatch):
    def raise_501(url, timeout=0):
        raise urllib.error.HTTPError(url, 501, "Not Implemented", {}, None)
    monkeypatch.setattr(wled_peek.urllib.request, "urlopen", raise_501)
    with pytest.raises(PeekError, match="websockets are enabled"):
        HttpLive("h").latest()


def test_http_live_with_no_leds_is_refused(monkeypatch):
    monkeypatch.setattr(wled_peek.urllib.request, "urlopen",
                        lambda url, timeout=0: _Resp(b"{}"))
    with pytest.raises(PeekError, match="no leds"):
        HttpLive("h").latest()


def test_open_live_falls_back_to_json_live(monkeypatch):
    def no_ws(self):
        raise PeekError("could not open ws")
    monkeypatch.setattr(LivePeek, "open", no_ws)
    monkeypatch.setattr(wled_peek.urllib.request, "urlopen",
                        lambda url, timeout=0: _Resp(b'{"leds": ["010101"]}'))
    assert open_live("h").source == "/json/live"


def test_open_live_lists_why_every_source_failed(monkeypatch):
    def no_ws(self):
        raise PeekError("could not open ws")

    def raise_501(url, timeout=0):
        raise urllib.error.HTTPError(url, 501, "Not Implemented", {}, None)
    monkeypatch.setattr(LivePeek, "open", no_ws)
    monkeypatch.setattr(wled_peek.urllib.request, "urlopen", raise_501)
    with pytest.raises(PeekError) as info:
        open_live("h")
    assert "could not open ws" in str(info.value)
    assert "501" in str(info.value)


def test_open_live_prefers_the_peek(device):
    view = open_live("127.0.0.1", port=device.port, timeout=2.0)
    try:
        assert view.source == "websocket peek"
    finally:
        view.close()
