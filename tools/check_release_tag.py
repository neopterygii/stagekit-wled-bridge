#!/usr/bin/env python3
"""Refuse a release tag that does not match the source it tags.

On a `v*` tag CI builds the image with BUILD_VERSION set from the tag, which
overrides `__version__`. That made a forgotten bump invisible in the one place
it could have been seen: the image tagged v1.2.0 would report "v1.2.0" on the
dashboard while its source still said 1.1.0, and every later `:latest` build
from main would go back to reporting 1.1.0. This check runs in CI's test job on
tag builds, so a mismatched tag never produces an image.

Three things must agree with the tag:

- `__version__` in `version.py`;
- the newest entry in `CHANGELOG.md`;
- that entry must carry a date, not the word "draft".

Usage:
    python tools/check_release_tag.py v1.2.0
    python tools/check_release_tag.py            # no tag: checks version.py
                                                 # against CHANGELOG.md only

Exit status 0 when consistent, 1 otherwise. Stdlib only, no network.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_SEMVER = r"\d+\.\d+\.\d+"
_VERSION_LINE = re.compile(r'^__version__\s*=\s*"([^"]*)"', re.MULTILINE)
# "## 1.2.0 — 2026-11-16" or "## 1.2.0 — draft". The dash may be em, en or "-".
_CHANGELOG_ENTRY = re.compile(
    r"^##\s+(" + _SEMVER + r")\s*(?:[—–-]\s*(.*?))?\s*$", re.MULTILINE)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def source_version(text: str) -> str:
    """`__version__` as written in version.py, or "" when it is not there.

    Read with a regex rather than imported: importing `version` shells out to
    `git describe`, and this only needs the literal.
    """
    match = _VERSION_LINE.search(text)
    return match.group(1) if match else ""


def newest_changelog_entry(text: str) -> tuple[str, str]:
    """(version, status) of the first entry in CHANGELOG.md, or ("", "")."""
    match = _CHANGELOG_ENTRY.search(text)
    if not match:
        return "", ""
    return match.group(1), (match.group(2) or "").strip()


def check(tag: str | None, version_text: str, changelog_text: str) -> list[str]:
    """Every way the release is inconsistent; empty means it may ship.

    `tag` is None for a non-release run, where only the two files are compared
    and an undated ("draft") changelog entry is allowed.
    """
    problems = []
    version = source_version(version_text)
    if not re.fullmatch(_SEMVER, version):
        problems.append(
            f"version.py: __version__ is {version!r}, expected MAJOR.MINOR.PATCH")

    entry, status = newest_changelog_entry(changelog_text)
    if not entry:
        problems.append("CHANGELOG.md: no '## X.Y.Z' entry found")
    elif entry != version:
        problems.append(
            f"CHANGELOG.md: newest entry is {entry}, but __version__ is {version}")

    if tag is None:
        return problems

    if not tag.startswith("v") or not re.fullmatch(_SEMVER, tag[1:]):
        problems.append(f"tag {tag!r} is not of the form vMAJOR.MINOR.PATCH")
    elif tag[1:] != version:
        problems.append(
            f"tag {tag} does not match __version__ = \"{version}\" in version.py; "
            "bump version.py in a commit on main and tag that commit instead")
    if entry and not _DATE.match(status):
        problems.append(
            f"CHANGELOG.md: entry {entry} is marked {status or 'with no date'!r}; "
            "replace it with the release date (YYYY-MM-DD) before tagging")
    return problems


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1 or (args and args[0] in ("-h", "--help")):
        print(__doc__)
        return 0 if args and args[0] in ("-h", "--help") else 1
    tag = args[0] if args else None
    problems = check(
        tag,
        (ROOT / "version.py").read_text(encoding="utf-8"),
        (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
    )
    for problem in problems:
        print(f"release check: {problem}", file=sys.stderr)
    if not problems:
        what = f"tag {tag}" if tag else "version.py and CHANGELOG.md"
        print(f"release check: {what} consistent")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
