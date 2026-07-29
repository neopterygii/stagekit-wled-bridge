"""Extract full colour ramps for our palettes from the vendored LedFx/WLED sources.

Our 12 palettes in `settings.py` carry four colours each, because that is all
the Stage Kit's zone model can hold. Most of them were *derived by truncating*
a richer upstream gradient. The continuous layers (vocal ribbon, the biases,
cue gradients) need the whole family, not four swatches — so this script goes
back to the originals and pulls the full stop list.

Run it offline; paste its output into `settings.py`. The generated ramps are
committed literals on purpose: the deployed image ships only
`stagekit-wled-bridge/`, so runtime must never reach for the vendor clones.

    .venv/bin/python tools/extract_palette_ramps.py            # emit literals
    .venv/bin/python tools/extract_palette_ramps.py --check    # audit vs zone colours

Three upstream formats are parsed:

  * LedFx  `LEDFX_GRADIENTS` — CSS `linear-gradient(90deg, rgb(r,g,b) P%, ...)`
  * WLED   `NAME_gp[]`       — flat `pos,r,g,b` quads with pos over 0..255
  * WLED   `TProgmemRGBPalette16` — 16 evenly spaced `0xRRGGBB` or `CRGB::Name`
    entries, the names resolved against FastLED's `HTMLColorCode` enum.

**The audit is the point of `--check`.** A ramp is only usable if the four zone
colours already sit inside the family it describes — the base wash paints the
zone colours while every other layer draws from the ramp, on the same strip at
the same time. Where an upstream ramp disagrees with the four colours we
actually ship, the ramp is wrong for us and the palette should fall back to the
four-colour ring instead. Do not paste a ramp that fails its audit.
"""
import argparse
import colorsys
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import PALETTES  # noqa: E402

# Vendor clones live beside the bridge checkout, not inside it.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.dirname(REPO)
LEDFX_COLOR = os.path.join(VENDOR, "LedFx", "ledfx", "color.py")
WLED_PALETTES = os.path.join(VENDOR, "WLED", "wled00", "palettes.cpp")
FASTLED_SLIM = os.path.join(
    VENDOR, "WLED", "wled00", "src", "dependencies", "fastled_slim",
    "fastled_slim.h")

# ── Chromatic filter ─────────────────────────────────────────────────
# Upstream ramps are authored for a *spatial* gradient, so they routinely open
# on black and close on white (lava_gp does both). Those stops carry no hue: a
# layer remapped onto them would go dark or grey rather than take the palette's
# colour. Drop them, keep what actually says something, and renormalise.
V_MIN = 0.15   # below this the stop is effectively black
S_MIN = 0.25   # below this the stop is effectively grey/white
# Consecutive near-identical stops (Forest repeats DarkGreen) add LUT work and
# no information.
DEDUPE_DIST = 12
# A ramp is only worth having if it is richer than the four colours it would
# replace. Trimming can leave less than that: LedFx *Frost* reduces to
# blue -> cyan, which says less than our own four ice tones do. Below this,
# fall back to the four-colour ring.
MIN_STOPS = 3


def _hsv(rgb):
    return colorsys.rgb_to_hsv(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)


def _vendor_ref(name):
    """Short git ref of a vendor clone, for the generated header.

    AGENTS.md: check the clones' refs before trusting them. Recording the ref in
    the output is what makes a regenerated ramp diff explainable later.
    """
    import subprocess
    try:
        return subprocess.run(
            ["git", "-C", os.path.join(VENDOR, name), "rev-parse", "--short",
             "HEAD"],
            capture_output=True, text=True, timeout=10,
            check=True).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _read(path):
    if not os.path.exists(path):
        sys.exit(f"missing vendor source: {path}\n"
                 "Clone LedFx and WLED beside the bridge checkout, or pass "
                 "--vendor.")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ── Parsers ──────────────────────────────────────────────────────────

