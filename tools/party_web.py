"""Phone page for tools/party_monitor.py: what the lights are doing and what looked wrong, read-only.

Served from inside the monitor (`--web 8733`), so it exists only while the monitor runs. It has no
controls at all: the bridge's own dashboard (:36180) is where settings change.

Who can reach it (same rules as ~/yarg/tools/x32_party_web.py):
  * it binds to this machine's Tailscale address (100.64.0.0/10) only, never 0.0.0.0, so guests on
    the house Wi-Fi can't open it
  * requests from outside 100.64.0.0/10 (or localhost, for testing) are refused anyway
"""

import ipaddress
import json
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TAILNET = ipaddress.ip_network("100.64.0.0/10")
LOCAL = ipaddress.ip_network("127.0.0.0/8")


def tailscale_ipv4():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout
        for line in out.split():
            if ipaddress.ip_address(line) in TAILNET:
                return line
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("100.100.100.100", 53))
            addr = s.getsockname()[0]
        return addr if ipaddress.ip_address(addr) in TAILNET else None
    except OSError:
        return None


def client_allowed(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip in TAILNET or ip in LOCAL


def state(session):
    with session.lock:
        snap, lines = dict(session.snap), list(session.lines)
    wled = dict(session.wled)
    det = session.detector
    open_eps = []
    if det is not None:
        from tools.party_monitor import KINDS
        for kind, ep in list(det.eps.items()):
            if ep.open:
                level, cls, _ = KINDS[kind]
                open_eps.append({"kind": kind, "level": level, "cls": cls, "msg": ep.msg})
    strip = (snap.get("preview") or {}).get("strip") or []
    render = snap.get("render") or {}
    return {
        "scene": snap.get("scene"), "cue": snap.get("cue"), "connected": snap.get("connected"),
        "paused": snap.get("paused"), "pps": snap.get("packets_per_sec"), "bpm": snap.get("bpm"),
        "auto_gen": snap.get("auto_gen"), "output_live": snap.get("output_live"),
        "strobe_hz": snap.get("strobe_hz"), "strip": strip,
        "render": {k: render.get(k) for k in ("fps", "gap_ms_max", "target_ms", "stalls", "skipped")},
        "version": (snap.get("version") or {}).get("label"),
        "song": det.song["label"] if det is not None and det.song else None,
        "wled": {k: wled.get(k) for k in ("live", "lip", "http_ms", "tcp_ms", "rssi", "freeheap", "error")},
        "open": open_eps, "lines": lines[-60:][::-1],
    }


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Lights Party</title>
<style>
:root{--bg:#111;--fg:#eee;--dim:#888;--card:#1c1c1c;--ok:#3c3;--warn:#e9a33a;--alert:#e44}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){--bg:#f6f6f6;--fg:#111;--dim:#666;--card:#fff}}
body{margin:0;padding:12px 16px;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
.card{background:var(--card);border-radius:10px;padding:10px 12px;margin:0 0 10px}
.row{display:flex;flex-wrap:wrap;gap:6px 14px}.k{color:var(--dim);font-size:12px}
#strip{display:flex;height:22px;border-radius:4px;overflow:hidden;margin-top:8px;background:#000}
#strip div{flex:1}.ALERT{color:var(--alert)}.WARN{color:var(--warn)}.SONG{color:var(--ok)}
.pill{display:inline-block;padding:2px 8px;border-radius:9px;margin:2px 4px 2px 0;font-size:13px}
.pill.ALERT{background:var(--alert);color:#fff}.pill.WARN{background:var(--warn);color:#000}
#lines div{font:12px/1.35 ui-monospace,monospace;padding:2px 0;border-bottom:1px solid #8882;word-break:break-word}
</style></head><body>
<div class="card"><div id="status" class="row"></div><div id="strip"></div></div>
<div class="card"><div class="k">open now</div><div id="open">—</div></div>
<div class="card"><div class="k">WLED</div><div id="wled" class="row"></div></div>
<div class="card"><div class="k">log (newest first)</div><div id="lines"></div></div>
<script>
const esc=s=>String(s??'—').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const kv=(k,v)=>`<div><div class="k">${k}</div>${esc(v)}</div>`;
async function tick(){
 try{const d=await (await fetch('state.json',{cache:'no-store'})).json();
  document.getElementById('status').innerHTML=kv('song',d.song)+kv('scene',d.scene+(d.paused?' (paused)':''))
   +kv('cue',d.cue)+kv('lighting',d.auto_gen?'auto-gen':'authored')+kv('bpm',d.bpm)+kv('pkts/s',d.pps)
   +kv('out',d.output_live?'live':'off')+kv('render gap max',d.render.gap_ms_max+' ms')+kv('bridge',d.version);
  const s=d.strip||[];let h='';for(let i=0;i+2<s.length;i+=3)h+=`<div style="background:rgb(${s[i]},${s[i+1]},${s[i+2]})"></div>`;
  document.getElementById('strip').innerHTML=h;
  document.getElementById('open').innerHTML=d.open.length?d.open.map(e=>`<div><span class="pill ${e.level}">${e.kind}</span>${esc(e.msg)}</div>`).join(''):'nothing — all good';
  const w=d.wled;document.getElementById('wled').innerHTML=w.error?`<span class="ALERT">${esc(w.error)}</span>`
   :kv('live',w.live)+kv('source',w.lip||'—')+kv('http',w.http_ms+' ms')+kv('tcp',w.tcp_ms+' ms')+kv('rssi',w.rssi)+kv('heap',w.freeheap);
  document.getElementById('lines').innerHTML=d.lines.map(l=>`<div class="${l.split(' ')[0]}">${esc(l)}</div>`).join('');
 }catch(e){document.getElementById('open').innerHTML='<span class="ALERT">monitor not answering</span>'}
}
tick();setInterval(tick,2000);
</script></body></html>"""


def make_handler(session):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if not client_allowed(self.client_address[0]):
                self.send_error(403)
                return
            if self.path in ("/", "/index.html"):
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            elif self.path == "/state.json":
                body, ctype = json.dumps(state(session)).encode(), "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return H


def serve(session, port, bind=None):
    bind = bind or tailscale_ipv4()
    if not bind:
        raise SystemExit("--web: no Tailscale address on this machine; pass --web-bind explicitly")
    ip = ipaddress.ip_address(bind)
    if ip not in TAILNET and ip not in LOCAL:
        raise SystemExit(f"--web: refusing to bind {bind}; tailnet (100.64.0.0/10) or localhost only")
    httpd = ThreadingHTTPServer((bind, port), make_handler(session))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://{bind}:{port}/"
