"""Tests for the multi-spot spotlight cues.

The spotlight cues light several evenly spaced spots rather than one centre
window — the operator's ask was "2-3 spotlights of about the size the current
one is", i.e. a stage lit by a few lamps rather than one bigger lamp. A
beat-driven chase pumps one spot to full while the others hold at
SPOT_DIM_FLOOR, so all of them stay visible at every instant.

These tests pin the spot geometry (count, width, placement), the chase's
levels/advance/wrap, and two properties that are easy to lose silently: that
the chase reads the engine's injected beat clock rather than the wall clock,
and that the cue still lights before any beat has been seen.

Nothing asserted spotlight geometry before this change, which is how the widths
drifted from what the backlog recorded.

Run: python -m pytest tests/test_spotlight.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import (  # noqa: E402
    CueEngine, SPOTLIGHT_COUNT, SPOTLIGHT_WIDTH_TIGHT, SPOTLIGHT_WIDTH_WIDE,
)
from effects.mapper import LEDMapper, MAPPED_REGION, SPOT_DIM_FLOOR  # noqa: E402
from protocol.yarg_packet import CueByte  # noqa: E402

# Arbitrary fixed instant — the engine takes its clock, so nothing here reads
# the wall clock.
NOW = 100.0

# Expected windows on the 120px strip, from the widths the cues ask for.
BLACKOUT_SPOTS = [(10, 30), (50, 70), (90, 110)]      # 20px each
SILHOUETTE_SPOTS = [(5, 35), (45, 75), (85, 115)]     # 30px each


def _render(cue, beat_clock=0.0, strictness=0.0):
    """Render one frame of `cue` at a chosen point on the beat clock.

    palette_strictness defaults to 0.0 so the cues' authored colours come
    through unremapped and the level assertions below stay readable; the
    remapping has its own tests in tests/test_palette_strictness.py.

    blur and mirror are forced off: blur softens the spot edges by a couple of
    pixels and mirror folds the strip, so leaving them on would test the
    post-process chain (tests/test_blur_mirror.py) instead of the geometry.
    """
    engine = CueEngine()
    mapper = LEDMapper()
    engine.bpm = 120.0
    engine._launch_cue(cue)
    engine.tick(NOW)
    fx = engine.get_effects()
    fx["fps"] = 40
    fx["beat_clock"] = beat_clock
    fx["blur"] = 0.0
    fx["mirror"] = False
    return mapper.render(engine.zones, effects=fx,
                         zone_cell_levels=engine.zone_cell_levels,
                         motion_sources=engine.motion_sources,
                         palette_strictness=strictness)


def _segments(px):
    """[(lo, hi, colour_at_lo)] for each contiguous run of lit pixels."""
    out = []
    start = None
    for i in range(MAPPED_REGION):
        o = i * 3
        lit = px[o] or px[o + 1] or px[o + 2]
        if lit and start is None:
            start = i
        elif not lit and start is not None:
            out.append((start, i, tuple(px[start * 3:start * 3 + 3])))
            start = None
    if start is not None:
        out.append((start, MAPPED_REGION,
                    tuple(px[start * 3:start * 3 + 3])))
    return out


def _windows(px):
    return [(lo, hi) for lo, hi, _c in _segments(px)]


def _levels(px):
    """Each spot's brightness as a fraction of the brightest spot."""
    peaks = [max(c) for _lo, _hi, c in _segments(px)]
    top = max(peaks)
    return [p / top for p in peaks]


# ── The generalisation is behaviour-preserving ───────────────────

def _spot(count, fraction):
    return LEDMapper._spot_windows(count, fraction)


def _old_center_window(fraction):
    """The single-window helper `_spot_windows` replaced, verbatim.

    Kept here rather than in the mapper: the spotlight cues were its last
    callers, so in production it is gone. It survives as the reference the
    generalisation is checked against.
    """
    half = max(1, int(MAPPED_REGION * fraction / 2.0))
    mid = MAPPED_REGION // 2
    return max(0, mid - half), min(MAPPED_REGION, mid + half)


def test_one_spot_is_exactly_the_old_centre_window():
    """At count=1 the generalisation must reduce to the window it replaced,
    for every fraction — not just the two the cues happen to use."""
    for fraction in (0.10, 0.18, 0.25, 0.40, 0.50, 0.9):
        assert _spot(1, fraction) == [_old_center_window(fraction)], \
            f"count=1 diverged from the old centre window at {fraction}"


def test_spots_are_evenly_spread_with_equal_gaps():
    windows = _spot(SPOTLIGHT_COUNT, SPOTLIGHT_WIDTH_TIGHT)
    gaps = [windows[i + 1][0] - windows[i][1] for i in range(len(windows) - 1)]
    assert len(set(gaps)) == 1, f"uneven gaps between spots: {gaps}"
    # Margins at the two ends match each other too.
    assert windows[0][0] == MAPPED_REGION - windows[-1][1]


