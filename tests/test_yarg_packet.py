"""Parser tests for the YARG datagram — v1/v3/v4/v5 layouts + malformed guards.

Parsing offsets are the one thing that silently renders wrong lighting, so
these assert the exact byte layout confirmed against YARG's
DataStreamController.cs and lock in the length-guarded v3/v4/v5 reads.

Run: python -m pytest tests/ -v   (or: python -m pytest tests/test_yarg_packet.py)
"""

import struct
import sys
from pathlib import Path

# Allow running from the repo root without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protocol.yarg_packet import (  # noqa: E402
    parse_packet, YARGPacket, PACKET_HEADER, MIN_PACKET_SIZE,
    CameraCutSubject, KNOWN_DATAGRAM_VERSIONS, FOG_TIMER_VERSION,
    CueByte, BeatByte, StrobeSpeed,
)


def _base(version: int, size: int, *, cue: int = 5, bpm: float = 140.0,
          camera_subject: int = 0, camera_priority: int = 0) -> bytearray:
    """Build a raw datagram of `size` bytes with the shared v1 prefix set."""
    buf = bytearray(size)
    struct.pack_into("<I", buf, 0, PACKET_HEADER)
    buf[4] = version
    buf[5] = 1            # platform
    buf[6] = 2            # scene = gameplay
    buf[7] = 1            # unpaused
    buf[8] = 1            # venue size
    struct.pack_into("<f", buf, 9, bpm)
    buf[34] = cue         # lighting cue
    buf[37] = 0           # strobe
    buf[38] = 2           # beat = strong
    if size >= 47:
        buf[44] = 0                 # camera constraint
        buf[45] = camera_priority
        buf[46] = camera_subject
    return buf


def _v4(players, *, camera_subject: int = 0) -> bytes:
    """Build a v4 datagram carrying the given [(amount, is_active)] players."""
    buf = _base(4, 49 + 2 * len(players), camera_subject=camera_subject)
    struct.pack_into("<H", buf, 47, len(players))
    off = 49
    for amount, is_active in players:
        buf[off] = amount & 0xFF
        buf[off + 1] = 1 if is_active else 0
        off += 2
    return bytes(buf)


# ── header / prefix ──────────────────────────────────────────────

def test_rejects_short_packet():
    assert parse_packet(b"\x00" * (MIN_PACKET_SIZE - 1)) is None


def test_rejects_bad_header():
    buf = _base(1, 44)
    buf[0] ^= 0xFF
    assert parse_packet(bytes(buf)) is None


def test_v1_prefix_parsed():
    pkt = parse_packet(bytes(_base(1, 44, cue=6, bpm=128.0)))
    assert isinstance(pkt, YARGPacket)
    assert pkt.datagram_version == 1
    assert pkt.lighting_cue == 6
    assert abs(pkt.bpm - 128.0) < 0.01
    assert pkt.beat == 2
    # v3/v4 fields default when absent
    assert pkt.camera_cut_subject == 0
    assert pkt.sp_player_count == 0
    assert pkt.sp_active is False
    assert pkt.star_power == []


# ── v3 camera cut ────────────────────────────────────────────────

def test_v3_camera_cut_parsed():
    pkt = parse_packet(bytes(_base(3, 47, camera_subject=11, camera_priority=1)))
    assert pkt.camera_cut_subject == 11
    assert pkt.camera_cut_priority == 1
    assert CameraCutSubject.name(11) == "Drums"
    # No star power in a v3 packet.
    assert pkt.sp_player_count == 0
    assert pkt.sp_active is False


# ── v4 star power ────────────────────────────────────────────────

def test_v4_no_players():
    pkt = parse_packet(_v4([]))
    assert pkt.datagram_version == 4
    assert pkt.sp_player_count == 0
    assert pkt.sp_active is False
    assert pkt.sp_amount == 0.0
    assert pkt.sp_charge == 0.0


def test_v4_aggregates_single_active():
    pkt = parse_packet(_v4([(128, True)]))
    assert pkt.sp_player_count == 1
    assert pkt.sp_active is True
    assert pkt.sp_active_count == 1
    assert abs(pkt.sp_amount - 128 / 255.0) < 1e-6
    assert abs(pkt.sp_charge - 128 / 255.0) < 1e-6
    assert pkt.star_power == [(128, True)]


