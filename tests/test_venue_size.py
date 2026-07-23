"""Tests for venue-size density branching (VISION signal inventory).

YARG sends the venue-size byte at offset 8 (VenueType: 0=None, 1=Small,
2=Large), set on chart load. YALCY branches per cue on it — Large venues get
denser pattern variants, anything else sparser ones. The bridge does the same
generically: a mask transform applied to chase-pattern steps at cue launch
(small thins opposing pairs to single heads, large fills single heads out to
opposing pairs) plus a sparkle-density scale. NoVenue/unknown is identity, so
the authored look is bit-exact when the chart doesn't say.

These tests pin: the two mask transforms (incl. idempotence), the measurable
density change per venue (lit-cell counts across a cue's pattern steps), the
sparkle rescale, the static-wash exemption, the next-cue application timing,
and the default/unknown bit-exactness.

Run: python -m pytest tests/test_venue_size.py -v
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import (  # noqa: E402
    CueEngine, _thin_opposites, _fill_opposites, _venue_safe,
    SPARKLE_SCALE_SMALL, SPARKLE_SCALE_LARGE,
    ZERO, ONE, TWO, THREE, FOUR, ALL,
    RED, YELLOW,
)
from protocol.yarg_packet import CueByte, VenueSizeByte  # noqa: E402
from status_server import StatusTracker  # noqa: E402
from settings import BridgeSettings  # noqa: E402
import main as bridge_main  # noqa: E402

# Authored CHORUS red chase: opposing pairs (see cue_engine._launch_cue).
_CHORUS_RED = [ZERO | FOUR, ONE | 0x20, 0x04 | 0x40, 0x08 | 0x80]


def _pattern_masks(eng: CueEngine, pattern_idx: int = 0) -> list[int]:
    """Masks of a launched time pattern's steps (zone is steps[i][0][0])."""
    return [step[0][1] for step in eng._time_patterns[pattern_idx].steps]


def _lit_cells(masks: list[int]) -> int:
    """Total lit StageKit cells across a pattern's steps — the density metric."""
    return sum(m.bit_count() for m in masks)


# ── Mask transforms ──────────────────────────────────────────────

def test_thin_collapses_opposite_pairs():
    assert _thin_opposites(ZERO | FOUR) == ZERO
    assert _thin_opposites(ONE | 0x20) == ONE      # ONE|FIVE → ONE
    assert _thin_opposites(ALL) == 0x0F            # one bit per pair survives


def test_thin_keeps_single_heads_and_is_idempotent():
    assert _thin_opposites(TWO) == TWO
    assert _thin_opposites(0) == 0
    for m in range(256):
        assert _thin_opposites(_thin_opposites(m)) == _thin_opposites(m)


def test_fill_pairs_single_heads():
    assert _fill_opposites(ZERO) == ZERO | FOUR
    assert _fill_opposites(TWO) == 0x04 | 0x40     # TWO|SIX


def test_fill_keeps_pairs_and_is_idempotent():
    assert _fill_opposites(ZERO | FOUR) == ZERO | FOUR
    assert _fill_opposites(ALL) == ALL
    assert _fill_opposites(0) == 0
    for m in range(256):
        assert _fill_opposites(_fill_opposites(m)) == _fill_opposites(m)


# ── Default / unknown preserves the authored look ────────────────

def test_default_venue_is_bit_exact():
    eng = CueEngine()                              # no on_venue_size call
    eng.on_cue(CueByte.CHORUS)
    assert _pattern_masks(eng) == _CHORUS_RED
    assert eng.get_effects()["sparkle"] == 0.10    # authored CHORUS density


def test_no_venue_byte_is_bit_exact():
    eng = CueEngine()
    eng.on_venue_size(VenueSizeByte.NO_VENUE)
    eng.on_cue(CueByte.CHORUS)
    assert _pattern_masks(eng) == _CHORUS_RED
    assert eng.get_effects()["sparkle"] == 0.10


