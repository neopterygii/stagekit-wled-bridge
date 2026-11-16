"""The datagram layout table, and the check that holds it to upstream.

`protocol/schema.py` derives every offset `parse_packet` reads. The offset
maps below are typed out by hand on purpose: they are the independent copy the
derivation is checked against, taken from YARG's serialiser and from a v5
datagram captured off the wire on 2026-08-23.

`tests/fixtures/DataStreamController_v5.cs` is YARG's file verbatim (LGPL-3.0,
YARC-Official/YARG `dev`, last changed in 1ee4fe52), kept so the drift check
itself is tested without the network.

Run: python -m pytest tests/test_packet_schema.py -v
"""

import re
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protocol import schema  # noqa: E402
from protocol.yarg_packet import (  # noqa: E402
    KNOWN_DATAGRAM_VERSIONS, MIN_PACKET_SIZE, PACKET_HEADER, YARGPacket,
    parse_packet,
)
from tools import check_yarg_schema as drift  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "DataStreamController_v5.cs"

_COMMON = {
    "header": 0, "datagram_version": 4, "platform": 5, "scene": 6, "paused": 7,
    "venue_size": 8, "bpm": 9, "song_section": 13, "guitar_notes": 14,
    "bass_notes": 15, "drum_notes": 16, "keys_notes": 17, "vocal_note": 18,
    "harmony0_note": 22, "harmony1_note": 26, "harmony2_note": 30,
    "lighting_cue": 34, "post_processing": 35, "fog_state": 36,
}
_V1_TAIL = {"strobe_state": 37, "beat": 38, "keyframe": 39, "bonus_effect": 40,
            "auto_gen": 41, "spotlight": 42, "singalong": 43}
_V3_CAMERA = {"camera_cut_constraint": 44, "camera_cut_priority": 45,
              "camera_cut_subject": 46}

EXPECTED_OFFSETS = {
    1: {**_COMMON, **_V1_TAIL},
    3: {**_COMMON, **_V1_TAIL, **_V3_CAMERA},
    4: {**_COMMON, **_V1_TAIL, **_V3_CAMERA, "sp_player_count": 47},
    5: {**_COMMON, "fog_remaining_cs": 37, "strobe_state": 39, "beat": 40,
        "keyframe": 41, "bonus_effect": 42, "auto_gen": 43, "spotlight": 44,
        "singalong": 45, "camera_cut_constraint": 46, "camera_cut_priority": 47,
        "camera_cut_subject": 48, "sp_player_count": 49},
}
EXPECTED_SIZE = {1: 44, 3: 47, 4: 49, 5: 51}


# ── the table ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("version", sorted(EXPECTED_OFFSETS))
def test_offsets_match_the_hand_written_map(version):
    lay = schema.layout(version)
    assert lay.offsets == EXPECTED_OFFSETS[version]
    assert lay.size == EXPECTED_SIZE[version]


def test_every_known_version_has_a_hand_written_map():
    assert set(EXPECTED_OFFSETS) == set(schema.KNOWN_VERSIONS)
    assert KNOWN_DATAGRAM_VERSIONS == schema.KNOWN_VERSIONS


def test_min_packet_size_is_the_oldest_core():
    assert MIN_PACKET_SIZE == schema.MIN_PACKET_SIZE == 44


def test_every_stored_field_exists_on_the_packet():
    attrs = set(YARGPacket().__dict__)
    for f in schema.FIELDS:
        if f.name != "header":
            assert f.name in attrs, f.name


def test_field_names_and_upstream_names_are_unique():
    assert len({f.name for f in schema.FIELDS}) == len(schema.FIELDS)
    assert len({f.upstream for f in schema.FIELDS}) == len(schema.FIELDS)


def test_layouts_are_precomputed_for_every_version_byte():
    assert len(schema.LAYOUTS) == 256
    assert schema.LAYOUTS[5].offsets == EXPECTED_OFFSETS[5]
    # newer than known -> newest known; older than known -> oldest known
    assert schema.LAYOUTS[99].offsets == EXPECTED_OFFSETS[5]
    assert schema.LAYOUTS[0].offsets == EXPECTED_OFFSETS[1]
    # a gap is laid out from the table: v2 predates the camera cut
    assert schema.LAYOUTS[2].offsets == EXPECTED_OFFSETS[1]


