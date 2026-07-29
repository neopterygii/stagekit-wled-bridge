"""Tests for event-stepped (counter-driven) cue patterns.

DEFAULT, WARM_MANUAL, COOL_MANUAL, STOMP and DISCHORD are driven by the chart
rather than the clock. They used to be asyncio coroutines awaiting a beat or
keyframe Event, which made them invisible to the replay harness — no event loop,
no stepping, dark zones. They now step from counters the packet path advances
and `tick()` reads (`_CounterPattern`), so they render deterministically.

Per the venue_scan inventory these are the library's most common cues, `next`
keyframes leading by roughly 10×, so their behaviour is worth pinning precisely
rather than only end to end.

Everything here injects `now` — into the engine clock, `on_keyframe`, `on_beat`
and `tick` — so no test depends on wall-clock timing.

Run: python -m pytest tests/test_counter_patterns.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import (  # noqa: E402
    ALL, BLUE, GREEN, KEYFRAME_DEDUP_WINDOW, KEYFRAME_FALLBACK_BEATS, NONE,
    RED, YELLOW, CueEngine,
)
from protocol.yarg_packet import BeatByte, CueByte, KeyframeByte  # noqa: E402


def _engine(bpm=120.0, now=1000.0):
    """An engine on a frozen injected clock, so nothing reads the wall clock."""
    e = CueEngine(clock=lambda: now)
    e.bpm = bpm
    return e


def _keyframe(engine, now):
    engine.on_keyframe(KeyframeByte.NEXT, now=now)


# ── apply_delay: which step is showing at launch ─────────────────
#
# The two coroutine shapes disagreed, and the difference is visible on the
# strip. Beat/chase loops applied step 0 *then* awaited; the listen loop awaited
# *then* applied, leaving the cue's own opening zones lit until the first event.

def test_manual_cues_show_step_zero_before_any_keyframe():
    """WARM_MANUAL painted pattern[0] immediately — apply_delay=0."""
    e = _engine()
    e.on_cue(CueByte.WARM_MANUAL)
    e.tick(1000.0)
    # RED's first step is ZERO|FOUR = bits 0 and 4.
    assert e.zones[RED] == (1 << 0) | (1 << 4)


def test_default_holds_its_opening_wash_until_the_first_keyframe():
    """DEFAULT awaited before assigning — apply_delay=1.

    Its cue sets BLUE=ALL; the pattern must not overwrite that with its own
    step 0 (which is NONE) before the chart has said anything. Getting this
    backwards starts the cue dark instead of blue.
    """
    e = _engine()
    e.on_cue(CueByte.DEFAULT)
    e.tick(1000.0)
    assert e.zones[BLUE] == ALL
    assert e.zones[RED] == NONE


def test_default_flips_on_the_first_keyframe():
    e = _engine()
    e.on_cue(CueByte.DEFAULT)
    e.tick(1000.0)
    _keyframe(e, 1000.1)
    e.tick(1000.1)
    # BLUE pattern is [NONE, ALL], RED is [ALL, NONE]: step 0 after one event.
    assert e.zones[BLUE] == NONE
    assert e.zones[RED] == ALL


def test_default_alternates_on_successive_keyframes():
    e = _engine()
    e.on_cue(CueByte.DEFAULT)
    seen = []
    for i in range(4):
        t = 1000.0 + 0.1 * (i + 1)
        _keyframe(e, t)
        e.tick(t)
        seen.append(e.zones[BLUE])
    assert seen == [NONE, ALL, NONE, ALL]


def test_keyframe_steps_advance_the_manual_scanner():
    e = _engine()
    e.on_cue(CueByte.WARM_MANUAL)
    e.tick(1000.0)
    first = e.zones[RED]
    _keyframe(e, 1000.1)
    e.tick(1000.1)
    assert e.zones[RED] != first
    # RED's 4-step pattern wraps back round after four keyframes.
    for i in range(2, 5):
        t = 1000.0 + 0.1 * i
        _keyframe(e, t)
        e.tick(t)
    assert e.zones[RED] == first


# ── duplicate datagrams ──────────────────────────────────────────

def test_a_duplicated_keyframe_datagram_advances_one_step():
    """asyncio.Event collapsed duplicates for free; a counter does not.

    YARG clears MLCKeyframe after each send, so a repeated NEXT can only be a
    duplicated datagram — which arrives within a packet interval, far inside the
    dedup window.
    """
    e = _engine()
    e.on_cue(CueByte.WARM_MANUAL)
    e.tick(1000.0)
    first = e.zones[RED]

    _keyframe(e, 1001.0)
    _keyframe(e, 1001.0 + KEYFRAME_DEDUP_WINDOW / 3)   # the duplicate
    e.tick(1001.0)
    assert e._keyframe_count == 1

    stepped_once = e.zones[RED]
    assert stepped_once != first
    # A genuinely later keyframe still counts.
    _keyframe(e, 1001.0 + KEYFRAME_DEDUP_WINDOW * 2)
    assert e._keyframe_count == 2


def test_non_next_keyframe_types_do_not_advance_the_counter():
    """FIRST/PREVIOUS are positional; the patterns this replaced ignored them."""
    e = _engine()
    e.on_keyframe(KeyframeByte.FIRST, now=1000.0)
    e.on_keyframe(KeyframeByte.PREVIOUS, now=1001.0)
    e.on_keyframe(KeyframeByte.OFF, now=1002.0)
    assert e._keyframe_count == 0


# ── the tempo fallback ───────────────────────────────────────────

def _fallback_interval(engine, pattern):
    return pattern.fallback_interval(engine.bpm)


def test_keyframe_pattern_falls_back_to_tempo_when_none_arrive():
    """DEFAULT and STOMP used to freeze solid here; now every keyframe cue coasts."""
    e = _engine()
    e.on_cue(CueByte.STOMP)
    p = e._counter_patterns[0]
    interval = _fallback_interval(e, p)
    assert interval > 0.0

    e.tick(1000.0)
    before = list(e.zones)
    e.tick(1000.0 + interval * 1.01)
    assert list(e.zones) != before, "the tempo fallback never fired"


def test_fallback_interval_is_two_nominal_step_intervals():
    """The asyncio wait_for used a 2x timeout and re-armed it, so the real
    fallback cadence was half the nominal rate — not the "BPM cadence" its
    comment claimed. Preserve the measured behaviour."""
    e = _engine(bpm=120.0)
    e.on_cue(CueByte.WARM_MANUAL)
    p = e._counter_patterns[0]          # RED: 4 steps at 0.25 cycles/beat
    nominal_steps_per_beat = 4 * 0.25   # = 1 step per beat
    expected = KEYFRAME_FALLBACK_BEATS * 60.0 / 120.0 / nominal_steps_per_beat
    assert _fallback_interval(e, p) == expected


def test_fallback_interval_tracks_a_tempo_change():
    """The coroutine recomputed its timeout every step, so a tempo change moved
    the fallback rate. Storing a rate rather than a duration keeps that."""
    e = _engine(bpm=120.0)
    e.on_cue(CueByte.WARM_MANUAL)
    p = e._counter_patterns[0]
    slow = _fallback_interval(e, p)
    e.bpm = 240.0
    assert _fallback_interval(e, p) == slow / 2.0


def test_a_real_keyframe_re_arms_the_fallback():
    """While the chart is driving, the timer must not also be stepping."""
    e = _engine()
    e.on_cue(CueByte.WARM_MANUAL)
    p = e._counter_patterns[0]
    interval = _fallback_interval(e, p)

    # Keyframes arriving comfortably inside the fallback window.
    t = 1000.0
    for _ in range(6):
        t += interval * 0.5
        _keyframe(e, t)
        e.tick(t)
    assert p.fallback_steps == 0, "the fallback fired while the chart was driving"
    assert e._keyframe_count == 6


def test_the_fallback_does_not_burst_after_a_long_gap():
    """A pause, host suspend or long GC leaves the deadline far in the past.

    Snap forward rather than emitting one step per missed interval, the same
    reasoning as _TimePattern's free-run scheduler.
    """
    e = _engine()
    e.on_cue(CueByte.WARM_MANUAL)
    p = e._counter_patterns[0]
    interval = _fallback_interval(e, p)

    e.tick(1000.0)
    e.tick(1000.0 + interval * 500)    # a very long gap in one jump
    assert p.fallback_steps == 1


# ── pause ────────────────────────────────────────────────────────

def test_patterns_do_not_step_while_paused():
    e = _engine()
    e.on_cue(CueByte.WARM_MANUAL)
    e.tick(1000.0)
    before = list(e.zones)

    e.paused = True
    _keyframe(e, 1001.0)
    e.tick(1001.0)
    assert list(e.zones) == before, "a paused engine stepped an event pattern"


def test_a_keyframe_arriving_while_paused_applies_on_resume():
    """The counter is advanced by the packet path regardless; tick() is what
    freezes. So the chart's position is not lost across a pause."""
    e = _engine()
    e.on_cue(CueByte.WARM_MANUAL)
    e.tick(1000.0)
    before = list(e.zones)

    e.paused = True
    _keyframe(e, 1001.0)
    e.tick(1001.0)
    e.paused = False
    e.tick(1002.0)
    assert list(e.zones) != before


