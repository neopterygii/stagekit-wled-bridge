"""Tests for the tempo-locked strobe in CueEngine.

The strobe rate follows the song BPM (YALCY StrobeDmxFromBpm: each speed byte
is a note division, hz = bpm * division / 60), with the fixed STROBE_RATES as
the fallback when BPM is unknown. `get_strobe_visible` takes an injectable
`now` so these are fully deterministic (no sleeping, no wall-clock).

Run: python -m pytest tests/test_strobe.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import CueEngine, STROBE_RATES  # noqa: E402
from protocol.yarg_packet import StrobeSpeed  # noqa: E402


def _engine(bpm=120.0):
    e = CueEngine()
    e.bpm = bpm
    return e


# ── BPM-derived rate ─────────────────────────────────────────────

def test_rate_is_note_division_at_known_bpm():
    # At 120 BPM a beat is 0.5 s, so divisions land exactly on the old fixed
    # rates: quarter=2 Hz, eighth=4, sixteenth=8, thirty-second=16.
    e = _engine(bpm=120.0)
    for byte, hz in ((StrobeSpeed.SLOW, 2.0), (StrobeSpeed.MEDIUM, 4.0),
                     (StrobeSpeed.FAST, 8.0), (StrobeSpeed.FASTEST, 16.0)):
        e.on_strobe(byte)
        assert abs(e.strobe_hz() - hz) < 1e-9


def test_rate_scales_with_bpm():
    # At 90 BPM a beat is 2/3 s: sixteenth-note strobe = 4 * 90/60 = 6 Hz.
    e = _engine(bpm=90.0)
    e.on_strobe(StrobeSpeed.FAST)
    assert abs(e.strobe_hz() - 6.0) < 1e-9


def test_tempo_change_retunes_live():
    # The byte is stored, not the rate — a BPM change retunes the strobe
    # without the byte being re-sent.
    e = _engine(bpm=120.0)
    e.on_strobe(StrobeSpeed.FASTEST)
    assert abs(e.strobe_hz() - 16.0) < 1e-9
    e.bpm = 60.0
    assert abs(e.strobe_hz() - 8.0) < 1e-9


def test_off_stays_off():
    e = _engine(bpm=120.0)
    e.on_strobe(StrobeSpeed.OFF)
    assert e.strobe_hz() == 0


def test_unknown_byte_is_off():
    e = _engine(bpm=120.0)
    e.on_strobe(99)
    assert e.strobe_hz() == 0


# ── fixed-Hz fallback when BPM is unknown ────────────────────────

def test_fallback_to_fixed_rates_when_bpm_zero():
    e = _engine(bpm=0.0)
    for byte, hz in STROBE_RATES.items():
        e.on_strobe(byte)
        assert e.strobe_hz() == hz


# ── gate cadence ─────────────────────────────────────────────────

def test_gate_open_when_off():
    e = _engine(bpm=120.0)
    e.on_strobe(StrobeSpeed.OFF)
    assert e.get_strobe_visible(now=100.0)
    assert e.get_strobe_visible(now=100.4)


def test_gate_produces_black_frames_at_half_cycle():
    # FAST at 120 BPM = 8 Hz → 0.125 s period: visible the first half of each
    # cycle, black the second half.
    e = _engine(bpm=120.0)
    e.on_strobe(StrobeSpeed.FAST)
    period = 0.125
    for cycle in range(4):
        t0 = 100.0 + cycle * period
        assert e.get_strobe_visible(now=t0 + 0.01)            # first half: lit
        assert not e.get_strobe_visible(now=t0 + period / 2)  # second half: black
        assert not e.get_strobe_visible(now=t0 + period - 0.01)


def test_gate_cadence_follows_tempo_change():
    # Same byte, slower tempo → longer period (60 BPM eighth-note = 2 Hz,
    # 0.5 s period).
    e = _engine(bpm=60.0)
    e.on_strobe(StrobeSpeed.MEDIUM)
    assert e.get_strobe_visible(now=100.1)
    assert not e.get_strobe_visible(now=100.3)
    assert e.get_strobe_visible(now=100.6)  # next cycle
