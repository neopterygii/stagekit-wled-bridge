"""YARG UDP packet parser.

Parses the YARG datagram protocol (header 0x59415247 "YARG") and extracts
the fields relevant to Stage Kit lighting control.

Where each field sits is declared once, in `protocol/schema.py`.
"""

import struct
from dataclasses import dataclass, field

from protocol import schema

# YARG packet header magic bytes
PACKET_HEADER = 0x59415247  # "YARG"
_MAGIC = struct.pack("<I", PACKET_HEADER)  # little-endian uint32 (C# BinaryWriter)
MIN_PACKET_SIZE = schema.MIN_PACKET_SIZE   # 44: the oldest known version's core

# Datagram versions whose layout has been verified against YARG's
# DataStreamController.cs:
#   1 = v0.14.0
#   3 = v0.15.0            (+3 camera-cut bytes)
#   4 = nightly (dev)      (+ushort count, then variable-length star-power)
#   5 = nightly (dev)      (+ushort FogRemainingCentiseconds INSERTED at 37)
#
# v1-v4 were purely append-only. v5 (YARG PR #1605, "Added fog timer and
# changed all bytes to a queue") is the first version to insert a field
# mid-packet, shifting every later field by 2 — which is why the layout is a
# per-version table in `protocol/schema.py` rather than offsets typed here.
#
# A version outside this set means the layout *may* have shifted again under
# the fields we read. parse_packet still parses at the newest known offsets
# (best effort); the caller warns so it gets re-verified against upstream
# rather than silently rendering wrong lighting.
# `tools/check_yarg_schema.py` does that verification, daily, in CI.
# See memory: yarg-datagram-version.
KNOWN_DATAGRAM_VERSIONS = schema.KNOWN_VERSIONS

# First datagram version carrying FogRemainingCentiseconds at offset 37.
FOG_TIMER_VERSION = 5

_LAYOUTS = schema.LAYOUTS
_PREFIX = schema.PREFIX


@dataclass
class YARGPacket:
    """Parsed YARG UDP datagram."""
    # Tech
    datagram_version: int = 0
    platform: int = 0
    # Game
    scene: int = 0
    paused: int = 0
    venue_size: int = 0
    # Song
    bpm: float = 120.0
    song_section: int = 0
    # Instruments
    guitar_notes: int = 0
    bass_notes: int = 0
    drum_notes: int = 0
    keys_notes: int = 0
    vocal_note: float = 0.0
    harmony0_note: float = 0.0
    harmony1_note: float = 0.0
    harmony2_note: float = 0.0
    # Lighting
    lighting_cue: int = 0
    post_processing: int = 0
    fog_state: bool = False
    # v5+ only (offset 37, ushort): centiseconds left on YARG's fog timer, so
    # a receiver can size fogger on/off times to the room. Parsed but not yet
    # used for lighting; stays 0 on v1-v4, which have no such field.
    fog_remaining_cs: int = 0
    strobe_state: int = 0
    beat: int = 0
    keyframe: int = 0
    bonus_effect: bool = False
    auto_gen: bool = False
    spotlight: int = 0
    singalong: int = 0
    # Camera cut (v3; offsets 44-46 on v1-v4, 46-48 on v5+). Parsed but not
    # yet used for lighting —
    # surfaced on the status page.
    camera_cut_constraint: int = 0
    camera_cut_priority: int = 0
    camera_cut_subject: int = 0
    # Star power (v4; count at offset 47 on v1-v4, 49 on v5+). Per-player on
    # the wire; the single strip has
    # no per-player geometry, so we keep aggregates for the surge overlay plus
    # the raw list for the status page / tests.
    sp_player_count: int = 0
    sp_active_count: int = 0
    sp_active: bool = False
    sp_amount: float = 0.0   # max amount among ACTIVE players, 0.0-1.0
    sp_charge: float = 0.0   # max amount among ALL players, 0.0-1.0
    star_power: list = field(default_factory=list)  # raw [(amount_byte, is_active)]


class CueByte:
    """Lighting cue identifiers — matches YALCY UdpIntake.CueByte enum (0-indexed)."""
    DEFAULT = 0
    DISCHORD = 1
    CHORUS = 2
    COOL_MANUAL = 3
    STOMP = 4
    VERSE = 5
    WARM_MANUAL = 6
    BIG_ROCK_ENDING = 7
    BLACKOUT_FAST = 8
    BLACKOUT_SLOW = 9
    BLACKOUT_SPOTLIGHT = 10
    COOL_AUTOMATIC = 11
    FLARE_FAST = 12
    FLARE_SLOW = 13
    FRENZY = 14
    INTRO = 15
    HARMONY = 16
    SILHOUETTES = 17
    SILHOUETTES_SPOTLIGHT = 18
    SEARCHLIGHTS = 19
    STROBE_FASTEST = 20
    STROBE_FAST = 21
    STROBE_MEDIUM = 22
    STROBE_SLOW = 23
    STROBE_OFF = 24
    SWEEP = 25
    WARM_AUTOMATIC = 26
    KEYFRAME_FIRST = 27
    KEYFRAME_NEXT = 28
    KEYFRAME_PREVIOUS = 29
    MENU = 30
    SCORE = 31
    NO_CUE = 32


