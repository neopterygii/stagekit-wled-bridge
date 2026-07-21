"""YARG UDP packet parser.

Parses the YARG datagram protocol (header 0x59415247 "YARG") and extracts
the fields relevant to Stage Kit lighting control.
"""

import struct
from dataclasses import dataclass, field

# YARG packet header magic bytes
PACKET_HEADER = 0x59415247  # "YARG"
MIN_PACKET_SIZE = 44

# Datagram versions whose first-44-byte layout this parser has been verified
# against in YARG's DataStreamController.cs. The bridge only reads offsets
# 0-43, and every version YARG has shipped keeps those identical and appends
# newer fields beyond them:
#   1 = v0.14.0
#   3 = v0.15.0            (+3 camera-cut bytes)
#   4 = nightly (dev)      (+ushort count, then variable-length star-power)
# A version outside this set means the layout *may* have shifted under the
# fields we read. parse_packet still parses at these offsets (best effort);
# the caller warns so it gets re-verified against upstream rather than
# silently rendering wrong lighting. See memory: yarg-datagram-version.
KNOWN_DATAGRAM_VERSIONS = frozenset({1, 3, 4})


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
    strobe_state: int = 0
    beat: int = 0
    keyframe: int = 0
    bonus_effect: bool = False
    auto_gen: bool = False
    spotlight: int = 0
    singalong: int = 0
    # Camera cut (v3, offsets 44-46). Parsed but not yet used for lighting —
    # surfaced on the status page.
    camera_cut_constraint: int = 0
    camera_cut_priority: int = 0
    camera_cut_subject: int = 0
    # Star power (v4, offset 47+). Per-player on the wire; the single strip has
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
    """Spotlight/Singalong performer bitmask (offsets 42, 43)."""
    NONE = 0
    GUITAR = 1
    BASS = 2
    DRUMS = 4
    VOCALS = 8
    KEYBOARD = 16


class CameraCutPriority:
    """Camera-cut priority (offset 45)."""
    NORMAL = 0
    DIRECTED = 1


class CameraCutConstraint:
    """Camera-cut constraint flags (offset 44)."""
    NONE = 0
    ONLY_CLOSE = 1
    ONLY_FAR = 2
    NO_CLOSE = 4
    NO_BEHIND = 8


class CameraCutSubject:
    """Camera-cut subject (offset 46). Values match YARG.Core / YALCY.

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
    """Parse a raw YARG UDP datagram. Returns None if invalid."""
    if len(data) < MIN_PACKET_SIZE:
        return None

    header = struct.unpack_from("<I", data, 0)[0]  # little-endian uint32 (C# BinaryWriter)
    if header != PACKET_HEADER:
        return None

    pkt = YARGPacket()
    # Byte 4 onward — BinaryReader-style sequential read (little-endian for floats)
    pkt.datagram_version = data[4]
    pkt.platform = data[5]
    pkt.scene = data[6]
    pkt.paused = data[7]
    pkt.venue_size = data[8]
    pkt.bpm = struct.unpack_from("<f", data, 9)[0]  # little-endian float32
    pkt.song_section = data[13]
    pkt.guitar_notes = data[14]
    pkt.bass_notes = data[15]
    pkt.drum_notes = data[16]
    pkt.keys_notes = data[17]
    pkt.vocal_note = struct.unpack_from("<f", data, 18)[0]
    pkt.harmony0_note = struct.unpack_from("<f", data, 22)[0]
    pkt.harmony1_note = struct.unpack_from("<f", data, 26)[0]
    pkt.harmony2_note = struct.unpack_from("<f", data, 30)[0]
    pkt.lighting_cue = data[34]
    pkt.post_processing = data[35]
    pkt.fog_state = bool(data[36])
    pkt.strobe_state = data[37]
    pkt.beat = data[38]
    pkt.keyframe = data[39]
    pkt.bonus_effect = bool(data[40])
    pkt.auto_gen = bool(data[41])
    pkt.spotlight = data[42]
    pkt.singalong = data[43]

    # ── v3+: camera cut (offsets 44-46) ─────────────────────────
    # Parsed by length, not version. YARG's datagram is append-only, so these
    # live at fixed offsets whenever the packet is long enough to hold them —
    # more robust than branching on the version byte (which we only warn on).
    if len(data) >= 47:
        pkt.camera_cut_constraint = data[44]
        pkt.camera_cut_priority = data[45]
        pkt.camera_cut_subject = data[46]

    # ── v4+: per-player star power ──────────────────────────────
    # uint16 count at offset 47, then <Amount:byte, IsActive:byte> pairs at 49.
    # Length-guarded: a truncated or garbage count never reads past the buffer
    # (clamp to the pairs actually present) so a malformed packet degrades to
    # "no star power" instead of crashing the UDP handler.
    if len(data) >= 49:
        count = struct.unpack_from("<H", data, 47)[0]
        avail = (len(data) - 49) // 2
        n = count if count <= avail else avail
        pkt.sp_player_count = n
        players = []
        max_all = 0
        max_active = 0
        active_count = 0
        off = 49
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
