"""YARG UDP packet parser.

Parses the YARG datagram protocol (header 0x59415247 "YARG") and extracts
the fields relevant to Stage Kit lighting control.
"""

import struct
from dataclasses import dataclass, field

# YARG packet header magic bytes
PACKET_HEADER = 0x59415247  # "YARG"
MIN_PACKET_SIZE = 44


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


class StrobeSpeed:
    OFF = CueByte.STROBE_OFF        # 24
    SLOW = CueByte.STROBE_SLOW      # 23
    MEDIUM = CueByte.STROBE_MEDIUM  # 22
    FAST = CueByte.STROBE_FAST      # 21
    FASTEST = CueByte.STROBE_FASTEST  # 20


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

    return pkt
