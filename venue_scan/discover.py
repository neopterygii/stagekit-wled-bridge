"""Walk a song library and yield one :class:`SongRef` per playable song.

Four packagings are recognised, matching what YARG itself scans:

``sng``      a ``.sng`` container holding ``notes.mid``/``notes.chart``
``folder``   a directory with ``song.ini`` beside a chart file
``con``      an STFS package (``*_rb3con``, ``*.rb3con``, ``*_con``); one
             package may contain several songs
``con_dir``  an extracted CON tree, identified by ``songs/songs.dta``

`SongRef.load()` returns the bytes the classifier needs and nothing else — the
audio is never read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .containers.dta import parse_songs_dta
from .containers.sng import SngError, SngFile
from .containers.stfs import ConFile, StfsError

#: Chart members YARG recognises, in its preference order.
CHART_FILES = (
    ("notes.mid", "mid"),
    ("notes.midi", "midi"),
    ("notes.chart", "chart"),
    ("notes.txt", "ultrastar"),
)

CON_SUFFIXES = ("_rb3con", ".rb3con", "_con", ".con")

MILO_SUFFIXES = (".milo_xbox", ".milo")


@dataclass
class SongLoad:
    """What the classifier needs to inspect one song."""

    chart_format: str | None
    chart_bytes: bytes | None
    milo_bytes: bytes | None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SongRef:
    """A locatable song, before any venue parsing has happened."""

    kind: str
    path: str
    #: For CON packages/dirs: the in-package asset path, e.g. ``songs/foo/foo``.
    asset_path: str | None = None

    @property
    def song_id(self) -> str:
        return f"{self.path}::{self.asset_path}" if self.asset_path else self.path

    def load(self) -> SongLoad:
        if self.kind == "sng":
            return _load_sng(self.path)
        if self.kind == "folder":
            return _load_folder(Path(self.path))
        if self.kind == "con":
            return _load_con(self.path, self.asset_path)
        if self.kind == "con_dir":
            return _load_con_dir(Path(self.path), self.asset_path)
        raise ValueError(f"unknown song kind {self.kind!r}")


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _load_sng(path: str) -> SongLoad:
    with SngFile(path) as sng:
        chart_format = None
        chart_bytes = None
        for member, fmt in CHART_FILES:
            if member in sng.listings:
                chart_format, chart_bytes = fmt, sng.read(member)
                break
        milo_bytes = None
        for name in sng.listings:
            if name.endswith(MILO_SUFFIXES):
                milo_bytes = sng.read(name)
                break
        return SongLoad(chart_format, chart_bytes, milo_bytes, dict(sng.metadata))


def _load_folder(directory: Path) -> SongLoad:
    chart_format = None
    chart_bytes = None
    for member, fmt in CHART_FILES:
        candidate = directory / member
        if candidate.is_file():
            chart_format, chart_bytes = fmt, candidate.read_bytes()
            break
    milo_bytes = None
    for entry in sorted(directory.iterdir()):
        if entry.is_file() and entry.name.lower().endswith(MILO_SUFFIXES):
            milo_bytes = entry.read_bytes()
            break
    return SongLoad(chart_format, chart_bytes, milo_bytes, _read_song_ini(directory))


def _load_con(path: str, asset_path: str | None) -> SongLoad:
    with ConFile(path) as con:
        return _load_from_listings(
            asset_path,
            lambda name: name in con.listings,
            con.read,
            _con_dta(con),
        )


def _load_con_dir(root: Path, asset_path: str | None) -> SongLoad:
    songs_dir = root / "songs"

    def exists(name: str) -> bool:
        return (root / name).is_file()

    def read(name: str) -> bytes:
        return (root / name).read_bytes()

    dta = {}
    dta_path = songs_dir / "songs.dta"
    if dta_path.is_file():
        dta = parse_songs_dta(dta_path.read_bytes())
    return _load_from_listings(asset_path, exists, read, dta)


def _load_from_listings(asset_path, exists, read, dta) -> SongLoad:
    chart_bytes = None
    chart_format = None
    for suffix, fmt in ((".mid", "mid"), (".midi", "midi")):
        if exists(f"{asset_path}{suffix}"):
            chart_format, chart_bytes = fmt, read(f"{asset_path}{suffix}")
            break

    milo_bytes = None
    subname = asset_path.split("/")[1] if asset_path.count("/") >= 1 else None
    if subname:
        for suffix in MILO_SUFFIXES:
            candidate = f"songs/{subname}/gen/{subname}{suffix}"
            if exists(candidate):
                milo_bytes = read(candidate)
                break

    return SongLoad(chart_format, chart_bytes, milo_bytes, dict(dta.get(asset_path, {})))


def _con_dta(con: ConFile) -> dict[str, dict[str, str]]:
    if "songs/songs.dta" not in con.listings:
        return {}
    try:
        return parse_songs_dta(con.read("songs/songs.dta"))
    except (StfsError, ValueError):
        return {}


def _read_song_ini(directory: Path) -> dict[str, str]:
    """Read song.ini without configparser — these files are frequently invalid."""
    path = directory / "song.ini"
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    text = path.read_text("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#", "[")):
            continue
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip().lower()] = value.strip()
    return out


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def discover(root: str | Path) -> list[SongRef]:
    """Walk ``root`` and return every song found, in path order."""
    return list(iter_songs(root))


def iter_songs(root: str | Path):
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        directory = Path(dirpath)
        names = set(filenames)

        # An extracted CON tree: stop descending, it is not a chart folder.
        if "songs.dta" in names and directory.name == "songs":
            yield from _refs_for_con_dir(directory.parent)
            dirnames.clear()
            continue

        if any(chart in names for chart, _ in CHART_FILES) and "song.ini" in names:
            yield SongRef(kind="folder", path=str(directory))

        for filename in sorted(filenames):
            lower = filename.lower()
            if lower.endswith(".sng"):
                yield SongRef(kind="sng", path=str(directory / filename))
            elif lower.endswith(CON_SUFFIXES):
                yield from _refs_for_con(directory / filename)


def _refs_for_con(path: Path):
    """A CON package may hold a whole pack, so enumerate its songs."""
    try:
        with ConFile(path) as con:
            asset_paths = _asset_paths_from_listings(con.listings)
    except (StfsError, OSError):
        # Surface it as a single unreadable song rather than dropping it.
        yield SongRef(kind="con", path=str(path))
        return
    if not asset_paths:
        yield SongRef(kind="con", path=str(path))
        return
    for asset_path in asset_paths:
        yield SongRef(kind="con", path=str(path), asset_path=asset_path)


def _refs_for_con_dir(root: Path):
    listings = {}
    for dirpath, _dirnames, filenames in os.walk(root / "songs"):
        rel = Path(dirpath).relative_to(root)
        for filename in filenames:
            listings[str(rel / filename)] = None
    asset_paths = _asset_paths_from_listings(listings)
    if not asset_paths:
        yield SongRef(kind="con_dir", path=str(root))
        return
    for asset_path in asset_paths:
        yield SongRef(kind="con_dir", path=str(root), asset_path=asset_path)


def _asset_paths_from_listings(listings) -> list[str]:
    """Derive ``songs/<sn>/<sn>`` asset paths from the MIDI members present.

    Deriving from listings rather than the DTA means a package with a corrupt
    or DTB-encrypted `songs.dta` still gets scanned.
    """
    out = []
    for name in listings:
        normalized = name.replace("\\", "/")
        lower = normalized.lower()
        if not lower.endswith((".mid", ".midi")):
            continue
        if not lower.startswith("songs/"):
            continue
        out.append(normalized.rsplit(".", 1)[0])
    return sorted(set(out))
