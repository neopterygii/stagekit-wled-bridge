"""The YARG datagram layout, as one table.

YARG serialises its lighting datagram with a C# `BinaryWriter` in
`DataStreamController.SerializeAndSend`: a fixed run of little-endian fields,
then a variable-length star-power table. There is no self-description on the
wire beyond a version byte, so a receiver has to know every offset.

Until v5 the format was append-only and hand-written offsets held. v5 inserted
a ushort mid-packet and shifted everything behind it by two; the bridge kept
reading the old offsets and held the strip solid white for a song (2026-08-23).
The fix then was an `if version >= 5` branch. This table replaces that branch:
a new version is one more row, and every offset is derived rather than typed.

`FIELDS` is in wire order. Each row is

    (name, fmt, since, until, upstream)

  name      attribute on `YARGPacket` (or "header", which is checked, not stored)
  fmt       one `struct` code: B byte, ? bool, H ushort, I uint, f float
  since     first datagram version that carries the field
  until     first version that no longer carries it, or None
  upstream  what YARG's serialiser writes for it; `tools/check_yarg_schema.py`
            compares these, in order, against upstream's source

To add a datagram version: add or retire rows here, add the number to
`KNOWN_VERSIONS`, add its offset map to `tests/test_packet_schema.py`, and
refresh the upstream fixture. `parse_packet` needs no change unless the
variable-length tail itself changes shape.
"""

import struct
from typing import NamedTuple


class Field(NamedTuple):
    name: str
    fmt: str
    since: int
    until: int | None
    upstream: str


# Version history, from the comment above DataStreamController.Sender:
#   v0 initial · v1 "HasVenueTrack?" byte · v2 Practice scene, pause fix
#   v3 camera cut · v4 per-player star power · v5 fog timer
# Rows marked since=1 are the 44-byte layout the bridge was first written
# against (v0.14.0); nothing older has been seen or verified.
FIELDS: tuple[Field, ...] = (
    Field("header",                "I", 1, None, "Header"),
    Field("datagram_version",      "B", 1, None, "DatagramVersion"),
    Field("platform",              "B", 1, None, "Platform"),
    Field("scene",                 "B", 1, None, "CurrentScene"),
    Field("paused",                "B", 1, None, "Paused"),
    Field("venue_size",            "B", 1, None, "VenueSize"),
    Field("bpm",                   "f", 1, None, "BeatsPerMinute"),
    Field("song_section",          "B", 1, None, "CurrentSongSection"),
    Field("guitar_notes",          "B", 1, None, "CurrentGuitarNotes"),
    Field("bass_notes",            "B", 1, None, "CurrentBassNotes"),
    Field("drum_notes",            "B", 1, None, "CurrentDrumNotes"),
    Field("keys_notes",            "B", 1, None, "CurrentKeysNotes"),
    Field("vocal_note",            "f", 1, None, "CurrentVocalNote"),
    Field("harmony0_note",         "f", 1, None, "CurrentHarmony0Note"),
    Field("harmony1_note",         "f", 1, None, "CurrentHarmony1Note"),
    Field("harmony2_note",         "f", 1, None, "CurrentHarmony2Note"),
    Field("lighting_cue",          "B", 1, None, "LightingCue"),
    Field("post_processing",       "B", 1, None, "PostProcessing"),
    Field("fog_state",             "?", 1, None, "FogState"),
    Field("fog_remaining_cs",      "H", 5, None, "FogRemainingCentiseconds"),
    Field("strobe_state",          "B", 1, None, "StrobeState"),
    Field("beat",                  "B", 1, None, "Beat"),
    Field("keyframe",              "B", 1, None, "Keyframe"),
    Field("bonus_effect",          "?", 1, None, "BonusEffect"),
    Field("auto_gen",              "?", 1, None, "AutoGenVenueTrack"),
    Field("spotlight",             "B", 1, None, "Spotlight"),
    Field("singalong",             "B", 1, None, "Singalong"),
    Field("camera_cut_constraint", "B", 3, None, "CameraCutConstraint"),
    Field("camera_cut_priority",   "B", 3, None, "CameraCutPriority"),
    Field("camera_cut_subject",    "B", 3, None, "CameraCutSubject"),
    Field("sp_player_count",       "H", 4, None, "playerStarPowerCount"),
)