def test_resuming_does_not_burst_the_fallback():
    e = _engine()
    e.on_cue(CueByte.WARM_MANUAL)
    p = e._counter_patterns[0]
    interval = _fallback_interval(e, p)

    e.tick(1000.0)
    e.paused = True
    for i in range(1, 40):
        e.tick(1000.0 + interval * i)
    e.paused = False
    e.tick(1000.0 + interval * 40)
    assert p.fallback_steps == 1


# ── beat-stepped patterns (DISCHORD) ─────────────────────────────

def test_dischord_steps_on_major_beats():
    e = _engine()
    e.on_cue(CueByte.DISCHORD)
    e.tick(1000.0)
    before = e.zones[YELLOW]
    e.on_beat(BeatByte.STRONG, now=1000.5)
    e.tick(1000.5)
    assert e.zones[YELLOW] != before


def test_dischord_does_not_step_on_weak_beats():
    """WEAK woke the coroutine but never advanced it — it re-waited. _beat_count
    counts only MEASURE/STRONG, which preserves that."""
    e = _engine()
    e.on_cue(CueByte.DISCHORD)
    e.tick(1000.0)
    before = e.zones[YELLOW]
    e.on_beat(BeatByte.WEAK, now=1000.5)
    e.tick(1000.5)
    assert e.zones[YELLOW] == before