def test_unknown_venue_byte_is_bit_exact():
    eng = CueEngine()
    eng.on_venue_size(7)                           # not a VenueSizeByte value
    eng.on_cue(CueByte.CHORUS)
    assert _pattern_masks(eng) == _CHORUS_RED
    assert eng.get_effects()["sparkle"] == 0.10


# ── Small venue: sparser ─────────────────────────────────────────

def test_small_venue_thins_chase_density():
    eng = CueEngine()
    eng.on_venue_size(VenueSizeByte.SMALL)
    eng.on_cue(CueByte.CHORUS)
    masks = _pattern_masks(eng)
    assert masks == [ZERO, ONE, 0x04, 0x08]        # pairs → single heads
    assert _lit_cells(masks) < _lit_cells(_CHORUS_RED)


def test_small_venue_halves_sparkle():
    eng = CueEngine()
    eng.on_venue_size(VenueSizeByte.SMALL)
    eng.on_cue(CueByte.CHORUS)
    assert eng.get_effects()["sparkle"] == 0.10 * SPARKLE_SCALE_SMALL


def test_small_venue_leaves_static_washes_alone():
    # Density branching shapes chases + sparkle, not a deliberate full wash.
    eng = CueEngine()
    eng.on_venue_size(VenueSizeByte.SMALL)
    eng.on_cue(CueByte.CHORUS)
    assert eng.zones[YELLOW] == ALL


# ── Large venue: denser ──────────────────────────────────────────

def test_large_venue_fills_chase_density():
    # SEARCHLIGHTS authors single-head chases; a large venue pairs them up.
    eng_default = CueEngine()
    eng_default.on_cue(CueByte.SEARCHLIGHTS)
    eng = CueEngine()
    eng.on_venue_size(VenueSizeByte.LARGE)
    eng.on_cue(CueByte.SEARCHLIGHTS)
    for i in range(2):                             # yellow + blue chases
        sparse = _pattern_masks(eng_default, i)
        dense = _pattern_masks(eng, i)
        assert all(m.bit_count() == 2 for m in dense)
        assert _lit_cells(dense) == 2 * _lit_cells(sparse)


def test_large_venue_keeps_authored_pairs():
    # The CHORUS chase is already opposing pairs — fill is a no-op there.
    eng = CueEngine()
    eng.on_venue_size(VenueSizeByte.LARGE)
    eng.on_cue(CueByte.CHORUS)
    assert _pattern_masks(eng) == _CHORUS_RED


def test_large_venue_boosts_sparkle():
    eng = CueEngine()
    eng.on_venue_size(VenueSizeByte.LARGE)
    eng.on_cue(CueByte.CHORUS)
    assert eng.get_effects()["sparkle"] == 0.10 * SPARKLE_SCALE_LARGE


def test_large_venue_fills_timed_patterns():
    # MENU's scanner is a timed (non-BPM) single-head pattern — same fill.
    eng = CueEngine()
    eng.on_venue_size(VenueSizeByte.LARGE)
    eng.on_cue(CueByte.MENU)
    assert all(m.bit_count() == 2 for m in _pattern_masks(eng))


# ── Application timing + tracker ─────────────────────────────────

def test_venue_change_applies_at_next_cue():
    # YALCY reads the byte at cue Enable(); a mid-cue change does not
    # retroactively rewrite the running pattern.
    eng = CueEngine()
    eng.on_cue(CueByte.CHORUS)
    eng.on_venue_size(VenueSizeByte.SMALL)
    assert _pattern_masks(eng) == _CHORUS_RED      # still the launched steps
    eng.on_cue(CueByte.VERSE)
    eng.on_cue(CueByte.CHORUS)                     # re-launch picks it up
    assert _pattern_masks(eng) == [ZERO, ONE, 0x04, 0x08]


