"""Lightweight status web server using stdlib only.

Serves a live dashboard page and an SSE endpoint that streams
bridge state (cue, zones, strobe, BPM, packets/sec) in real time.
Includes built-in test pattern controls to trigger cues from the web UI.
"""

import asyncio
import json
import time
from http import HTTPStatus

from protocol.yarg_packet import CueByte, BeatByte, StrobeSpeed


# Reverse lookup: cue byte value → name
_CUE_NAMES = {v: k for k, v in vars(CueByte).items() if isinstance(v, int)}

# Test patterns available from the web UI
TEST_PATTERNS = {
    "default": CueByte.DEFAULT,
    "verse": CueByte.VERSE,
    "chorus": CueByte.CHORUS,
    "intro": CueByte.INTRO,
    "warm_automatic": CueByte.WARM_AUTOMATIC,
    "warm_manual": CueByte.WARM_MANUAL,
    "cool_automatic": CueByte.COOL_AUTOMATIC,
    "cool_manual": CueByte.COOL_MANUAL,
    "big_rock_ending": CueByte.BIG_ROCK_ENDING,
    "frenzy": CueByte.FRENZY,
    "searchlights": CueByte.SEARCHLIGHTS,
    "sweep": CueByte.SWEEP,
    "harmony": CueByte.HARMONY,
    "dischord": CueByte.DISCHORD,
    "stomp": CueByte.STOMP,
    "flare_slow": CueByte.FLARE_SLOW,
    "flare_fast": CueByte.FLARE_FAST,
    "silhouettes": CueByte.SILHOUETTES,
    "silhouettes_spotlight": CueByte.SILHOUETTES_SPOTLIGHT,
    "menu": CueByte.MENU,
    "score": CueByte.SCORE,
}


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
        self.test_active = False
        self.test_pattern = ""
        self.render_thread = None  # set after creation in main()

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

    def on_render(self, zones: list[int], strobe_rate: float, bpm: float, ddp_sent: bool = False):
        self.zones = list(zones)
        self.strobe_rate = strobe_rate
        self.bpm = bpm
        if ddp_sent:
            self.ddp_frames_sent += 1

    def on_beat(self, beat: int):
        self.last_beat = beat

    def snapshot(self, wled_power=None, settings=None) -> dict:
        d = {
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
            "test_active": self.test_active,
            "test_pattern": self.test_pattern,
        }
        if wled_power:
            d["wled_power"] = wled_power.power_snapshot()
        if settings:
            d["settings"] = settings.snapshot()
        if self.render_thread:
            d["render"] = self.render_thread.render_stats()
        return d

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._sse_queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._sse_queues:
            self._sse_queues.remove(q)

    async def broadcast_loop(self, wled_power=None, settings=None):
        """Pushes snapshots to all SSE subscribers at ~10Hz."""
        while True:
            data = self.snapshot(wled_power=wled_power, settings=settings)
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
<link rel="icon" type="image/png" href="data:image/png;base64,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">
<style>
  :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #e6edf3;
          --dim: #8b949e; --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
          --accent: #1f6feb; --accent-hover: #388bfd; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont,
         'Segoe UI', Helvetica, Arial, sans-serif; padding: 1rem; }
  h1 { font-size: 1.4rem; margin-bottom: 1rem; }
  h3 { color: var(--dim); margin-bottom: 0.5rem; }
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
  .zone-label { font-size: 0.7rem; color: var(--dim); text-transform: uppercase; margin-bottom: 4px;
               display: flex; align-items: center; gap: 6px; }
  .zone-swatch { display: inline-block; width: 10px; height: 10px; border-radius: 3px; }

  .log { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
         padding: 0.75rem; max-height: 300px; overflow-y: auto; font-family: 'SF Mono', Monaco,
         'Cascadia Code', monospace; font-size: 0.8rem; line-height: 1.6; }
  .log-entry { color: var(--dim); }
  .log-entry .ts { color: var(--blue); }
  .log-entry .cue { color: var(--green); font-weight: 600; }
  .log-entry .strobe { color: var(--yellow); }
  .log-entry .beat { color: var(--red); }

  /* Test Controls */
  .test-panel { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
                padding: 1rem; margin-bottom: 1rem; }
  .test-panel .section-label { font-size: 0.75rem; color: var(--dim); text-transform: uppercase;
                                letter-spacing: 0.05em; margin-bottom: 0.5rem; }
  .btn-grid { display: flex; flex-wrap: wrap; gap: 0.5rem; }
  .test-btn { background: var(--card); color: var(--text); border: 1px solid var(--border);
              border-radius: 6px; padding: 0.4rem 0.75rem; font-size: 0.8rem; cursor: pointer;
              transition: all 0.15s; font-family: inherit; }
  .test-btn:hover { background: var(--accent); border-color: var(--accent); }
  .test-btn.active { background: var(--accent); border-color: var(--accent-hover);
                     box-shadow: 0 0 8px rgba(31,111,235,0.4); }
  .test-btn.stop { border-color: var(--red); color: var(--red); }
  .test-btn.stop:hover { background: var(--red); color: var(--text); }
  .test-btn.strobe-btn { border-color: var(--yellow); color: var(--yellow); }
  .test-btn.strobe-btn:hover { background: var(--yellow); color: var(--bg); }
  .test-btn.strobe-btn.active { background: var(--yellow); color: var(--bg); }

  .bpm-control { display: flex; align-items: center; gap: 0.75rem; margin-top: 0.75rem; }
  .bpm-control label { font-size: 0.75rem; color: var(--dim); text-transform: uppercase; }
  .bpm-control input[type=range] { flex: 1; accent-color: var(--accent); }
  .bpm-control .bpm-val { font-size: 0.9rem; font-weight: 600; min-width: 3em; font-variant-numeric: tabular-nums; }

  .test-indicator { display: inline-block; font-size: 0.7rem; padding: 0.15rem 0.5rem;
                    border-radius: 4px; margin-left: 0.75rem; vertical-align: middle; }
  .test-indicator.on { background: var(--accent); color: white; }
  .test-indicator.off { display: none; }
