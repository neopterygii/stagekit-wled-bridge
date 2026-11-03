"""Compare what the bridge is sending with what the strip is displaying.

Timing cannot be measured on a static strip, but *content* can — and during a
pause the frame is unchanging, which makes this the one comparison that gets
easier rather than harder. If the device's realtime buffer has diverged from
the bridge's output, the strip is showing something nobody asked for, and that
would be a fault no latency measurement could see.

Bridge preview is 60 cells; the device is 120 LEDs, so each bridge cell is
compared against its LED pair. Realtime gamma is off on this rig. What the
device value should equal depends on the live view (see `tools.wled_peek`):

- websocket peek (WLED 16.x): the raw buffer, i.e. the bridge value itself;
- `/json/live` (0.14.x): the bridge value scaled by WLED's `bri`/255.

    python tools/frame_match.py [--live auto|ws|http]
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.wled_peek import LIVE_SOURCES, PeekError, open_live, realtime_source  # noqa: E402

MATCH_TOLERANCE = 24  # per-channel, 0-255


def bridge_cells(addr: str):
    d = json.load(urllib.request.urlopen(f"http://{addr}/api/status", timeout=6))
    s = d["preview"]["strip"]
    return [tuple(s[i:i + 3]) for i in range(0, len(s), 3)], d


def compare(cells, leds, scale: float) -> dict:
    """Per-channel differences between each bridge cell and its device LEDs."""
    ratio = len(leds) // len(cells) if cells else 1
    worst, worst_i, diffs = 0.0, -1, []
    for i, cell in enumerate(cells):
        for k in range(ratio):
            j = i * ratio + k
            if j >= len(leds):
                break
            exp = tuple(c * scale for c in cell)
            d = max(abs(e - g) for e, g in zip(exp, leds[j]))
            diffs.append(d)
            if d > worst:
                worst, worst_i = d, j
    return {
        "ratio": ratio,
        "mean": sum(diffs) / len(diffs) if diffs else 0.0,
        "worst": worst,
        "worst_led": worst_i,
        "bridge_sum": sum(sum(c) for c in cells) * ratio * scale,
        "device_sum": sum(sum(led) for led in leds),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wled", default="192.168.0.53")
    parser.add_argument("--bridge", default="192.168.0.230:36180")
    parser.add_argument("--live", choices=LIVE_SOURCES, default="auto")
    args = parser.parse_args(argv)

    try:
        with open_live(args.wled, prefer=args.live) as view:
            cells, status = bridge_cells(args.bridge)
            leds = view.leds()
            source, prescaled = view.source, view.prescaled
    except PeekError as exc:
        print(f"frame match failed: {exc}", file=sys.stderr)
        return 2
    if not cells:
        # Zero cells would compare nothing and "match" vacuously.
        print("frame match failed: the bridge preview is empty — it is not "
              "rendering (idle or no YARG stream), so there is nothing to "
              "compare against", file=sys.stderr)
        return 2

    live, mode, sender_ip = realtime_source(args.wled)
    bridge_ip = args.bridge.split(":")[0]
    if live and sender_ip and sender_ip != bridge_ip:
        print(f"!! WLED's realtime buffer is being written by {sender_ip} ({mode}), "
              f"not the bridge at {bridge_ip} — a second sender. Expect DIVERGED.")

    if prescaled:
        state = json.load(urllib.request.urlopen(f"http://{args.wled}/json/state",
                                                 timeout=6))
        bri = state.get("bri", 255)
        scale = bri / 255.0
        scale_note = f"bri={bri} (scale {scale:.3f})"
    else:
        scale = 1.0
        scale_note = "raw buffer (scale 1.000)"

    r = compare(cells, leds, scale)
    print(f"cue={status['cue']} paused={status['paused']} via {source}, {scale_note}")
    print(f"bridge cells={len(cells)} device leds={len(leds)} ratio={r['ratio']}")
    print(f"per-channel abs diff: mean {r['mean']:.1f}  worst {r['worst']:.0f} "
          f"at led {r['worst_led']}")
    print(f"bridge frame sum={r['bridge_sum']:.0f}  device frame sum={r['device_sum']}")
    ok = r["worst"] <= MATCH_TOLERANCE
    print("MATCH (device is showing what the bridge sent)" if ok else
          "DIVERGED — the strip is not showing the bridge's frame")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