def test_widening_a_spot_does_not_move_the_others():
    """spotlight_region is per-spot width, not total coverage — the centres
    are fixed by the count alone."""
    narrow = _spot(SPOTLIGHT_COUNT, SPOTLIGHT_WIDTH_TIGHT)
    wide = _spot(SPOTLIGHT_COUNT, SPOTLIGHT_WIDTH_WIDE)
    centres = [((lo + hi) // 2) for lo, hi in narrow]
    assert [((lo + hi) // 2) for lo, hi in wide] == centres


# ── Cue geometry ─────────────────────────────────────────────────

def test_blackout_spotlight_lights_three_tight_spots():
    assert _windows(_render(CueByte.BLACKOUT_SPOTLIGHT)) == BLACKOUT_SPOTS


def test_silhouettes_spotlight_lights_three_wider_spots():
    assert _windows(_render(CueByte.SILHOUETTES_SPOTLIGHT)) == SILHOUETTE_SPOTS


def test_each_spot_is_the_size_the_single_spot_used_to_be():
    """The ask was more spotlights, not bigger ones: BLACKOUT's spots must
    still be the width the one centre window was."""
    was = _old_center_window(SPOTLIGHT_WIDTH_TIGHT)
    old_width = was[1] - was[0]
    for lo, hi in _windows(_render(CueByte.BLACKOUT_SPOTLIGHT)):
        assert hi - lo == old_width


def test_the_two_cues_stay_a_tight_and_wide_pair():
    tight = _windows(_render(CueByte.BLACKOUT_SPOTLIGHT))[0]
    wide = _windows(_render(CueByte.SILHOUETTES_SPOTLIGHT))[0]
    assert (wide[1] - wide[0]) > (tight[1] - tight[0])


def test_the_gaps_between_spots_are_fully_dark():
    """A spotlight cue that leaks light between the spots is a wash."""
    px = _render(CueByte.SILHOUETTES_SPOTLIGHT)
    lit = set()
    for lo, hi in SILHOUETTE_SPOTS:
        lit.update(range(lo, hi))
    for i in range(MAPPED_REGION):
        if i not in lit:
            o = i * 3
            assert px[o] == px[o + 1] == px[o + 2] == 0, f"pixel {i} leaked"


# ── The chase ────────────────────────────────────────────────────

def test_chase_lights_one_spot_at_full_and_the_rest_at_the_floor():
    levels = _levels(_render(CueByte.BLACKOUT_SPOTLIGHT, beat_clock=0.0))
    assert len(levels) == SPOTLIGHT_COUNT
    assert sum(1 for v in levels if v == 1.0) == 1
    for v in levels:
        if v < 1.0:
            assert abs(v - SPOT_DIM_FLOOR) < 0.02


def test_chase_advances_one_spot_per_beat_and_wraps():
    seen = []
    for beat in range(SPOTLIGHT_COUNT + 1):
        levels = _levels(_render(CueByte.BLACKOUT_SPOTLIGHT,
                                 beat_clock=float(beat)))
        seen.append(levels.index(1.0))
    assert seen[:SPOTLIGHT_COUNT] == list(range(SPOTLIGHT_COUNT))
    assert seen[SPOTLIGHT_COUNT] == 0, "chase did not wrap back to the first spot"


def test_chase_runs_on_silhouettes_too():
    a = _levels(_render(CueByte.SILHOUETTES_SPOTLIGHT, beat_clock=0.0))
    b = _levels(_render(CueByte.SILHOUETTES_SPOTLIGHT, beat_clock=1.0))
    assert a.index(1.0) != b.index(1.0)


def test_chase_is_driven_by_the_injected_clock_not_the_wall_clock():
    """The failure mode tests/test_replay.py warns about for strobe: sample the
    wall clock and replayed frames stop being reproducible."""
    a = _render(CueByte.BLACKOUT_SPOTLIGHT, beat_clock=1.0)
    b = _render(CueByte.BLACKOUT_SPOTLIGHT, beat_clock=1.0)
    assert a == b


def test_all_spots_stay_lit_before_any_beat_arrives():
    """beat_clock is 0.0 until the first beat. The cue must still light — a
    song that never sends a beat must not render a blackout."""
    px = _render(CueByte.BLACKOUT_SPOTLIGHT, beat_clock=0.0)
    assert _windows(px) == BLACKOUT_SPOTS


def test_a_dim_spot_is_dimmer_but_not_off():
    """SPOT_DIM_FLOOR is what keeps '2-3 spotlights' true at every instant
    rather than one moving spot."""
    segments = _segments(_render(CueByte.BLACKOUT_SPOTLIGHT, beat_clock=0.0))
    dim = [c for _lo, _hi, c in segments if max(c) < 255]
    assert len(dim) == SPOTLIGHT_COUNT - 1
    for colour in dim:
        assert max(colour) > 0


# ── Cues that ask for no spotlight are untouched ─────────────────

def test_a_non_spotlight_cue_lights_the_whole_strip():
    """spotlight_count defaults to 1 and spotlight_region to 0.0, so the
    multi-spot path must be inert for every other cue."""
    px = _render(CueByte.SILHOUETTES)
    assert _windows(px) == [(0, MAPPED_REGION)]
