"""Tests for the performer highlight bias (VISION Phase 4).

Spotlight + singalong (offsets 42-43) are performer bitmasks; their union leans
the lit wash a little toward the highlighted performers' colours. These tests
pin: engine unions the two masks, the tint only touches lit pixels (a blackout
stays dark), the lean is toward the right hue, and it stays bounded.

Run: python -m pytest tests/test_performer_bias.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import CueEngine  # noqa: E402
from effects.mapper import (  # noqa: E402
    LEDMapper, MAPPED_REGION, PERFORMER_COLORS, PERFORMER_BIAS_STRENGTH,
)
from protocol.yarg_packet import Performer  # noqa: E402

_COLORS = {"red": (255, 0, 0), "green": (0, 255, 0),
           "blue": (0, 0, 255), "yellow": (255, 255, 0)}


def _render(performers, zones=(0xFF, 0, 0, 0)):
    # Default scene: a solid red wash so a hue lean is measurable.
    m = LEDMapper(MAPPED_REGION)
    return m.render(list(zones), zone_colors=_COLORS,
                    effects={"performers": performers}, brightness=1.0)


# ── Engine union ─────────────────────────────────────────────────

def test_engine_unions_spotlight_and_singalong():
    eng = CueEngine()
    eng.on_performers(Performer.GUITAR, Performer.VOCALS)
    assert eng.get_effects()["performers"] == (Performer.GUITAR | Performer.VOCALS)


def test_no_performers_is_noop():
    a = _render(0)
    b = _render(Performer.NONE)
    # A red wash unchanged when nobody is highlighted.
    assert a == b
    # First pixel is still pure red.
    assert (a[0], a[1], a[2]) == (255, 0, 0)


# ── Tint behaviour ───────────────────────────────────────────────

def test_bias_leans_toward_performer_hue():
    # Red wash, VOCALS highlighted (cyan) → blue channel must rise.
    base = _render(0)
    tinted = _render(Performer.VOCALS)
    assert tinted[2] > base[2]           # blue lifted toward cyan
    assert tinted[0] < base[0]           # red pulled down by the convex mix


def test_bias_only_touches_lit_pixels():
    # All zones off → strip dark → bias must not light anything.
    px = _render(Performer.DRUMS, zones=(0, 0, 0, 0))
    assert max(px) == 0


def test_bias_strength_matches_formula():
    # One performer, known hue, on a full-red pixel: exact convex blend.
    pr, pg, pb = PERFORMER_COLORS[Performer.VOCALS]
    px = _render(Performer.VOCALS)
    t = PERFORMER_BIAS_STRENGTH
    exp_r = int(255 * (1.0 - t) + pr * t)
    exp_g = int(0 * (1.0 - t) + pg * t)
    exp_b = int(0 * (1.0 - t) + pb * t)
    assert (px[0], px[1], px[2]) == (exp_r, exp_g, exp_b)


def test_two_performers_average_hue():
    # GUITAR (amber) + VOCALS (cyan) → average of the two colours.
    px = _render(Performer.GUITAR | Performer.VOCALS)
    gr, gg, gb = PERFORMER_COLORS[Performer.GUITAR]
    vr, vg, vb = PERFORMER_COLORS[Performer.VOCALS]
    ar, ag, ab = (gr + vr) // 2, (gg + vg) // 2, (gb + vb) // 2
    t = PERFORMER_BIAS_STRENGTH
    assert px[0] == int(255 * (1.0 - t) + ar * t)
    assert px[1] == int(0 * (1.0 - t) + ag * t)
    assert px[2] == int(0 * (1.0 - t) + ab * t)


def test_bias_stays_bounded():
    px = _render(Performer.GUITAR | Performer.VOCALS | Performer.DRUMS,
                 zones=(0xFF, 0xFF, 0xFF, 0xFF))
    assert max(px) <= 255