def parse_ledfx(text):
    """LEDFX_GRADIENTS -> {name: [(pos, (r,g,b)), ...]}."""
    block = re.search(r"LEDFX_GRADIENTS\s*=\s*\{(.*?)\n\}", text, re.S)
    if not block:
        sys.exit("could not locate LEDFX_GRADIENTS in LedFx color.py")
    out = {}
    for name, css in re.findall(r'"([^"]+)":\s*"([^"]+)"', block.group(1)):
        stops = []
        for r, g, b, pct in re.findall(
                r"rgb\((\d+),\s*(\d+),\s*(\d+)\)\s*([\d.]+)%", css):
            stops.append((float(pct) / 100.0, (int(r), int(g), int(b))))
        if stops:
            out[name] = stops
    return out


def parse_fastled_names(text):
    """HTMLColorCode enum -> {"MidnightBlue": (25,25,112), ...}."""
    out = {}
    for name, hexval in re.findall(r"^\s*(\w+)\s*=\s*0x([0-9A-Fa-f]{6})\s*,",
                                   text, re.M):
        v = int(hexval, 16)
        out[name] = ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)
    return out


def parse_wled_gp(text, symbol):
    """`const uint8_t NAME[] PROGMEM = {pos,r,g,b, ...}` -> stops."""
    m = re.search(
        r"const\s+(?:uint8_t|byte)\s+" + re.escape(symbol) +
        r"\s*\[\s*\]\s*PROGMEM\s*=\s*\{(.*?)\}\s*;", text, re.S)
    if not m:
        sys.exit(f"could not locate {symbol} in WLED palettes.cpp")
    nums = [int(n) for n in re.findall(r"\d+", re.sub(r"//[^\n]*", "",
                                                      m.group(1)))]
    if len(nums) % 4:
        sys.exit(f"{symbol}: expected pos,r,g,b quads, got {len(nums)} values")
    return [(nums[i] / 255.0, (nums[i + 1], nums[i + 2], nums[i + 3]))
            for i in range(0, len(nums), 4)]