# After the fixed fields, v4+ repeats this pair `sp_player_count` times. It is
# the one part of the datagram a table of fixed offsets cannot describe, so
# `parse_packet` walks it by hand.
STAR_POWER_ENTRY: tuple[tuple[str, str], ...] = (("Amount", "B"), ("IsActive", "B"))

# Versions whose layout has been verified against YARG's source.
KNOWN_VERSIONS = frozenset({1, 3, 4, 5})
NEWEST_VERSION = max(KNOWN_VERSIONS)

# The last field every version must carry. Everything through it is the
# *core*: a datagram too short to hold its version's core is truncated.
# Everything after it arrives in optional groups, one per introducing version.
LAST_CORE_FIELD = "singalong"

_NOT_STORED = frozenset({"header"})


class Block(NamedTuple):
    """A run of fields read with one `struct` call."""
    offset: int
    end: int
    unpack_from: object      # bound Struct.unpack_from
    names: tuple[str, ...]   # attributes, in the order unpack_from yields them


class Layout(NamedTuple):
    version: int
    offsets: dict            # field name -> byte offset, every fixed field
    size: int                # bytes of fixed fields; the star-power table follows
    core: Block
    groups: tuple[Block, ...]            # optional, each length-guarded as a unit
    star_power_count: int | None         # offset of the count, None before v4


def fields_for(version: int) -> tuple[Field, ...]:
    """The rows a datagram of `version` carries, in wire order."""
    return tuple(f for f in FIELDS
                 if f.since <= version and (f.until is None or version < f.until))


def _block(fields, offsets) -> Block:
    first = offsets[fields[0].name]
    fmt = "<" + "".join(f"{struct.calcsize('<' + f.fmt)}x" if f.name in _NOT_STORED
                        else f.fmt for f in fields)
    compiled = struct.Struct(fmt)
    return Block(first, first + compiled.size, compiled.unpack_from,
                 tuple(f.name for f in fields if f.name not in _NOT_STORED))


def layout(version: int) -> Layout:
    """Offsets and read blocks for one datagram version."""
    fields = fields_for(version)
    offsets, pos = {}, 0
    for f in fields:
        offsets[f.name] = pos
        pos += struct.calcsize("<" + f.fmt)

    names = [f.name for f in fields]
    split = names.index(LAST_CORE_FIELD) + 1
    core = _block(fields[:split], offsets)

    # The star-power count is read by the tail walker, not as a stored block:
    # the value on the wire is clamped to the pairs actually present.
    count_off = offsets.get("sp_player_count")
    groups, run = [], []
    for f in fields[split:]:
        if f.name == "sp_player_count":
            continue
        if run and f.since != run[-1].since:
            groups.append(_block(run, offsets))
            run = []
        run.append(f)
    if run:
        groups.append(_block(run, offsets))

    return Layout(version, offsets, pos, core, tuple(groups), count_off)


def _prefix() -> Block:
    """Leading fields that sit at the same offset in every known version.

    What a datagram too short for its own version's core can still be trusted
    for: the fields before the first one any version inserted or retired.
    """
    oldest = min(KNOWN_VERSIONS)
    run = []
    for f in FIELDS:
        if f.since > oldest or f.until is not None:
            break
        run.append(f)
    offsets, pos = {}, 0
    for f in run:
        offsets[f.name] = pos
        pos += struct.calcsize("<" + f.fmt)
    return _block(run, offsets)


# ── precomputed at import: nothing below is built per datagram ──────────

PREFIX: Block = _prefix()

# Indexed by the version byte, so the parser's lookup cannot miss. A version
# newer than any known one gets the newest known layout, and one older than
# any known one the oldest (both best effort; the caller warns). A gap between
# known versions — v2 — is laid out from the table like any other.
LAYOUTS: tuple[Layout, ...] = tuple(
    layout(max(min(KNOWN_VERSIONS), min(v, NEWEST_VERSION))) for v in range(256))

# Smallest datagram worth parsing: the oldest known version's core.
MIN_PACKET_SIZE: int = LAYOUTS[min(KNOWN_VERSIONS)].core.end
