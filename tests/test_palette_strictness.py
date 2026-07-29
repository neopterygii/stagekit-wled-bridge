"""Tests for palette strictness — the reactivity layers following the palette.

Several Phase 4-6 layers paint from fixed colour tables of their own (the vocal
ribbon's full-spectrum rainbow, the per-performer hues, the section hues, the
cue gradients, the star-power tint), so they looked correct under `default`
(RGBY) and foreign under every other palette. `effects/palette_map.py` routes
each of those colours through a ring built from the palette, and
`palette_strictness` scales how far.

The central assertion is **family membership, not set membership**: a remapped
colour landing *between* two ramp stops is the intended behaviour, since the
whole point is to recover the colours the four-colour Stage Kit truncation threw
away. So these tests ask whether a hue is something the ring can produce, never
whether it equals one of the four zone colours.

Run: python -m pytest tests/test_palette_strictness.py -v
"""

import colorsys
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from effects.mapper import (  # noqa: E402
    LEDMapper, MAPPED_REGION, VOCAL_CHROMA, PERFORMER_COLORS,
    CAMERA_CHANNEL_COLORS, SP_TINT,
)
from effects.gradient import GRADIENTS  # noqa: E402
from effects.palette_map import build_ring, remap, remap_at  # noqa: E402
from protocol.yarg_packet import Performer  # noqa: E402
from settings import PALETTES  # noqa: E402

# Four palettes with different shapes: our own RGBY fallback, a hue-narrow warm
# ramp, a hue-narrow cool fallback, and a wide upstream ramp.
PALETTE_KEYS = ["default", "lava", "frost", "borealis"]

# A remapped hue must be within this of something the ring can produce. The
# slack covers the LUT's smootherstep quantisation and the 8-bit rounding in
# the blend loops, not a genuinely foreign colour.
HUE_TOL = 0.06
# Below this saturation a colour has no meaningful hue, so it is exempt — white
# accents are supposed to stay white.
SAT_FLOOR = 0.20


def _hsv(rgb):
    return colorsys.rgb_to_hsv(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)


def _hue_gap(a, b):
    d = abs(a - b) % 1.0
    return min(d, 1.0 - d)


def ring_for(key):
    p = PALETTES[key]
    return build_ring(p["colors"], p.get("ramp"))


def ring_hues(key, samples=512):
    """Every chromatic hue the palette's ring can produce."""
    ring = ring_for(key)
    hues = []
    for i in range(samples):
        h, s, v = _hsv(ring.color_at(i / samples))
        if s >= SAT_FLOOR and v > 0.05:
            hues.append(h)
    return hues


def assert_in_family(rgb, key, what):
    h, s, v = _hsv(rgb)
    if s < SAT_FLOOR or v <= 0.05:
        return   # nothing to be off-palette about
    hues = ring_hues(key)
    gap = min(_hue_gap(h, x) for x in hues)
    assert gap <= HUE_TOL, (
        f"{what} under {key}: {rgb} (hue {h:.3f}) is {gap:.3f} from the "
        f"nearest hue the palette's ring can produce")


def _mapper():
    return LEDMapper(MAPPED_REGION)   # no mirror tail


def _render(key, effects, strictness=1.0, zones=(0, 0, 0, 0)):
    p = PALETTES[key]
    return _mapper().render(
        list(zones), zone_colors=p["colors"], effects=effects, brightness=1.0,
        palette_ring=build_ring(p["colors"], p.get("ramp")),
        palette_strictness=strictness)


