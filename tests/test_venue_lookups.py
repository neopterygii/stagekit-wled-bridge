"""Invariants on the tables transcribed from YARG.Core.

These guard the transcription itself. If YARG.Core changes and the tables are
re-copied, these are what catch a dropped or mistyped row.
"""

from venue_scan import lookups
from venue_scan.lookups import VenueType


def test_note_lookup_covers_the_documented_ranges():
    notes = lookups.VENUE_NOTE_LOOKUP
    assert set(range(96, 111)) <= set(notes)          # post-processing
    assert set(range(37, 42)) <= set(notes)           # spotlights
    assert set(range(48, 51)) <= set(notes)           # lighting keyframes
    assert set(range(60, 65)) <= set(notes)           # camera cuts
    assert set(range(70, 74)) <= set(notes)           # camera cut constraints
    assert {85, 86, 87} <= set(notes)                 # singalongs
    # Nothing outside those ranges is defined.
    assert set(notes) == (
        set(range(96, 111)) | set(range(37, 42)) | set(range(48, 51))
        | set(range(60, 65)) | set(range(70, 74)) | {85, 86, 87}
    )


def test_camera_constraint_notes_are_not_reversed():
    """72 is only_close and 73 is no_close — the reverse of some community docs."""
    assert lookups.VENUE_NOTE_LOOKUP[72] == (VenueType.CAMERA_CUT_CONSTRAINT, "only_close")
    assert lookups.VENUE_NOTE_LOOKUP[73] == (VenueType.CAMERA_CUT_CONSTRAINT, "no_close")


def test_no_singalong_note_for_vocals_or_keys():
    """Only guitar/drums/bass have singalong notes; the others cannot be authored."""
    singalongs = {
        text for kind, text in lookups.VENUE_NOTE_LOOKUP.values()
        if kind is VenueType.SINGALONG
    }
    assert singalongs == {"guitar", "drums", "bass"}


def test_every_note_lookup_value_resolves_to_a_real_type():
    """A typo'd value would silently produce a cue YARG then drops."""
    for note, (kind, text) in lookups.VENUE_NOTE_LOOKUP.items():
        if kind is VenueType.LIGHTING:
            assert text in lookups.LIGHTING_TYPES, note
        elif kind is VenueType.POST_PROCESSING:
            assert text in lookups.POST_PROCESSING_TYPES, note
        elif kind in (VenueType.SPOTLIGHT, VenueType.SINGALONG):
            assert text in lookups.PERFORMERS, note
        elif kind is VenueType.CAMERA_CUT:
            assert text in lookups.CAMERA_CUT_SUBJECTS, note
        elif kind is VenueType.CAMERA_CUT_CONSTRAINT:
            assert text in lookups.CAMERA_CUT_CONSTRAINTS, note


def test_every_text_lookup_value_resolves_to_a_real_type():
    for source, (kind, text) in lookups.VENUE_TEXT_CONVERSION_LOOKUP.items():
        # `bonusfx_optional` carries the flag prefix in its value.
        text = text.split(" ")[-1]
        if kind is VenueType.LIGHTING:
            assert text in lookups.LIGHTING_TYPES, source
        elif kind is VenueType.POST_PROCESSING:
            assert text in lookups.POST_PROCESSING_TYPES, source
        elif kind is VenueType.STAGE_EFFECT:
            assert text in lookups.STAGE_EFFECT_TYPES, source


def test_lighting_conversion_values_are_all_real_lighting_types():
    for source, text in lookups.VENUE_LIGHTING_CONVERSION_LOOKUP.items():
        assert text in lookups.LIGHTING_TYPES, source


def test_camera_lookups_all_have_subjects():
    for table in (lookups.VENUE_DIRECTED_CUT_LOOKUP, lookups.VENUE_CAMERA_CUT_LOOKUP):
        for source, text in table.items():
            assert text in lookups.CAMERA_CUT_SUBJECTS, source


def test_lighting_lookup_omits_yarg_internal_cues():
    """StrobeFastest/Menu/Score/NoCue exist in YARG but no chart can produce them."""
    for internal in ("strobe_fastest", "strobe_medium", "strobe_off", "menu", "score",
                     "no_cue"):
        assert internal not in lookups.LIGHTING_TYPES


def test_empty_lighting_parens_are_not_in_the_table():
    """`lighting ()` must fall through to the default, not match an entry."""
    assert "" not in lookups.VENUE_LIGHTING_CONVERSION_LOOKUP


def test_crowd_and_drums_pnt_collapse_to_one_cut():
    """Upstream maps both names onto the same value; keep that quirk."""
    table = lookups.VENUE_DIRECTED_CUT_LOOKUP
    assert table["directed_crowd_pnt"] == table["directed_drums_pnt"]
