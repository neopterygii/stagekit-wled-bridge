"""Summarise a venue index into a markdown report and a fixture shortlist.

    .venv/bin/python -m venue_scan.report --index venue-index.jsonl --out venue-report.md

The questions this is meant to answer:

* how much of the library has authored lighting at all, and how much is YARG
  synthesising for us;
* which concrete cues are common enough that the bridge must render them well;
* which songs make good demo/regression fixtures;
* whether a Milo-only song exists (authored venue in the Milo, empty MIDI
  venue) — the fixture the evidence notes have been missing.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from .classify import SongVenueRecord, richness

TOP_FIXTURES = 40
TOP_CUES = 25


def load_index(path: Path) -> list[dict]:
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def pack_of(record: dict) -> str:
    """The top-level library folder a song lives in."""
    parts = Path(record["path"]).parts
    for i, part in enumerate(parts):
        if part == "clonehero" and i + 1 < len(parts):
            return parts[i + 1]
    return parts[1] if len(parts) > 1 else "?"


def _rank(record: dict) -> int:
    return richness(SongVenueRecord(**record))


def build_report(records: list[dict]) -> str:
    total = len(records)
    ok = [r for r in records if not r["error"]]
    errored = [r for r in records if r["error"]]

    by_source = Counter(r["venue_source"] for r in ok)
    by_kind = Counter(r["kind"] for r in records)
    by_format = Counter(str(r["chart_format"]) for r in records)

    autogen_lighting = sum(1 for r in ok if r["autogen_lighting"])
    autogen_fog = sum(1 for r in ok if r["autogen_fog"])
    truncated = [r for r in ok if r["venue_truncated"]]
    milo_present = sum(1 for r in ok if r["has_milo"])

    # The divergence that makes "authored venue" and "authored lighting"
    # different questions.
    cuts_without_lighting = [
        r for r in ok
        if r["venue_source"] != "none" and r["counts"].get("lighting", 0) == 0
    ]
    milo_only = [r for r in ok if r["venue_source"] == "milo"]

    lines: list[str] = []
    add = lines.append

    add("# Library lighting inventory\n")
    add(f"Scanned **{total:,}** songs; {len(errored):,} failed to parse "
        f"({len(errored) / total * 100:.2f}%).\n")

    add("## Where lighting comes from\n")
    add("| Venue source | Songs | Share |")
    add("|---|---:|---:|")
    for source in ("midi", "milo", "none"):
        count = by_source.get(source, 0)
        add(f"| `{source}` | {count:,} | {count / len(ok) * 100:.1f}% |")
    add("")
    add(f"- **{len(ok) - autogen_lighting:,}** songs "
        f"({(len(ok) - autogen_lighting) / len(ok) * 100:.1f}%) have authored "
        f"lighting cues; YARG synthesises lighting for the other "
        f"**{autogen_lighting:,}**.")
    add(f"- **{autogen_fog:,}** songs get synthesised fog "
        f"({autogen_fog / len(ok) * 100:.1f}%) — fog generation runs even on "
        f"fully authored venues.")
    add(f"- **{len(cuts_without_lighting):,}** songs have an authored venue but "
        f"*no* lighting cues. These never load their Milo and still receive "
        f"synthesised lighting.")
    milo_with_anim = [r for r in ok if r.get("milo_has_anim")]
    add(f"- **{milo_present:,}** songs ship a Milo, but only **{len(milo_with_anim):,}** "
        f"of those Milos contain a `song.anim` member at all — the rest are "
        f"stubs with no animation data. **{len(milo_only):,}** songs actually "
        f"take their venue from a Milo.")
    add(f"- **{len(truncated):,}** songs have a VENUE track that YARG truncates "
        f"early (an unmatched note-off aborts the rest of the track).\n")

    add("## By packaging\n")
    add("| Packaging | Songs | Authored lighting |")
    add("|---|---:|---:|")
    for kind, count in by_kind.most_common():
        authored = sum(
            1 for r in ok if r["kind"] == kind and not r["autogen_lighting"]
        )
        add(f"| `{kind}` | {count:,} | {authored:,} ({authored / count * 100:.0f}%) |")
    add("")

    add("| Chart format | Songs |")
    add("|---|---:|")
    for fmt, count in by_format.most_common():
        add(f"| `{fmt}` | {count:,} |")
    add("")

    add("## By pack\n")
    add("| Pack | Songs | Authored lighting | Milo-sourced |")
    add("|---|---:|---:|---:|")
    packs: dict[str, list[dict]] = defaultdict(list)
    for record in ok:
        packs[pack_of(record)].append(record)
    for pack, group in sorted(packs.items(), key=lambda kv: -len(kv[1])):
        authored = sum(1 for r in group if not r["autogen_lighting"])
        milo = sum(1 for r in group if r["venue_source"] == "milo")
        add(f"| `{pack}` | {len(group):,} | {authored:,} "
            f"({authored / len(group) * 100:.0f}%) | {milo:,} |")
    add("")

    add("## Which cues the bridge must render well\n")
    add("Frequency across every authored venue in the library.\n")
    for category in ("lighting", "post_processing", "stage", "performer", "camera_cuts"):
        counter: Counter[str] = Counter()
        songs_with: Counter[str] = Counter()
        for record in ok:
            histogram = record["histograms"].get(category, {})
            for cue, count in histogram.items():
                counter[cue] += count
                songs_with[cue] += 1
        if not counter:
            continue
        add(f"### {category}\n")
        add("| Cue | Events | Songs |")
        add("|---|---:|---:|")
        for cue, count in counter.most_common(TOP_CUES):
            add(f"| `{cue}` | {count:,} | {songs_with[cue]:,} |")
        add("")

    add("## Fixture shortlist\n")
    add("Richest authored venues, best candidates for demos and regression "
        "fixtures.\n")
    add("| Rank | Artist — Title | Source | Lighting | PostProc | Camera | "
        "Performer | Stage |")
    add("|---:|---|---|---:|---:|---:|---:|---:|")
    ranked = sorted(ok, key=_rank, reverse=True)[:TOP_FIXTURES]
    for i, record in enumerate(ranked, 1):
        counts = record["counts"]
        label = f"{record['artist']} — {record['title']}".strip(" —") or record["path"]
        add(f"| {i} | {label[:60]} | `{record['venue_source']}` | "
            f"{counts.get('lighting', 0)} | {counts.get('post_processing', 0)} | "
            f"{counts.get('camera_cuts', 0)} | {counts.get('performer', 0)} | "
            f"{counts.get('stage', 0)} |")
    add("")

    add("## Milo-only venues\n")
    if milo_only:
        add("Songs whose MIDI venue is empty and whose authored venue comes from "
            "the Milo. This is the fixture class the evidence notes were missing.\n")
        add("| Artist — Title | Path | Lighting | Camera |")
        add("|---|---|---:|---:|")
        for record in sorted(milo_only, key=_rank, reverse=True)[:TOP_FIXTURES]:
            label = f"{record['artist']} — {record['title']}".strip(" —")
            add(f"| {label[:44]} | `{Path(record['path']).name[:44]}` | "
                f"{record['counts'].get('lighting', 0)} | "
                f"{record['counts'].get('camera_cuts', 0)} |")
    else:
        add("**None exist in this library.** Two independent reasons, and both "
            "matter:\n")
        add(f"1. Of the {milo_present:,} songs that ship a Milo, "
            f"{milo_present - len(milo_with_anim):,} of those Milos are stubs "
            f"with no `song.anim` member — there is no venue data in them to "
            f"load.")
        add(f"2. The {len(milo_with_anim):,} Milo(s) that *do* carry animation "
            f"data belong to songs whose MIDI venue is already non-empty, so "
            f"YARG's `IsEmpty` gate blocks the Milo load anyway.\n")
        if milo_with_anim:
            add("Milos containing real animation data (shadowed by their own "
                "MIDI venue):\n")
            add("| Artist — Title | Path | MIDI venue source |")
            add("|---|---|---|")
            for record in milo_with_anim:
                label = f"{record['artist']} — {record['title']}".strip(" —")
                add(f"| {label[:44]} | `{Path(record['path']).name[:46]}` | "
                    f"`{record['venue_source']}` |")
            add("")
            add("To exercise the Milo code path, drive it directly from one of "
                "these rather than waiting for a song that triggers it "
                "naturally — no such song is present.")
    add("")

    if errored:
        add("## Parse failures\n")
        reasons = Counter(r["error"].split(":")[0] for r in errored)
        add("| Error | Songs |")
        add("|---|---:|")
        for reason, count in reasons.most_common():
            add(f"| `{reason}` | {count:,} |")
        add("")
        add("Examples:\n")
        for record in errored[:15]:
            add(f"- `{Path(record['path']).name[:70]}` — {record['error'][:110]}")
        add("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=Path("venue-index.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("venue-report.md"))
    args = parser.parse_args(argv)

    records = load_index(args.index)
    args.out.write_text(build_report(records), encoding="utf-8")
    print(f"wrote {args.out} from {len(records):,} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