class BeatByte:
    OFF = 0
    MEASURE = 1
    STRONG = 2
    WEAK = 3


class KeyframeByte:
    OFF = 0
    FIRST = 27
    NEXT = 28
    PREVIOUS = 29


class SceneIndexByte:
    """YARG scene index (matches DataStreamController.SceneIndexByte)."""
    UNKNOWN = 0
    MENU = 1
    GAMEPLAY = 2
    SCORE = 3
    CALIBRATION = 4


class StrobeSpeed:
    OFF = CueByte.STROBE_OFF        # 24
    SLOW = CueByte.STROBE_SLOW      # 23
    MEDIUM = CueByte.STROBE_MEDIUM  # 22
    FAST = CueByte.STROBE_FAST      # 21
    FASTEST = CueByte.STROBE_FASTEST  # 20


class Performer:
    """Spotlight/Singalong performer bitmask (42/43 on v1-v4, 44/45 on v5+)."""
    NONE = 0
    GUITAR = 1
    BASS = 2
    DRUMS = 4
    VOCALS = 8
    KEYBOARD = 16


class PostProcessing:
    """Venue post-processing grade (offset 35).

    The wire byte is YARG's `PostProcessingType` cast to a byte
    (DataStreamController writes `(byte) message.PostProcessing`). The values
    below are 0-indexed sequential, matching YALCY's canonical `PostProcessingByte`
    enum — the reference receiver reads the same offset we do. (Not the spread
    0/4/14…255 numbering in YARG's sACN interpreter, which is a separate DMX-only
    mapping.) The bridge only acts on the colour-tint grades (see mapper
    POST_GRADES); camera-only grades — Bloom/Bright/Posterize/Mirror/Grainy/
    Scanlines geometry/Trails — carry no colour change and pass through.
    """
    DEFAULT = 0
    BLOOM = 1
    BRIGHT = 2
    CONTRAST = 3
    POSTERIZE = 4
    PHOTO_NEGATIVE = 5
    MIRROR = 6
    BLACK_AND_WHITE = 7
    SEPIA_TONE = 8
    SILVER_TONE = 9
    CHOPPY_BLACK_AND_WHITE = 10
    PHOTO_NEGATIVE_RED_AND_BLACK = 11
    POLARIZED_BLACK_AND_WHITE = 12
    POLARIZED_RED_AND_BLUE = 13
    DESATURATED_BLUE = 14
    DESATURATED_RED = 15
    CONTRAST_RED = 16
    CONTRAST_GREEN = 17
    CONTRAST_BLUE = 18
    GRAINY_FILM = 19
    GRAINY_CHROMATIC_ABBERATION = 20
    SCANLINES = 21
    SCANLINES_BLACK_AND_WHITE = 22
    SCANLINES_BLUE = 23
    SCANLINES_SECURITY = 24
    TRAILS = 25
    TRAILS_LONG = 26
    TRAILS_DESATURATED = 27
    TRAILS_FLICKERY = 28
    TRAILS_SPACEY = 29


class VenueSizeByte:
    """Venue size (offset 8) — matches YARG's VenueType / YALCY VenueSizeByte.

    Set on chart load by YARG's GameplayMonitor; NoVenue (0) at menu or when
    the chart doesn't say. NAMES gives a display label for the status page.
    """
    NO_VENUE = 0
    SMALL = 1
    LARGE = 2

    NAMES = {0: "None", 1: "Small", 2: "Large"}

    @classmethod
    def name(cls, value: int) -> str:
        return cls.NAMES.get(value, str(value))


class SongSectionByte:
    """Song section (offset 13) — matches YARG's LightingType section values.

    YARG writes the LightingType of the most recent Verse/Chorus lighting
    event (DataStreamController: MLCCurrentSongSection is set on lighting-cue
    change and cleared to 0 at menu), so the values match CueByte — Chorus=2,
    Verse=5; YALCY's SongSectionByte enum confirms the same pair. 0 = none
    (menu, or before the first section event of the song). NAMES gives a
    display label for the status page.
    """
    NONE = 0
    CHORUS = CueByte.CHORUS   # 2
    VERSE = CueByte.VERSE     # 5

    NAMES = {0: "None", 2: "Chorus", 5: "Verse"}

    @classmethod
    def name(cls, value: int) -> str:
        return cls.NAMES.get(value, str(value))


