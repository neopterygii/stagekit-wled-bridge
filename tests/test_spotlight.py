"""Tests for the multi-spot spotlight cues.

The spotlight cues light several evenly spaced spots rather than one centre
window — the operator's ask was "2-3 spotlights of about the size the current
one is", i.e. a stage lit by a few lamps rather than one bigger lamp. All the
spots then PULSE together on the beat: full the instant a beat lands, easing
back to a lit trough over the beat.

They used to *chase* instead, the emphasis stepping spot→spot once per beat.
The operator asked for a pulse: a chase reads as the lamps switching around,
and at three spots it imposes a 3-beat cycle the music does not have.

These tests pin the spot geometry (count, width, placement), the pulse's
envelope (peak on the beat, monotonic decay, lit trough, all spots in lockstep),
and three properties that are easy to lose silently: that the pulse reads the
engine's injected beat clock rather than the wall clock, and that the cue stays
lit both before any beat has been seen and after the beats stop.

Nothing asserted spotlight geometry before this change, which is how the widths
drifted from what the backlog recorded.

Run: python -m pytest tests/test_spotlight.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import (  # noqa: E402
    CueEngine, SPOTLIGHT_COUNT, SPOTLIGHT_WIDTH_TIGHT, SPOTLIGHT_WIDTH_WIDE,
    SPOTLIGHT_PULSE_DEPTH,
)
from effects.mapper import LEDMapper, MAPPED_REGION  # noqa: E402
from protocol.yarg_packet import CueByte  # noqa: E402

# Arbitrary fixed instant — the engine takes its clock, so nothing here reads
# the wall clock.
NOW = 100.0

# The trough the pulse eases back to between beats.
SPOT_TROUGH = 1.0 - SPOTLIGHT_PULSE_DEPTH

# Expected windows on the 120px strip, from the widths the cues ask for.
BLACKOUT_SPOTS = [(10, 30), (50, 70), (90, 110)]      # 20px each
SILHOUETTE_SPOTS = [(5, 35), (45, 75), (85, 115)]     # 30px each


def _render(cue, beat_phase=0.0, beats_live=True, strictness=0.0):
    """Render one frame of `cue` at a chosen point on the beat.

    beat_phase is the engine's continuous 0→1 phase across one beat: 0.0 is the
    instant a beat lands (the pulse peak), 1.0 the end of the beat (its trough).

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
    fx["beat_phase"] = beat_phase
    fx["beats_live"] = beats_live
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


def _peaks(px):
    """Each spot's absolute brightness (brightest channel of its colour)."""
    return [max(c) for _lo, _hi, c in _segments(px)]


def _levels(cue, beat_phase, **kw):
    """Each spot's brightness as a fraction of its on-the-beat brightness.

    Measured against the same cue rendered at the pulse peak rather than against
    a hardcoded colour, so it reads the envelope alone — SILHOUETTES_SPOTLIGHT
    also breathes, and both cues get their colours remapped under a strict
    palette.
    """
    ref = max(_peaks(_render(cue, beat_phase=0.0, **kw)))
    return [p / ref for p in _peaks(_render(cue, beat_phase=beat_phase, **kw))]


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


# ── The beat pulse ───────────────────────────────────────────────

CUES = (CueByte.BLACKOUT_SPOTLIGHT, CueByte.SILHOUETTES_SPOTLIGHT)


def test_every_spot_pulses_in_lockstep_not_one_at_a_time():
    """The operator's ask, and the difference from the chase this replaced: the
    emphasis is a moment in time, so no spot is ever singled out."""
    for cue in CUES:
        for phase in (0.0, 0.25, 0.5, 0.75, 1.0):
            levels = _levels(cue, phase)
            assert len(levels) == SPOTLIGHT_COUNT
            assert max(levels) - min(levels) < 0.02, \
                f"{cue!r} spots diverged at phase {phase}: {levels}"


def test_the_pulse_peaks_the_instant_the_beat_lands():
    for cue in CUES:
        assert _levels(cue, 0.0)[0] == 1.0


def test_the_pulse_decays_monotonically_across_the_beat():
    """A pulse that brightens again mid-beat would read as two hits."""
    for cue in CUES:
        levels = [_levels(cue, p / 20.0)[0] for p in range(21)]
        for earlier, later in zip(levels, levels[1:]):
            assert later <= earlier + 1e-9, f"{cue!r} brightened mid-beat: {levels}"


def test_the_pulse_bottoms_out_at_the_trough_by_the_end_of_the_beat():
    for cue in CUES:
        assert abs(_levels(cue, 1.0)[0] - SPOT_TROUGH) < 0.02


def test_the_trough_is_dimmer_but_never_dark():
    """The trough is what keeps '2-3 spotlights' true at every instant rather
    than a stage that blinks off between beats."""
    for cue in CUES:
        assert 0.0 < SPOT_TROUGH < 1.0
        for _lo, _hi, colour in _segments(_render(cue, beat_phase=1.0)):
            assert max(colour) > 0


def test_the_pulse_is_driven_by_the_injected_clock_not_the_wall_clock():
    """The failure mode tests/test_replay.py warns about for strobe: sample the
    wall clock and replayed frames stop being reproducible."""
    a = _render(CueByte.BLACKOUT_SPOTLIGHT, beat_phase=0.4)
    b = _render(CueByte.BLACKOUT_SPOTLIGHT, beat_phase=0.4)
    assert a == b


def test_all_spots_stay_lit_before_any_beat_arrives():
    """beat_phase is 0.0 until the first beat. The cue must still light — a
    song that never sends a beat must not render a blackout."""
    px = _render(CueByte.BLACKOUT_SPOTLIGHT, beat_phase=0.0)
    assert _windows(px) == BLACKOUT_SPOTS
    assert _levels(CueByte.BLACKOUT_SPOTLIGHT, 0.0) == [1.0] * SPOTLIGHT_COUNT


def test_stopped_beats_rest_the_spots_at_full_not_at_the_trough():
    """beat_phase saturates at 1.0 and stays there once beats stop arriving, so
    reading it alone would park a finished or disconnected song on a stage dimmed
    to the trough. beats_live is the signal that separates that from one dropped
    beat packet, which does rest at the trough (a beat's worth of no motion)."""
    for cue in CUES:
        assert _levels(cue, 1.0, beats_live=False) == [1.0] * SPOTLIGHT_COUNT
        assert _levels(cue, 1.0, beats_live=True)[0] < 1.0


# ── Cues that ask for no spotlight are untouched ─────────────────

def test_a_non_spotlight_cue_lights_the_whole_strip():
    """spotlight_count defaults to 1 and spotlight_region to 0.0, so the
    multi-spot path must be inert for every other cue."""
    px = _render(CueByte.SILHOUETTES)
    assert _windows(px) == [(0, MAPPED_REGION)]
