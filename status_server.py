"""Lightweight status web server using stdlib only.

Serves a live dashboard page and an SSE endpoint that streams
bridge state (cue, zones, strobe, BPM, packets/sec) in real time.
"""

import asyncio
import json
import time
from http import HTTPStatus

from protocol.yarg_packet import CueByte


# Reverse lookup: cue byte value → name
_CUE_NAMES = {v: k for k, v in vars(CueByte).items() if isinstance(v, int)}


class StatusTracker:
    """Collects bridge state for the status page."""

    def __init__(self):
        self.current_cue = CueByte.NO_CUE
        self.current_cue_name = "NO_CUE"
        self.bpm = 0.0
        self.strobe_rate = 0
        self.zones = [0, 0, 0, 0]
        self.packets_received = 0
        self.packets_per_sec = 0.0
        self.ddp_frames_sent = 0
        self.last_beat = 0
        self.connected = False

        self._pkt_count_window: list[float] = []
        self._sse_queues: list[asyncio.Queue] = []

    def on_packet(self):
        now = time.monotonic()
        self._pkt_count_window.append(now)
        self.packets_received += 1
        # Trim to last 2 seconds
        cutoff = now - 2.0
        self._pkt_count_window = [t for t in self._pkt_count_window if t > cutoff]
        self.packets_per_sec = len(self._pkt_count_window) / 2.0
        self.connected = True

    def on_cue(self, cue_byte: int):
        self.current_cue = cue_byte
        self.current_cue_name = _CUE_NAMES.get(cue_byte, f"UNKNOWN({cue_byte})")

    def on_render(self, zones: list[int], strobe_rate: float, bpm: float):
        self.zones = list(zones)
        self.strobe_rate = strobe_rate
        self.bpm = bpm
        self.ddp_frames_sent += 1

    def on_beat(self, beat: int):
        self.last_beat = beat

    def snapshot(self) -> dict:
        return {
            "cue": self.current_cue_name,
            "cue_id": self.current_cue,
            "bpm": round(self.bpm, 1),
            "strobe_hz": self.strobe_rate,
            "zones": {
                "red": f"{self.zones[0]:08b}",
                "green": f"{self.zones[1]:08b}",
                "blue": f"{self.zones[2]:08b}",
                "yellow": f"{self.zones[3]:08b}",
            },
            "zones_raw": self.zones,
            "packets_received": self.packets_received,
            "packets_per_sec": round(self.packets_per_sec, 1),
            "ddp_frames_sent": self.ddp_frames_sent,
            "last_beat": self.last_beat,
            "connected": self.connected,
        }

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._sse_queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._sse_queues:
            self._sse_queues.remove(q)

    async def broadcast_loop(self):
        """Pushes snapshots to all SSE subscribers at ~10Hz."""
        while True:
            data = self.snapshot()
            dead = []
            for q in self._sse_queues:
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                self._sse_queues.remove(q)
            await asyncio.sleep(0.1)


# ── Embedded HTML page ──────────────────────────────────────────────

