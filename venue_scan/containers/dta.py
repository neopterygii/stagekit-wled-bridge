"""Minimal reader for Rock Band `songs.dta` metadata.

We only need three things per song: the shortname, the display name/artist, and
the `name` field that gives the in-package asset path
(`songs/<shortname>/<shortname>`). A full DTA parser is out of scope — this
tokenizes the s-expression enough to pull those out and gives up gracefully on
binary (DTB-encrypted) files.
"""

from __future__ import annotations

import re

#: `(song (name "songs/foo/foo") ...)` — real files wrap keys in single quotes
#: and split every token onto its own CRLF line, so be liberal about both.
_NAME_FIELD = re.compile(r"\(\s*'?name'?\s+(?:\"([^\"]*)\"|([^\s()]+))\s*\)")
_SIMPLE_FIELD = r"\(\s*'?{field}'?\s+(?:\"([^\"]*)\"|([^\s()]+))\s*\)"


def _tokenize_top_level(text: str):
    """Yield each top-level `(...)` block, which is one song entry."""
    depth = 0
    start = None
    in_string = False
    for i, char in enumerate(text):
        if in_string:
            if char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            if depth == 0:
                start = i
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and start is not None:
                yield text[start:i + 1]
                start = None
            elif depth < 0:
                depth = 0
    return


def _field(block: str, field: str) -> str | None:
    match = re.search(_SIMPLE_FIELD.format(field=re.escape(field)), block)
    if not match:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


def parse_songs_dta(data: bytes) -> dict[str, dict[str, str]]:
    """Return ``{asset_path: {shortname, name, artist}}``.

    ``asset_path`` is the value of the inner ``(song (name ...))`` field, e.g.
    ``songs/foo/foo``. Songs whose path cannot be determined are skipped;
    callers fall back to deriving paths from the package's file listing.
    """
    text = data.decode("utf-8", "replace")
    # Strip line comments (;) outside of strings — good enough for this format.
    text = re.sub(r'(?<!")\s;[^\n]*', " ", text)

    out: dict[str, dict[str, str]] = {}
    for block in _tokenize_top_level(text):
        shortname_match = re.match(r"\(\s*([^\s()]+)", block)
        if not shortname_match:
            continue
        shortname = shortname_match.group(1).strip("'\"")

        song_block = _extract_song_block(block)
        asset_path = None
        if song_block:
            name_match = _NAME_FIELD.search(song_block)
            if name_match:
                asset_path = name_match.group(1) or name_match.group(2)
        if not asset_path:
            continue

        # The nested (song (name ...)) would otherwise shadow the display name.
        outer = block.replace(song_block, " ") if song_block else block
        out[asset_path.strip("'\"")] = {
            "shortname": shortname,
            "name": _field(outer, "name") or "",
            "artist": _field(outer, "artist") or "",
        }
    return out


_SONG_BLOCK_START = re.compile(r"\(\s*'?song'?[\s(]")


def _extract_song_block(block: str) -> str | None:
    """Pull the nested `(song ...)` sub-block out of a song entry."""
    match = _SONG_BLOCK_START.search(block)
    if not match:
        return None
    index = match.start()
    depth = 0
    for i in range(index, len(block)):
        if block[i] == "(":
            depth += 1
        elif block[i] == ")":
            depth -= 1
            if depth == 0:
                return block[index:i + 1]
    return None