def test_sparkle_scale_tracks_venue_live():
    # The scale is read per frame, so it follows the byte without a re-launch.
    eng = CueEngine()
    eng.on_cue(CueByte.CHORUS)
    eng.on_venue_size(VenueSizeByte.SMALL)
    assert eng.get_effects()["sparkle"] == 0.10 * SPARKLE_SCALE_SMALL
    eng.on_venue_size(VenueSizeByte.NO_VENUE)
    assert eng.get_effects()["sparkle"] == 0.10


def test_tracker_surfaces_venue_size():
    tracker = StatusTracker()
    tracker.on_venue_size(VenueSizeByte.LARGE)
    snap = tracker.snapshot()
    assert snap["venue_size"] == "Large"
    assert snap["venue_size_id"] == VenueSizeByte.LARGE


# ── Transform safety guard ───────────────────────────────────────

def test_venue_safe_predicate():
    # Opposite pairs (low nibble == high nibble) and single-nibble heads are
    # well-defined; a nibble-spanning pair is not.
    assert _venue_safe(ZERO | FOUR)          # opposite pair
    assert _venue_safe(ALL)                    # every opposite pair
    assert _venue_safe(ZERO)                   # single low head
    assert _venue_safe(FOUR)                   # single high head
    assert _venue_safe(ZERO | ONE)             # two heads, one nibble
    assert _venue_safe(0)                       # empty
    assert not _venue_safe(THREE | FOUR)       # 0x18 spans the nibble boundary
    assert not _venue_safe(ZERO | ONE | FOUR)  # mixed low pair + lone high


def test_unsafe_masks_pass_through_untransformed():
    # The guard applies the transform only to safe masks; an unsafe step is
    # left exactly as authored rather than silently mis-thinned.
    eng = CueEngine()
    eng.on_venue_size(VenueSizeByte.SMALL)     # _thin_opposites active
    spanning = THREE | FOUR                     # unsafe
    out = eng._venue_transform_pattern([spanning, ZERO | FOUR])
    assert out[0] == spanning                   # untouched
    assert out[1] == ZERO                        # safe pair still thinned


def test_every_transformed_cue_mask_is_venue_safe():
    """Invariant: every mask the engine routes through the venue transform is
    opposite-pair/single-head safe. A future cue that feeds a nibble-spanning
    step trips this loudly instead of shipping a subtly wrong stage look.

    Runs inside a throwaway event loop so event-driven cues (which schedule
    asyncio tasks at launch) can be exercised alongside the time-driven ones.
    """
    seen: list[int] = []
    orig = CueEngine._venue_transform_pattern

    def spy(self, pattern):
        seen.extend(pattern)
        return orig(self, pattern)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        CueEngine._venue_transform_pattern = spy
        for cue in range(CueByte.NO_CUE + 1):
            eng = CueEngine()
            eng.on_venue_size(VenueSizeByte.SMALL)
            eng.on_cue(cue)
    finally:
        CueEngine._venue_transform_pattern = orig
        # Event-driven cues scheduled tasks we never ran; cancel and drain them
        # so their coroutines close cleanly (no "never awaited" warnings).
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)

    assert seen, "expected some cues to route patterns through the transform"
    unsafe = [hex(m) for m in seen if not _venue_safe(m)]
    assert not unsafe, f"unsafe masks routed to venue transform: {unsafe}"


# ── Operator intensity knob ──────────────────────────────────────

def test_venue_sparkle_intensity_zero_leaves_chase_toggle_independent():
    # The continuous slider controls sparkle only. Chase density is discrete
    # and remains enabled until its dedicated toggle is switched off.
    eng = CueEngine()
    eng.on_venue_size(VenueSizeByte.SMALL)
    eng.set_venue_intensity(0.0)
    eng.on_cue(CueByte.CHORUS)
    assert _pattern_masks(eng) == [ZERO, ONE, 0x04, 0x08]
    assert eng.get_effects()["sparkle"] == 0.10


