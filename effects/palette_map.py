"""Constrain the reactivity layers to the selected palette's colour family.

The Stage Kit model gives a palette four zones, so `settings.PALETTES` carries
four colours — and the base wash and scanner heads paint from exactly those.
But every Phase 4-6 layer added since then paints from a fixed table of its own:
the vocal ribbon's full-spectrum rainbow, one hue per performer, per-section
hues, the cue gradients, the star-power tint. Those tables look right under
`default` (RGBY) and foreign under any other palette — pick Lava and the ribbon
still emits cyan and magenta.

This module maps those fixed colours into the palette's family. Two pieces:

  * **The ring** — a cyclic `Gradient` describing everything the palette may
    show. Where a palette carries a `ramp` (its full upstream LedFx/WLED
    gradient, recovered by `tools/extract_palette_ramps.py`) the ring is built
    from that, so the layers get back the colours the four-colour truncation
    threw away. Otherwise it is built from the four zone colours.
  * **The remap** — take a colour's hue, ask the ring what the palette shows at
    that hue, and move the colour that far.

The cost is trivial by construction: these layers choose a *handful of colours
per frame* — at most four vocal voices, one averaged performer colour, one or
two camera channels, one section hue — never one per pixel. Cue gradients are
the exception and are remapped LUT-wise once and cached by the caller.
"""

import colorsys

from effects.gradient import Gradient

# How much of the ring the palette's own stops occupy, with the remainder a
# smooth return to the first colour.
#
# The ring has to close: the vocal ribbon indexes it by pitch *class*, so the
# octave wrap (B back to C) must not show a seam. But source ramps are open —
# Lava runs red to yellow and stops. The obvious close is to mirror the ramp,
# and that is wrong: mirroring maps pitch class p and 12-p to the same colour,
# halving the ribbon's meaning, which is the thing this module exists to
# protect. So lay the ramp across [0, RING_ARC] and spend the last stretch
# returning to the start through in-family blends. Distinct pitch classes, no
# seam.
RING_ARC = 0.8

# Below this saturation a colour has no meaningful hue — the note/sparkle
# accents are white by construction, and a cross-fade passing through grey has
# a numerically unstable hue. Both should be left alone, which the saturation
# weighting in `remap` does continuously rather than as a cliff.
_EPS = 1e-6

# How much of a remapped colour's brightness comes from the source rather than
# from the ring stop it landed on. Taking the source's brightness outright is
# what keeps a dim ramp stop (Ocean opens on midnight blue) from draining a
# layer — but taken to the limit it also flattens away the ring's *own*
# brightness variation, and on a hue-narrow palette that variation is most of
# what separates one stop from the next. Keep a quarter of it.
VALUE_FLOOR = 0.75


def _hue_of(rgb):
    return colorsys.rgb_to_hsv(rgb[0] / 255.0, rgb[1] / 255.0,
                               rgb[2] / 255.0)[0]


def _fit_value(target, src_peak):
    """Scale `target` to sit at the source's brightness, keeping some of its own."""
    peak = max(target)
    if peak <= 0:
        return (0, 0, 0)
    want = src_peak * (VALUE_FLOOR + (1.0 - VALUE_FLOOR) * (peak / 255.0))
    scale = want / peak
    out = []
    for c in target:
        v = int(c * scale)
        out.append(255 if v > 255 else v)
    return tuple(out)