</style>
</head>
<body>
<h1><img src="data:image/png;base64,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" alt="YARG" style="width:24px;height:24px;vertical-align:middle;margin-right:6px"> Stage Kit Bridge <span class="test-indicator off" id="test-badge">TEST MODE</span></h1>

<div class="grid">
  <div class="card">
    <div class="label">YARG Connection</div>
    <div class="value"><span class="status-dot off" id="dot"></span><span id="conn">Disconnected</span></div>
  </div>
  <div class="card">
    <div class="label">Current Cue</div>
    <div class="value" id="cue">&mdash;</div>
  </div>
  <div class="card">
    <div class="label">BPM</div>
    <div class="value" id="bpm">&mdash;</div>
  </div>
  <div class="card">
    <div class="label">Strobe</div>
    <div class="value" id="strobe">Off</div>
  </div>
  <div class="card">
    <div class="label">Packets/sec</div>
    <div class="value" id="pps">0</div>
  </div>
  <div class="card" id="ddp-card">
    <div class="label">DDP Frames</div>
    <div class="value" id="ddp">0</div>
    <div style="font-size:0.75rem;color:var(--dim);margin-top:0.25rem;font-variant-numeric:tabular-nums">
      <span id="ddp-send-time"></span>
      <span id="ddp-errors" style="color:var(--dim)"></span>
    </div>
  </div>
  <div class="card" id="render-card">
    <div class="label">Render</div>
    <div class="value" id="render-fps" style="font-size:1.2rem">—</div>
    <div style="font-size:0.75rem;color:var(--dim);margin-top:0.25rem;font-variant-numeric:tabular-nums">
      <span id="render-gap"></span> &middot;
      <span id="render-work"></span> &middot;
      <span id="render-stalls" style="color:var(--red)"></span>
    </div>
  </div>
  <div class="card" id="power-card">
    <div class="label">WLED Connection</div>
    <div class="value"><span class="status-dot off" id="power-dot"></span><span id="power-state">Unknown</span></div>
    <div id="power-timer" style="font-size:0.8rem;color:var(--dim);margin-top:0.35rem;font-variant-numeric:tabular-nums"></div>
    <div id="wifi-info" style="font-size:0.7rem;color:var(--dim);margin-top:0.25rem;font-variant-numeric:tabular-nums"></div>
    <button class="test-btn" id="btn-power" onclick="togglePower()" style="margin-top:0.5rem;font-size:0.75rem">Toggle</button>
  </div>
  <div class="card" id="brightness-card">
    <div class="label">Brightness</div>
    <div class="value" id="brightness-pct">100%</div>
    <div class="btn-grid" id="brightness-btns" style="margin-top:0.4rem">
      <button class="test-btn" data-brightness="10" onclick="setBrightness(10)">10%</button>
      <button class="test-btn" data-brightness="25" onclick="setBrightness(25)">25%</button>
      <button class="test-btn" data-brightness="50" onclick="setBrightness(50)">50%</button>
      <button class="test-btn" data-brightness="75" onclick="setBrightness(75)">75%</button>
      <button class="test-btn active" data-brightness="100" onclick="setBrightness(100)">100%</button>
    </div>
  </div>
  <div class="card" id="palette-card">
    <div class="label">Color Palette</div>
    <select id="palette-select" style="width:100%;margin-top:0.5rem;padding:0.4rem;border-radius:6px;
            border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:0.85rem;
            font-family:inherit;cursor:pointer">
      <option value="default">Default (RGBY)</option>
    </select>
    <div id="palette-preview" style="display:flex;gap:2px;margin-top:0.5rem;height:16px;border-radius:4px;overflow:hidden"></div>
  </div>
  <div class="card" id="fps-card">
    <div class="label">Target FPS</div>
    <div class="value" id="fps-val">40</div>
    <div class="btn-grid" id="fps-btns" style="margin-top:0.4rem"></div>
  </div>
  <div class="card" id="direction-card">
    <div class="label">LED Direction</div>
    <div class="value" id="direction-val">Normal</div>
    <button class="test-btn" id="btn-direction" onclick="toggleDirection()" style="margin-top:0.5rem;font-size:0.75rem">Reverse</button>
  </div>
