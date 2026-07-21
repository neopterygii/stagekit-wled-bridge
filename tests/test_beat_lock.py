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