def test_the_shared_prefix_stops_at_the_first_moved_field():
    assert schema.PREFIX.end == 37
    assert "fog_state" in schema.PREFIX.names
    assert "strobe_state" not in schema.PREFIX.names


# ── the parser reads where the table says ───────────────────────────────


def _marked(version: int, players: int = 0) -> bytes:
    """A datagram whose every fixed field holds a value unique to that field."""
    lay = schema.layout(version)
    buf = bytearray(lay.size + 2 * players)
    for i, f in enumerate(schema.fields_for(version)):
        if f.name == "header":
            value = PACKET_HEADER
        elif f.name == "datagram_version":
            value = version
        elif f.name == "sp_player_count":
            value = players
        elif f.fmt == "f":
            value = 100.0 + i
        elif f.fmt == "?":
            value = True
        else:
            value = 100 + i
        struct.pack_into("<" + f.fmt, buf, lay.offsets[f.name], value)
    return bytes(buf)


@pytest.mark.parametrize("version", sorted(EXPECTED_OFFSETS))
def test_parser_reads_each_field_from_its_own_offset(version):
    pkt = parse_packet(_marked(version))
    assert pkt is not None
    for i, f in enumerate(schema.fields_for(version)):
        if f.name in ("header", "sp_player_count"):
            continue
        got = getattr(pkt, f.name)
        if f.name == "datagram_version":
            assert got == version
        elif f.fmt == "f":
            assert got == pytest.approx(100.0 + i), f.name
        elif f.fmt == "?":
            assert got is True, f.name
        else:
            assert got == 100 + i, f.name


def test_fields_a_version_lacks_keep_their_defaults():
    pkt = parse_packet(_marked(1))
    assert pkt.fog_remaining_cs == 0
    assert pkt.camera_cut_subject == 0
    assert pkt.sp_player_count == 0


def test_a_version_label_decides_the_layout_not_the_length():
    # v3 bytes with trailing data are still v3: nothing past the camera cut.
    pkt = parse_packet(_marked(3) + bytes([2, 0, 255, 1, 255, 1]))
    assert pkt.camera_cut_subject != 0
    assert pkt.sp_player_count == 0


def test_star_power_tail_follows_the_fixed_fields():
    for version in (4, 5):
        raw = bytearray(_marked(version, players=2))
        base = schema.layout(version).size
        raw[base:base + 4] = bytes([200, 1, 90, 0])
        pkt = parse_packet(bytes(raw))
        assert pkt.star_power == [(200, True), (90, False)]
        assert pkt.sp_active_count == 1


# ── a new version is one row ────────────────────────────────────────────


def test_a_hypothetical_v6_is_a_one_row_change(monkeypatch):
    """Insert a byte after `beat` in v6; nothing but the table changes."""
    rows = list(schema.FIELDS)
    at = [f.name for f in rows].index("beat") + 1
    rows.insert(at, schema.Field("beat_phase", "B", 6, None, "BeatPhase"))
    monkeypatch.setattr(schema, "FIELDS", tuple(rows))

    v5, v6 = schema.layout(5), schema.layout(6)
    assert v5.offsets == EXPECTED_OFFSETS[5]          # older versions untouched
    assert v6.offsets["beat_phase"] == 41
    for name, off in EXPECTED_OFFSETS[5].items():
        assert v6.offsets[name] == off + (1 if off > 40 else 0), name
    assert v6.size == 52


# ── the upstream drift check ────────────────────────────────────────────


@pytest.fixture(scope="module")
def upstream_source():
    return FIXTURE.read_text(encoding="utf-8")


def _run(tmp_path, source, capsys):
    path = tmp_path / "DataStreamController.cs"
    path.write_text(source, encoding="utf-8")
    code = drift.main(["--file", str(path)])
    return code, capsys.readouterr().out