</div>

<h3 style="margin-bottom:0.5rem">Zone Bitmasks</h3>
<div id="zones"></div>

<h3 style="margin:1rem 0 0.5rem">Test Patterns</h3>
<div class="test-panel">
  <div class="section-label">Cues</div>
  <div class="btn-grid" id="cue-btns">
    <button class="test-btn" data-pattern="default">Default</button>
    <button class="test-btn" data-pattern="verse">Verse</button>
    <button class="test-btn" data-pattern="chorus">Chorus</button>
    <button class="test-btn" data-pattern="intro">Intro</button>
    <button class="test-btn" data-pattern="warm_automatic">Warm Auto</button>
    <button class="test-btn" data-pattern="warm_manual">Warm Manual</button>
    <button class="test-btn" data-pattern="cool_automatic">Cool Auto</button>
    <button class="test-btn" data-pattern="cool_manual">Cool Manual</button>
    <button class="test-btn" data-pattern="big_rock_ending">Big Rock Ending</button>
    <button class="test-btn" data-pattern="frenzy">Frenzy</button>
    <button class="test-btn" data-pattern="searchlights">Searchlights</button>
    <button class="test-btn" data-pattern="sweep">Sweep</button>
    <button class="test-btn" data-pattern="harmony">Harmony</button>
    <button class="test-btn" data-pattern="dischord">Dischord</button>
    <button class="test-btn" data-pattern="stomp">Stomp</button>
    <button class="test-btn" data-pattern="flare_slow">Flare Slow</button>
    <button class="test-btn" data-pattern="flare_fast">Flare Fast</button>
    <button class="test-btn" data-pattern="silhouettes">Silhouettes</button>
    <button class="test-btn" data-pattern="silhouettes_spotlight">Silhouettes Spot</button>
    <button class="test-btn" data-pattern="menu">Menu</button>
    <button class="test-btn" data-pattern="score">Score</button>
  </div>

  <div class="section-label" style="margin-top:0.75rem">Strobe</div>
  <div class="btn-grid" id="strobe-btns">
    <button class="test-btn strobe-btn" data-strobe="slow">Slow</button>
    <button class="test-btn strobe-btn" data-strobe="medium">Medium</button>
    <button class="test-btn strobe-btn" data-strobe="fast">Fast</button>
    <button class="test-btn strobe-btn" data-strobe="fastest">Fastest</button>
    <button class="test-btn strobe-btn" data-strobe="off">Strobe Off</button>
  </div>

  <div class="bpm-control">
    <label>BPM</label>
    <input type="range" id="test-bpm" min="60" max="240" value="120" step="1">
    <span class="bpm-val" id="test-bpm-val">120</span>
  </div>

  <div class="btn-grid" style="margin-top:0.75rem">
    <button class="test-btn stop" id="btn-stop" onclick="sendTest('stop')" style="display:none">&#9632; Stop Test</button>
  </div>