def _lit(px):
    """Every lit pixel as an (r,g,b) tuple."""
    out = []
    for i in range(len(px) // 3):
        o = i * 3
        rgb = (px[o], px[o + 1], px[o + 2])
        if any(rgb):
            out.append(rgb)
    return out


def _brightest(px):
    return max(_lit(px), key=lambda c: max(c))


# ── The remap primitive ──────────────────────────────────────────

@pytest.mark.parametrize("key", PALETTE_KEYS)
def test_remap_puts_any_hue_in_the_family(key):
    ring = ring_for(key)
    for i in range(24):
        src = colorsys.hsv_to_rgb(i / 24.0, 1.0, 1.0)
        src = tuple(int(c * 255) for c in src)
        assert_in_family(remap(src, ring, 1.0), key, f"remap({src})")


@pytest.mark.parametrize("key", PALETTE_KEYS)
def test_remap_at_zero_strength_is_identity(key):
    ring = ring_for(key)
    for src in [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
                (17, 200, 90)]:
        assert remap(src, ring, 0.0) == src


@pytest.mark.parametrize("key", PALETTE_KEYS)
def test_remap_leaves_greys_alone(key):
    """Weighting by source saturation keeps the white accents white."""
    ring = ring_for(key)
    for src in [(255, 255, 255), (128, 128, 128), (10, 10, 10)]:
        out = remap(src, ring, 1.0)
        assert max(abs(a - b) for a, b in zip(out, src)) <= 2


@pytest.mark.parametrize("key", PALETTE_KEYS)
def test_remap_preserves_brightness(key):
    """A dim ramp stop must not drain a layer's punch."""
    ring = ring_for(key)
    for i in range(12):
        src = tuple(int(c * 255)
                    for c in colorsys.hsv_to_rgb(i / 12.0, 1.0, 1.0))
        out = remap(src, ring, 1.0)
        assert max(out) >= 0.75 * max(src), f"{src} -> {out} lost brightness"


# ── Per-layer: every lit pixel stays in the family ───────────────

@pytest.mark.parametrize("key", PALETTE_KEYS)
def test_vocal_ribbon_stays_in_family(key):
    px = _render(key, {"vocal_notes": [60.0, 67.0, 0.0, 0.0]})
    for rgb in _lit(px):
        assert_in_family(rgb, key, "vocal ribbon")


@pytest.mark.parametrize("key", PALETTE_KEYS)
def test_performer_bias_stays_in_family(key):
    px = _render(key, {"performers": int(Performer.GUITAR)},
                 zones=(0xFF, 0, 0, 0))
    for rgb in _lit(px):
        assert_in_family(rgb, key, "performer bias")


@pytest.mark.parametrize("key", PALETTE_KEYS)
def test_section_bias_stays_in_family(key):
    px = _render(key, {"section": ((255, 170, 70), 1.0, 1.0)},
                 zones=(0xFF, 0, 0, 0))
    for rgb in _lit(px):
        assert_in_family(rgb, key, "section bias")


@pytest.mark.parametrize("key", PALETTE_KEYS)
def test_camera_bias_stays_in_family(key):
    px = _render(key, {"camera": (7, 1.0, 0.0)}, zones=(0xFF, 0, 0, 0))
    for rgb in _lit(px):
        assert_in_family(rgb, key, "camera bias")


@pytest.mark.parametrize("key", PALETTE_KEYS)
def test_star_power_tint_stays_in_family(key):
    px = _render(key, {"star_power": 1.0, "star_power_players": 1},
                 zones=(0xFF, 0, 0, 0))
    for rgb in _lit(px):
        assert_in_family(rgb, key, "star-power tint")


@pytest.mark.parametrize("key", PALETTE_KEYS)
def test_cue_gradient_recolour_stays_in_family(key):
    """The loudest violation: a cue gradient repaints every lit pixel's hue."""
    px = _render(key, {"gradient": GRADIENTS["cool"]},
                 zones=(0xFF, 0xFF, 0xFF, 0xFF))
    for rgb in _lit(px):
        assert_in_family(rgb, key, "cue gradient recolour")


# ── The ribbon keeps its meaning ─────────────────────────────────

def _octave_colors(key):
    ring = ring_for(key)
    return [remap_at(VOCAL_CHROMA.color_at(p / 12.0), ring, p / 12.0, 1.0)
            for p in range(12)]


@pytest.mark.parametrize("key", sorted(PALETTES))
def test_an_octave_still_sweeps_the_palette(key):
    """The ribbon must keep saying something after being constrained.

    Not "all twelve pitch classes are mutually distinct" — that is unachievable
    and the wrong bar. Twelve samples of any bounded colour family will have
    near-collisions, and a ring that doubles back (Ocean returns to midnight
    blue twice) produces them by design. What must hold is that an octave
    *travels* the family instead of sitting still.

    This is also what justifies two decisions in `palette_map`: closing the
    ring with a short return arc rather than mirroring the ramp (mirroring maps
    pitch class p and 12-p onto one colour), and spacing ring stops by
    perceptual distance rather than the upstream's own positions (lava_gp
    spends its first quarter deepening a single red, which would swallow three
    pitch classes).
    """
    colors = _octave_colors(key)
    spread = max(sum(abs(a - b) for a, b in zip(colors[i], colors[j]))
                 for i in range(12) for j in range(i + 1, 12))
    assert spread >= 140, f"{key}: an octave only spans L1 {spread}"


@pytest.mark.parametrize("key", sorted(PALETTES))
def test_pitch_classes_do_not_collapse(key):
    """Most of the twelve notes remain telling apart, at 16 levels per channel."""
    quantised = {tuple(c // 16 for c in rgb) for rgb in _octave_colors(key)}
    assert len(quantised) >= 9, (
        f"{key}: only {len(quantised)} of 12 pitch classes are distinguishable")


def test_same_pitch_class_keeps_the_same_colour():
    """An octave apart is still one colour — the ribbon's whole idea."""
    ring = ring_for("lava")
    for p in range(12):
        lo, hi = p / 12.0, (p + 12) / 12.0
        assert (remap_at(VOCAL_CHROMA.color_at(lo), ring, lo, 1.0) ==
                remap_at(VOCAL_CHROMA.color_at(hi), ring, hi, 1.0))


# ── Strictness 0.0 is the rollback path ──────────────────────────

@pytest.mark.parametrize("key", PALETTE_KEYS)
def test_zero_strictness_uses_the_original_tables(key):
    """The operator's way back to today's look has a test."""
    ring = ring_for(key)
    for src in [VOCAL_CHROMA.color_at(0.3),
                PERFORMER_COLORS[Performer.GUITAR],
                CAMERA_CHANNEL_COLORS["drums"], SP_TINT]:
        assert remap(src, ring, 0.0) == tuple(src)


def test_zero_strictness_render_matches_unremapped_render():
    px_off = _render("lava", {"vocal_notes": [60.0, 0.0, 0.0, 0.0]},
                     strictness=0.0)
    px_none = _mapper().render(
        [0, 0, 0, 0], zone_colors=PALETTES["lava"]["colors"],
        effects={"vocal_notes": [60.0, 0.0, 0.0, 0.0]}, brightness=1.0,
        palette_strictness=0.0)
    assert bytes(px_off) == bytes(px_none)


# ── Cross-fade stability ─────────────────────────────────────────

def test_section_crossfade_remaps_without_a_spike():
    """The engine cross-fades verse-blue -> chorus-amber through a desaturated
    middle where hue is numerically unstable. Weighting the remap by source
    saturation must keep that path smooth rather than letting it snap."""
    ring = ring_for("lava")
    verse, chorus = (70, 110, 255), (255, 170, 70)
    prev, worst = None, 0
    for i in range(64):
        f = i / 63.0
        src = tuple(int(verse[k] + (chorus[k] - verse[k]) * f)
                    for k in range(3))
        out = remap(src, ring, 1.0)
        if prev is not None:
            worst = max(worst, sum(abs(a - b) for a, b in zip(out, prev)))
        prev = out
    assert worst <= 60, f"section cross-fade jumps by L1 {worst} in one step"
