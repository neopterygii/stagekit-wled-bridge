"""Tests for the beat oscillator in CueEngine.

The oscillator turns YARG's BPM + discrete beat edges into a continuous phase.
`on_beat` takes an injectable `now` so these are fully deterministic (no
sleeping, no wall-clock dependence).

Run: python -m pytest tests/test_beat_oscillator.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import CueEngine  # noqa: E402
from protocol.yarg_packet import BeatByte  # noqa: E402


def _engine(bpm=120.0):
    e = CueEngine()
    e.bpm = bpm            # interval = 60/120 = 0.5 s per beat
    return e


# ── phase ramp ───────────────────────────────────────────────────

def test_no_beat_phase_is_zero():
    e = _engine()
    assert e.beat_phase(1000.0) == 0.0
    assert e.bar_phase(1000.0) == 0.0


def test_phase_ramps_linearly_then_saturates():
    e = _engine(bpm=120.0)                 # 0.5 s beat
    e.on_beat(BeatByte.STRONG, now=100.0)
    assert e.beat_phase(100.0) == 0.0
    assert abs(e.beat_phase(100.125) - 0.25) < 1e-9
    assert abs(e.beat_phase(100.25) - 0.5) < 1e-9
    # Late/dropped next beat: saturate at 1.0, never wrap or overshoot.
    assert e.beat_phase(100.5) == 1.0
    assert e.beat_phase(101.7) == 1.0


def test_phase_tracks_bpm():
    e = _engine(bpm=60.0)                   # 1.0 s beat
    e.on_beat(BeatByte.STRONG, now=10.0)
    assert abs(e.beat_phase(10.5) - 0.5) < 1e-9


# ── bar position ─────────────────────────────────────────────────

def test_measure_resets_bar_strong_advances():
    e = _engine(bpm=120.0)
    e.on_beat(BeatByte.MEASURE, now=100.0)
    assert e._bar_beat == 0
    e.on_beat(BeatByte.STRONG, now=100.5)
    assert e._bar_beat == 1
    e.on_beat(BeatByte.STRONG, now=101.0)
    assert e._bar_beat == 2
    e.on_beat(BeatByte.STRONG, now=101.5)
    assert e._bar_beat == 3
    # Downbeat of the next bar
    e.on_beat(BeatByte.MEASURE, now=102.0)
    assert e._bar_beat == 0


def test_bar_phase_is_beat_index_plus_phase():
    e = _engine(bpm=120.0)
    e.on_beat(BeatByte.MEASURE, now=100.0)
    e.on_beat(BeatByte.STRONG, now=100.5)   # bar_beat = 1
    assert abs(e.bar_phase(100.75) - 1.5) < 1e-9   # 1 + 0.5


# ── ignored edges ────────────────────────────────────────────────

def test_weak_and_off_do_not_drive_oscillator():
    e = _engine(bpm=120.0)
    e.on_beat(BeatByte.MEASURE, now=100.0)
    assert e._beat_at == 100.0 and e._bar_beat == 0
    # WEAK is ambiguous with YARG's "3" sentinel — must not move anything.
    e.on_beat(BeatByte.WEAK, now=100.2)
    assert e._beat_at == 100.0 and e._bar_beat == 0
    # OFF likewise.
    e.on_beat(BeatByte.OFF, now=100.3)
    assert e._beat_at == 100.0 and e._bar_beat == 0


def test_duplicate_strong_is_debounced():
    e = _engine(bpm=120.0)                  # 0.5 s beat, debounce = 0.15 s
    e.on_beat(BeatByte.MEASURE, now=100.0)  # bar 0
    e.on_beat(BeatByte.STRONG, now=100.5)   # bar 1, counted
    e.on_beat(BeatByte.STRONG, now=100.51)  # duplicate within debounce
    assert e._bar_beat == 1
    assert e._beat_at == 100.5              # phase still ramps from the real edge


# ── integration with the effects dict ────────────────────────────

def test_get_effects_exposes_phase():
    e = _engine(bpm=120.0)
    e.on_beat(BeatByte.STRONG, now=100.0)
    fx = e.get_effects()
    assert "beat_phase" in fx
    assert "bar_phase" in fx
    assert "bar_beat" in fx


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