def test_dischord_bridges_a_dropped_beat_packet():
    """_advance_clock rounds elapsed time to bridge a lost packet, so the chase
    stays in musical phase rather than falling a step behind — the rule the
    time-driven PLL already follows."""
    # One packet lost: beats land at 1000.5 and 1001.5, a two-beat gap.
    dropped = _engine(bpm=120.0)       # 0.5 s per beat
    dropped.on_cue(CueByte.DISCHORD)
    dropped.tick(1000.0)
    dropped.on_beat(BeatByte.STRONG, now=1000.5)
    dropped.tick(1000.5)
    one_beat = dropped.zones[YELLOW]
    dropped.on_beat(BeatByte.STRONG, now=1001.5)
    dropped.tick(1001.5)

    # The same passage with every packet received.
    intact = _engine(bpm=120.0)
    intact.on_cue(CueByte.DISCHORD)
    intact.tick(1000.0)
    for t in (1000.5, 1001.0, 1001.5):
        intact.on_beat(BeatByte.STRONG, now=t)
        intact.tick(t)

    # The lost packet was bridged, so both are three beats into the song and on
    # the same step — two steps past where the first beat left it, not one.
    assert dropped._beat_count == intact._beat_count == 3
    assert dropped.zones[YELLOW] == intact.zones[YELLOW]
    assert dropped.zones[YELLOW] != one_beat


