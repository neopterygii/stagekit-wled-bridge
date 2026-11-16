"""The status server's HTTP layer: POST routing and the cross-origin refusal.

The control endpoints have no authentication. Before 2026-11-16 they also
accepted a POST from any web page, because a browser sends a cross-site POST
with a "simple" content type without a preflight. These tests drive the real
server over a loopback socket.

Run: python -m pytest tests/test_status_http.py -v
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import status_server  # noqa: E402
from status_server import StatusServer, StatusTracker, _same_origin  # noqa: E402


class _Settings:
    def snapshot(self):
        return {}


def _serve(coro_fn, **server_kwargs):
    """Start a StatusServer on a free loopback port and run coro_fn(server, port)."""
    async def run():
        server = StatusServer(StatusTracker(), "127.0.0.1", 0, **server_kwargs)
        listener = await asyncio.start_server(
            server._handle_connection, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        try:
            return await coro_fn(server, port)
        finally:
            # Not wait_closed(): it waits for every connection, and an event
            # stream never ends by itself. asyncio.run cancels what is left.
            listener.close()
    return asyncio.run(run())


async def _request(port, method, path, body=None, headers=None, raw_body=None):
    payload = raw_body if raw_body is not None else (
        json.dumps(body).encode() if body is not None else b"")
    lines = [f"{method} {path} HTTP/1.1"]
    hdrs = {"Host": f"127.0.0.1:{port}"}
    hdrs.update(headers or {})
    if payload:
        hdrs["Content-Length"] = str(len(payload))
    lines += [f"{k}: {v}" for k, v in hdrs.items() if v is not None]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + payload)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), timeout=5.0)
    writer.close()
    head, _, resp_body = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, head.decode().lower(), resp_body


def _recording(server):
    """Replace every POST handler with one that records its body."""
    calls = []
    for path in list(server._post_routes):
        server._post_routes[path] = (
            lambda body, _p=path: (calls.append((_p, body)) or (200, "done")))
    return calls


POST_PATHS = ["/api/test", "/api/power", "/api/settings", "/api/capture"]


# ── origin check ────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", POST_PATHS)
def test_a_cross_origin_post_is_refused_and_never_reaches_the_handler(path):
    async def go(server, port):
        calls = _recording(server)
        result = await _request(port, "POST", path, {"action": "stop"},
                                headers={"Origin": "http://evil.example"})
        return result, calls
    (status, _head, body), calls = _serve(go)
    assert status == 403
    assert json.loads(body)["status"] == "error"
    assert calls == []


@pytest.mark.parametrize("path", POST_PATHS)
def test_a_same_origin_post_reaches_its_handler(path):
    async def go(server, port):
        calls = _recording(server)
        result = await _request(port, "POST", path, {"action": "x"},
                                headers={"Origin": f"http://127.0.0.1:{port}"})
        return result, calls
    (status, _head, body), calls = _serve(go)
    assert status == 200
    assert json.loads(body) == {"status": "ok", "message": "done"}
    assert calls == [(path, {"action": "x"})]


def test_a_post_with_no_origin_is_accepted():
    # curl, scripts and the party watcher send no Origin.
    async def go(server, port):
        calls = _recording(server)
        result = await _request(port, "POST", "/api/capture", {"action": "stop"})
        return result, calls
    (status, _head, _body), calls = _serve(go)
    assert status == 200
    assert calls == [("/api/capture", {"action": "stop"})]


def test_the_origin_null_of_a_sandboxed_page_is_refused():
    async def go(server, port):
        calls = _recording(server)
        result = await _request(port, "POST", "/api/power", {"action": "off"},
                                headers={"Origin": "null"})
        return result, calls
    (status, _head, _body), calls = _serve(go)
    assert status == 403 and calls == []


def test_a_refused_post_is_logged(caplog):
    async def go(server, port):
        _recording(server)
        return await _request(port, "POST", "/api/test", {"action": "stop"},
                              headers={"Origin": "http://evil.example"})
    with caplog.at_level("WARNING"):
        _serve(go)
    assert any("evil.example" in r.getMessage() for r in caplog.records)


def test_a_cross_origin_get_is_still_served():
    # Reads change nothing, and without CORS headers the page cannot see them.
    async def go(_server, port):
        return await _request(port, "GET", "/api/status",
                              headers={"Origin": "http://evil.example"})
    status, head, _body = _serve(go)
    assert status == 200
    assert "access-control-allow-origin" not in head


@pytest.mark.parametrize("origin, host, allowed", [
    (None, "tower:36180", True),
    ("http://tower:36180", "tower:36180", True),
    ("http://TOWER:36180", "tower:36180", True),
    ("https://tower", "tower", True),
    ("http://tower:36180", "tower:8080", False),
    ("http://tower", "tower:36180", False),
    ("http://evil.example", "tower:36180", False),
    ("http://tower:36180.evil.example", "tower:36180", False),
    ("null", "tower:36180", False),
    ("", "tower:36180", False),
    ("http://tower:36180", None, False),
    ("tower:36180", "tower:36180", False),
])
def test_same_origin(origin, host, allowed):
    assert _same_origin(origin, host) is allowed


# ── bodies and routing ──────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b'"stop"', b"7"])
def test_a_body_that_is_not_a_json_object_reads_as_empty(raw):
    async def go(server, port):
        calls = _recording(server)
        await _request(port, "POST", "/api/test", raw_body=raw)
        return calls
    assert _serve(go) == [("/api/test", {})]


def test_a_post_with_no_body_reads_as_empty():
    async def go(server, port):
        calls = _recording(server)
        await _request(port, "POST", "/api/power")
        return calls
    assert _serve(go) == [("/api/power", {})]


def test_a_non_object_body_gets_a_json_error_from_the_real_handler():
    # Before the shared body reader, a JSON list reached handlers that call
    # body.get() and the connection closed with a bare 400 text reply.
    async def go(_server, port):
        return await _request(port, "POST", "/api/capture", raw_body=b"[1]")
    status, head, body = _serve(go)
    assert status >= 400
    assert "application/json" in head
    assert json.loads(body)["status"] == "error"


def test_an_error_from_a_handler_keeps_its_status_and_message():
    async def go(server, port):
        server._post_routes["/api/test"] = lambda _b: (409, "busy")
        return await _request(port, "POST", "/api/test", {})
    status, _head, body = _serve(go)
    assert status == 409
    assert json.loads(body) == {"status": "error", "message": "busy"}


def test_post_to_an_unknown_path_is_404():
    async def go(_server, port):
        return await _request(port, "POST", "/api/nope", {})
    assert _serve(go)[0] == 404


def test_post_to_a_get_only_path_is_404():
    async def go(_server, port):
        return await _request(port, "POST", "/api/status", {})
    assert _serve(go)[0] == 404


def test_get_routes_still_answer():
    async def go(_server, port):
        return [(await _request(port, "GET", p))[0]
                for p in ("/", "/api/status", "/api/settings", "/api/capture", "/api/wled")]
    assert _serve(go, settings=_Settings()) == [200] * 5


def test_a_bad_content_length_is_a_400():
    async def go(_server, port):
        return await _request(port, "POST", "/api/test",
                              headers={"Content-Length": "lots"})
    assert _serve(go)[0] == 400


def test_the_event_stream_sends_no_cors_header():
    async def go(_server, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET /events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        writer.close()
        return head.decode().lower()
    head = _serve(go)
    assert "text/event-stream" in head
    assert "access-control-allow-origin" not in head


def test_every_post_the_dashboard_makes_has_a_route():
    import re
    posted = set(re.findall(r"fetch\('(/api/\w+)',\s*\{\s*method:\s*'POST'",
                            status_server.STATUS_HTML))
    server = StatusServer(StatusTracker(), "127.0.0.1", 0)
    assert posted and posted <= set(server._post_routes)
