"""Reader for Xbox 360 STFS packages (`.rb3con` / `_rb3con` / CON / LIVE / PIRS).

Port of the read path in YARG.Core's `IO/ConHandler/CONFile.cs` and
`CONFileStream.cs`. This is deliberately *not* a general extractor — it pulls
named members (the `.mid`, the `.milo_xbox`, `songs.dta`) and nothing else. No
hash verification, no decryption, no writes.

This supersedes `evidence/rb3con-extraction-tooling-2026-07-27.md`, which
concluded RB3CON was blocked here. That survey looked at general-purpose
extractors; YARG's own algorithm is small and self-contained.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

TAGS = (b"CON ", b"LIVE", b"PIRS")

METADATA_POSITION = 0x340
FILETABLE_BLOCKCOUNT_POSITION = 0x37C
FILETABLE_FIRSTBLOCK_POSITION = 0x37E
BYTES_PER_BLOCK = 0x1000
SIZEOF_FILELISTING = 0x40
FIRSTBLOCK_OFFSET = 0xC000
BLOCKS_PER_SECTION = 170
NUM_BLOCKS_SQUARED = BLOCKS_PER_SECTION * BLOCKS_PER_SECTION
BYTES_PER_HASH_ENTRY = 0x18
NEXT_BLOCK_HASH_OFFSET = 0x15

FLAG_CONSECUTIVE = 0x40
FLAG_DIRECTORY = 0x80


class StfsError(Exception):
    """Raised when a file is not a readable STFS package."""


@dataclass(frozen=True)
class ConListing:
    name: str
    flags: int
    num_blocks: int
    first_block: int
    length: int

    @property
    def is_directory(self) -> bool:
        return bool(self.flags & FLAG_DIRECTORY)

    @property
    def is_consecutive(self) -> bool:
        return bool(self.flags & FLAG_CONSECUTIVE)


def block_location(block: int, shift: int) -> int:
    """CONFileStream.CalculateBlockLocation — skip the interleaved hash tables."""
    adjust = 0
    if block >= BLOCKS_PER_SECTION:
        adjust += ((block // BLOCKS_PER_SECTION) + 1) << shift
        if block >= NUM_BLOCKS_SQUARED:
            adjust += ((block // NUM_BLOCKS_SQUARED) + 1) << shift
    return FIRSTBLOCK_OFFSET + (adjust + block) * BYTES_PER_BLOCK


class ConFile:
    """Lazily reads members out of an STFS package."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._handle = open(self.path, "rb")
        try:
            self.shift, self.listings = self._parse()
        except StfsError:
            self._handle.close()
            raise
        except (OSError, struct.error, IndexError, MemoryError) as exc:
            self._handle.close()
            raise StfsError(f"malformed STFS package: {exc}") from exc

    def _parse(self) -> tuple[int, dict[str, ConListing]]:
        handle = self._handle
        header = handle.read(0x400)
        if len(header) < 0x400 or header[:4] not in TAGS:
            raise StfsError("missing CON/LIVE/PIRS tag")

        # "If bit 12, 13 and 15 of the Entry ID are on, there are 2 hash tables
        # every 0xAA blocks" — CONFile.cs
        entry_id = struct.unpack_from(">I", header, METADATA_POSITION)[0]
        shift = 1 if ((entry_id + 0xFFF) & 0xF000) >> 0xC != 0xB else 0

        table_blocks = struct.unpack_from("<H", header, FILETABLE_BLOCKCOUNT_POSITION)[0]
        first_block = int.from_bytes(
            header[FILETABLE_FIRSTBLOCK_POSITION:FILETABLE_FIRSTBLOCK_POSITION + 3], "big"
        )

        table = bytearray()
        for i in range(table_blocks):
            handle.seek(block_location(first_block + i, shift))
            table += handle.read(BYTES_PER_BLOCK)

        ordered: list[ConListing] = []
        listings: dict[str, ConListing] = {}
        for offset in range(0, len(table), SIZEOF_FILELISTING):
            record = table[offset:offset + SIZEOF_FILELISTING]
            if len(record) < SIZEOF_FILELISTING or record[0] == 0:
                break
            name_len = record[0x28] & 0x3F
            name = record[:name_len].decode("utf-8", "replace")
            parent = struct.unpack_from(">h", record, 0x32)[0]
            if parent >= 0:
                if parent >= len(ordered):
                    raise StfsError(f"listing {name!r} references unknown parent {parent}")
                name = f"{ordered[parent].name}/{name}"
            listing = ConListing(
                name=name,
                flags=record[0x28] & 0xC0,
                num_blocks=int.from_bytes(record[0x29:0x2C], "little"),
                first_block=int.from_bytes(record[0x2F:0x32], "little"),
                length=struct.unpack_from(">i", record, 0x34)[0],
            )
            ordered.append(listing)
            listings[listing.name] = listing
        return shift, listings

    def read(self, name: str) -> bytes:
        """Return the bytes of a member, keyed by its full `parent/child` path."""
        return self._read_listing(self.listings[name])

    def _read_listing(self, listing: ConListing) -> bytes:
        handle = self._handle
        shift = self.shift
        data = bytearray()
        block = listing.first_block
        remaining = listing.length

        if listing.is_consecutive:
            while remaining > 0:
                run = min(
                    BLOCKS_PER_SECTION - (block % BLOCKS_PER_SECTION),
                    -(-remaining // BYTES_PER_BLOCK),
                )
                handle.seek(block_location(block, shift))
                chunk = handle.read(run * BYTES_PER_BLOCK)
                if not chunk:
                    raise StfsError(f"unexpected EOF reading {listing.name!r}")
                data += chunk[:remaining]
                remaining -= min(remaining, len(chunk))
                block += run
        else:
            while remaining > 0:
                location = block_location(block, shift)
                handle.seek(location)
                chunk = handle.read(BYTES_PER_BLOCK)
                if not chunk:
                    raise StfsError(f"unexpected EOF reading {listing.name!r}")
                data += chunk[:remaining]
                remaining -= min(remaining, BYTES_PER_BLOCK)
                if remaining <= 0:
                    break
                # Next block number lives in the hash table preceding this section.
                hash_table = location - ((block % BLOCKS_PER_SECTION) + 1) * BYTES_PER_BLOCK
                handle.seek(
                    hash_table
                    + (block % BLOCKS_PER_SECTION) * BYTES_PER_HASH_ENTRY
                    + NEXT_BLOCK_HASH_OFFSET
                )
                block = int.from_bytes(handle.read(3), "big")
        return bytes(data)

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> ConFile:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