def build_ring(zone_colors: dict, ramp=None) -> Gradient:
    """Build the cyclic ring of everything the palette may show.

    Args:
        zone_colors: the palette's four Stage Kit zone colours.
        ramp: the palette's full upstream gradient as `(pos, (r,g,b))` stops,
            when it has one. Six palettes do not — `default` and `neon` are ours
            with no upstream, and four more whose four colours turned out not to
            match the family their upstream describes (see `settings.PALETTES`).

    Returns:
        A `Gradient` over [0, 1) whose ends meet.
    """
    if ramp:
        colors = [tuple(rgb) for _, rgb in ramp]
    else:
        # No upstream ramp: order the four zone colours around the hue circle.
        # Ordering by hue rather than by zone name is what makes the ring a
        # smooth loop instead of a zigzag; Stage Kit zone IDs carry no colour
        # meaning ("red" need not be red), so there is nothing to preserve.
        colors = [tuple(c) for c in sorted(zone_colors.values(), key=_hue_of)]

    # Space the stops by how far apart they *look*, discarding whatever
    # positions the source used. Those positions pace a spatial gradient —
    # lava_gp spends its first quarter deepening a single red — and this is not
    # a spatial gradient, it is an index into a colour family. Even perceptual
    # spacing is what makes twelve equally spaced pitch classes come out as
    # twelve visibly different colours.
    steps = [0.0]
    for i in range(1, len(colors)):
        a, b = colors[i - 1], colors[i]
        steps.append(steps[-1] + sum(abs(a[k] - b[k]) for k in range(3)))
    total = steps[-1]
    if total <= 0:
        span = max(1, len(colors) - 1)
        stops = [(i / span * RING_ARC, c) for i, c in enumerate(colors)]
    else:
        stops = [(d / total * RING_ARC, c) for d, c in zip(steps, colors)]

    # Close the ring: the return arc runs from the last stop back to the first,
    # occupying whatever is left after RING_ARC.
    stops.append((1.0, stops[0][1]))
    return Gradient(stops)


def remap(rgb, ring: Gradient, strength: float):
    """Move `rgb` toward what the palette shows at the same hue.

    Args:
        rgb: the layer's own colour.
        ring: from `build_ring`.
        strength: 0.0 leaves the colour exactly as it was — the operator's way
            back to the pre-palette look — and 1.0 constrains it fully.

    The move is additionally weighted by the source's own saturation, which is
    what keeps the white accents white and what keeps a cross-fade through grey
    from snapping as its unstable hue swings.
    """
    if strength <= 0.0:
        return tuple(rgb)

    r, g, b = rgb
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    if s <= _EPS or v <= _EPS:
        return tuple(rgb)

    tr, tg, tb = _fit_value(ring.color_at(h), max(r, g, b))

    t = strength * s
    inv = 1.0 - t
    return (int(r * inv + tr * t),
            int(g * inv + tg * t),
            int(b * inv + tb * t))


def remap_at(base_rgb, ring: Gradient, t: float, strength: float):
    """Remap a colour that was itself sampled from a ramp at position `t`.

    Some layers do not have a *colour* with meaning, they have a *parameter*
    with meaning: the vocal ribbon's hue is pitch class, cue gradients index
    position along the strip. For those, going through the source colour's hue
    would be a needless round trip — and a lossy one, because two different
    parameters can share a hue and would then collapse onto one palette colour.
    Index the ring by the same parameter instead, so equally spaced inputs stay
    equally spaced outputs.
    """
    if strength <= 0.0:
        return tuple(base_rgb)
    br, bg, bb = base_rgb
    tr, tg, tb = _fit_value(ring.color_at(t), max(br, bg, bb))
    inv = 1.0 - strength
    return (int(br * inv + tr * strength),
            int(bg * inv + tg * strength),
            int(bb * inv + tb * strength))


def remap_gradient(gradient: Gradient, ring: Gradient,
                   strength: float) -> Gradient:
    """Remap a whole gradient's lookup table into the palette's family.

    The cue gradient recolour replaces the hue of *every* lit pixel, so a VERSE
    cue running `GRADIENTS["cool"]` paints blue-teal straight over Lava — a
    louder violation than any of the subtle biases. Remapping per pixel would be
    the one place where the cost mattered, so remap the ramp once instead. The
    caller owns the cache; see `LEDMapper._ring_gradient`.
    """
    if strength <= 0.0:
        return gradient
    n = 64
    stops = []
    for i in range(n):
        t = i / (n - 1)
        stops.append((t, remap_at(gradient.color_at(t), ring, t, strength)))
    return Gradient(stops)
