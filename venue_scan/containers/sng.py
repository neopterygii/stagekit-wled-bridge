"""Reader for the YARG `.sng` container.

Port of YARG.Core's `IO/SngHandler/SngFile.cs` + `SngMask.cs` (spec:
https://github.com/mdsitton/SngFileFormat). Read-only, no dependencies.

Layout, all little-endian::

    0   6   magic "SNGPKG"
    6   4   uint32 version
    10  16  XOR key seed
    26  8   int64  metadata section length
    34  8   uint64 metadata pair count
    42  ..  metadata pairs: u32 keyLen, key, u32 valLen, val
    ..  8   int64  listing section length
    ..  8   uint64 listing count
    ..  ..  listings: u8 nameLen, name, int64 length, int64 position
    ..  ..  masked file data

Both section lengths are measured from their own count field, i.e. they
*include* those 8 bytes. Getting that wrong yields absurd lengths and an
immediate MemoryError, so `_read_section` centralises it.

Note the listing record stores **length before position** — the reverse of what
you'd guess.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"SNGPKG"
MASK_SIZE = 256
NUM_KEYS = 16


class SngError(Exception):
    """Raised when a file is not a readable SNG package."""


@dataclass(frozen=True)
class SngListing:
    position: int
    length: int


def _build_mask(keys: bytes) -> bytes:
    """SngMask.LoadMask: mask[i] = keys[i % 16] ^ i."""
    return bytes((keys[i % NUM_KEYS] ^ i) & 0xFF for i in range(MASK_SIZE))


def _read_section(handle) -> tuple[int, bytes]:
    """Read a (length, count, blob) section, returning the count and blob."""
    length = struct.unpack("<q", handle.read(8))[0]
    count = struct.unpack("<Q", handle.read(8))[0]
    if length < 8:
        raise SngError(f"section length {length} is too small")
    return count, handle.read(length - 8)


class SngFile:
    """Lazily reads members out of an SNG package.

    Only the header and listing table are read up front; member bytes are
    fetched (and unmasked) on demand, so scanning a library never touches the
    audio.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._handle = open(self.path, "rb")
        try:
            self.version, self.metadata, self.listings, self._mask = self._parse()
        except SngError:
            self._handle.close()
            raise
        except (OSError, struct.error, IndexError, MemoryError) as exc:
            self._handle.close()
            raise SngError(f"malformed SNG: {exc}") from exc

    def _parse(self):
        handle = self._handle
        header = handle.read(26)
        if len(header) < 26 or header[:6] != MAGIC:
            raise SngError("missing SNGPKG magic")
        version = struct.unpack_from("<I", header, 6)[0]
        keys = header[10:26]

        # Metadata pairs (unmasked). Cheap enough to keep — it is the song.ini.
        pair_count, metadata_blob = _read_section(handle)
        metadata = _parse_metadata(metadata_blob, pair_count)

        listing_count, listing_blob = _read_section(handle)
        listings = _parse_listings(listing_blob, listing_count)
        return version, metadata, listings, _build_mask(keys)

    def read(self, name: str) -> bytes:
        """Return the decrypted bytes of a member, keyed by lowercase name."""
        listing = self.listings[name]
        self._handle.seek(listing.position)
        data = self._handle.read(listing.length)
        mask = self._mask
        # Mask index is the offset within the member, not within the file.
        return bytes(byte ^ mask[i % MASK_SIZE] for i, byte in enumerate(data))

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> SngFile:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _parse_metadata(blob: bytes, count: int) -> dict[str, str]:
    """Parse the `song.ini`-equivalent key/value pairs."""
    out: dict[str, str] = {}
    pos = 0
    for _ in range(count):
        key_len = struct.unpack_from("<I", blob, pos)[0]
        pos += 4
        key = blob[pos:pos + key_len].decode("utf-8", "replace")
        pos += key_len
        value_len = struct.unpack_from("<I", blob, pos)[0]
        pos += 4
        value = blob[pos:pos + value_len].decode("utf-8", "replace")
        pos += value_len
        out[key.lower()] = value
    return out


def _parse_listings(blob: bytes, count: int) -> dict[str, SngListing]:
    out: dict[str, SngListing] = {}
    pos = 0
    for _ in range(count):
        name_len = blob[pos]
        pos += 1
        name = blob[pos:pos + name_len].decode("utf-8", "replace")
        pos += name_len
        length = struct.unpack_from("<q", blob, pos)[0]
        pos += 8
        position = struct.unpack_from("<q", blob, pos)[0]
        pos += 8
        out[name.lower()] = SngListing(position=position, length=length)
    if pos != len(blob):
        raise SngError(f"listing table over/underrun: consumed {pos} of {len(blob)}")
    return out