</div>

<h3 style="margin:1rem 0 0.5rem">Event Log</h3>
<div class="log" id="log"></div>

<script>
const ZONE_NAMES = ['red','green','blue','yellow'];
let ZONE_COLORS = { red: '#f85149', green: '#3fb950', blue: '#58a6ff', yellow: '#d29922' };
const ZONE_OFF = '#21262d';
let lastCue = '', lastStrobe = -1, lastBeat = -1, activePattern = '';

function rgbToHex(r, g, b) {
  return '#' + ((1<<24)+(r<<16)+(g<<8)+b).toString(16).slice(1);
}

function updateZoneColors(colors) {
  if (!colors) return;
  for (const name of ZONE_NAMES) {
    if (colors[name]) {
      ZONE_COLORS[name] = rgbToHex(colors[name][0], colors[name][1], colors[name][2]);
    }
  }
  // Update zone swatches to match palette
  document.querySelectorAll('.zone-section').forEach(sec => {
    const label = sec.querySelector('.zone-label');
    if (!label) return;
    const zone = label.dataset.zone;
    if (zone && ZONE_COLORS[zone]) {
      const swatch = label.querySelector('.zone-swatch');
      if (swatch) swatch.style.background = ZONE_COLORS[zone];
    }
  });
}

function initZones() {
  const container = document.getElementById('zones');
  for (const name of ['red','green','blue','yellow']) {
    const sec = document.createElement('div');
    sec.className = 'zone-section';
    const idx = ZONE_NAMES.indexOf(name) + 1;
    sec.innerHTML = '<div class="zone-label" data-zone="' + name + '"><span class="zone-swatch" style="background:' + ZONE_COLORS[name] + '"></span>Zone ' + idx + '</div><div class="led-row" id="zone-' + name + '"></div>';
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

function togglePower() {
  fetch('/api/power', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ action: 'toggle' }) });
}

// Brightness step buttons
function setBrightness(pct) {
  const raw = Math.round(pct / 100 * 255);
  fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                           body: JSON.stringify({ brightness: raw }) });
  highlightBrightness(pct);
}
function highlightBrightness(pct) {
  document.querySelectorAll('#brightness-btns .test-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.brightness) === pct);
  });
  document.getElementById('brightness-pct').textContent = pct + '%';
}

// Palette select
const paletteSelect = document.getElementById('palette-select');
let palettesPopulated = false;
paletteSelect.addEventListener('change', () => {
  fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                           body: JSON.stringify({ palette: paletteSelect.value }) });
});

function updatePalettePreview(colors) {
  const preview = document.getElementById('palette-preview');
  preview.innerHTML = '';
  if (!colors) return;
  for (const [name, rgb] of Object.entries(colors)) {
    const swatch = document.createElement('div');
    swatch.style.cssText = 'flex:1;background:rgb(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ')';
    swatch.title = name;
    preview.appendChild(swatch);
  }
}

// FPS buttons
let fpsPopulated = false;
function setFps(val) {
  fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                           body: JSON.stringify({ fps: val }) });
  highlightFps(val);
}
function highlightFps(val) {
  document.querySelectorAll('#fps-btns .test-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.fps) === val);
  });
  document.getElementById('fps-val').textContent = val;
}

