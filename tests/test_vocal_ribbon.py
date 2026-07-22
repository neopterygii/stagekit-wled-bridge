"""Tests for the vocal pitch ribbon (VISION Phase 4).

Each sounding voice (lead + 3 harmonies, MIDI pitch at offsets 18-33) is painted
as a soft colour blob whose position tracks absolute pitch and whose hue is the
pitch class. These tests pin: silence → dark, higher pitch → further right,
pitch-class → colour (octaves match), multiple voices → multiple blobs, and that
the alpha-over ribbon stays bounded.

Run: python -m pytest tests/test_vocal_ribbon.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import CueEngine  # noqa: E402
from effects.mapper import (  # noqa: E402
    LEDMapper, MAPPED_REGION, VOCAL_MIDI_LO, VOCAL_MIDI_HI, VOCAL_CHROMA,
)

_COLORS = {"red": (255, 0, 0), "green": (0, 255, 0),
           "blue": (0, 0, 255), "yellow": (255, 255, 0)}


def _lum(px):
    n = len(px) // 3
    return [max(px[i * 3], px[i * 3 + 1], px[i * 3 + 2]) for i in range(n)]


def _render(vocal_notes, zones=(0, 0, 0, 0)):
    m = LEDMapper(MAPPED_REGION)   # no mirror tail
    return m.render(list(zones), zone_colors=_COLORS,
                    effects={"vocal_notes": vocal_notes}, brightness=1.0)


def _centroid(px):
    lum = _lum(px)
    tot = sum(lum)
    assert tot > 0
    return sum(i * v for i, v in enumerate(lum)) / tot


# ── Engine passthrough ───────────────────────────────────────────

def test_on_vocals_stores_pitches():
    eng = CueEngine()
    eng.on_vocals(60.0, 64.0, 0.0, 0.0)
    fx = eng.get_effects()
    assert fx["vocal_notes"] == [60.0, 64.0, 0.0, 0.0]


# ── Rendering ────────────────────────────────────────────────────

def test_silence_leaves_strip_dark():
    assert max(_lum(_render([0.0, 0.0, 0.0, 0.0]))) == 0
    assert max(_lum(_render(None))) == 0


def test_single_voice_lights_a_blob():
    px = _render([60.0, 0.0, 0.0, 0.0])
    assert max(_lum(px)) > 0


def test_higher_pitch_sits_further_right():
    lo = _centroid(_render([VOCAL_MIDI_LO + 2.0, 0.0, 0.0, 0.0]))
    hi = _centroid(_render([VOCAL_MIDI_HI - 2.0, 0.0, 0.0, 0.0]))
    assert hi > lo


def test_pitch_out_of_range_clamps_not_crashes():
    left = _centroid(_render([VOCAL_MIDI_LO - 24.0, 0.0, 0.0, 0.0]))
    right = _centroid(_render([VOCAL_MIDI_HI + 24.0, 0.0, 0.0, 0.0]))
    assert left < right
    # Clamped to the strip.
    assert 0 <= left < MAPPED_REGION and 0 <= right < MAPPED_REGION


def test_octave_shares_hue():
    # Same pitch class (C at 48 and 60) → same chroma colour, different place.
    c_low = VOCAL_CHROMA.color_at((48.0 % 12.0) / 12.0)
    c_high = VOCAL_CHROMA.color_at((60.0 % 12.0) / 12.0)
    assert c_low == c_high
    # And their blobs land at different positions.
    assert _centroid(_render([48.0, 0, 0, 0])) < _centroid(_render([60.0, 0, 0, 0]))


def test_two_voices_two_blobs():
    # Well-separated voices → two lit clusters with a dark gap between.
    px = _render([VOCAL_MIDI_LO + 1.0, 0.0, 0.0, VOCAL_MIDI_HI - 1.0])
    lum = _lum(px)
    assert lum[0] > 0 or max(lum[:MAPPED_REGION // 4]) > 0        # left blob
    assert max(lum[3 * MAPPED_REGION // 4:]) > 0                  # right blob
    assert min(lum[MAPPED_REGION // 3: 2 * MAPPED_REGION // 3]) == 0  # dark gap


def test_ribbon_stays_bounded_over_wash():
    # A full white wash plus a voice must not overflow (alpha-over is convex).
    px = _render([60.0, 62.0, 64.0, 67.0], zones=(0xFF, 0xFF, 0xFF, 0xFF))
    assert max(_lum(px)) <= 255
