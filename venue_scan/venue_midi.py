"""Decode a MIDI `VENUE` track into YARG's normalized VenueTrack model.

Mirrors `MidReader.ReadVenueEvents` (note/text -> MoonVenue) followed by
`MoonSongLoader.LoadVenueTrack` (MoonVenue -> the five VenueTrack lists).

Two YARG behaviours are replicated deliberately, because classifying against a
"cleaner" parser would give answers the game disagrees with:

* a note-off with no matching note-on **aborts the rest of the track**;
* the final pending camera-cut group is never flushed, so the last cut in a
  song is dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import lookups
from .lookups import VenueType
from .midi import MidiTrack
from .venue_track import VenueTrack

VENUE_TRACK_NAME = "VENUE"

LIGHTING_RE = re.compile(r"lighting\s+\((.*)\)", re.DOTALL)
DIRECTED_CUT_RE = re.compile(r"(directed_\w+)", re.DOTALL)
CAMERA_CUT_RE = re.compile(r"coop_(\w+_\w+)", re.DOTALL)

#: Regex tiers, in MidIOHelper.VENUE_EVENT_REGEX_TO_LOOKUP order.
REGEX_TIERS = (
    (LIGHTING_RE, lookups.VENUE_LIGHTING_CONVERSION_LOOKUP,
     VenueType.LIGHTING, lookups.LIGHTING_DEFAULT),
    (DIRECTED_CUT_RE, lookups.VENUE_DIRECTED_CUT_LOOKUP,
     VenueType.CAMERA_CUT, lookups.CAMERA_DEFAULT),
    (CAMERA_CUT_RE, lookups.VENUE_CAMERA_CUT_LOOKUP,
     VenueType.CAMERA_CUT, lookups.CAMERA_DEFAULT),
)


@dataclass
class RawVenueEvent:
    """A MoonVenue: a typed, normalized venue event before track assembly."""

    tick: int
    type: VenueType
    text: str
    length: int = 0


@dataclass
class VenueParseResult:
    events: list[RawVenueEvent] = field(default_factory=list)
    #: Text events that matched nothing. YARG drops these; we keep a sample
    #: because they are a good signal of non-standard authoring.
    unknown_text: list[str] = field(default_factory=list)
    #: True if an unmatched note-off truncated the track, as it would in YARG.
    truncated: bool = False


def normalize_text_event(text: str) -> str:
    """TextEvents.NormalizeTextEvent: strip one [...] wrapper, then trim."""
    start = text.find("[")
    end = text.find("]")
    if start >= 0 and end >= 0 and start <= end:
        return text[start + 1:end].strip()
    return text.strip()


def parse_venue_track(track: MidiTrack) -> VenueParseResult:
    """Decode note and text events into normalized MoonVenue equivalents."""
    result = VenueParseResult()
    unpaired: list[tuple[int, int]] = []  # (note, start tick)

    for event in track.events:
        if event.kind == "note_on":
            if not any(note == event.note for note, _ in unpaired):
                unpaired.append((event.note, event.tick))
            # A duplicate note-on is logged and ignored by YARG.
        elif event.kind == "note_off":
            match = next(
                (i for i, (note, _) in enumerate(unpaired) if note == event.note),
                None,
            )
            if match is None:
                # YARG bails out of the whole track here.
                result.truncated = True
                break
            _note, start_tick = unpaired.pop(match)
            entry = lookups.VENUE_NOTE_LOOKUP.get(event.note)
            if entry is None:
                continue
            venue_type, text = entry
            result.events.append(
                RawVenueEvent(tick=start_tick, type=venue_type, text=text,
                              length=event.tick - start_tick)
            )
        elif event.kind == "text":
            converted = _convert_text(normalize_text_event(event.text))
            if converted is None:
                result.unknown_text.append(normalize_text_event(event.text))
                continue
            venue_type, text = converted
            result.events.append(
                RawVenueEvent(tick=event.tick, type=venue_type, text=text)
            )

    result.events.sort(key=lambda e: e.tick)
    return result


def _convert_text(text: str) -> tuple[VenueType, str] | None:
    exact = lookups.VENUE_TEXT_CONVERSION_LOOKUP.get(text)
    if exact is not None:
        return exact
    for regex, lookup, venue_type, default in REGEX_TIERS:
        match = regex.search(text)
        if not match:
            continue
        converted = lookup.get(match.group(1))
        if converted is None:
            if not default:
                continue
            converted = default
        return venue_type, converted
    return None


def build_venue_track(events: list[RawVenueEvent]) -> VenueTrack:
    """MoonSongLoader.LoadVenueTrack: normalized events -> the five lists."""
    track = VenueTrack()

    spotlight_tick: int | None = None
    spotlight_performers: set[str] = set()
    singalong_tick: int | None = None
    singalong_performers: set[str] = set()

    cut_tick: int | None = None
    cut_constraints: set[str] = set()
    cut_subjects: list[str] = []

    for event in events:
        flags, text = _split_flags(event.text)

        if event.type is VenueType.LIGHTING:
            if text in lookups.LIGHTING_TYPES:
                track.lighting.append((event.tick, text))
        elif event.type is VenueType.POST_PROCESSING:
            if text in lookups.POST_PROCESSING_TYPES:
                track.post_processing.append((event.tick, text))
        elif event.type is VenueType.STAGE_EFFECT:
            if text in lookups.STAGE_EFFECT_TYPES:
                track.stage.append((event.tick, text, flags))
        elif event.type is VenueType.SPOTLIGHT:
            spotlight_tick, spotlight_performers = _handle_performer(
                track, "spotlight", event, spotlight_tick, spotlight_performers
            )
        elif event.type is VenueType.SINGALONG:
            singalong_tick, singalong_performers = _handle_performer(
                track, "singalong", event, singalong_tick, singalong_performers
            )
        elif event.type in (VenueType.CAMERA_CUT, VenueType.CAMERA_CUT_CONSTRAINT):
            cut_tick, cut_constraints, cut_subjects = _handle_camera_cut(
                track, event, cut_tick, cut_constraints, cut_subjects
            )
        # VenueType.UNKNOWN is dropped, matching YARG's `default:` case.

    # Performer events are flushed at the end; camera cuts deliberately are not
    # (YARG has no equivalent finalizer, so the last cut group is lost).
    _finalize_performer(track, "spotlight", spotlight_tick, spotlight_performers)
    _finalize_performer(track, "singalong", singalong_tick, singalong_performers)
    return track


def _split_flags(text: str) -> tuple[frozenset[str], str]:
    """Strip a leading flag token such as `optional` off the event text."""
    head, _, rest = text.partition(" ")
    if head in lookups.FLAG_PREFIXES:
        return frozenset([head]), rest
    return frozenset(), text


def _handle_performer(track, kind, event, current_tick, performers):
    if current_tick is None:
        current_tick = event.tick
    elif current_tick != event.tick and performers:
        _finalize_performer(track, kind, current_tick, performers)
        current_tick = event.tick
        performers = set()
    if event.text in lookups.PERFORMERS:
        performers = performers | {event.text}
    return current_tick, performers


def _finalize_performer(track, kind, tick, performers) -> None:
    if tick is not None:
        track.performer.append((tick, kind, frozenset(performers)))


def _handle_camera_cut(track, event, cut_tick, constraints, subjects):
    is_constraint = event.type is VenueType.CAMERA_CUT_CONSTRAINT

    if cut_tick is None:
        if is_constraint:
            if event.text not in lookups.CAMERA_CUT_CONSTRAINTS:
                return cut_tick, constraints, subjects
            constraints = {event.text}
        else:
            subject = lookups.CAMERA_CUT_SUBJECTS.get(event.text)
            if subject is None:
                return cut_tick, constraints, subjects
            subjects = [subject]
            constraints = set()
        return event.tick, constraints, subjects

    if cut_tick != event.tick:
        track.camera_cuts.append(
            (cut_tick, _resolve_subject(subjects), frozenset(constraints),
             tuple(s for s in subjects if s != "Random") if len(subjects) > 1 else ())
        )
        cut_tick = event.tick
        constraints = set()
        subjects = []

    if is_constraint:
        if event.text in lookups.CAMERA_CUT_CONSTRAINTS:
            constraints = constraints | {event.text}
    else:
        subject = lookups.CAMERA_CUT_SUBJECTS.get(event.text)
        if subject is not None:
            subjects = subjects + [subject]
    return cut_tick, constraints, subjects


def _resolve_subject(subjects: list[str]) -> str:
    """One subject wins outright; several become a Random choice."""
    return subjects[0] if len(subjects) == 1 else "Random"
