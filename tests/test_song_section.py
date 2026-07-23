"""Tests for the song-section palette/energy bias (VISION signal inventory).

YARG sends the section byte at offset 13 — the LightingType of the most
recent Verse/Chorus lighting event (Verse=5, Chorus=2, 0=None), matching
YALCY's SongSectionByte. The bridge applies a slow, subtle per-section bias
that modulates the current look without repainting it: a convex hue lean on
lit pixels (mapper, eased in over SECTION_EASE) plus an energy scale on the
breathing swing and beat-pump depth (verse settles, chorus lifts). None /
unknown sections are identity, so the authored look stays bit-exact.

These tests pin: the wire parsing + enum, the engine plumbing (knobs resolved
at signal-change time), the ease-in (no snap), the measurable render shift in
the intended direction per section, lit-pixels-only safety, default/unknown
bit-exactness, and the dashboard surfacing.

Run: python -m pytest tests/test_song_section.py -v
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import (  # noqa: E402
    CueEngine, SECTION_BIAS, SECTION_EASE,
)
from effects.mapper import (  # noqa: E402
    LEDMapper, MAPPED_REGION, SECTION_BIAS_STRENGTH,
)
from protocol.yarg_packet import (  # noqa: E402
    parse_packet, PACKET_HEADER, SongSectionByte,
)
from status_server import StatusTracker  # noqa: E402
from settings import BridgeSettings  # noqa: E402

# Dim red wash so the beat-pump lift has headroom below the 255 clamp.
_COLORS = {"red": (100, 0, 0), "green": (0, 100, 0),
           "blue": (0, 0, 100), "yellow": (100, 100, 0)}

_VERSE_HUE, _VERSE_ENERGY = SECTION_BIAS[SongSectionByte.VERSE]
_CHORUS_HUE, _CHORUS_ENERGY = SECTION_BIAS[SongSectionByte.CHORUS]


def _render(section, zones=(0xFF, 0, 0, 0), **effects):
    m = LEDMapper(MAPPED_REGION)
    if section is not None:
        effects["section"] = section
    return m.render(list(zones), zone_colors=_COLORS,
                    effects=effects, brightness=1.0)


# ── Parsing + enum ───────────────────────────────────────────────

def _datagram(section: int) -> bytes:
    """Minimal 44-byte v1 datagram carrying the given section byte."""
    buf = bytearray(44)
    struct.pack_into("<I", buf, 0, PACKET_HEADER)
    buf[4] = 1                      # datagram version
    buf[13] = section               # song section
    struct.pack_into("<f", buf, 9, 120.0)
    return bytes(buf)


def test_packet_parses_song_section():
    assert parse_packet(_datagram(5)).song_section == SongSectionByte.VERSE
    assert parse_packet(_datagram(2)).song_section == SongSectionByte.CHORUS
    assert parse_packet(_datagram(0)).song_section == SongSectionByte.NONE


def test_section_values_match_yarg_lighting_type():
    # YARG writes the LightingType of the last Verse/Chorus event, so the
    # section values match the cue bytes (YALCY SongSectionByte agrees).
    assert SongSectionByte.VERSE == 5
    assert SongSectionByte.CHORUS == 2
    assert SongSectionByte.NONE == 0
    assert SongSectionByte.name(5) == "Verse"
    assert SongSectionByte.name(2) == "Chorus"
    assert SongSectionByte.name(0) == "None"
    assert SongSectionByte.name(9) == "9"      # unknown → numeric fallback


# ── Engine plumbing ──────────────────────────────────────────────

def test_default_engine_section_is_identity():
    eng = CueEngine()                            # no on_song_section call
    assert eng.get_effects()["section"] is None


def test_none_and_unknown_sections_are_identity():
    eng = CueEngine()
    eng.on_song_section(SongSectionByte.NONE)
    assert eng.get_effects()["section"] is None
    eng.on_song_section(9)                       # not a SongSectionByte value
    assert eng.get_effects()["section"] is None


def test_section_change_resolves_bias_knobs():
    eng = CueEngine()
    eng.on_song_section(SongSectionByte.CHORUS, now=100.0)
    hue, gain, energy = eng._section_state(100.0 + SECTION_EASE)
    assert hue == _CHORUS_HUE
    assert energy == _CHORUS_ENERGY
    assert gain == 1.0


def test_verse_energy_settles_chorus_lifts():
    # The intended direction: chorus more energetic than the authored look,
    # verse calmer.
    assert _VERSE_ENERGY < 1.0 < _CHORUS_ENERGY


def test_section_gain_eases_in_then_holds():
    eng = CueEngine()
    eng.on_song_section(SongSectionByte.VERSE, now=100.0)
    assert eng._section_gain(100.0) == 0.0
    assert 0.0 < eng._section_gain(100.0 + SECTION_EASE / 2) < 1.0
    assert eng._section_gain(100.0 + SECTION_EASE + 5.0) == 1.0


def test_section_to_section_transition_is_continuous():
    eng = CueEngine()
    eng.on_song_section(SongSectionByte.VERSE, now=100.0)
    changed_at = 100.0 + SECTION_EASE
    verse_state = eng._section_state(changed_at)

    eng.on_song_section(SongSectionByte.CHORUS, now=changed_at)
    assert eng._section_state(changed_at) == verse_state

    hue, gain, energy = eng._section_state(changed_at + SECTION_EASE / 2)
    assert hue == tuple(
        int(round((a + b) / 2)) for a, b in zip(_VERSE_HUE, _CHORUS_HUE)
    )
    assert gain == 1.0
    assert abs(energy - (_VERSE_ENERGY + _CHORUS_ENERGY) / 2) < 1e-9
    assert eng._section_state(changed_at + SECTION_EASE) == (
        _CHORUS_HUE, 1.0, _CHORUS_ENERGY)


def test_section_to_none_eases_back_to_identity():
    eng = CueEngine()
    eng.on_song_section(SongSectionByte.CHORUS, now=100.0)
    changed_at = 100.0 + SECTION_EASE
    eng.on_song_section(SongSectionByte.NONE, now=changed_at)

    assert eng._section_state(changed_at) == (
        _CHORUS_HUE, 1.0, _CHORUS_ENERGY)
    hue, gain, energy = eng._section_state(changed_at + SECTION_EASE / 2)
    assert hue == _CHORUS_HUE
    assert gain == 0.5
    assert abs(energy - (1.0 + _CHORUS_ENERGY) / 2) < 1e-9
    assert eng._section_state(changed_at + SECTION_EASE) == (
        _CHORUS_HUE, 0.0, 1.0)


def test_pause_shifts_section_timer():
    eng = CueEngine()
    eng.on_song_section(SongSectionByte.CHORUS)
    before = eng._section_changed_at
    eng.on_paused(True)
    eng.on_paused(False)
    # Deadline shifts forward by the (tiny) pause duration, never backward.
    assert eng._section_changed_at >= before


# ── Mapper: hue lean ─────────────────────────────────────────────

def test_chorus_hue_lean_shifts_lit_pixels_warm():
    base = _render(None)
    biased = _render((_CHORUS_HUE, 1.0, _CHORUS_ENERGY))
    # Red wash leaning toward the warm amber chorus hue: green channel rises
    # from 0, red stays lit. A measurable shift in the intended direction.
    assert base[1] == 0
    assert biased[1] > 0
    assert biased[0] > 0


def test_verse_hue_lean_shifts_lit_pixels_cool():
    base = _render(None)
    biased = _render((_VERSE_HUE, 1.0, _VERSE_ENERGY))
    # Verse leans cool: blue channel rises from 0.
    assert base[2] == 0
    assert biased[2] > 0


def test_lean_is_convex_and_bounded():
    # Blending by t = STRENGTH * gain never overshoots the target hue.
    t = SECTION_BIAS_STRENGTH
    biased = _render((_CHORUS_HUE, 1.0, _CHORUS_ENERGY))
    lo = int(100 * (1.0 - t) + _CHORUS_HUE[0] * t)
    hi = int(100 * (1.0 - t) + _CHORUS_HUE[0] * t) + 1
    assert lo <= biased[0] <= hi


def test_zero_gain_is_noop():
    base = _render(None)
    biased = _render((_CHORUS_HUE, 0.0, _CHORUS_ENERGY))
    assert base == biased


def test_bias_only_touches_lit_pixels():
    # Strip dark → the section bias must not light anything.
    px = _render((_CHORUS_HUE, 1.0, _CHORUS_ENERGY), zones=(0, 0, 0, 0))
    assert max(px) == 0


# ── Mapper: energy scaling of the beat pump ─────────────────────

def test_chorus_energy_deepens_beat_pump():
    pump = {"beat_pulse": 0.5, "beat_phase": 0.0}   # peak of the pump
    base = _render(None, **pump)
    chorus = _render((_CHORUS_HUE, 1.0, _CHORUS_ENERGY), **pump)
    # Chorus scales the pump depth up → brighter lit pixels at the beat.
    assert chorus[0] > base[0]


def test_verse_energy_softens_beat_pump():
    pump = {"beat_pulse": 0.5, "beat_phase": 0.0}
    base = _render(None, **pump)
    verse = _render((_VERSE_HUE, 1.0, _VERSE_ENERGY), **pump)
    # Verse scales the pump depth down → dimmer red at the beat. (The hue
    # lean lifts blue a little; the red channel shows the energy direction.)
    assert verse[0] < base[0]


def test_partial_gain_scales_energy_between():
    pump = {"beat_pulse": 0.5, "beat_phase": 0.0}
    base = _render(None, **pump)
    half = _render((_CHORUS_HUE, 0.5, _CHORUS_ENERGY), **pump)
    full = _render((_CHORUS_HUE, 1.0, _CHORUS_ENERGY), **pump)
    assert base[0] < half[0] < full[0]


# ── Identity / bit-exactness ─────────────────────────────────────

def test_no_section_key_is_identity():
    m = LEDMapper(MAPPED_REGION)
    a = m.render([0xFF, 0, 0, 0], zone_colors=_COLORS, effects={},
                 brightness=1.0)
    b = _render(None)
    assert a == b


def test_section_eases_toward_full_bias():
    # Mid-ease, the hue lean sits partway between the authored look and the
    # full section bias — the change drifts rather than snaps.
    base = _render(None)
    half = _render((_CHORUS_HUE, 0.5, _CHORUS_ENERGY))
    full = _render((_CHORUS_HUE, 1.0, _CHORUS_ENERGY))
    assert base[1] < half[1] < full[1]      # green channel climbs with gain


# ── Operator intensity knob ──────────────────────────────────────

def test_section_intensity_scales_emitted_gain():
    eng = CueEngine()
    eng.on_song_section(SongSectionByte.CHORUS)
    eng._section_changed_at = 1.0                 # far past → fully eased in
    full_hue, full_gain, full_energy = eng.get_effects()["section"]
    assert full_gain > 0.0
    eng.set_section_intensity(0.5)
    _, half_gain, _ = eng.get_effects()["section"]
    assert abs(half_gain - full_gain * 0.5) < 1e-9
    # Only the gain scales — the hue/energy the mapper reads are unchanged.
    hue, _, energy = eng.get_effects()["section"]
    assert hue == full_hue and energy == full_energy


def test_section_intensity_zero_emits_zero_gain():
    # Gain 0 is the mapper's identity signal, so intensity 0 = bit-exact off.
    eng = CueEngine()
    eng.on_song_section(SongSectionByte.CHORUS)
    eng._section_changed_at = 1.0
    eng.set_section_intensity(0.0)
    _, gain, _ = eng.get_effects()["section"]
    assert gain == 0.0


def test_section_intensity_is_clamped():
    eng = CueEngine()
    eng.set_section_intensity(5.0)
    assert eng._section_intensity == 1.0
    eng.set_section_intensity(-1.0)
    assert eng._section_intensity == 0.0


def test_section_intensity_setting_defaults_clamps_persists(tmp_path):
    path = str(tmp_path / "settings.json")
    s = BridgeSettings(path=path)
    assert s.section_intensity == 1.0
    s.section_intensity = 2.0
    assert s.section_intensity == 1.0
    s.section_intensity = -0.5
    assert s.section_intensity == 0.0
    s.section_intensity = 0.6
    assert BridgeSettings(path=path).section_intensity == 0.6
    assert s.snapshot()["section_intensity"] == 0.6


# ── Dashboard surfacing ──────────────────────────────────────────

def test_tracker_surfaces_song_section():
    tracker = StatusTracker()
    assert tracker.snapshot()["song_section"] == "None"
    assert tracker.snapshot()["song_section_id"] == 0
    tracker.on_song_section(SongSectionByte.CHORUS)
    snap = tracker.snapshot()
    assert snap["song_section"] == "Chorus"
    assert snap["song_section_id"] == SongSectionByte.CHORUS
    tracker.on_song_section(SongSectionByte.VERSE)
    assert tracker.snapshot()["song_section"] == "Verse"
