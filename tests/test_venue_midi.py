"""Decoding a MIDI VENUE track the way YARG does, quirks included."""

from tests.venue_fixtures import note_off, note_on, text_event, venue_midi
from venue_scan.midi import find_track, track_names
from venue_scan.venue_midi import (
    VENUE_TRACK_NAME, build_venue_track, normalize_text_event, parse_venue_track,
)


def decode(events):
    """Build a VENUE track from (delta, payload) pairs and normalize it."""
    track = find_track(venue_midi(events), VENUE_TRACK_NAME)
    parsed = parse_venue_track(track)
    return parsed, build_venue_track(parsed.events)


def test_track_names_are_listed_without_decoding_events():
    data = venue_midi([(0, text_event("[verse]"))])
    assert track_names(data) == ["VENUE"]


def test_venue_track_must_match_name_exactly():
    from tests.venue_fixtures import midi_file, track_chunk

    data = midi_file([track_chunk("VENUE TRACK", [(0, text_event("[verse]"))])])
    assert find_track(data, VENUE_TRACK_NAME) is None


def test_note_cues_decode_with_length():
    _parsed, venue = decode([
        (0, note_on(48)), (480, note_off(48)),      # lighting keyframe next
        (0, note_on(103)), (480, note_off(103)),    # bloom
        (0, note_on(39)), (480, note_off(39)),      # guitar spotlight
    ])
    assert venue.lighting == [(0, "next")]
    assert venue.post_processing == [(480, "bloom")]
    assert [kind for _t, kind, _p in venue.performer] == ["spotlight"]


def test_notes_outside_the_lookup_are_dropped():
    _parsed, venue = decode([(0, note_on(1)), (480, note_off(1))])
    assert venue.is_empty


def test_unmatched_note_off_truncates_the_rest_of_the_track():
    """YARG returns early here, losing every later cue. Real songs hit this."""
    parsed, venue = decode([
        (0, note_on(48)), (480, note_off(48)),
        (10, note_off(72)),                          # no matching note-on
        (10, note_on(103)), (480, note_off(103)),    # never reached
    ])
    assert parsed.truncated is True
    assert venue.lighting == [(0, "next")]
    assert venue.post_processing == []


def test_text_events_are_unbracketed_and_trimmed():
    assert normalize_text_event("[ verse ]") == "verse"
    assert normalize_text_event("  bonusfx  ") == "bonusfx"


def test_exact_text_lookup_wins():
    _parsed, venue = decode([(0, text_event("[ProFilm_b.pp]"))])
    assert venue.post_processing == [(0, "desaturated_red")]


def test_lighting_regex_tier():
    _parsed, venue = decode([
        (0, text_event("[lighting (manual_warm)]")),
        (10, text_event("[lighting (blackout_spot)]")),
    ])
    assert venue.lighting == [(0, "warm_manual"), (10, "blackout_spotlight")]


def test_empty_lighting_parens_fall_back_to_default():
    _parsed, venue = decode([(0, text_event("[lighting ()]"))])
    assert venue.lighting == [(0, "default")]


def test_coop_camera_cut_tier():
    _parsed, venue = decode([
        (0, text_event("[coop_d_near]")),
        (480, text_event("[coop_all_far]")),
    ])
    # The last group is never flushed, so only the first cut survives.
    assert venue.camera_cuts == [(0, "Drums", frozenset(), ())]


def test_unknown_directed_cut_falls_back_to_default_subject():
    _parsed, venue = decode([
        (0, text_event("[directed_nonsense]")),
        (480, text_event("[coop_all_far]")),
    ])
    assert venue.camera_cuts[0][1] == "Stage"  # default -> Stage


def test_unmatched_text_is_recorded_but_not_counted():
    parsed, venue = decode([(0, text_event("[some_charter_note]"))])
    assert parsed.unknown_text == ["some_charter_note"]
    assert venue.is_empty


def test_optional_flag_prefix_is_stripped_off_stage_effects():
    _parsed, venue = decode([(0, text_event("[bonusfx_optional]"))])
    assert venue.stage == [(0, "bonus_fx", frozenset({"optional"}))]


def test_fog_events_are_stage_effects():
    _parsed, venue = decode([
        (0, text_event("[FogOn]")),
        (480, text_event("[FogOff]")),
    ])
    assert [effect for _t, effect, _f in venue.stage] == ["fog_on", "fog_off"]
    assert venue.has_fog is True


def test_performers_at_the_same_tick_merge_into_one_event():
    _parsed, venue = decode([
        (0, note_on(39)), (0, note_on(38)),          # guitar + drums spotlight
        (480, note_off(39)), (0, note_off(38)),
    ])
    assert len(venue.performer) == 1
    _tick, kind, performers = venue.performer[0]
    assert kind == "spotlight"
    assert performers == frozenset({"guitar", "drums"})


def test_spotlight_and_singalong_are_tracked_separately():
    _parsed, venue = decode([
        (0, note_on(39)), (480, note_off(39)),       # guitar spotlight
        (0, note_on(87)), (480, note_off(87)),       # guitar singalong
    ])
    assert sorted(kind for _t, kind, _p in venue.performer) == ["singalong", "spotlight"]


def test_camera_constraints_merge_into_the_cut_at_the_same_tick():
    _parsed, venue = decode([
        (0, note_on(60)), (0, note_on(70)),          # random cut + no_behind
        (480, note_off(60)), (0, note_off(70)),
        (480, text_event("[coop_all_far]")),         # forces the first group to flush
    ])
    assert len(venue.camera_cuts) == 1
    _tick, subject, constraints, _choices = venue.camera_cuts[0]
    assert subject == "Random"
    assert constraints == frozenset({"no_behind"})


def test_multiple_subjects_at_one_tick_become_a_random_choice():
    _parsed, venue = decode([
        (0, text_event("[coop_d_near]")),
        (0, text_event("[coop_g_near]")),
        (480, text_event("[coop_all_far]")),
    ])
    _tick, subject, _constraints, choices = venue.camera_cuts[0]
    assert subject == "Random"
    assert set(choices) == {"Drums", "Guitar"}