def test_v4_aggregates_multi_player():
    # sp_amount = max among ACTIVE; sp_charge = max among ALL.
    pkt = parse_packet(_v4([(255, False), (100, True), (60, True)]))
    assert pkt.sp_player_count == 3
    assert pkt.sp_active is True
    assert pkt.sp_active_count == 2
    assert abs(pkt.sp_amount - 100 / 255.0) < 1e-6   # highest active
    assert abs(pkt.sp_charge - 255 / 255.0) < 1e-6   # highest overall (inactive)


def test_v4_charging_not_active():
    pkt = parse_packet(_v4([(200, False)]))
    assert pkt.sp_active is False
    assert pkt.sp_active_count == 0
    assert pkt.sp_amount == 0.0                       # none active
    assert abs(pkt.sp_charge - 200 / 255.0) < 1e-6


def test_v4_camera_and_star_power_coexist():
    pkt = parse_packet(_v4([(255, True)], camera_subject=20))
    assert pkt.camera_cut_subject == 20
    assert CameraCutSubject.name(20) == "Vocals"
    assert pkt.sp_active is True


# ── malformed / truncation guards ────────────────────────────────

def test_v4_count_larger_than_payload_is_clamped():
    # Claim 5 players but only include 1 pair — must clamp, not read past end.
    buf = bytearray(_v4([(128, True)]))
    struct.pack_into("<H", buf, 47, 5)
    pkt = parse_packet(bytes(buf))
    assert pkt is not None
    assert pkt.sp_player_count == 1          # clamped to what's present
    assert pkt.sp_active is True


def test_absurd_count_does_not_crash():
    buf = bytearray(_v4([]))
    struct.pack_into("<H", buf, 47, 0xFFFF)
    pkt = parse_packet(bytes(buf))
    assert pkt is not None
    assert pkt.sp_player_count == 0          # no pairs present → clamp to 0


def test_truncated_between_camera_and_starpower():
    # 48 bytes: has camera (>=47) but a truncated star-power count (<49).
    buf = _base(4, 48, camera_subject=7)
    pkt = parse_packet(bytes(buf))
    assert pkt is not None
    assert pkt.camera_cut_subject == 7
    assert pkt.sp_player_count == 0          # count needs offset 47-48



# ── v5: FogRemainingCentiseconds inserted at offset 37 ─────────────
#
# YARG PR #1605 ("Added fog timer and changed all bytes to a queue") is the
# first datagram change that is not append-only. Reading a v5 packet at v4
# offsets is not a subtle degradation: YARG's Beat byte falls under the
# bridge's `bonus_effect` read, and its dequeue fallback is a *non-zero* 3
# (DataStreamController: `DequeueByteValue(_beatQueue, (byte) 3)`), so
# bonus_effect latches true on every frame and the mapper's white bonus
# overlay pins the whole strip to solid white. These tests pin the offsets.

def _v5_packet(**kw) -> bytes:
    from test_sender import build_packet
    kw.setdefault("fog_remaining_cs", 0)
    return build_packet(**kw)


def test_v5_is_a_known_version():
    assert FOG_TIMER_VERSION == 5
    assert 5 in KNOWN_DATAGRAM_VERSIONS


def test_v5_shifts_every_field_after_fog_by_two():
    raw = _v5_packet(cue=CueByte.SEARCHLIGHTS, strobe=StrobeSpeed.MEDIUM,
                     beat=BeatByte.STRONG, keyframe=28, fog_remaining_cs=1234,
                     camera_subject=11, camera_priority=1)
    pkt = parse_packet(raw)
    assert pkt is not None
    assert pkt.datagram_version == 5
    # Unmoved: everything up to and including FogState at 36.
    assert pkt.lighting_cue == CueByte.SEARCHLIGHTS
    assert pkt.fog_state is False
    # New field, then the shifted tail.
    assert pkt.fog_remaining_cs == 1234
    assert pkt.strobe_state == StrobeSpeed.MEDIUM
    assert pkt.beat == BeatByte.STRONG
    assert pkt.keyframe == 28
    assert pkt.bonus_effect is False
    assert pkt.camera_cut_subject == 11
    assert pkt.camera_cut_priority == 1