STATUS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stage Kit Bridge</title>
<style>
  :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #e6edf3;
          --dim: #8b949e; --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont,
         'Segoe UI', Helvetica, Arial, sans-serif; padding: 1rem; }
  h1 { font-size: 1.4rem; margin-bottom: 1rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 0.75rem; margin-bottom: 1rem; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1rem; }
  .card .label { font-size: 0.75rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.05em; }
  .card .value { font-size: 1.5rem; font-weight: 600; margin-top: 0.25rem; font-variant-numeric: tabular-nums; }
  .status-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 0.5rem; }
  .status-dot.on { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .status-dot.off { background: var(--red); }

  .zone-strip { display: flex; gap: 2px; margin-top: 1rem; }
  .zone-strip h3 { margin-bottom: 0.5rem; font-size: 0.85rem; color: var(--dim); }
  .led-row { display: flex; gap: 2px; }
  .led { width: 28px; height: 28px; border-radius: 4px; border: 1px solid var(--border);
         transition: background 0.08s; }
  .zone-section { margin-bottom: 0.75rem; }
  .zone-label { font-size: 0.7rem; color: var(--dim); text-transform: uppercase; margin-bottom: 4px; }

  .log { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
         padding: 0.75rem; max-height: 300px; overflow-y: auto; font-family: 'SF Mono', Monaco,
         'Cascadia Code', monospace; font-size: 0.8rem; line-height: 1.6; }
  .log-entry { color: var(--dim); }
  .log-entry .ts { color: var(--blue); }
  .log-entry .cue { color: var(--green); font-weight: 600; }
  .log-entry .strobe { color: var(--yellow); }
  .log-entry .beat { color: var(--red); }
</style>
</head>
<body>
<h1>&#127911; Stage Kit Bridge</h1>

<div class="grid">
  <div class="card">
    <div class="label">Status</div>
    <div class="value"><span class="status-dot off" id="dot"></span><span id="conn">Disconnected</span></div>
  </div>
  <div class="card">
    <div class="label">Current Cue</div>
    <div class="value" id="cue">—</div>
  </div>
  <div class="card">
    <div class="label">BPM</div>
    <div class="value" id="bpm">—</div>
  </div>
  <div class="card">
    <div class="label">Strobe</div>
    <div class="value" id="strobe">Off</div>
  </div>
  <div class="card">
    <div class="label">Packets/sec</div>
    <div class="value" id="pps">0</div>
  </div>
  <div class="card">
    <div class="label">DDP Frames</div>
    <div class="value" id="ddp">0</div>
  </div>
</div>

<h3 style="color:var(--dim);margin-bottom:0.5rem">Zone Bitmasks</h3>
<div id="zones"></div>

<h3 style="color:var(--dim);margin:1rem 0 0.5rem">Event Log</h3>
<div class="log" id="log"></div>

<script>
const ZONE_COLORS = { red: '#f85149', green: '#3fb950', blue: '#58a6ff', yellow: '#d29922' };
const ZONE_OFF = '#21262d';
let lastCue = '', lastStrobe = -1, lastBeat = -1;

function initZones() {
  const container = document.getElementById('zones');
  for (const name of ['red','green','blue','yellow']) {
    const sec = document.createElement('div');
    sec.className = 'zone-section';
    sec.innerHTML = '<div class="zone-label">' + name + '</div><div class="led-row" id="zone-' + name + '"></div>';
    const row = sec.querySelector('.led-row');
    for (let i = 0; i < 8; i++) {
      const led = document.createElement('div');
      led.className = 'led';
      led.id = 'led-' + name + '-' + i;
      led.style.background = ZONE_OFF;
      row.appendChild(led);
    }
    container.appendChild(sec);
  }
}

function addLog(msg) {
  const log = document.getElementById('log');
  const now = new Date().toLocaleTimeString();
  const el = document.createElement('div');
  el.className = 'log-entry';
  el.innerHTML = '<span class="ts">[' + now + ']</span> ' + msg;
  log.prepend(el);
  while (log.children.length > 200) log.removeChild(log.lastChild);
}

function update(d) {
  const dot = document.getElementById('dot');
  const conn = document.getElementById('conn');
  if (d.connected) { dot.className = 'status-dot on'; conn.textContent = 'Connected'; }
  else { dot.className = 'status-dot off'; conn.textContent = 'Disconnected'; }

  document.getElementById('bpm').textContent = d.bpm || '—';
  document.getElementById('pps').textContent = d.packets_per_sec;
  document.getElementById('ddp').textContent = d.ddp_frames_sent.toLocaleString();

  if (d.cue !== lastCue) {
    document.getElementById('cue').textContent = d.cue;
    addLog('<span class="cue">CUE → ' + d.cue + '</span>');
    lastCue = d.cue;
  }

  const sh = d.strobe_hz;
  if (sh !== lastStrobe) {
    document.getElementById('strobe').textContent = sh > 0 ? sh + ' Hz' : 'Off';
    if (sh > 0) addLog('<span class="strobe">STROBE ' + sh + ' Hz</span>');
    else if (lastStrobe > 0) addLog('<span class="strobe">STROBE Off</span>');
    lastStrobe = sh;
  }

  if (d.last_beat !== 0 && d.last_beat !== lastBeat) {
    const names = {1:'MEASURE',2:'STRONG',3:'WEAK'};
    addLog('<span class="beat">BEAT ' + (names[d.last_beat]||d.last_beat) + '</span>');
    lastBeat = d.last_beat;
  }

  for (const name of ['red','green','blue','yellow']) {
    const mask = d.zones_raw[['red','green','blue','yellow'].indexOf(name)];
    for (let i = 0; i < 8; i++) {
      const on = (mask >> i) & 1;
      document.getElementById('led-' + name + '-' + i).style.background = on ? ZONE_COLORS[name] : ZONE_OFF;
    }
  }
}

initZones();

const evtSource = new EventSource('/events');
evtSource.onmessage = function(e) { update(JSON.parse(e.data)); };
evtSource.onerror = function() {
  document.getElementById('dot').className = 'status-dot off';
  document.getElementById('conn').textContent = 'Reconnecting...';
};
</script>
</body>
</html>
"""


class StatusServer:
    """Async HTTP server serving the status page and SSE stream."""

    def __init__(self, tracker: StatusTracker, host: str = "0.0.0.0", port: int = 8080):
        self.tracker = tracker
        self.host = host
        self.port = port

    async def start(self):
        server = await asyncio.start_server(self._handle_connection, self.host, self.port)
        print(f"Status page: http://{self.host}:{self.port}/")
        return server

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            # Read remaining headers (discard)
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b'\r\n', b'\n', b''):
                    break

            parts = request_line.decode('utf-8', errors='replace').strip().split()
            if len(parts) < 2:
                writer.close()
                return

            method, path = parts[0], parts[1]

            if path == '/events' and method == 'GET':
                await self._handle_sse(writer)
            elif path in ('/', '/index.html') and method == 'GET':
                await self._send_response(writer, 200, 'text/html', STATUS_HTML.encode())
            elif path == '/api/status' and method == 'GET':
                body = json.dumps(self.tracker.snapshot()).encode()
                await self._send_response(writer, 200, 'application/json', body)
            else:
                await self._send_response(writer, 404, 'text/plain', b'Not Found')
        except (asyncio.TimeoutError, ConnectionError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _send_response(self, writer: asyncio.StreamWriter, status: int, content_type: str, body: bytes):
        reason = HTTPStatus(status).phrase
        header = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        writer.write(header.encode() + body)
        await writer.drain()

    async def _handle_sse(self, writer: asyncio.StreamWriter):
        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "\r\n"
        )
        writer.write(header.encode())
        await writer.drain()

        q = self.tracker.subscribe()
        try:
            while True:
                data = await q.get()
                msg = f"data: {json.dumps(data)}\n\n"
                writer.write(msg.encode())
                await writer.drain()
        except (ConnectionError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            self.tracker.unsubscribe(q)
