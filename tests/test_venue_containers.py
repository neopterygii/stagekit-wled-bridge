"""Container readers: SNG masking/layout and STFS block walking."""

import struct

import pytest

from tests.venue_fixtures import con_file, midi_file, sng_file, track_chunk
from venue_scan.containers.dta import parse_songs_dta
from venue_scan.containers.sng import SngError, SngFile
from venue_scan.containers.stfs import ConFile, StfsError, block_location


# --------------------------------------------------------------------------
# SNG
# --------------------------------------------------------------------------


def test_sng_round_trips_members(tmp_path):
    chart = midi_file([track_chunk("VENUE", [])])
    path = tmp_path / "song.sng"
    path.write_bytes(sng_file({"notes.mid": chart, "album.jpg": b"\xff\xd8jpeg"}))

    with SngFile(path) as sng:
        assert set(sng.listings) == {"notes.mid", "album.jpg"}
        assert sng.read("notes.mid") == chart
        assert sng.read("album.jpg") == b"\xff\xd8jpeg"


def test_sng_reads_metadata(tmp_path):
    """Metadata is the song.ini equivalent and is stored unmasked."""
    path = tmp_path / "song.sng"
    path.write_bytes(
        sng_file({"notes.mid": b"MThd"}, metadata={"artist": "Test", "name": "Song"})
    )
    with SngFile(path) as sng:
        assert sng.metadata["artist"] == "Test"
        assert sng.metadata["name"] == "Song"


def test_sng_mask_is_offset_within_member_not_file(tmp_path):
    """Each member restarts the 256-byte mask, so identical members match."""
    payload = bytes(range(256)) * 3
    path = tmp_path / "song.sng"
    path.write_bytes(sng_file({"a.bin": payload, "b.bin": payload}))
    with SngFile(path) as sng:
        assert sng.read("a.bin") == payload
        assert sng.read("b.bin") == payload


def test_sng_rejects_foreign_file(tmp_path):
    path = tmp_path / "not.sng"
    path.write_bytes(b"NOTASNG" + bytes(64))
    with pytest.raises(SngError):
        SngFile(path)


def test_sng_listing_table_must_consume_exactly(tmp_path):
    """A truncated listing table is corruption, not something to guess through."""
    raw = bytearray(sng_file({"notes.mid": b"MThd"}))
    # Inflate the declared listing count without adding records.
    offset = raw.index(b"\x09notes.mid") - 8
    struct.pack_into("<Q", raw, offset, 4)
    path = tmp_path / "bad.sng"
    path.write_bytes(bytes(raw))
    with pytest.raises(SngError):
        SngFile(path)


# --------------------------------------------------------------------------
# STFS
# --------------------------------------------------------------------------


def test_con_extracts_members_with_joined_paths(tmp_path):
    chart = midi_file([track_chunk("VENUE", [])])
    members = {
        "songs/songs.dta": b"(dta)",
        "songs/foo/foo.mid": chart,
        "songs/foo/gen/foo.milo_xbox": b"\xaf\xde\xbe\xca" + bytes(32),
    }
    path = tmp_path / "pack_rb3con"
    path.write_bytes(con_file(members))

    with ConFile(path) as con:
        assert "songs/foo/foo.mid" in con.listings
        assert "songs/foo/gen/foo.milo_xbox" in con.listings
        assert con.read("songs/foo/foo.mid") == chart
        assert con.read("songs/songs.dta") == b"(dta)"


def test_con_extracts_multi_block_member(tmp_path):
    """Files longer than one 0x1000 block must walk consecutive blocks."""
    payload = bytes((i * 7) % 251 for i in range(0x2500))
    path = tmp_path / "pack_rb3con"
    path.write_bytes(con_file({"songs/foo/foo.mid": payload}))
    with ConFile(path) as con:
        assert con.read("songs/foo/foo.mid") == payload


def test_con_rejects_foreign_file(tmp_path):
    path = tmp_path / "not_rb3con"
    path.write_bytes(b"ZIP!" + bytes(0x400))
    with pytest.raises(StfsError):
        ConFile(path)


def test_block_location_skips_hash_tables():
    """Every 170 blocks a hash table is interleaved and must be stepped over."""
    assert block_location(0, 0) == 0xC000
    assert block_location(169, 0) == 0xC000 + 169 * 0x1000
    # Block 170 begins a new section, so one hash block is skipped.
    assert block_location(170, 0) == 0xC000 + 172 * 0x1000
    # shift=1 means two hash tables per section.
    assert block_location(170, 1) == 0xC000 + 174 * 0x1000


# --------------------------------------------------------------------------
# DTA
# --------------------------------------------------------------------------


def test_dta_parses_quoted_keys_and_crlf():
    """Real songs.dta quote their keys and split every token onto its own line."""
    raw = (
        b"(\r\n   'o1_song'\r\n   (\r\n      'name'\r\n      \"The Path\"\r\n   )\r\n"
        b"   ('artist' \"HIM\")\r\n   (\r\n      'song'\r\n      (\r\n"
        b"         'name'\r\n         \"songs/o1_song/o1_song\"\r\n      )\r\n   )\r\n)\r\n"
    )
    parsed = parse_songs_dta(raw)
    assert parsed == {
        "songs/o1_song/o1_song": {
            "shortname": "o1_song",
            "name": "The Path",
            "artist": "HIM",
        }
    }


def test_dta_display_name_is_not_shadowed_by_asset_path():
    """The nested (song (name ...)) must not be mistaken for the song title."""
    raw = b'(id (name "Real Title") (song (name "songs/id/id")))'
    parsed = parse_songs_dta(raw)
    assert parsed["songs/id/id"]["name"] == "Real Title"