def test_v5_beat_byte_does_not_leak_into_bonus_effect():
    """The exact regression: a held Beat=3 must not read as a bonus burst."""
    pkt = parse_packet(_v5_packet(cue=CueByte.SCORE, beat=BeatByte.WEAK))
    assert pkt is not None
    assert pkt.beat == BeatByte.WEAK
    assert pkt.bonus_effect is False


def test_v5_star_power_tail_is_two_bytes_later():
    raw = _v5_packet(fog_remaining_cs=50, star_power=[(255, True), (128, False)])
    pkt = parse_packet(raw)
    assert pkt is not None
    assert pkt.sp_player_count == 2
    assert pkt.star_power == [(255, True), (128, False)]
    assert pkt.sp_active_count == 1
    assert pkt.sp_active is True


def test_v5_and_v4_are_told_apart_by_version_not_length():
    """A v4 packet with one player and a v5 packet with none are both 51 bytes."""
    from test_sender import build_packet
    v4_raw = build_packet(strobe=StrobeSpeed.FAST, star_power=[(10, False)])
    v5_raw = build_packet(strobe=StrobeSpeed.FAST, fog_remaining_cs=0)
    assert len(v4_raw) == len(v5_raw) == 51
    assert parse_packet(v4_raw).strobe_state == StrobeSpeed.FAST
    assert parse_packet(v5_raw).strobe_state == StrobeSpeed.FAST


def test_v4_layout_is_unchanged_by_the_v5_branch():
    from test_sender import build_packet
    pkt = parse_packet(build_packet(cue=CueByte.FRENZY, strobe=StrobeSpeed.SLOW,
                                    beat=BeatByte.MEASURE, camera_subject=20,
                                    star_power=[(64, True)]))
    assert pkt is not None
    assert pkt.datagram_version == 4
    assert pkt.fog_remaining_cs == 0          # field absent on v4
    assert pkt.strobe_state == StrobeSpeed.SLOW
    assert pkt.beat == BeatByte.MEASURE
    assert pkt.camera_cut_subject == 20
    assert pkt.star_power == [(64, True)]


def test_unknown_future_version_parses_at_v5_offsets():
    """Versions past the newest known fall back to the newest known layout."""
    raw = bytearray(_v5_packet(strobe=StrobeSpeed.FASTEST, fog_remaining_cs=7))
    raw[4] = 99
    pkt = parse_packet(bytes(raw))
    assert pkt is not None
    assert pkt.strobe_state == StrobeSpeed.FASTEST
    assert pkt.fog_remaining_cs == 7


def test_truncated_v5_degrades_instead_of_raising():
    for size in range(MIN_PACKET_SIZE, 51):
        raw = bytearray(_v5_packet(cue=CueByte.STOMP, fog_remaining_cs=999))[:size]
        pkt = parse_packet(bytes(raw))
        assert pkt is not None, f"{size}-byte v5 packet returned None"
        assert pkt.lighting_cue == CueByte.STOMP   # valid below the cut
        assert pkt.sp_player_count == 0


def test_live_capture_packet_from_yarg_nightly():
    """Byte-for-byte v5 datagram captured off the wire on 2026-08-23.

    Score screen: cue=SCORE(31), StrobeOff(24) at offset 39, Beat fallback 3
    at 40, camera subject AllFar(3) at 48, zero star-power players.
    """
    raw = bytes([
        0x47, 0x52, 0x41, 0x59, 5, 3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        31, 0, 0, 0, 0, 24, 3, 0, 0, 0, 0, 0, 0, 0, 3, 0, 0,
    ])
    assert len(raw) == 51
    pkt = parse_packet(raw)
    assert pkt is not None
    assert pkt.datagram_version == 5
    assert pkt.lighting_cue == CueByte.SCORE
    assert pkt.fog_state is False
    assert pkt.fog_remaining_cs == 0
    assert pkt.strobe_state == StrobeSpeed.OFF     # 24, not 0
    assert pkt.beat == 3
    assert pkt.bonus_effect is False               # the solid-white regression
    assert pkt.camera_cut_subject == 3
    assert pkt.sp_player_count == 0


if __name__ == "__main__":
    # Standalone runner so the suite works without pytest installed.
    import traceback
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