// Direction toggle
function toggleDirection() {
  const current = document.getElementById('direction-val').textContent.toLowerCase();
  const next = current === 'normal' ? 'reverse' : 'normal';
  fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                           body: JSON.stringify({ direction: next }) });
}

function updateSettings(s) {
  if (!s) return;
  // Sync brightness buttons from server value
  const pct = Math.round(s.brightness / 255 * 100);
  // Snap to nearest available step for display
  const steps = [10, 25, 50, 75, 100];
  const snapped = steps.reduce((prev, curr) => Math.abs(curr - pct) < Math.abs(prev - pct) ? curr : prev);
  highlightBrightness(snapped);
  // Populate palette dropdown once
  if (!palettesPopulated && s.palettes) {
    paletteSelect.innerHTML = '';
    for (const [key, label] of Object.entries(s.palettes)) {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = label;
      paletteSelect.appendChild(opt);
    }
    palettesPopulated = true;
  }
  if (paletteSelect.value !== s.palette) {
    paletteSelect.value = s.palette;
  }
  // Update color preview from palette colors
  if (s.colors) {
    updatePalettePreview(s.colors);
    updateZoneColors(s.colors);
  }
  // Populate FPS buttons once
  if (!fpsPopulated && s.fps_options) {
    const container = document.getElementById('fps-btns');
    container.innerHTML = '';
    for (const f of s.fps_options) {
      const btn = document.createElement('button');
      btn.className = 'test-btn';
      btn.dataset.fps = f;
      btn.textContent = f;
      btn.onclick = () => setFps(f);
      container.appendChild(btn);
    }
    fpsPopulated = true;
  }
  if (s.fps) highlightFps(s.fps);
  // Sync direction
  if (s.direction) {
    const label = s.direction === 'reverse' ? 'Reverse' : 'Normal';
    document.getElementById('direction-val').textContent = label;
    document.getElementById('btn-direction').textContent = s.direction === 'reverse' ? 'Normal' : 'Reverse';
  }
}

function fmtTime(s) {
  if (s <= 0) return '';
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return m > 0 ? m + 'm ' + sec + 's' : sec + 's';
}

function updateRender(r) {
  if (!r) return;
  const fps = document.getElementById('render-fps');
  const gap = document.getElementById('render-gap');
  const work = document.getElementById('render-work');
  const stalls = document.getElementById('render-stalls');

  fps.textContent = r.fps + ' FPS';
  gap.textContent = 'gap ' + r.gap_ms_avg.toFixed(1) + '/' + r.gap_ms_max.toFixed(1) + 'ms';
  work.textContent = 'work ' + r.work_ms_avg.toFixed(1) + '/' + r.work_ms_max.toFixed(1) + 'ms';

  const stall_text = r.stalls + ' stalls, ' + r.skipped + ' skips';
  stalls.textContent = stall_text;
  stalls.style.color = (r.stalls > 0 || r.skipped > 0) ? 'var(--red)' : 'var(--dim)';

  // Flag the card if gap max exceeds 2x target
  const card = document.getElementById('render-card');
  if (r.gap_ms_max > r.target_ms * 2) {
    card.style.borderColor = 'var(--red)';
  } else {
    card.style.borderColor = '';
  }

  // DDP send stats
  const ddp = r.ddp;
  if (ddp) {
    const sendEl = document.getElementById('ddp-send-time');
    const errEl = document.getElementById('ddp-errors');
    sendEl.textContent = 'send ' + ddp.send_us_avg.toFixed(0) + '/' + ddp.send_us_max.toFixed(0) + '\u00b5s';
    if (ddp.send_errors > 0) {
      errEl.textContent = ' \u00b7 ' + ddp.send_errors + ' errors';
      errEl.style.color = 'var(--red)';
      document.getElementById('ddp-card').style.borderColor = 'var(--red)';
    } else {
      errEl.textContent = '';
      errEl.style.color = 'var(--dim)';
      document.getElementById('ddp-card').style.borderColor = '';
    }
  }
}

