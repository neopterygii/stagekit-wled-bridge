"""Walk a song library and write one JSON record per song.

    .venv/bin/python -m venue_scan.scan --root /mnt/remotes/ragnar_data/syncthing/clonehero

Resumable: re-running with the same ``--out`` skips songs already present.
Parallel by default. Only container headers and chart members are read, so a
full pass never touches the library's audio.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from multiprocessing import Pool
from pathlib import Path

from .classify import classify
from .discover import SongRef, iter_songs

DEFAULT_ROOT = "/mnt/remotes/ragnar_data/syncthing/clonehero"


def _classify_ref(payload: tuple) -> dict:
    kind, path, asset_path = payload
    return classify(SongRef(kind=kind, path=path, asset_path=asset_path)).to_json()


def _existing_ids(out_path: Path) -> set[str]:
    if not out_path.exists():
        return set()
    done: set[str] = set()
    with out_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line)["song_id"])
            except (ValueError, KeyError):
                continue
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--out", default="venue-index.jsonl", type=Path)
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 4))
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after N songs (for smoke tests)")
    parser.add_argument("--restart", action="store_true",
                        help="ignore and overwrite any existing index")
    args = parser.parse_args(argv)

    out_path: Path = args.out
    if args.restart and out_path.exists():
        out_path.unlink()
    done = _existing_ids(out_path)
    if done:
        print(f"resuming: {len(done)} songs already indexed", file=sys.stderr)

    print(f"discovering songs under {args.root} ...", file=sys.stderr)
    started = time.monotonic()
    refs = [ref for ref in iter_songs(args.root) if ref.song_id not in done]
    if args.limit:
        refs = refs[:args.limit]
    print(
        f"discovered {len(refs)} songs to scan in {time.monotonic() - started:.1f}s",
        file=sys.stderr,
    )

    payloads = [(ref.kind, ref.path, ref.asset_path) for ref in refs]
    started = time.monotonic()
    written = 0
    errors = 0

    with out_path.open("a", encoding="utf-8") as handle:
        if args.workers > 1:
            with Pool(args.workers) as pool:
                results = pool.imap_unordered(_classify_ref, payloads, chunksize=32)
                written, errors = _drain(results, handle, len(payloads), started)
        else:
            results = (_classify_ref(payload) for payload in payloads)
            written, errors = _drain(results, handle, len(payloads), started)

    elapsed = time.monotonic() - started
    rate = f"{elapsed / written * 1000:.1f} ms/song" if written else "n/a"
    print(
        f"\nscanned {written} songs in {elapsed:.1f}s ({rate}); "
        f"{errors} errors ({errors / written * 100:.2f}%)" if written else "nothing to do",
        file=sys.stderr,
    )
    return 0


def _drain(results, handle, total: int, started: float) -> tuple[int, int]:
    written = 0
    errors = 0
    for record in results:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        written += 1
        errors += bool(record.get("error"))
        if written % 500 == 0:
            handle.flush()
            elapsed = time.monotonic() - started
            print(
                f"  {written}/{total}  {written / elapsed:.0f} songs/s  {errors} errors",
                file=sys.stderr,
            )
    return written, errors


if __name__ == "__main__":
    raise SystemExit(main())
