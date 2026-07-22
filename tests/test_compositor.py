"""Tests for the layer/slot compositor (VISION Phase 3).

The old mapper ran every overlay as an in-place pass on one shared buffer, so
stacked whitening overlays (bonus + sparkle + flash + star-power) each clamped
independently and clipped to white in an order-dependent "fight". The
compositor folds independent layers with explicit blend modes; MIX is convex,
so stacked whitening screen-combines and stays bounded. These tests pin the
blend math and that the fight is gone, plus that the mapper still renders a
single overlay the way it used to.

Run: .venv/bin/python -m pytest tests/test_compositor.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.compositor import (  # noqa: E402
    Layer, Compositor, REPLACE, ADD, MIX, MIX_LIT, MIX_PREMULT,
)
from effects.mapper import LEDMapper, MAPPED_REGION, CELL_SIZE  # noqa: E402
from effects.cue_engine import BLUE, YELLOW  # noqa: E402


def _mklayer(n, mode, rgb, opacity=1.0, alpha=None):
    lyr = Layer(n, mode, per_pixel_alpha=(alpha is not None))
    r, g, b = rgb
    for i in range(n):
        lyr.buf[i * 3] = r
        lyr.buf[i * 3 + 1] = g
        lyr.buf[i * 3 + 2] = b
    lyr.opacity = opacity
    if alpha is not None:
        for i in range(n):
            lyr.alpha[i] = alpha[i]
    lyr.active = True
    return lyr


# ── Blend-mode math ──────────────────────────────────────────────

def test_replace_copies_layer_over_base():
    base = bytearray([10, 20, 30, 40, 50, 60])
    lyr = _mklayer(2, REPLACE, (1, 2, 3))
    Compositor.composite(base, (lyr,))
    assert list(base) == [1, 2, 3, 1, 2, 3]


def test_add_sums_and_clamps():
    base = bytearray([200, 100, 0, 10, 10, 10])
    lyr = _mklayer(2, ADD, (100, 100, 100))
    Compositor.composite(base, (lyr,))
    assert list(base) == [255, 200, 100, 110, 110, 110]  # first pixel clamps


def test_add_scales_by_opacity():
    base = bytearray([0, 0, 0])
    lyr = _mklayer(1, ADD, (100, 100, 100), opacity=0.5)
    Compositor.composite(base, (lyr,))
    assert list(base) == [50, 50, 50]


def test_mix_is_convex_alpha_over():
    base = bytearray([0, 0, 0])
    lyr = _mklayer(1, MIX, (255, 255, 255), opacity=0.5)
    Compositor.composite(base, (lyr,))
    assert list(base) == [127, 127, 127]  # halfway to white


def test_mix_lit_skips_dark_pixels():
    base = bytearray([0, 0, 0, 100, 0, 0])       # pixel 0 dark, pixel 1 lit
    lyr = _mklayer(2, MIX_LIT, (255, 255, 255), opacity=1.0)
    Compositor.composite(base, (lyr,))
    assert list(base[:3]) == [0, 0, 0]           # dark pixel untouched
    assert list(base[3:]) == [255, 255, 255]     # lit pixel whitened


def test_mix_premult_matches_hand_alpha_over():
    # Premultiplied colour: layer buffer already holds colour*coverage.
    base = bytearray([100, 100, 100])
    lyr = _mklayer(1, MIX_PREMULT, (0, 0, 128), alpha=[0.5])  # premult blue @ .5
    Compositor.composite(base, (lyr,))
    # base*(1-a) + premult = 100*0.5 + 0, ..., 100*0.5 + 128
    assert list(base) == [50, 50, 178]


def test_inactive_layer_is_skipped():
    base = bytearray([1, 2, 3])
    lyr = _mklayer(1, REPLACE, (9, 9, 9))
    lyr.active = False
    Compositor.composite(base, (lyr,))
    assert list(base) == [1, 2, 3]


# ── The "stop fighting" property ─────────────────────────────────

def test_stacked_whitening_screen_combines_and_never_clips():
    # Three MIX-white layers at 0.5 each. Convex blends compose as
    # 1-(1-.5)^3 = 0.875 toward white; a bounded, order-independent result,
    # NOT three independent add-and-clamp passes slamming to 255.
    base = bytearray([0, 0, 0])
    layers = [_mklayer(1, MIX, (255, 255, 255), opacity=0.5) for _ in range(3)]
    Compositor.composite(base, layers)
    expected = int(255 * (1 - 0.5 ** 3))         # 223
    assert all(abs(c - expected) <= 1 for c in base), list(base)
    assert max(base) < 255                        # did not clip to white


def test_stacked_whitening_is_order_independent():
    def run(order):
        base = bytearray([40, 10, 0])
        layers = [_mklayer(1, MIX, (255, 255, 255), opacity=op) for op in order]
        Compositor.composite(base, layers)
        return list(base)
    assert run([0.2, 0.5, 0.7]) == run([0.7, 0.2, 0.5])  # commutes (convex)


# ── Mapper-level: single-overlay parity + no stacked clip war ────

def _full_wash():
    levels = [[0.0] * 8 for _ in range(4)]
    for c in range(8):
        levels[BLUE][c] = 1.0
    return levels


def test_mapper_bonus_on_dark_matches_old_grey_burst():
    # On a dark strip, a bonus burst reproduces the old grey value 255*0.85*t.
    m = LEDMapper()
    zero = [[0.0] * 8 for _ in range(4)]
    px = m.render([0, 0, 0, 0], effects={"bonus_t": 1.0}, zone_cell_levels=zero)
    expected = int(255 * 1.0 * 0.85)
    assert all(abs(px[i] - expected) <= 1 for i in range(MAPPED_REGION * 3))


def test_mapper_single_bonus_preserves_look_on_wash():
    # One overlay active over a blue wash: blue stays blue-dominant, lifted.
    m = LEDMapper()
    px = m.render([0, 0, 0xFF, 0], effects={"bonus_t": 0.5},
                  zone_cell_levels=_full_wash())
    mid = (4 * CELL_SIZE) * 3
    assert px[mid + 2] > px[mid]        # blue channel still dominant
    assert px[mid] > 0                  # but whitened (red lifted off zero)


def test_mapper_stacked_accents_keep_the_wash_readable():
    # Bonus + sparkle stacked over a blue wash. The old code blended toward
    # white sequentially (sparkle then bonus, each clamping), over-whitening a
    # lit pixel until blue barely led red; the compositor screen-combines them
    # convexly so the wash colour stays clearly readable underneath. (beat_pulse
    # is deliberately excluded — it's a brightness *multiply*, a separate
    # transform that legitimately saturates, not the accent clip-fight.)
    m = LEDMapper()
    fx = {
        "bonus_t": 0.6,
        "sparkle": 0.8, "sparkle_continuous": True, "beat_flash": True,
        "fps": 40.0,
    }
    px = m.render([0, 0, 0xFF, 0], effects=fx, zone_cell_levels=_full_wash())
    n = MAPPED_REGION
    # Blue stays clearly above red on the lit strip (wash survives the stack).
    blue_dominant = sum(1 for i in range(n)
                        if px[i * 3 + 2] > px[i * 3] + 30)
    assert blue_dominant > n // 3, \
        "stacked accents washed the strip to white (the old clip-fight)"
    # And every pixel is bounded — no channel forced past a lit-blue whitening.
    assert max(px[i * 3] for i in range(n)) < 255, "red channel clipped to white"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v"]))