function updatePower(p) {
  if (!p) return;
  const dot = document.getElementById('power-dot');
  const state = document.getElementById('power-state');
  const timer = document.getElementById('power-timer');
  const btn = document.getElementById('btn-power');
  const wifiEl = document.getElementById('wifi-info');
  // WiFi info (updated every ~30s)
  const w = p.wifi;
  if (w && w.signal) {
    const sigColor = w.signal >= 60 ? 'var(--green)' : w.signal >= 30 ? 'var(--yellow)' : 'var(--red)';
    wifiEl.innerHTML = 'WiFi: <span style="color:' + sigColor + '">' + w.signal + '%</span> (' + w.rssi + ' dBm) &middot; ch' + w.channel + ' &middot; ' + w.bssid;
  } else if (p.reachable) {
    wifiEl.textContent = 'WiFi: polling...';
  } else {
    wifiEl.textContent = '';
  }
  if (!p.reachable) {
    dot.className = 'status-dot off';
    state.textContent = 'Unreachable';
    btn.textContent = 'Turn On';
    timer.textContent = 'WLED device not responding';
    return;
  }
  if (p.on) {
    dot.className = 'status-dot on';
    state.textContent = 'On';
    btn.textContent = 'Turn Off';
    if (p.enabled && p.remaining > 0) {
      timer.textContent = 'Auto-off in ' + fmtTime(p.remaining);
    } else {
      timer.textContent = p.enabled ? 'Waiting for timeout...' : 'Auto-off disabled';
    }
  } else {
    dot.className = 'status-dot off';
    state.textContent = 'Off';
    btn.textContent = 'Turn On';
    timer.textContent = p.enabled ? 'Will auto-on when YARG starts' : 'Auto-off disabled';
  }
}

function sendTest(action, extra) {
  const body = { action: action };
  if (extra) Object.assign(body, extra);
  body.bpm = parseInt(document.getElementById('test-bpm').value);
  fetch('/api/test', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                       body: JSON.stringify(body) });
}

function highlightActivePattern(pattern) {
  document.querySelectorAll('#cue-btns .test-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.pattern === pattern);
  });
  activePattern = pattern;
}

// Wire up cue buttons
document.querySelectorAll('#cue-btns .test-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    sendTest('pattern', { pattern: btn.dataset.pattern });
    highlightActivePattern(btn.dataset.pattern);
  });
});

// Wire up strobe buttons
document.querySelectorAll('#strobe-btns .test-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    sendTest('strobe', { level: btn.dataset.strobe });
    document.querySelectorAll('#strobe-btns .test-btn').forEach(b => b.classList.remove('active'));
    if (btn.dataset.strobe !== 'off') btn.classList.add('active');
  });
});

// BPM slider
const bpmSlider = document.getElementById('test-bpm');
const bpmVal = document.getElementById('test-bpm-val');
bpmSlider.addEventListener('input', () => {
  bpmVal.textContent = bpmSlider.value;
  sendTest('bpm', { bpm: parseInt(bpmSlider.value) });
});