def test_the_saved_upstream_file_matches_the_schema(capsys):
    assert drift.main(["--file", str(FIXTURE)]) == drift.OK
    assert "OK" in capsys.readouterr().out


def test_upstream_writes_are_read_in_order(upstream_source):
    fixed, looped = drift.upstream_writes(upstream_source)
    assert fixed[0] == ("Header", "I")
    assert fixed[19] == ("FogRemainingCentiseconds", "H")
    assert fixed[-1] == ("playerStarPowerCount", "H")
    assert looped == [("Amount", "B"), ("IsActive", "B")]


def test_an_inserted_field_is_drift(tmp_path, upstream_source, capsys):
    src = upstream_source.replace(
        "_writer.Write(message.Beat); //byte",
        "_writer.Write(message.Beat); //byte\n"
        "                _writer.Write((byte) message.Performer);")
    assert src != upstream_source
    code, out = _run(tmp_path, src, capsys)
    assert code == drift.DRIFT
    assert "Performer" in out


def test_a_reordered_field_is_drift(tmp_path, upstream_source, capsys):
    a = "_writer.Write((byte) message.Spotlight);"
    b = "_writer.Write((byte) message.Singalong);"
    src = (upstream_source.replace(a, "@@").replace(b, a).replace("@@", b))
    code, _out = _run(tmp_path, src, capsys)
    assert code == drift.DRIFT


def test_a_changed_type_is_drift(tmp_path, upstream_source, capsys):
    src = upstream_source.replace(
        "public ushort                             FogRemainingCentiseconds;",
        "public uint                               FogRemainingCentiseconds;")
    assert src != upstream_source
    code, out = _run(tmp_path, src, capsys)
    assert code == drift.DRIFT
    assert "FogRemainingCentiseconds" in out


def test_a_version_bump_alone_is_drift(tmp_path, upstream_source, capsys):
    src = re.sub(r"DATAGRAM_VERSION(\s*)=(\s*)5;", r"DATAGRAM_VERSION\1=\g<2>6;",
                 upstream_source)
    assert src != upstream_source
    code, out = _run(tmp_path, src, capsys)
    assert code == drift.DRIFT
    assert "DATAGRAM_VERSION is 6" in out


def test_a_changed_star_power_entry_is_drift(tmp_path, upstream_source, capsys):
    src = upstream_source.replace(
        "_writer.Write(playerStarPower[i].Amount);",
        "_writer.Write(playerStarPower[i].Amount);\n"
        "                    _writer.Write((byte) i);")
    assert src != upstream_source
    code, out = _run(tmp_path, src, capsys)
    assert code == drift.DRIFT
    assert "star-power entry" in out


@pytest.mark.parametrize("source", [
    "",
    "not C# at all",
    "private const byte DATAGRAM_VERSION = 5;",                       # no serialiser
    "private const byte DATAGRAM_VERSION = 5;\n"
    "void SerializeAndSend(DataMessage m) { Send(m); }",              # no writes
    "void SerializeAndSend(DataMessage m) { _writer.Write(m.X); }",   # no version
])
def test_an_unreadable_source_never_passes(tmp_path, source, capsys):
    code, out = _run(tmp_path, source, capsys)
    assert code == drift.UNCHECKED
    assert "UNCHECKED" in out


def test_an_untypeable_write_never_passes(tmp_path, upstream_source, capsys):
    src = upstream_source.replace("_writer.Write(message.Beat); //byte",
                                  "_writer.Write(message.Mystery);")
    code, _out = _run(tmp_path, src, capsys)
    assert code == drift.UNCHECKED


def test_a_missing_file_never_passes(tmp_path, capsys):
    assert drift.main(["--file", str(tmp_path / "absent.cs")]) == drift.UNCHECKED
    capsys.readouterr()


def test_a_failed_fetch_never_passes(monkeypatch, capsys):
    def boom(_url):
        raise OSError("network unreachable")
    monkeypatch.setattr(drift, "_fetch", boom)
    assert drift.main([]) == drift.UNCHECKED
    assert "UNCHECKED" in capsys.readouterr().out
