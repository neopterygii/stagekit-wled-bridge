"""Tests for camera-cut lighting (VISION Phase 5).

The camera subject (offset 46) biases the wash toward the on-camera player: a
brightness lift + hue lean confined to that player's *region* of the strip,
eased in after each cut. A *directed* cut (priority 1) also arms a brief global
bloom; auto-cuts (priority 0) move the subject silently. These tests pin: the
engine stores the subject + arms the accent only when directed, the accent and
ease decay/ramp on the injected clock, the subject→channel map, the region hue
lean touches lit pixels only, and the whole thing disables cleanly to None.

Run: python -m pytest tests/test_camera_cut.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import (  # noqa: E402
    CueEngine, CAMERA_CUT_DURATION, CAMERA_EASE)
from effects.mapper import (  # noqa: E402
    LEDMapper, MAPPED_REGION, CAMERA_CHANNEL_ORDER, CAMERA_CHANNEL_COLORS,
    CAMERA_SUBJECT_CHANNELS, CAMERA_BIAS_STRENGTH, CAMERA_BIAS_LIFT,
)
from protocol.yarg_packet import CameraCutSubject, CameraCutPriority  # noqa: E402

_COLORS = {"red": (255, 0, 0), "green": (0, 255, 0),
           "blue": (0, 0, 255), "yellow": (255, 255, 0)}

_GUITAR = 7    # CameraCutSubject.NAMES[7] == "Guitar"
_DRUMS = 11    # "Drums"
_CROWD = CameraCutSubject.CROWD  # 0 — whole-stage, no subject bias


def _render(camera, zones=(0xFF, 0, 0, 0)):
    # Default scene: a solid red wash so a region hue lean is measurable.
    m = LEDMapper(MAPPED_REGION)
    return m.render(list(zones), zone_colors=_COLORS,
                    effects={"camera": camera}, brightness=1.0)


def _region(name):
    return LEDMapper._camera_region(CAMERA_CHANNEL_ORDER.index(name))


# ── Engine: subject storage + accent arming ──────────────────────

def test_directed_cut_arms_accent():
    # get_effects() reads the real monotonic clock, so arm on it (not injected).
    eng = CueEngine()
    eng.on_camera_cut(_GUITAR, CameraCutPriority.DIRECTED)
    subject, gain, cut_t = eng.get_effects()["camera"]
    assert subject == _GUITAR
    assert cut_t > 0.0            # directed → accent armed (just fired)


def test_auto_cut_does_not_arm_accent():
    eng = CueEngine()
    eng.on_camera_cut(_GUITAR, CameraCutPriority.NORMAL)
    subject, gain, cut_t = eng.get_effects()["camera"]
    assert subject == _GUITAR     # subject still moves
    assert cut_t == 0.0           # but no accent


def test_accent_decays_to_zero():
    eng = CueEngine()
    eng.on_camera_cut(_GUITAR, CameraCutPriority.DIRECTED, now=100.0)
    # Sample mid-decay and past the decay window on the injected clock.
    assert 0.0 < eng._camera_accent(100.0 + CAMERA_CUT_DURATION / 2) < 1.0
    assert eng._camera_accent(100.0 + CAMERA_CUT_DURATION + 0.01) == 0.0


def test_bias_gain_eases_in_then_holds():
    eng = CueEngine()
    eng.on_camera_cut(_GUITAR, CameraCutPriority.NORMAL, now=100.0)
    assert eng._camera_gain(100.0) == 0.0                     # snaps to 0 at cut
    assert 0.0 < eng._camera_gain(100.0 + CAMERA_EASE / 2) < 1.0
    assert eng._camera_gain(100.0 + CAMERA_EASE + 5.0) == 1.0  # then holds at 1


def test_pause_shifts_camera_timers():
    eng = CueEngine()
    eng.on_camera_cut(_GUITAR, CameraCutPriority.DIRECTED)
    before = eng._camera_cut_at
    eng.on_paused(True)
    eng.on_paused(False)
    # Deadlines shift forward by the (tiny) pause duration, never backward.
    assert eng._camera_cut_at >= before


# ── Subject → channel map ────────────────────────────────────────

def test_subject_channel_map_is_consistent():
    # Every mapped channel name is a known channel with a colour.
    for names in CAMERA_SUBJECT_CHANNELS.values():
        for name in names:
            assert name in CAMERA_CHANNEL_ORDER
            assert name in CAMERA_CHANNEL_COLORS


def test_whole_stage_subject_has_no_bias():
    assert CAMERA_SUBJECT_CHANNELS.get(_CROWD) is None
    assert CAMERA_SUBJECT_CHANNELS.get(CameraCutSubject.STAGE) is None
    assert CAMERA_SUBJECT_CHANNELS.get(CameraCutSubject.RANDOM) is None


def test_channel_regions_are_disjoint_and_tile_the_strip():
    covered = []
    for i in range(len(CAMERA_CHANNEL_ORDER)):
        lo, hi = LEDMapper._camera_region(i)
        covered.append((lo, hi))
    # Contiguous, non-overlapping, and spanning the whole strip.
    assert covered[0][0] == 0
    assert covered[-1][1] == MAPPED_REGION
    for (_, hi), (lo, _) in zip(covered, covered[1:]):
        assert hi == lo


# ── Mapper: region hue lean ──────────────────────────────────────

def test_subject_biases_only_its_region():
    # Red wash, camera on Guitar (amber). Guitar's slice leans amber (green
    # channel rises); a pixel outside that slice is untouched.
    px = _render((_GUITAR, 1.0, 0.0))
    lo, hi = _region("guitar")
    inside = lo * 3
    assert px[inside + 1] > 0                 # amber lifted green inside the band
    # A drums-region pixel (different slice) stays pure red.
    dlo, _ = _region("drums")
    o = dlo * 3
    assert (px[o], px[o + 1], px[o + 2]) == (255, 0, 0)


def test_zero_gain_is_noop():
    base = _render((_CROWD, 0.0, 0.0))     # no subject, no gain
    biased = _render((_GUITAR, 0.0, 0.0))  # subject present but gain 0
    assert base == biased


def test_bias_only_touches_lit_pixels():
    # Strip dark → the region bias must not light anything.
    px = _render((_GUITAR, 1.0, 0.0), zones=(0, 0, 0, 0))
    assert max(px) == 0


def test_directed_bloom_lifts_lit_pixels():
    base = _render((_CROWD, 1.0, 0.0))       # no subject, no accent
    bloomed = _render((_CROWD, 1.0, 1.0))    # full accent, no region bias
    # The bloom lifts the whole lit wash even with no subject bias.
    assert bloomed[0] >= base[0]
    assert max(bloomed[0], bloomed[1], bloomed[2]) >= max(base[0], base[1], base[2])


def test_bias_matches_formula():
    # Full gain, camera on Guitar, on a full-red pixel in guitar's region.
    cr, cg, cb = CAMERA_CHANNEL_COLORS["guitar"]
    px = _render((_GUITAR, 1.0, 0.0))
    lo, _ = _region("guitar")
    o = lo * 3
    t = CAMERA_BIAS_STRENGTH
    lift = 1.0 + CAMERA_BIAS_LIFT
    exp_r = min(255, int((255 * (1.0 - t) + cr * t) * lift))
    exp_g = min(255, int((0 * (1.0 - t) + cg * t) * lift))
    exp_b = min(255, int((0 * (1.0 - t) + cb * t) * lift))
    assert (px[o], px[o + 1], px[o + 2]) == (exp_r, exp_g, exp_b)


def test_stays_bounded():
    px = _render((_GUITAR, 1.0, 1.0), zones=(0xFF, 0xFF, 0xFF, 0xFF))
    assert max(px) <= 255


def test_none_camera_is_noop():
    # Toggled off → effects["camera"] is None → the whole effect is skipped.
    m = LEDMapper(MAPPED_REGION)
    off = m.render([0xFF, 0, 0, 0], zone_colors=_COLORS,
                   effects={"camera": None}, brightness=1.0)
    absent = m.render([0xFF, 0, 0, 0], zone_colors=_COLORS,
                      effects={}, brightness=1.0)
    assert off == absent