def parse_wled_p16(text, symbol, names):
    """`const TProgmemRGBPalette16 NAME PROGMEM = {...}` -> 16 even stops."""
    m = re.search(
        r"const\s+TProgmemRGBPalette16\s+" + re.escape(symbol) +
        r"\s*PROGMEM\s*=\s*\{(.*?)\}\s*;", text, re.S)
    if not m:
        sys.exit(f"could not locate {symbol} in WLED palettes.cpp")
    body = re.sub(r"//[^\n]*", "", m.group(1))
    colors = []
    for tok in re.findall(r"CRGB::(\w+)|0x([0-9A-Fa-f]{6})", body):
        if tok[0]:
            if tok[0] not in names:
                sys.exit(f"{symbol}: unknown FastLED colour name {tok[0]}")
            colors.append(names[tok[0]])
        else:
            v = int(tok[1], 16)
            colors.append(((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF))
    if len(colors) != 16:
        sys.exit(f"{symbol}: expected 16 entries, got {len(colors)}")
    return [(i / 15.0, c) for i, c in enumerate(colors)]


# ── Conditioning ─────────────────────────────────────────────────────

def condition(stops):
    """Drop achromatic stops, dedupe, renormalise positions onto [0, 1]."""
    kept = []
    for pos, rgb in stops:
        _, s, v = _hsv(rgb)
        if v >= V_MIN and s >= S_MIN:
            kept.append((pos, rgb))
    if len(kept) < 2:
        return []

    deduped = [kept[0]]
    for pos, rgb in kept[1:]:
        pr, pg, pb = deduped[-1][1]
        if abs(pr - rgb[0]) + abs(pg - rgb[1]) + abs(pb - rgb[2]) > DEDUPE_DIST:
            deduped.append((pos, rgb))
    if len(deduped) < 2:
        return []

    lo = deduped[0][0]
    span = deduped[-1][0] - lo
    if span <= 0:
        return []
    return [(round((p - lo) / span, 4), rgb) for p, rgb in deduped]


def _hue_gap(a, b):
    """Cyclic hue distance in [0, 0.5]."""
    d = abs(a - b) % 1.0
    return min(d, 1.0 - d)


def _ramp_hues(ramp):
    """Every chromatic hue the ramp can interpolate to."""
    hues = []
    prev = None
    for _, stop in ramp:
        for t in range(11):
            if prev is None:
                mixed = stop
            else:
                f = t / 10.0
                mixed = tuple(int(prev[i] + (stop[i] - prev[i]) * f)
                              for i in range(3))
            mh, ms, mv = _hsv(mixed)
            if ms >= S_MIN and mv >= V_MIN:
                hues.append(mh)
        prev = stop
    return hues


def family_distance(rgb, hues):
    """Smallest hue distance from `rgb` to any hue in `hues`.

    Achromatic colours report 0 — they have no hue to be outside the family.
    """
    h, s, _ = _hsv(rgb)
    if s < S_MIN:
        return 0.0
    return min((_hue_gap(h, x) for x in hues), default=1.0)


def trim_to_zones(key, ramp):
    """Cut ramp ends that wander outside the family our four colours describe.

    The point of this script is to *recover* the colours the four-colour
    truncation threw away, so a stop being absent from `colors` is not by itself
    a reason to drop it — Sunset_Real's deep dusk blue is as much "sunset" as
    its orange. What we must not do is import a whole arc the palette never
    had: LedFx *Frost* continues past blue and cyan into purple and pink, and
    our Frost is ice.

    The two cases differ by position. An arc we never had hangs off one *end* of
    the ramp; a colour we truncated *through* sits in the middle, between stops
    we did keep. So trim the ends back to the last stop that is still in family
    and leave the interior alone.
    """
    zone_hues = [_hsv(c)[0] for c in PALETTES[key]["colors"].values()
                 if _hsv(c)[1] >= S_MIN]
    if not zone_hues:
        return ramp

    def in_family(rgb):
        h, s, v = _hsv(rgb)
        if s < S_MIN or v < V_MIN:
            return True   # achromatic: no hue to be out of family
        return min(_hue_gap(h, z) for z in zone_hues) <= AUDIT_TOL

    lo, hi = 0, len(ramp) - 1
    while lo < hi and not in_family(ramp[lo][1]):
        lo += 1
    while hi > lo and not in_family(ramp[hi][1]):
        hi -= 1
    kept = ramp[lo:hi + 1]
    if len(kept) < MIN_STOPS:
        return []

    base = kept[0][0]
    span = kept[-1][0] - base
    if span <= 0:
        return []
    return [(round((p - base) / span, 4), rgb) for p, rgb in kept]


def audit(key, ramp):
    """Both directions: zone colours inside the ramp, and ramp inside the zones.

    One direction is not enough. LedFx *Frost* contains blue, so our pale ice
    zone colours sit happily inside it — but the ramp also runs through purple
    and magenta, which is not what "ice and cold whites" puts on the strip. A
    ramp is only ours if the two sets describe the same family *both* ways.
    """
    zone_rgb = list(PALETTES[key]["colors"].values())
    ramp_hues = _ramp_hues(ramp)
    zone_hues = [_hsv(c)[0] for c in zone_rgb if _hsv(c)[1] >= S_MIN]

    fwd, fwd_who = 0.0, ""
    for zone, rgb in PALETTES[key]["colors"].items():
        d = family_distance(rgb, ramp_hues)
        if d > fwd:
            fwd, fwd_who = d, zone

    rev = max((min((_hue_gap(h, z) for z in zone_hues), default=1.0)
               for h in ramp_hues), default=1.0)
    return fwd, fwd_who, rev


# ── Source map ───────────────────────────────────────────────────────
# Our palette key -> (upstream kind, symbol). `default` and `neon` are ours,
# with no upstream, and are absent here by design: they fall back to the
# four-colour ring.
SOURCES = {
    "party":      ("wled16", "PartyColors_gc22"),
    "dancefloor": ("ledfx",  "Dancefloor"),
    "plasma":     ("ledfx",  "Plasma"),
    "lava":       ("wledgp", "lava_gp"),
    "ocean":      ("wled16", "OceanColors_p"),
    "forest":     ("wled16", "ForestColors_p"),
    "sunset":     ("wledgp", "Sunset_Real_gp"),
    "borealis":   ("ledfx",  "Borealis"),
    "frost":      ("ledfx",  "Frost"),
    "sakura":     ("wledgp", "Sakura_gp"),
}

# A zone colour further than this (cyclic hue) from everything the ramp can
# produce means the ramp and our four colours are not the same palette.
AUDIT_TOL = 0.12


def build():
    ledfx = parse_ledfx(_read(LEDFX_COLOR))
    wled = _read(WLED_PALETTES)
    names = parse_fastled_names(_read(FASTLED_SLIM))

    ramps = {}
    for key, (kind, symbol) in SOURCES.items():
        if kind == "ledfx":
            if symbol not in ledfx:
                sys.exit(f"{key}: no LedFx gradient named {symbol}")
            raw = ledfx[symbol]
        elif kind == "wledgp":
            raw = parse_wled_gp(wled, symbol)
        else:
            raw = parse_wled_p16(wled, symbol, names)
        ramps[key] = trim_to_zones(key, condition(raw))
    return ramps


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="audit each ramp against that palette's zone colours "
                         "instead of emitting literals")
    args = ap.parse_args()

    ramps = build()

    if args.check:
        print(f"{'palette':<12} {'stops':>5} {'zones->ramp':>12} "
              f"{'ramp->zones':>12}  verdict")
        print("-" * 66)
        bad = 0
        for key, ramp in sorted(ramps.items()):
            if not ramp:
                print(f"{key:<12} {0:>5} {'-':>12} {'-':>12}  EMPTY after filter")
                bad += 1
                continue
            fwd, who, rev = audit(key, ramp)
            ok = fwd <= AUDIT_TOL and rev <= AUDIT_TOL
            bad += not ok
            if ok:
                verdict = "ok"
            elif fwd > AUDIT_TOL:
                verdict = f"MISMATCH (zone {who} outside ramp)"
            else:
                verdict = "MISMATCH (ramp strays off-palette)"
            print(f"{key:<12} {len(ramp):>5} {fwd:>12.3f} {rev:>12.3f}  "
                  f"{verdict}")
        print(f"\n{len(ramps) - bad}/{len(ramps)} ramps agree with their zone "
              f"colours in both directions (tolerance {AUDIT_TOL}).")
        print("Palettes that mismatch should keep the four-colour ring "
              "fallback — do not paste their ramp.")
        return

    print("# Generated by tools/extract_palette_ramps.py — do not hand-edit.")
    print(f"# LedFx {_vendor_ref('LedFx')} ledfx/color.py, "
          f"WLED {_vendor_ref('WLED')} wled00/palettes.cpp.")
    print(f"# Filter: v>={V_MIN}, s>={S_MIN}, dedupe L1>{DEDUPE_DIST}, "
          f"trim to zone family +/-{AUDIT_TOL}.")
    for key, ramp in ramps.items():
        if not ramp:
            print(f'# {key}: too few chromatic stops survived — ring fallback.')
            continue
        fwd, who, rev = audit(key, ramp)
        if fwd > AUDIT_TOL or rev > AUDIT_TOL:
            # Emitting a ramp that failed its audit would put the wash and the
            # reactivity layers in different colour families at the same time.
            print(f'# {key}: audit failed (zones->ramp {fwd:.3f} via {who}, '
                  f'ramp->zones {rev:.3f}) — ring fallback.')
            continue
        print(f'\n# {key} — from {SOURCES[key][1]}')
        print('"ramp": [')
        for pos, rgb in ramp:
            print(f"    ({pos}, {rgb}),")
        print("],")


if __name__ == "__main__":
    main()
