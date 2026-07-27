"""Read authored venue data out of a Rock Band Milo archive.

Port of YARG.Core's `IO/Milo/YARGMiloReader.cs` + `MiloAnimation.cs` +
`Chart/Tracks/MiloVenue.cs` (themselves adapted from Onyx's Haskell parser).

The archive is decompressed, `song.anim` is located, and its animation tracks
are read by scanning for byte identifiers — which is what YARG does too; it
does not walk the structure. Times come back in frames at 30 fps.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .venue_track import VenueTrack
from . import lookups

MILO_A = 0xCABEDEAF  # uncompressed
MILO_B = 0xCBBEDEAF  # compressed
MILO_C = 0xCCBEDEAF  # compressed
MILO_D = 0xCDBEDEAF  # per-block compression flag

ANIMATION_MEMBER = b"song.anim"
FRAMES_PER_SECOND = 30.0


class MiloError(Exception):
    """Raised when a byte stream is not a readable Milo archive."""


@dataclass(frozen=True)
class AnimTrack:
    identifier: bytes
    offset: int
    kind: str


#: MiloAnimation.GetMiloAnimation identifiers. `offset` is how many bytes sit
#: between the identifier and the u32 event count.
ANIM_TRACKS = (
    AnimTrack(b"lightpreset_interp", 5, "lighting"),
    AnimTrack(b"lightpreset_keyframe_interp", 5, "lighting"),
    AnimTrack(b"postproc_interp", 5, "post_processing"),
    AnimTrack(b"stagekit_fog", 13, "fog"),
    AnimTrack(b"shot_bg", 13, "camera"),
    AnimTrack(b"world_event", 13, "world_event"),
    AnimTrack(b"spot_guitar", 13, "spotlight:guitar"),
    AnimTrack(b"spot_bass", 13, "spotlight:bass"),
    AnimTrack(b"spot_drums", 13, "spotlight:drums"),
    AnimTrack(b"spot_vocal", 13, "spotlight:vocals"),
    AnimTrack(b"spot_keyboard", 13, "spotlight:keys"),
    AnimTrack(b"part2_sing", 13, "singalong:guitar"),
    AnimTrack(b"part3_sing", 13, "singalong:bass"),
    AnimTrack(b"part4_sing", 13, "singalong:drums"),
)


def decompress(data: bytes) -> bytes:
    """Unwrap the Milo block container into one contiguous buffer."""
    if len(data) < 16:
        raise MiloError("too short to be a Milo archive")
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic not in (MILO_A, MILO_B, MILO_C, MILO_D):
        raise MiloError(f"unknown Milo magic {magic:#010x}")

    data_offset, block_count, _largest = struct.unpack_from("<III", data, 4)
    sizes = list(struct.unpack_from(f"<{block_count}I", data, 16))

    out = bytearray()
    pos = data_offset
    for size in sizes:
        if magic == MILO_D:
            compressed = not (size & (1 << 24))
            size &= ~(1 << 24)
        else:
            compressed = magic in (MILO_B, MILO_C)
        block = data[pos:pos + size]
        pos += size
        if compressed:
            try:
                # MILO_C/D skip a 4-byte inflate header; MILO_B does not.
                payload = block[4:] if magic in (MILO_C, MILO_D) else block
                block = zlib.decompress(payload, -zlib.MAX_WBITS)
            except zlib.error as exc:
                raise MiloError(f"block inflate failed: {exc}") from exc
        out += block
    return bytes(out)


def parse_milo_venue(data: bytes) -> VenueTrack:
    """Return the venue events a Milo contributes, or an empty track."""
    decompressed = decompress(data)
    if ANIMATION_MEMBER not in decompressed:
        return VenueTrack()
    return _read_animation(decompressed)


def _read_animation(blob: bytes) -> VenueTrack:
    track = VenueTrack()
    for anim in ANIM_TRACKS:
        index = blob.find(anim.identifier)
        if index < 0:
            continue
        pos = index + len(anim.identifier) + anim.offset
        try:
            events = _read_events(blob, pos, prepend_skip=anim.kind == "post_processing")
        except (struct.error, IndexError, UnicodeDecodeError):
            continue
        _apply(track, anim.kind, events)
    _sort(track)
    return track


def _read_events(blob: bytes, pos: int, prepend_skip: bool) -> list[tuple[str, float]]:
    count = struct.unpack_from(">I", blob, pos)[0]
    pos += 4
    if count > 100_000:
        raise IndexError(f"implausible event count {count}")

    out: list[tuple[str, float]] = []
    previous_name = ""
    for i in range(count):
        if prepend_skip:
            pos += 4
        name_len = struct.unpack_from(">I", blob, pos)[0]
        pos += 4
        if name_len == 0:
            if i == 0:
                # Leading empty name: skip this event and 4 more bytes.
                pos += 4
                continue
            name = previous_name
        else:
            name = blob[pos:pos + name_len].decode("utf-8", "replace")
            pos += name_len
        frames = struct.unpack_from(">f", blob, pos)[0]
        pos += 4
        previous_name = name
        out.append((name, max(frames, 0.0) / FRAMES_PER_SECOND))
    return out


def _apply(track: VenueTrack, kind: str, events: list[tuple[str, float]]) -> None:
    if kind == "lighting":
        for name, time in events:
            converted = lookups.VENUE_LIGHTING_CONVERSION_LOOKUP.get(name, name)
            if converted in lookups.LIGHTING_TYPES:
                track.lighting.append((_tick(time), converted))
    elif kind == "post_processing":
        for name, time in events:
            entry = lookups.VENUE_TEXT_CONVERSION_LOOKUP.get(name)
            converted = entry[1] if entry else name
            if converted in lookups.POST_PROCESSING_TYPES:
                track.post_processing.append((_tick(time), converted))
    elif kind == "fog":
        for name, time in events:
            effect = {"on": "fog_on", "off": "fog_off"}.get(name)
            if effect:
                track.stage.append((_tick(time), effect, frozenset()))
    elif kind == "world_event":
        for name, time in events:
            if name == "bonusfx":
                track.stage.append((_tick(time), "bonus_fx", frozenset()))
    elif kind == "camera":
        for name, time in events:
            subject = lookups.CAMERA_CUT_SUBJECTS.get(
                name[len("coop_"):] if name.startswith("coop_") else name
            )
            if subject:
                track.camera_cuts.append((_tick(time), subject, frozenset(), ()))
    elif kind.startswith(("spotlight:", "singalong:")):
        event_kind, performer = kind.split(":", 1)
        for name, time in events:
            if name in ("on", "singalong_on"):
                track.performer.append((_tick(time), event_kind, frozenset([performer])))


def _tick(time_seconds: float) -> int:
    """Milo events carry time, not ticks; store milliseconds as a stand-in.

    Nothing downstream converts these back to ticks — they exist only to order
    and count events — so a monotonic integer is sufficient.
    """
    return int(time_seconds * 1000)


def _sort(track: VenueTrack) -> None:
    track.lighting.sort()
    track.post_processing.sort()
    track.stage.sort()
    track.camera_cuts.sort(key=lambda e: e[0])
    track.performer.sort(key=lambda e: e[0])