def test_venue_pattern_toggle_off_is_bit_exact():
    eng = CueEngine()
    eng.on_venue_size(VenueSizeByte.SMALL)
    eng.set_venue_patterns_enabled(False)
    eng.on_cue(CueByte.CHORUS)
    assert _pattern_masks(eng) == _CHORUS_RED


def test_venue_intensity_scales_sparkle_deviation():
    eng = CueEngine()
    eng.on_venue_size(VenueSizeByte.SMALL)
    eng.set_venue_intensity(0.5)
    eng.on_cue(CueByte.CHORUS)
    # Halfway between authored (1.0) and the full small scale.
    expected = 1.0 + (SPARKLE_SCALE_SMALL - 1.0) * 0.5
    assert eng.get_effects()["sparkle"] == 0.10 * expected


def test_venue_intensity_full_matches_default_branch():
    eng = CueEngine()
    eng.on_venue_size(VenueSizeByte.LARGE)
    eng.set_venue_intensity(1.0)               # the engine default too
    eng.on_cue(CueByte.CHORUS)
    assert eng.get_effects()["sparkle"] == 0.10 * SPARKLE_SCALE_LARGE


def test_venue_intensity_is_clamped():
    eng = CueEngine()
    eng.set_venue_intensity(5.0)
    assert eng._venue_intensity == 1.0
    eng.set_venue_intensity(-1.0)
    assert eng._venue_intensity == 0.0


# ── Settings persistence (dashboard slider) ──────────────────────

def test_venue_intensity_setting_defaults_clamps_persists(tmp_path):
    path = str(tmp_path / "settings.json")
    s = BridgeSettings(path=path)
    assert s.venue_intensity == 1.0            # default = full sparkle branch
    s.venue_intensity = 2.0
    assert s.venue_intensity == 1.0            # clamped high
    s.venue_intensity = -0.5
    assert s.venue_intensity == 0.0            # clamped low
    s.venue_intensity = 0.4
    assert BridgeSettings(path=path).venue_intensity == 0.4   # survives reload
    assert s.snapshot()["venue_intensity"] == 0.4


def test_venue_pattern_toggle_defaults_and_persists(tmp_path):
    path = str(tmp_path / "settings.json")
    s = BridgeSettings(path=path)
    assert s.effect_enabled("venue_patterns")
    assert s.set_effect("venue_patterns", False)
    assert not BridgeSettings(path=path).effect_enabled("venue_patterns")


# ── Protocol ordering ─────────────────────────────────────────────

def test_protocol_applies_venue_before_cue_from_same_packet(monkeypatch):
    calls = []

    class Engine:
        bpm = 120.0

        def on_venue_size(self, value):
            calls.append(("venue", value))

        def on_cue(self, value):
            calls.append(("cue", value))

        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    class Sink:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    pkt = SimpleNamespace(
        datagram_version=1, bpm=120.0,
        venue_size=VenueSizeByte.SMALL, lighting_cue=CueByte.CHORUS,
        strobe_state=0, beat=0, keyframe=0,
        guitar_notes=0, bass_notes=0, drum_notes=0, keys_notes=0,
        vocal_note=0.0, harmony0_note=0.0, harmony1_note=0.0,
        harmony2_note=0.0, spotlight=0, singalong=0, post_processing=0,
        fog_state=False, song_section=0, bonus_effect=False, paused=1,
        sp_active=False, sp_amount=0.0, sp_charge=0.0, sp_active_count=0,
        camera_cut_subject=0, camera_cut_priority=0, scene=0, auto_gen=False,
    )
    monkeypatch.setattr(bridge_main, "parse_packet", lambda _data: pkt)
    protocol = bridge_main.YARGProtocol(Engine(), Sink(), Sink())
    protocol.datagram_received(b"packet", ("127.0.0.1", 36107))

    assert calls[:2] == [
        ("venue", VenueSizeByte.SMALL),
        ("cue", CueByte.CHORUS),
    ]