class CameraCutPriority:
    """Camera-cut priority (offset 45 on v1-v4, 47 on v5+)."""
    NORMAL = 0
    DIRECTED = 1


class CameraCutConstraint:
    """Camera-cut constraint flags (offset 44 on v1-v4, 46 on v5+)."""
    NONE = 0
    ONLY_CLOSE = 1
    ONLY_FAR = 2
    NO_CLOSE = 4
    NO_BEHIND = 8


class CameraCutSubject:
    """Camera-cut subject (offset 46 on v1-v4, 48 on v5+). Matches YARG.Core / YALCY.

    NAMES gives a display label for the status page; unknown values fall back
    to the numeric id via NAMES.get(v, str(v)).
    """
    CROWD = 0
    STAGE = 1
    RANDOM = 42  # always the last / catch-all value in YARG

    NAMES = {
        0: "Crowd", 1: "Stage", 2: "AllBehind", 3: "AllFar", 4: "AllNear",
        5: "BehindNoDrum", 6: "NearNoDrum", 7: "Guitar", 8: "GuitarBehind",
        9: "GuitarCloseup", 10: "GuitarCloseupHead", 11: "Drums", 12: "DrumsKick",
        13: "DrumsBehind", 14: "DrumsCloseupHand", 15: "DrumsCloseupHead",
        16: "Bass", 17: "BassBehind", 18: "BassCloseup", 19: "BassCloseupHead",
        20: "Vocals", 21: "VocalsCloseup", 22: "VocalsBehind", 23: "Keys",
        24: "KeysBehind", 25: "KeysCloseupHand", 26: "KeysCloseupHead",
        27: "DrumsVocals", 28: "BassDrums", 29: "DrumsGuitar",
        30: "BassVocalsBehind", 31: "BassVocals", 32: "GuitarVocalsBehind",
        33: "GuitarVocals", 34: "KeysVocalsBehind", 35: "KeysVocals",
        36: "BassGuitarBehind", 37: "BassGuitar", 38: "BassKeysBehind",
        39: "BassKeys", 40: "GuitarKeysBehind", 41: "GuitarKeys", 42: "Random",
    }

    @classmethod
    def name(cls, value: int) -> str:
        return cls.NAMES.get(value, str(value))


def parse_packet(data: bytes) -> YARGPacket | None:
    """Parse a raw YARG UDP datagram. Returns None if invalid.

    Offsets come from `protocol/schema.py`; nothing here names one. The layout
    is chosen by the version byte, NOT by length: a v4 packet carrying one
    star-power player is 51 bytes, exactly like a v5 packet carrying none, so
    length cannot tell the two apart.
    """
    size = len(data)
    if size < MIN_PACKET_SIZE or data[:4] != _MAGIC:
        return None

    pkt = YARGPacket()
    fields = pkt.__dict__
    lay = _LAYOUTS[data[4]]

    core = lay.core
    if size < core.end:
        # Truncated for its version. The fields ahead of the first one any
        # version moved are still at known offsets; the rest keep defaults.
        fields.update(zip(_PREFIX.names, _PREFIX.unpack_from(data, 0)))
        return pkt
    fields.update(zip(core.names, core.unpack_from(data, 0)))

    # Later additions (camera cut, …), each guarded as a unit so a short
    # packet degrades instead of raising.
    for block in lay.groups:
        if size >= block.end:
            fields.update(zip(block.names, block.unpack_from(data, block.offset)))

    # ── v4+: per-player star power ──────────────────────────────
    # uint16 count, then <Amount:byte, IsActive:byte> pairs. Length-guarded: a
    # truncated or garbage count never reads past the buffer (clamp to the
    # pairs actually present) so a malformed packet degrades to "no star
    # power" instead of crashing the UDP handler.
    sp_count_off = lay.star_power_count
    if sp_count_off is not None and size >= sp_count_off + 2:
        count = data[sp_count_off] | (data[sp_count_off + 1] << 8)
        first = sp_count_off + 2
        avail = (size - first) // 2
        n = count if count <= avail else avail
        pkt.sp_player_count = n
        players = []
        max_all = 0
        max_active = 0
        active_count = 0
        off = first
        for _ in range(n):
            amount = data[off]
            is_active = data[off + 1] != 0
            off += 2
            players.append((amount, is_active))
            if amount > max_all:
                max_all = amount
            if is_active:
                active_count += 1
                if amount > max_active:
                    max_active = amount
        pkt.star_power = players
        pkt.sp_active_count = active_count
        pkt.sp_active = active_count > 0
        pkt.sp_charge = max_all / 255.0
        pkt.sp_amount = max_active / 255.0

    return pkt
