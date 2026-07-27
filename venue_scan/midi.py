"""Just enough Standard MIDI File parsing to read a `VENUE` track.

We need track names, note on/off pairs, and text meta events — not a general
MIDI library, and not a new dependency in the bridge venv.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

#: Meta event types YARG refuses to treat as text (MidIOHelper.DisallowedTextEventTypes).
#: 0x03 is SequenceTrackName, 0x02 is CopyrightNotice.
DISALLOWED_TEXT_META = {0x02, 0x03}

#: Meta types that do count as text: Text, InstrumentName, Lyric, Marker, CuePoint,
#: ProgramName, DeviceName.
TEXT_META = {0x01, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09}


class MidiError(Exception):
    """Raised when a byte stream is not a readable Standard MIDI File."""


@dataclass
class MidiEvent:
    tick: int
    kind: str          # "note_on" | "note_off" | "text"
    note: int = 0
    text: str = ""


@dataclass
class MidiTrack:
    name: str
    events: list[MidiEvent]


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise MidiError("variable-length quantity longer than 4 bytes")


def parse_tracks(data: bytes, wanted: str | None = None) -> list[MidiTrack]:
    """Parse a SMF into tracks.

    ``wanted`` restricts full event parsing to the track with that exact name;
    other tracks are still listed (so callers can see the track layout) but
    their events are skipped. Scanning tens of thousands of songs, this is the
    difference between reading the VENUE track and decoding every note in the
    chart.
    """
    if len(data) < 14 or data[:4] != b"MThd":
        raise MidiError("missing MThd header")
    header_len = struct.unpack_from(">I", data, 4)[0]
    track_count = struct.unpack_from(">H", data, 10)[0]

    tracks: list[MidiTrack] = []
    pos = 8 + header_len
    for _ in range(track_count):
        if pos + 8 > len(data):
            break
        if data[pos:pos + 4] != b"MTrk":
            raise MidiError(f"expected MTrk at offset {pos}")
        length = struct.unpack_from(">I", data, pos + 4)[0]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 8 + length

        name = _track_name(chunk)
        if wanted is not None and name != wanted:
            tracks.append(MidiTrack(name=name, events=[]))
            continue
        tracks.append(MidiTrack(name=name, events=list(_parse_events(chunk))))
    return tracks


def _track_name(chunk: bytes) -> str:
    """Read the SequenceTrackName, which by convention is the first event."""
    try:
        _delta, pos = _read_varint(chunk, 0)
        if chunk[pos] == 0xFF and chunk[pos + 1] == 0x03:
            length, pos = _read_varint(chunk, pos + 2)
            return chunk[pos:pos + length].decode("utf-8", "replace")
    except (IndexError, MidiError):
        pass
    return ""


def _parse_events(chunk: bytes):
    pos = 0
    tick = 0
    running_status = 0
    length = len(chunk)

    while pos < length:
        delta, pos = _read_varint(chunk, pos)
        tick += delta
        if pos >= length:
            break

        status = chunk[pos]
        if status & 0x80:
            pos += 1
            running_status = status
        else:
            status = running_status
            if not status:
                raise MidiError("running status with no preceding status byte")

        if status == 0xFF:
            meta_type = chunk[pos]
            pos += 1
            meta_len, pos = _read_varint(chunk, pos)
            payload = chunk[pos:pos + meta_len]
            pos += meta_len
            if meta_type in TEXT_META and meta_type not in DISALLOWED_TEXT_META:
                yield MidiEvent(
                    tick=tick, kind="text",
                    text=payload.decode("utf-8", "replace"),
                )
            continue

        if status in (0xF0, 0xF7):
            sysex_len, pos = _read_varint(chunk, pos)
            pos += sysex_len
            continue

        high = status & 0xF0
        if high in (0x80, 0x90):
            note = chunk[pos]
            velocity = chunk[pos + 1]
            pos += 2
            # A note-on with zero velocity is a note-off.
            kind = "note_on" if high == 0x90 and velocity > 0 else "note_off"
            yield MidiEvent(tick=tick, kind=kind, note=note)
        elif high in (0xA0, 0xB0, 0xE0):
            pos += 2
        elif high in (0xC0, 0xD0):
            pos += 1
        else:
            raise MidiError(f"unknown status byte {status:#04x} at {pos}")


def find_track(data: bytes, name: str) -> MidiTrack | None:
    """Return the named track, or None. Track names must match exactly.

    A track that exists but is empty is still returned, so callers can tell
    "no VENUE track" apart from "VENUE track with nothing in it".
    """
    for track in parse_tracks(data, wanted=name):
        if track.name == name:
            return track
    return None


def track_names(data: bytes) -> list[str]:
    return [track.name for track in parse_tracks(data, wanted="\0")]
