#!/usr/bin/env python3
"""Compare the bridge's datagram table against YARG's serialiser.

    python tools/check_yarg_schema.py                 # fetch YARG `dev` and compare
    python tools/check_yarg_schema.py --file X.cs     # compare a local copy

The bridge can only read YARG's datagram by knowing every offset, and YARG has
changed the layout without warning before (v5, 2026-08-23: a field inserted
mid-packet, a song played solid white). This reads the C# that writes the
datagram — `DataStreamController.SerializeAndSend` — and checks it field by
field, in order, against `protocol/schema.py`.

Exit status:
    0  upstream writes exactly what the newest schema version describes
    1  drift: the version constant or the field sequence differs
    2  could not check — fetch failed, or the source no longer has the shape
       this script knows how to read

Status 2 is deliberately not 0. A check that cannot run has not passed, and
upstream reorganising the file is itself worth a look.

Stdlib only. Run daily by `.github/workflows/upstream-drift.yml`.
"""

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protocol import schema  # noqa: E402

UPSTREAM_URL = ("https://raw.githubusercontent.com/YARC-Official/YARG/dev/"
                "Assets/Script/Integration/Data%20Stream/DataStreamController.cs")

OK, DRIFT, UNCHECKED = 0, 1, 2

# C# primitive -> struct code, as BinaryWriter.Write serialises each.
CSHARP_TYPES = {
    "byte": "B", "sbyte": "b", "bool": "?",
    "short": "h", "ushort": "H", "int": "i", "uint": "I",
    "long": "q", "ulong": "Q", "float": "f", "double": "d",
}

_WRITE = re.compile(r"_writer\.Write\((.*)\)\s*;")
_CAST = re.compile(r"^\(\s*(\w+)\s*\)\s*(.*)$", re.S)
_LOOP = re.compile(r"\b(for|foreach|while)\s*\(")


class Unreadable(Exception):
    """The source does not have the shape this script can interpret."""


def upstream_version(source: str) -> int:
    m = re.search(r"\bDATAGRAM_VERSION\s*=\s*(\d+)\s*;", source)
    if not m:
        raise Unreadable("no DATAGRAM_VERSION constant found")
    return int(m.group(1))


def _method_body(source: str, name: str) -> str:
    m = re.search(rf"\bvoid\s+{name}\s*\(", source)
    if not m:
        raise Unreadable(f"no {name}() method found")
    start = source.find("{", m.end())
    if start < 0:
        raise Unreadable(f"{name}() has no body")
    depth = 0
    for i in range(start, len(source)):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1:i]
    raise Unreadable(f"{name}() body is not brace-balanced")


def _declared_type(source: str, name: str) -> str:
    """The primitive type `name` is declared with, anywhere in the file."""
    found = set(re.findall(
        rf"\b({'|'.join(CSHARP_TYPES)})\s+{re.escape(name)}\b", source))
    if len(found) != 1:
        raise Unreadable(
            f"cannot tell the type of {name!r} "
            f"({'no declaration' if not found else 'declared as ' + '/'.join(sorted(found))})")
    return found.pop()


def _written(source: str, expr: str) -> tuple[str, str]:
    """(member name, struct code) for one `_writer.Write(expr)` argument."""
    expr = expr.strip()
    cast = None
    m = _CAST.match(expr)
    if m and m.group(1) in CSHARP_TYPES:
        cast, expr = m.group(1), m.group(2)
    member = re.search(r"\.\s*(\w+)", expr) or re.search(r"([A-Za-z_]\w*)", expr)
    if not member:
        raise Unreadable(f"cannot name what Write({expr}) writes")
    name = member.group(1)
    ctype = cast or _declared_type(source, name)
    return name, CSHARP_TYPES[ctype]


def upstream_writes(source: str) -> tuple[list, list]:
    """What SerializeAndSend writes: (fixed fields, per-loop-iteration fields)."""
    fixed, looped = [], []
    depth, loop_depths = 0, []
    pending_loop = False
    for line in _method_body(source, "SerializeAndSend").splitlines():
        code = line.split("//", 1)[0]
        if _LOOP.search(code):
            pending_loop = True
        m = _WRITE.search(code)
        if m:
            (looped if loop_depths else fixed).append(_written(source, m.group(1)))
        for ch in code:
            if ch == "{":
                depth += 1
                if pending_loop:
                    loop_depths.append(depth)
                    pending_loop = False
            elif ch == "}":
                if loop_depths and loop_depths[-1] == depth:
                    loop_depths.pop()
                depth -= 1
    if not fixed:
        raise Unreadable("SerializeAndSend() contains no _writer.Write calls")
    return fixed, looped


def schema_writes() -> tuple[list, list]:
    newest = schema.fields_for(schema.NEWEST_VERSION)
    return [(f.upstream, f.fmt) for f in newest], list(schema.STAR_POWER_ENTRY)


def _diff(label: str, ours: list, theirs: list) -> list[str]:
    if ours == theirs:
        return []
    out = [f"{label}: the bridge expects {len(ours)} fields, upstream writes {len(theirs)}"]
    for i in range(max(len(ours), len(theirs))):
        a = ours[i] if i < len(ours) else None
        b = theirs[i] if i < len(theirs) else None
        if a != b:
            show = lambda x: "(nothing)" if x is None else f"{x[0]}:{x[1]}"  # noqa: E731
            out.append(f"  #{i:<2} bridge {show(a):<32} upstream {show(b)}")
    return out


def compare(source: str) -> list[str]:
    """Every way upstream differs from the schema. Empty means in step."""
    problems = []
    version = upstream_version(source)
    if version != schema.NEWEST_VERSION:
        problems.append(
            f"DATAGRAM_VERSION is {version} upstream; the newest version "
            f"protocol/schema.py describes is {schema.NEWEST_VERSION}")
    fixed, looped = upstream_writes(source)
    ours_fixed, ours_looped = schema_writes()
    problems += _diff("fixed fields", ours_fixed, fixed)
    problems += _diff("star-power entry", ours_looped, looped)
    return problems


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "yarg-schema-check"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--file", help="read a local DataStreamController.cs")
    src.add_argument("--url", default=UPSTREAM_URL, help="where to fetch it from")
    args = ap.parse_args(argv)

    where = args.file or args.url
    try:
        source = (Path(args.file).read_text(encoding="utf-8", errors="replace")
                  if args.file else _fetch(args.url))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        print(f"UNCHECKED: could not read {where}: {exc}")
        return UNCHECKED

    try:
        problems = compare(source)
    except Unreadable as exc:
        print(f"UNCHECKED: {where} is not in a shape this check can read: {exc}")
        print("Upstream may have restructured the serialiser. Read it by hand.")
        return UNCHECKED

    if problems:
        print(f"DRIFT: {where} no longer matches protocol/schema.py")
        for line in problems:
            print(line)
        print("Add the new version's rows to protocol/schema.py (see its docstring).")
        return DRIFT

    n = len(schema.fields_for(schema.NEWEST_VERSION))
    print(f"OK: upstream is datagram v{schema.NEWEST_VERSION}, "
          f"{n} fixed fields + star-power table, in the order the bridge reads them")
    return OK


if __name__ == "__main__":
    sys.exit(main())