def test_dischord_beat_pattern_has_no_tempo_fallback():
    """With no beats there is no tempo to fall back to; beat_clock coasts."""
    e = _engine()
    e.on_cue(CueByte.DISCHORD)
    beat_patterns = [p for p in e._counter_patterns if p.source == 'beat']
    assert beat_patterns
    assert all(p.fallback_steps_per_beat == 0.0 for p in beat_patterns)


def test_dischords_time_driven_half_is_untouched():
    """Only YELLOW is event-stepped; GREEN is a plain BPM chase and must stay a
    _TimePattern, gliding as it always did."""
    e = _engine()
    e.on_cue(CueByte.DISCHORD)
    assert len(e._counter_patterns) == 1
    assert e._time_patterns
    assert any(GREEN in p.owned_zones for p in e._time_patterns)


# ── lifecycle ────────────────────────────────────────────────────

def test_a_cue_change_drops_the_previous_counter_patterns():
    e = _engine()
    e.on_cue(CueByte.WARM_MANUAL)
    assert e._counter_patterns
    e.on_cue(CueByte.CHORUS)           # time-driven only
    assert e._counter_patterns == []


def test_counter_patterns_claim_no_zones_so_they_render_as_blocks():
    """Gliding needs the next step's arrival time, which a chart keyframe stream
    does not give. These write self.zones and are painted as cell blocks, which
    is what the coroutines did — the reason the migration did not change the
    look of any fixture but authored_venue."""
    e = _engine()
    e.on_cue(CueByte.WARM_MANUAL)
    e.tick(1000.0)
    assert e._time_patterns == []
    assert e.motion_sources == []
    # The zone's cell levels are populated from the bitmask, not zeroed for a
    # motion renderer to take over.
    assert any(lv > 0.0 for lv in e.zone_cell_levels[RED])


def test_the_step_index_is_a_pure_function_of_the_counter():
    """Two engines fed the same events in different real-time orderings land on
    the same step — the property replay determinism rests on."""
    a, b = _engine(), _engine()
    for e in (a, b):
        e.on_cue(CueByte.COOL_MANUAL)
        e.tick(1000.0)
    for i in range(1, 6):
        _keyframe(a, 1000.0 + i * 0.1)
        a.tick(1000.0 + i * 0.1)
    for i in range(1, 6):
        _keyframe(b, 1000.0 + i * 0.037)   # same events, different arrival times
        b.tick(1000.0 + i * 0.037)
    assert a.zones[BLUE] == b.zones[BLUE]
    assert a.zones[GREEN] == b.zones[GREEN]


# ── venue density transform ──────────────────────────────────────

def test_the_density_transform_still_applies_to_manual_chases():
    """Applied at launch, never per frame — unchanged by the migration."""
    e = _engine()
    e.on_venue_size(1)                 # Small: thins chase steps
    e.on_cue(CueByte.WARM_MANUAL)
    thinned = [list(p.steps) for p in e._counter_patterns]

    e2 = _engine()
    e2.on_venue_size(0)                # no transform
    e2.on_cue(CueByte.WARM_MANUAL)
    assert thinned != [list(p.steps) for p in e2._counter_patterns]


def test_default_is_exempt_from_the_density_transform():
    """DEFAULT's steps are washes (NONE/ALL), not a chase. Thinning them would
    dim the cue rather than sparsen a pattern, so the coroutine never applied
    the transform and neither does its replacement."""
    e = _engine()
    e.on_venue_size(1)
    e.on_cue(CueByte.DEFAULT)
    masks = {mask for p in e._counter_patterns for step in p.steps
             for _, mask in step}
    assert masks == {NONE, ALL}
