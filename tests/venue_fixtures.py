"""Builders for synthetic venue_scan fixtures.

Fixtures are generated rather than checked in: the library's charts are
copyrighted and, in the `.sng` case, encrypted, so committing real ones is out.
Synthesising them also lets a test state exactly which cue it is exercising.
"""

from __future__ import annotations

import struct


# ---------------------------------------------------------------------------
# MIDI
# ---------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


def track_chunk(name: str, events: list[tuple[int, bytes]]) -> bytes:
    """Build an MTrk from (delta, payload) pairs, prefixed with a track name."""
    body = bytearray()
    name_bytes = name.encode("utf-8")
    body += _varint(0) + b"\xff\x03" + _varint(len(name_bytes)) + name_bytes
    for delta, payload in events:
        body += _varint(delta) + payload
    body += _varint(0) + b"\xff\x2f\x00"
    return b"MTrk" + struct.pack(">I", len(body)) + bytes(body)


def note_on(note: int, velocity: int = 100) -> bytes:
    return bytes([0x90, note, velocity])


def note_off(note: int) -> bytes:
    return bytes([0x80, note, 0])


def text_event(text: str) -> bytes:
    payload = text.encode("utf-8")
    return b"\xff\x01" + _varint(len(payload)) + payload


def midi_file(tracks: list[bytes], division: int = 480) -> bytes:
    header = struct.pack(">HHH", 1, len(tracks), division)
    return b"MThd" + struct.pack(">I", len(header)) + header + b"".join(tracks)


def venue_midi(events: list[tuple[int, bytes]], extra_tracks: list[bytes] = ()) -> bytes:
    """A MIDI file with a `VENUE` track built from (delta, payload) pairs."""
    return midi_file([track_chunk("VENUE", events), *extra_tracks])


# ---------------------------------------------------------------------------
# SNG
# ---------------------------------------------------------------------------


def sng_file(members: dict[str, bytes], metadata: dict[str, str] | None = None,
             keys: bytes = bytes(range(16))) -> bytes:
    """Build a valid SNGPKG container around ``members``."""
    metadata = metadata or {}

    meta_blob = bytearray()
    for key, value in metadata.items():
        key_bytes = key.encode("utf-8")
        value_bytes = value.encode("utf-8")
        meta_blob += struct.pack("<I", len(key_bytes)) + key_bytes
        meta_blob += struct.pack("<I", len(value_bytes)) + value_bytes

    mask = bytes((keys[i % 16] ^ i) & 0xFF for i in range(256))

    # Header + both sections must be laid out before positions are known, so
    # size the listing table first with placeholder offsets.
    def build_listings(positions: dict[str, int]) -> bytes:
        blob = bytearray()
        for name, payload in members.items():
            name_bytes = name.encode("utf-8")
            blob += bytes([len(name_bytes)]) + name_bytes
            blob += struct.pack("<q", len(payload))
            blob += struct.pack("<q", positions[name])
        return bytes(blob)

    placeholder = build_listings({name: 0 for name in members})
    header_size = (
        26
        + 8 + 8 + len(meta_blob)     # metadata section
        + 8 + 8 + len(placeholder)   # listing section
    )

    positions = {}
    cursor = header_size
    for name, payload in members.items():
        positions[name] = cursor
        cursor += len(payload)

    listing_blob = build_listings(positions)
    assert len(listing_blob) == len(placeholder)

    out = bytearray()
    out += b"SNGPKG" + struct.pack("<I", 1) + keys
    out += struct.pack("<q", len(meta_blob) + 8)
    out += struct.pack("<Q", len(metadata))
    out += meta_blob
    out += struct.pack("<q", len(listing_blob) + 8)
    out += struct.pack("<Q", len(members))
    out += listing_blob
    for name, payload in members.items():
        out += bytes(b ^ mask[i % 256] for i, b in enumerate(payload))
    return bytes(out)


# ---------------------------------------------------------------------------
# STFS / CON
# ---------------------------------------------------------------------------

BLOCK = 0x1000
FIRSTBLOCK_OFFSET = 0xC000


def con_file(members: dict[str, bytes]) -> bytes:
    """Build a minimal single-section CON package.

    Only the consecutive-block layout is produced, and only enough header for
    the reader: an entry ID that yields shift 0, a one-block file table, and
    contiguous data blocks. Directory entries are synthesised for each path
    component so the reader's parent-index joining is exercised.
    """
    records: list[tuple[str, int, int, int, int]] = []  # name, flags, blocks, first, len
    index_of: dict[str, int] = {}

    def ensure_dir(path: str) -> int:
        if path in index_of:
            return index_of[path]
        parent, _, leaf = path.rpartition("/")
        parent_index = ensure_dir(parent) if parent else -1
        records.append((leaf, 0xC0, 0, 0, 0, parent_index))
        index_of[path] = len(records) - 1
        return index_of[path]

    # Directories first so parent indices always precede their children.
    for name in members:
        parent = name.rpartition("/")[0]
        if parent:
            ensure_dir(parent)

    next_block = 1  # block 0 holds the file table
    for name, payload in members.items():
        parent, _, leaf = name.rpartition("/")
        parent_index = index_of[parent] if parent else -1
        blocks = max(1, -(-len(payload) // BLOCK))
        records.append((leaf, 0x40, blocks, next_block, len(payload), parent_index))
        next_block += blocks

    table = bytearray()
    for leaf, flags, blocks, first, length, parent_index in records:
        record = bytearray(0x40)
        name_bytes = leaf.encode("utf-8")
        record[:len(name_bytes)] = name_bytes
        record[0x28] = len(name_bytes) | flags
        record[0x29:0x2C] = blocks.to_bytes(3, "little")
        record[0x2F:0x32] = first.to_bytes(3, "little")
        struct.pack_into(">h", record, 0x32, parent_index)
        struct.pack_into(">i", record, 0x34, length)
        table += record

    out = bytearray(FIRSTBLOCK_OFFSET)
    out[:4] = b"CON "
    # Entry ID chosen so (id + 0xFFF) & 0xF000 >> 12 == 0xB, giving shift 0.
    struct.pack_into(">I", out, 0x340, 0xB000)
    struct.pack_into("<H", out, 0x37C, 1)
    out[0x37E:0x381] = (0).to_bytes(3, "big")

    blocks_out = bytearray(table.ljust(BLOCK, b"\0"))
    for payload in members.values():
        padded = len(payload) + (-len(payload) % BLOCK)
        blocks_out += payload.ljust(padded, b"\0")
    return bytes(out) + bytes(blocks_out)
