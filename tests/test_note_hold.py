"""Tests for note-hold rising-edge accents (VISION Phase 4).

A YARG note bitmask (offsets 14-17) carries the frets/pads currently lit for
each instrument. A single hit often lives in one ~88 Hz packet, so the engine
turns each *rising edge* into a per-instrument accent held for at least a 1/32
note, then decaying — and the mapper paints it in that instrument's slice of the
strip. These tests pin the edge detection, the hold/decay envelope, and the
per-instrument spatial placement.

Run: python -m pytest tests/test_note_hold.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import CueEngine, NOTE_HOLD_FLOOR, NOTE_HOLD_BEATS  # noqa: E402
from effects.mapper import (  # noqa: E402
    LEDMapper, MAPPED_REGION, CELL_SIZE, NOTE_CELLS_PER_INSTRUMENT,
)


def _lum(px):
    n = len(px) // 3
    return [max(px[i * 3], px[i * 3 + 1], px[i * 3 + 2]) for i in range(n)]


# ── Rising-edge detection ────────────────────────────────────────

def test_rising_edge_seeds_accent():
    eng = CueEngine()
    eng.bpm = 120.0
    # Guitar bit 0 goes 0→1 at t=0.
    eng.on_notes(0b0001, 0, 0, 0, now=0.0)
    accents = eng._note_accents(0.0)
    assert accents[0] > 0.0            # guitar accent live
    assert accents[1] == accents[2] == accents[3] == 0.0


def test_held_bit_does_not_retrigger():
    eng = CueEngine()
    eng.bpm = 120.0
    eng.on_notes(0b0001, 0, 0, 0, now=0.0)
    # Same bit still held later — no rising edge, so the accent keeps decaying
    # from the ORIGINAL seed rather than being refreshed.
    dur = eng._note_dur[0]
    eng.on_notes(0b0001, 0, 0, 0, now=dur * 0.5)
    # deadline unchanged (no reseed)
    assert abs(eng._note_until[0] - dur) < 1e-9


def test_new_bit_while_others_held_retriggers():
    eng = CueEngine()
    eng.bpm = 120.0
    eng.on_notes(0b0001, 0, 0, 0, now=0.0)
    dur = eng._note_dur[0]
    # bit 1 rises later while bit 0 is still held → reseed at the new time.
    eng.on_notes(0b0011, 0, 0, 0, now=dur * 0.5)
    assert abs(eng._note_until[0] - (dur * 0.5 + dur)) < 1e-9


# ── Hold / decay envelope ────────────────────────────────────────

def test_accent_decays_to_zero_after_hold():
    eng = CueEngine()
    eng.bpm = 120.0
    eng.on_notes(0, 0, 0b0001, 0, now=0.0)   # drums
    dur = eng._note_dur[2]
    assert eng._note_accents(0.0)[2] > 0.0
    assert eng._note_accents(dur * 0.5)[2] > 0.0
    # At/after the deadline it's fully gone.
    assert eng._note_accents(dur)[2] == 0.0
    assert eng._note_accents(dur + 1.0)[2] == 0.0


def test_decay_is_monotonic():
    eng = CueEngine()
    eng.bpm = 120.0
    eng.on_notes(0b0001, 0, 0, 0, now=0.0)
    dur = eng._note_dur[0]
    prev = 2.0
    for k in range(11):
        v = eng._note_accents(dur * k / 10.0)[0]
        assert v <= prev + 1e-9
        prev = v


def test_hold_tracks_tempo_with_floor():
    # At a slow tempo the 1/32 note is long; at a fast tempo the floor kicks in.
    slow = CueEngine(); slow.bpm = 60.0
    slow.on_notes(0b0001, 0, 0, 0, now=0.0)
    assert abs(slow._note_dur[0] - (60.0 / 60.0) * NOTE_HOLD_BEATS) < 1e-9

    fast = CueEngine(); fast.bpm = 400.0
    fast.on_notes(0b0001, 0, 0, 0, now=0.0)
    assert fast._note_dur[0] == NOTE_HOLD_FLOOR   # floored


def test_chord_seeds_stronger_than_single():
    single = CueEngine(); single.bpm = 120.0
    single.on_notes(0b0001, 0, 0, 0, now=0.0)
    chord = CueEngine(); chord.bpm = 120.0
    chord.on_notes(0b0111, 0, 0, 0, now=0.0)   # three simultaneous new bits
    assert chord._note_accents(0.0)[0] > single._note_accents(0.0)[0]


# ── Spatial placement in the mapper ──────────────────────────────

def _render_with_notes(note_accents):
    m = LEDMapper(MAPPED_REGION)   # led_count == mapped region (no mirror tail)
    colors = {"red": (255, 0, 0), "green": (0, 255, 0),
              "blue": (0, 0, 255), "yellow": (255, 255, 0)}
    # Dark scene so only the note accents light up.
    return m.render([0, 0, 0, 0], zone_colors=colors,
                    effects={"note_accents": note_accents}, brightness=1.0)


def test_instrument_lights_only_its_region():
    region = NOTE_CELLS_PER_INSTRUMENT * CELL_SIZE
    # Only drums (index 2) hit.
    px = _render_with_notes([0.0, 0.0, 1.0, 0.0])
    lum = _lum(px)
    drum_lo, drum_hi = 2 * region, 3 * region
    assert max(lum[drum_lo:drum_hi]) > 0            # drums region lit
    # Every other region stays dark.
    assert max(lum[:drum_lo]) == 0
    assert max(lum[drum_hi:MAPPED_REGION]) == 0


def test_no_accents_leaves_strip_dark():
    px = _render_with_notes([0.0, 0.0, 0.0, 0.0])
    assert max(_lum(px)) == 0


def test_stacked_accents_stay_bounded():
    # All four instruments at full — whitening must never exceed 255.
    px = _render_with_notes([1.0, 1.0, 1.0, 1.0])
    assert max(_lum(px)) <= 255
