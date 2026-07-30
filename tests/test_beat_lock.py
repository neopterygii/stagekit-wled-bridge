"""Tests for phase-locked (PLL) chase motion.

Beat-locked patterns free-run on tempo and are pulled smoothly toward the
beat-locked target; they fall back to pure free-run when beats are absent and
never reverse. `_TimePattern` is constructed with explicit `now`, and both
`on_beat` and `tick` take injectable times, so these are deterministic.

Run: python -m pytest tests/test_beat_lock.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import CueEngine, _TimePattern, RED  # noqa: E402
from protocol.yarg_packet import BeatByte, CueByte  # noqa: E402


def _pattern(engine, n=8, cpb=0.25, now=0.0):
    steps = [[(RED, 1 << i)] for i in range(n)]
    p = _TimePattern(steps, bpm_sync=True, param=cpb, now=now, init_bpm=120.0)
    engine._time_patterns = [p]
    return p


def _wrapped_err(target, pos, n):
    return (target - pos + n * 0.5) % n - n * 0.5


# ── flag assignment ──────────────────────────────────────────────

def test_forward_chase_is_beat_locked_frenzy_is_not():
    e = CueEngine()
    e.on_cue(CueByte.WARM_AUTOMATIC)          # forward BPM chases
    assert e._time_patterns and all(p.beat_lock for p in e._time_patterns)
    e.on_cue(CueByte.FRENZY)                  # reverse_on_beat chaos
    assert e._time_patterns and not any(p.beat_lock for p in e._time_patterns)


# ── fallback: free-run without beats ─────────────────────────────

def test_freeruns_on_tempo_without_beats():
    e = CueEngine(); e.bpm = 120.0
    p = _pattern(e, n=4, cpb=1.0, now=1000.0)  # 4*1*120/60 = 8 steps/sec
    e.tick(1000.0)                             # dt=0 → no advance
    assert p.pos == 0.0
    e.tick(1000.1)                             # dt=0.1 → 0.8 steps
    assert abs(p.pos - 0.8) < 1e-9
    e.tick(1000.2)
    assert abs(p.pos - 1.6) < 1e-9


# ── lock: converges to the beat-locked target ────────────────────

def test_converges_to_lock_with_steady_beats():
    e = CueEngine(); e.bpm = 120.0            # 0.5 s beat
    p = _pattern(e, n=8, cpb=0.25, now=0.0)
    p.pos = 3.0                                # start well out of phase
    p.last_tick = 0.0
    t = 0.0
    next_beat = 0.0
    for _ in range(240):                       # ~6 s at 40 fps
        t += 0.025
        while t >= next_beat:
            e.on_beat(BeatByte.STRONG, now=next_beat)
            next_beat += 0.5
        e.tick(t)
    target = (e.beat_clock(t) * 8 * p.param) % 8
    assert abs(_wrapped_err(target, p.pos, 8)) < 0.4, "PLL failed to lock"


# ── never reverses (hesitates instead) ───────────────────────────

def test_advance_never_reverses_when_target_is_behind():
    e = CueEngine(); e.bpm = 120.0
    p = _pattern(e, n=8, cpb=0.25, now=0.0)
    e.on_beat(BeatByte.STRONG, now=0.0)        # beats live, target near 0
    p.pos = 5.0                                # far ahead of the target
    p.last_tick = 0.0
    before = p.pos
    e.tick(0.05)                               # correction wants to pull back
    # Clamp holds motion (advance >= 0): pos never drops below where it was.
    assert p.pos >= before - 1e-9


# ── stale beats fall back to free-run ────────────────────────────

def test_falls_back_when_beats_go_stale():
    e = CueEngine(); e.bpm = 120.0
    p = _pattern(e, n=4, cpb=1.0, now=0.0)     # 8 steps/sec
    e.on_beat(BeatByte.STRONG, now=0.0)
    # Tick far past BEAT_LOCK_TIMEOUT → beats stale → pure free-run.
    e.tick(0.0)
    p.pos = 0.0
    p.last_tick = 10.0
    e.tick(10.1)                               # dt 0.1 → 0.8 steps, no lock tug
    assert abs(p.pos - 0.8) < 1e-9


# ── launch phase: the PLL starts locked, not at step 0 ───────────
#
# A cue used to launch every beat-locked pattern at step 0 regardless of where
# the song was, handing the PLL an arbitrary phase error of up to half the
# pattern to eat. Because the correction is clamped forward-only (see
# test_advance_never_reverses_when_target_is_behind, which pins that clamp), the
# two signs of that error looked different and both looked wrong: forward error
# sprinted, backward error stopped the chase dead until the target came round.
# The operator saw it on SEARCHLIGHTS — "a rough start before smoothing out".
#
# Measured on the cue set before the fix: up to 20x the steady rate for ~0.3 s,
# or up to 1.0 s frozen. The slower the cue, the worse it was.

FPS = 60.0


def _launch_and_measure(cue, nbeats, launch_offset, bpm=120.0, seconds=1.0):
    """Per-frame motion of `cue` over its first second, launched `launch_offset`
    into the beat after `nbeats` beats of song. Returns (speeds, steady_rate)."""
    beat = 60.0 / bpm
    t = [1000.0]
    e = CueEngine(clock=lambda: t[0])
    e.bpm = bpm
    for i in range(nbeats):
        t[0] = 1000.0 + i * beat
        e.on_beat(BeatByte.MEASURE if i % 4 == 0 else BeatByte.STRONG, now=t[0])
    last_beat = t[0]
    t[0] = last_beat + launch_offset
    e.on_cue(cue)

    speeds, prev, nb = [], None, last_beat
    for f in range(int(seconds * FPS)):
        now = last_beat + launch_offset + f / FPS
        while nb + beat <= now:
            nb += beat
            e.on_beat(BeatByte.STRONG, now=nb)
        t[0] = now
        e.tick(now)
        pos = [p.pos for p in e._time_patterns]
        sizes = [len(p.steps) for p in e._time_patterns]
        if prev is not None:
            speeds.append([((a - b + n * 0.5) % n) - n * 0.5
                           for a, b, n in zip(pos, prev, sizes)])
        prev = pos
    # Steady rate is a property of the pattern, not of the measurement.
    steady = [p.steps_per_second(bpm) / FPS for p in e._time_patterns]
    return speeds, steady


# Every cue whose look is a time-driven beat-locked chase.
LOCKED_CUES = (CueByte.CHORUS, CueByte.WARM_AUTOMATIC, CueByte.COOL_AUTOMATIC,
               CueByte.SEARCHLIGHTS, CueByte.SWEEP, CueByte.HARMONY,
               CueByte.BIG_ROCK_ENDING, CueByte.DISCHORD)


def test_every_beat_locked_cue_launches_at_its_steady_rate():
    """No sprint and no freeze at cue change, at any launch phase or beat parity.

    Both beat parities are swept because the sign of the old error depended on
    them: the target advances half a pattern per beat for a cpb=0.5 chase, so an
    odd beat count put the step-0 launch on the far side of the ring.
    """
    for cue in LOCKED_CUES:
        for nbeats in (8, 9, 10, 11):
            for k in range(10):
                offset = k * 0.5 / 10
                speeds, steady = _launch_and_measure(cue, nbeats, offset)
                assert speeds, f"{cue!r} launched no time-driven pattern"
                for frame in speeds:
                    for got, want in zip(frame, steady):
                        assert abs(got - want) < 0.02 * want + 1e-9, (
                            f"{cue!r} at beat {nbeats} phase {offset:.2f}: "
                            f"moved {got:.4f} steps/frame against a steady "
                            f"{want:.4f} — the launch transient is back")


def test_a_launched_pattern_starts_in_phase_with_the_beat_clock():
    """The mechanism, stated directly: pos is seeded from the beat clock."""
    t = [1000.0]
    e = CueEngine(clock=lambda: t[0]); e.bpm = 120.0
    e.on_beat(BeatByte.MEASURE, now=t[0])
    for _ in range(5):                          # a few beats of song
        e.on_beat(BeatByte.STRONG, now=e._beat_at + 0.5)
    now = t[0] = e._beat_at + 0.3                # mid-beat cue change
    e.on_cue(CueByte.SEARCHLIGHTS)
    for p in e._time_patterns:
        target = p.beat_target(e.beat_clock(now)) % len(p.steps)
        assert abs(p.pos - target) < 1e-9
        assert p.step == int(p.pos)


def test_a_cold_start_still_launches_at_step_zero():
    """Before the first beat there is no phase to lock to, so the old behaviour
    has to survive — this is the invariant that keeps the golden digests of the
    beat-less fixtures (warm_beats, star_power_run) bit-identical."""
    e = CueEngine(); e.bpm = 120.0
    e.on_cue(CueByte.SEARCHLIGHTS)              # no beat ever delivered
    assert e._time_patterns
    for p in e._time_patterns:
        assert p.pos == 0.0
        assert p.step == 0


def test_a_cue_launched_after_the_beats_stop_starts_at_step_zero():
    """Stale beats are not a phase either — beat_clock has coasted on past
    anything real, so seeding from it would be inventing a position."""
    e = CueEngine(clock=lambda: 1000.0 + 30.0); e.bpm = 120.0
    e.on_beat(BeatByte.MEASURE, now=1000.0)     # long past BEAT_LOCK_TIMEOUT
    e.on_cue(CueByte.SEARCHLIGHTS)
    assert e._time_patterns
    for p in e._time_patterns:
        assert p.pos == 0.0


def test_frenzy_is_unseeded_because_it_is_not_beat_locked():
    """reverse_on_beat patterns keep the free-run scheduler, which has no phase
    target; seeding one would just start it somewhere arbitrary."""
    t = [1000.0]
    e = CueEngine(clock=lambda: t[0]); e.bpm = 120.0
    e.on_beat(BeatByte.MEASURE, now=t[0])
    for _ in range(4):
        e.on_beat(BeatByte.STRONG, now=e._beat_at + 0.5)
    t[0] = e._beat_at + 0.3                     # beats live, so a phase exists
    assert e.beats_live(t[0]), "test would pass vacuously on stale beats"
    e.on_cue(CueByte.FRENZY)
    assert e._time_patterns
    for p in e._time_patterns:
        assert not p.beat_lock
        assert p.pos == 0.0


if __name__ == "__main__":
    import traceback
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
