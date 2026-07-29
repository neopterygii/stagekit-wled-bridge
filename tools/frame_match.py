"""Compare what the bridge is sending with what the strip is displaying.

Timing cannot be measured on a static strip, but *content* can — and during a
pause the frame is unchanging, which makes this the one comparison that gets
easier rather than harder. If the device's realtime buffer has diverged from
the bridge's output, the strip is showing something nobody asked for, and that
would be a fault no latency measurement could see.

Bridge preview is 60 cells; the device is 120 LEDs; the device is the bridge
value scaled by bri/255 (128/255 here) with realtime gamma off. So compare
device LED pairs against their bridge cell, after scaling.
"""
import json, sys, urllib.request

def bridge_cells(addr="192.168.0.230:36180"):
    d = json.load(urllib.request.urlopen(f"http://{addr}/api/status", timeout=6))
    s = d["preview"]["strip"]
    return [tuple(s[i:i+3]) for i in range(0, len(s), 3)], d

def device_leds(host="192.168.0.53"):
    d = json.load(urllib.request.urlopen(f"http://{host}/json/live", timeout=6))
    out = []
    for h in d["leds"]:
        v = int(h, 16)
        out.append(((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF))
    return out

cells, status = bridge_cells()
leds = device_leds()
state = json.load(urllib.request.urlopen("http://192.168.0.53/json/state", timeout=6))
bri = state.get("bri", 255)
scale = bri / 255.0
ratio = len(leds) // len(cells) if cells else 1

worst = 0.0; worst_i = -1; diffs = []
for i, cell in enumerate(cells):
    for k in range(ratio):
        j = i * ratio + k
        if j >= len(leds): break
        exp = tuple(c * scale for c in cell)
        got = leds[j]
        d = max(abs(e - g) for e, g in zip(exp, got))
        diffs.append(d)
        if d > worst: worst, worst_i = d, j

mean = sum(diffs) / len(diffs) if diffs else 0
print(f"cue={status['cue']} paused={status['paused']} bri={bri} (scale {scale:.3f})")
print(f"bridge cells={len(cells)} device leds={len(leds)} ratio={ratio}")
print(f"per-channel abs diff: mean {mean:.1f}  worst {worst:.0f} at led {worst_i}")
print(f"bridge frame sum={sum(sum(c) for c in cells)*ratio*scale:.0f}  device frame sum={sum(sum(l) for l in leds)}")
verdict = "MATCH (device is showing what the bridge sent)" if worst <= 24 else \
          "DIVERGED — the strip is not showing the bridge's frame"
print(verdict)
sys.exit(0 if worst <= 24 else 1)