function update(d) {
  const dot = document.getElementById('dot');
  const conn = document.getElementById('conn');
  if (d.connected || d.test_active) { dot.className = 'status-dot on'; conn.textContent = d.test_active ? 'Test Mode' : 'Connected'; }
  else { dot.className = 'status-dot off'; conn.textContent = 'Disconnected'; }

  const badge = document.getElementById('test-badge');
  badge.className = d.test_active ? 'test-indicator on' : 'test-indicator off';

  // Show/hide Stop Test button based on test state
  document.getElementById('btn-stop').style.display = d.test_active ? '' : 'none';

  document.getElementById('bpm').textContent = d.bpm || '\\u2014';
  document.getElementById('pps').textContent = d.packets_per_sec;
  document.getElementById('ddp').textContent = d.ddp_frames_sent.toLocaleString();

  if (d.cue !== lastCue) {
    document.getElementById('cue').textContent = d.cue;
    addLog('<span class="cue">CUE \\u2192 ' + d.cue + '</span>');
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

  // Sync active button highlight with server state
  if (!d.test_active && activePattern) {
    highlightActivePattern('');
  } else if (d.test_active && d.test_pattern !== activePattern) {
    highlightActivePattern(d.test_pattern);
  }

  // Update WLED power info
  updatePower(d.wled_power);

  // Update render performance stats
  if (d.render) updateRender(d.render);

  // Update settings (brightness, palette)
  updateSettings(d.settings);
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
    """Async HTTP server serving the status page, SSE stream, and test controls."""

    def __init__(self, tracker: StatusTracker, host: str = "0.0.0.0", port: int = 8080,
                 engine=None, wled_power=None, settings=None):
        self.tracker = tracker
        self.host = host
        self.port = port
        self.engine = engine
        self.wled_power = wled_power
        self.settings = settings
        self._beat_task: asyncio.Task | None = None

    async def start(self):
        server = await asyncio.start_server(self._handle_connection, self.host, self.port)
        print(f"Status page: http://{self.host}:{self.port}/")
        return server

    def _start_test_beats(self, bpm: float):
        """Start a background task that fires synthetic beats at the given BPM."""
        self._stop_test_beats()
        self._beat_task = asyncio.ensure_future(self._run_test_beats(bpm))

    def _stop_test_beats(self):
        if self._beat_task is not None:
            self._beat_task.cancel()
            self._beat_task = None

    async def _run_test_beats(self, bpm: float):
        """Fires alternating MEASURE/STRONG beats and periodic keyframes at the given BPM."""
        from protocol.yarg_packet import KeyframeByte
        beat_count = 0
        try:
            while True:
                beat_count += 1
                beat_type = BeatByte.MEASURE if beat_count % 4 == 0 else BeatByte.STRONG
                if self.engine:
                    self.engine.on_beat(beat_type)
                    self.tracker.on_beat(beat_type)
                    # Fire keyframe every other beat for cues that listen for keyframes
                    if beat_count % 2 == 0:
                        self.engine.on_keyframe(KeyframeByte.NEXT)
                interval = 60.0 / max(bpm, 30.0) / 4  # sub-beats at 4× rate
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    def _handle_test_action(self, body: dict) -> tuple[int, str]:
        """Process a test control request. Returns (status_code, message)."""
        if self.engine is None:
            return 500, "Engine not connected"

        action = body.get("action", "")
        bpm = body.get("bpm", 120)

        if action == "pattern":
            pattern = body.get("pattern", "")
            if pattern not in TEST_PATTERNS:
                return 400, f"Unknown pattern: {pattern}"
            cue_byte = TEST_PATTERNS[pattern]
            self.engine.bpm = float(bpm)
            self.engine.on_cue(cue_byte)
            self.engine.on_strobe(StrobeSpeed.OFF)
            self.tracker.on_cue(cue_byte)
            self.tracker.test_active = True
            self.tracker.test_pattern = pattern
            self._start_test_beats(float(bpm))
            if self.wled_power:
                self.wled_power.on_test_activity()
            return 200, f"Playing {pattern}"

        elif action == "strobe":
            level = body.get("level", "off")
            strobe_map = {
                "off": StrobeSpeed.OFF, "slow": StrobeSpeed.SLOW,
                "medium": StrobeSpeed.MEDIUM, "fast": StrobeSpeed.FAST,
                "fastest": StrobeSpeed.FASTEST,
            }
            self.engine.on_strobe(strobe_map.get(level, StrobeSpeed.OFF))
            return 200, f"Strobe {level}"

        elif action == "bpm":
            self.engine.bpm = float(bpm)
            if self._beat_task is not None:
                self._start_test_beats(float(bpm))
            return 200, f"BPM set to {bpm}"

        elif action == "stop":
            self._stop_test_beats()
            self.engine.on_cue(CueByte.NO_CUE)
            self.engine.on_strobe(StrobeSpeed.OFF)
            self.tracker.on_cue(CueByte.NO_CUE)
            self.tracker.test_active = False
            self.tracker.test_pattern = ""
            return 200, "Stopped"

        return 400, f"Unknown action: {action}"

    def _handle_power_action(self, body: dict) -> tuple[int, str]:
        """Process a WLED power control request."""
        if self.wled_power is None:
            return 500, "Power manager not connected"
        action = body.get("action", "")
        if action == "on":
            self.wled_power.manual_power(True)
            return 200, "WLED powered on"
        elif action == "off":
            self.wled_power.manual_power(False)
            return 200, "WLED powered off"
        elif action == "toggle":
            is_on = self.wled_power._wled_on
            self.wled_power.manual_power(not is_on)
            return 200, f"WLED powered {'off' if is_on else 'on'}"
        return 400, f"Unknown power action: {action}"

    def _handle_settings_action(self, body: dict) -> tuple[int, str]:
        """Process a settings update request."""
        if self.settings is None:
            return 500, "Settings not connected"
        changed = []
        if "brightness" in body:
            try:
                self.settings.brightness = int(body["brightness"])
                changed.append(f"brightness={self.settings.brightness}")
            except (ValueError, TypeError):
                return 400, "Invalid brightness value"
        if "palette" in body:
            old = self.settings.palette_name
            self.settings.palette_name = str(body["palette"])
            if self.settings.palette_name != old:
                changed.append(f"palette={self.settings.palette_name}")
            elif str(body["palette"]) != old:
                return 400, f"Unknown palette: {body['palette']}"
        if "fps" in body:
            try:
                old_fps = self.settings.fps
                self.settings.fps = int(body["fps"])
                if self.settings.fps != old_fps:
                    changed.append(f"fps={self.settings.fps}")
            except (ValueError, TypeError):
                return 400, "Invalid FPS value"
        if "direction" in body:
            old_dir = self.settings.direction
            self.settings.direction = str(body["direction"])
            if self.settings.direction != old_dir:
                changed.append(f"direction={self.settings.direction}")
        if not changed:
            return 400, "No valid settings provided"
        return 200, "Updated: " + ", ".join(changed)

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            # Read headers, capture Content-Length
            content_length = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line in (b'\r\n', b'\n', b''):
                    break
                header = line.decode('utf-8', errors='replace').strip().lower()
                if header.startswith('content-length:'):
                    content_length = int(header.split(':', 1)[1].strip())

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
            elif path == '/api/test' and method == 'POST':
                raw = b''
                if content_length > 0:
                    raw = await asyncio.wait_for(reader.readexactly(min(content_length, 4096)), timeout=5.0)
                try:
                    req_body = json.loads(raw) if raw else {}
                except (json.JSONDecodeError, ValueError):
                    req_body = {}
                status, msg = self._handle_test_action(req_body)
                resp = json.dumps({"status": "ok" if status == 200 else "error", "message": msg}).encode()
                await self._send_response(writer, status, 'application/json', resp)
            elif path == '/api/power' and method == 'POST':
                raw = b''
                if content_length > 0:
                    raw = await asyncio.wait_for(reader.readexactly(min(content_length, 4096)), timeout=5.0)
                try:
                    req_body = json.loads(raw) if raw else {}
                except (json.JSONDecodeError, ValueError):
                    req_body = {}
                status, msg = self._handle_power_action(req_body)
                resp = json.dumps({"status": "ok" if status == 200 else "error", "message": msg}).encode()
                await self._send_response(writer, status, 'application/json', resp)
            elif path == '/api/settings' and method == 'POST':
                raw = b''
                if content_length > 0:
                    raw = await asyncio.wait_for(reader.readexactly(min(content_length, 4096)), timeout=5.0)
                try:
                    req_body = json.loads(raw) if raw else {}
                except (json.JSONDecodeError, ValueError):
                    req_body = {}
                status, msg = self._handle_settings_action(req_body)
                resp = json.dumps({"status": "ok" if status == 200 else "error", "message": msg}).encode()
                await self._send_response(writer, status, 'application/json', resp)
            elif path == '/api/settings' and method == 'GET':
                body = json.dumps(self.settings.snapshot() if self.settings else {}).encode()
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
