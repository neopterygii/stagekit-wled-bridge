"""Parser tests for the YARG datagram — v1/v3/v4 layouts + malformed guards.

Parsing offsets are the one thing that silently renders wrong lighting, so
these assert the exact byte layout confirmed against YARG's
DataStreamController.cs (v4 builder) and lock in the length-guarded v3/v4 reads.

Run: python -m pytest tests/ -v   (or: python -m pytest tests/test_yarg_packet.py)
"""

import struct
import sys
from pathlib import Path

# Allow running from the repo root without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protocol.yarg_packet import (  # noqa: E402
    parse_packet, YARGPacket, PACKET_HEADER, MIN_PACKET_SIZE,
    CameraCutSubject,
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
