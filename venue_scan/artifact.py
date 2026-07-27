"""Render the venue index as a self-contained HTML page.

    .venv/bin/python -m venue_scan.artifact --index venue-index.jsonl --out venue.html

Everything is inlined — the Artifact CSP blocks external hosts — and the song
table is embedded as compact JSON so it can be filtered client-side.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from .classify import SongVenueRecord, richness
from .report import load_index, pack_of

#: Songs embedded in the browsable table. The full index stays in the JSONL.
TABLE_LIMIT = 600
TOP_CUES = 18


def summarise(records: list[dict]) -> dict:
    ok = [r for r in records if not r["error"]]
    errored = [r for r in records if r["error"]]

    by_source = Counter(r["venue_source"] for r in ok)
    packs: dict[str, list[dict]] = defaultdict(list)
    for record in ok:
        packs[pack_of(record)].append(record)

    cue_tables = {}
    for category in ("lighting", "post_processing", "stage", "performer", "camera_cuts"):
        events: Counter[str] = Counter()
        songs: Counter[str] = Counter()
        for record in ok:
            for cue, count in record["histograms"].get(category, {}).items():
                events[cue] += count
                songs[cue] += 1
        if events:
            cue_tables[category] = [
                {"cue": cue, "events": count, "songs": songs[cue]}
                for cue, count in events.most_common(TOP_CUES)
            ]

    ranked = sorted(ok, key=lambda r: richness(SongVenueRecord(**r)), reverse=True)
    table = [
        {
            "a": r["artist"][:60],
            "t": r["title"][:70],
            "s": r["venue_source"],
            "k": r["kind"],
            "p": pack_of(r),
            "l": r["counts"].get("lighting", 0),
            "pp": r["counts"].get("post_processing", 0),
            "c": r["counts"].get("camera_cuts", 0),
            "pe": r["counts"].get("performer", 0),
            "st": r["counts"].get("stage", 0),
            "af": r["autogen_fog"],
        }
        for r in ranked[:TABLE_LIMIT]
    ]

    return {
        "total": len(records),
        "ok": len(ok),
        "errors": len(errored),
        "error_reasons": Counter(r["error"].split(":")[0] for r in errored).most_common(),
        "by_source": {k: by_source.get(k, 0) for k in ("midi", "milo", "none")},
        "authored_lighting": sum(1 for r in ok if not r["autogen_lighting"]),
        "autogen_lighting": sum(1 for r in ok if r["autogen_lighting"]),
        "autogen_fog": sum(1 for r in ok if r["autogen_fog"]),
        "has_milo": sum(1 for r in ok if r["has_milo"]),
        "milo_with_anim": sum(1 for r in ok if r.get("milo_has_anim")),
        "truncated": sum(1 for r in ok if r["venue_truncated"]),
        "cuts_no_lighting": sum(
            1 for r in ok
            if r["venue_source"] != "none" and r["counts"].get("lighting", 0) == 0
        ),
        "by_kind": Counter(r["kind"] for r in records).most_common(),
        "by_format": Counter(str(r["chart_format"]) for r in records).most_common(),
        "packs": sorted(
            (
                {
                    "name": name,
                    "songs": len(group),
                    "authored": sum(1 for r in group if not r["autogen_lighting"]),
                    "milo": sum(1 for r in group if r["venue_source"] == "milo"),
                }
                for name, group in packs.items()
            ),
            key=lambda p: -p["songs"],
        ),
        "cues": cue_tables,
        "table": table,
    }


CATEGORY_LABELS = {
    "lighting": "Lighting cues",
    "post_processing": "Post-processing",
    "stage": "Stage effects",
    "performer": "Spotlights &amp; singalongs",
    "camera_cuts": "Camera cuts",
}


def render(data: dict) -> str:
    pct = lambda n: f"{n / data['ok'] * 100:.1f}" if data["ok"] else "0"  # noqa: E731
    src = data["by_source"]

    cue_sections = []
    for category, rows in data["cues"].items():
        peak = max(row["events"] for row in rows)
        bars = "\n".join(
            f'<tr><th scope="row"><code>{row["cue"]}</code></th>'
            f'<td class="bar"><span style="--w:{row["events"] / peak * 100:.1f}%"></span></td>'
            f'<td class="num">{row["events"]:,}</td>'
            f'<td class="num muted">{row["songs"]:,}</td></tr>'
            for row in rows
        )
        cue_sections.append(
            f'<section class="cues"><h3>{CATEGORY_LABELS[category]}</h3>'
            f'<table><thead><tr><th scope="col">Cue</th><th scope="col"></th>'
            f'<th scope="col" class="num">Events</th>'
            f'<th scope="col" class="num">Songs</th></tr></thead>'
            f"<tbody>{bars}</tbody></table></section>"
        )

    pack_rows = "\n".join(
        f'<tr><th scope="row"><code>{p["name"]}</code></th>'
        f'<td class="num">{p["songs"]:,}</td>'
        f'<td class="num">{p["authored"]:,}</td>'
        f'<td class="num muted">{p["authored"] / p["songs"] * 100:.0f}%</td>'
        f'<td class="num">{p["milo"] or ""}</td></tr>'
        for p in data["packs"]
    )

    kind_rows = "\n".join(
        f'<tr><th scope="row"><code>{kind}</code></th><td class="num">{count:,}</td></tr>'
        for kind, count in data["by_kind"]
    )
    format_rows = "\n".join(
        f'<tr><th scope="row"><code>{fmt}</code></th><td class="num">{count:,}</td></tr>'
        for fmt, count in data["by_format"]
    )

    error_note = (
        f'<p class="note">{data["errors"]:,} songs failed to parse '
        f'({data["errors"] / data["total"] * 100:.2f}%): '
        + ", ".join(f"<code>{reason}</code> ×{count:,}"
                    for reason, count in data["error_reasons"])
        + ".</p>"
        if data["errors"] else
        '<p class="note">Every song parsed cleanly.</p>'
    )

    return f"""<title>Library lighting inventory</title>
<style>
:root {{
  color-scheme: light dark;
  --page:      #f7f6f3;
  --surface:   #fffffe;
  --ink:       #16161b;
  --ink-2:     #55545e;
  --muted:     #8a8894;
  --rule:      #e3e1dc;
  --rule-firm: #c9c6c0;
  --midi:      #c2701a;
  --milo:      #7350c9;
  --none:      #a9a6b0;
  --bar:       #c2701a;
  --bar-track: #ebe8e2;
  --mono: ui-monospace, "SFMono-Regular", "SF Mono", Menlo, Consolas,
          "Liberation Mono", monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --page:      #101015;
    --surface:   #17171d;
    --ink:       #f1f0ee;
    --ink-2:     #b6b4bd;
    --muted:     #83818c;
    --rule:      #2a2a33;
    --rule-firm: #3d3c47;
    --midi:      #cf7c1c;
    --milo:      #8560db;
    --none:      #56545e;
    --bar:       #cf7c1c;
    --bar-track: #24242c;
  }}
}}
:root[data-theme="light"] {{
  --page: #f7f6f3; --surface: #fffffe; --ink: #16161b; --ink-2: #55545e;
  --muted: #8a8894; --rule: #e3e1dc; --rule-firm: #c9c6c0;
  --midi: #c2701a; --milo: #7350c9; --none: #a9a6b0;
  --bar: #c2701a; --bar-track: #ebe8e2;
}}
:root[data-theme="dark"] {{
  --page: #101015; --surface: #17171d; --ink: #f1f0ee; --ink-2: #b6b4bd;
  --muted: #83818c; --rule: #2a2a33; --rule-firm: #3d3c47;
  --midi: #cf7c1c; --milo: #8560db; --none: #56545e;
  --bar: #cf7c1c; --bar-track: #24242c;
}}

body {{
  background: var(--page);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.6;
  margin: 0;
  padding: clamp(1.5rem, 4vw, 4rem) clamp(1rem, 5vw, 3rem) 6rem;
}}
main {{ max-width: 62rem; margin: 0 auto; display: flex; flex-direction: column; gap: 3.5rem; }}

h1, h2, h3 {{ font-family: var(--mono); font-weight: 600; text-wrap: balance; margin: 0; }}
h1 {{ font-size: clamp(1.6rem, 4vw, 2.3rem); letter-spacing: -0.02em; line-height: 1.15; }}
h2 {{ font-size: 1.05rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-2); }}
h3 {{ font-size: 0.95rem; letter-spacing: 0.02em; }}
p {{ margin: 0; max-width: 62ch; color: var(--ink-2); }}
code {{ font-family: var(--mono); font-size: 0.88em; }}

header {{ display: flex; flex-direction: column; gap: 0.9rem;
  border-bottom: 2px solid var(--rule-firm); padding-bottom: 2rem; }}
.eyebrow {{ font-family: var(--mono); font-size: 0.78rem; letter-spacing: 0.16em;
  text-transform: uppercase; color: var(--muted); }}
.lede {{ font-size: 1.05rem; color: var(--ink-2); }}

section {{ display: flex; flex-direction: column; gap: 1.1rem; }}

/* Headline stats */
.stats {{ display: grid; gap: 1px; background: var(--rule);
  grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
  border: 1px solid var(--rule); }}
.stat {{ background: var(--surface); padding: 1.1rem 1.2rem;
  display: flex; flex-direction: column; gap: 0.25rem; }}
.stat .v {{ font-family: var(--mono); font-size: 1.85rem; font-weight: 600;
  font-variant-numeric: tabular-nums; letter-spacing: -0.02em; line-height: 1; }}
.stat .k {{ font-size: 0.8rem; color: var(--muted); letter-spacing: 0.03em; }}

/* Coverage bar */
.coverage {{ display: flex; height: 3rem; gap: 2px; }}
.coverage div {{ display: flex; align-items: center; padding-inline: 0.7rem;
  font-family: var(--mono); font-size: 0.8rem; font-weight: 600; color: #fff;
  white-space: nowrap; overflow: hidden; }}
.cv-midi {{ background: var(--midi); }}
.cv-milo {{ background: var(--milo); }}
.cv-none {{ background: var(--none); color: var(--surface); }}
.legend {{ display: flex; flex-wrap: wrap; gap: 1.2rem; font-size: 0.85rem;
  color: var(--ink-2); }}
.legend span {{ display: inline-flex; align-items: center; gap: 0.45rem; }}
.swatch {{ width: 0.7rem; height: 0.7rem; flex: none; }}

table {{ border-collapse: collapse; width: 100%; font-size: 0.88rem; }}
.scroll {{ overflow-x: auto; }}
th, td {{ text-align: left; padding: 0.34rem 0.7rem 0.34rem 0;
  border-bottom: 1px solid var(--rule); font-weight: 400; }}
thead th {{ font-family: var(--mono); font-size: 0.72rem; letter-spacing: 0.09em;
  text-transform: uppercase; color: var(--muted); border-bottom: 1px solid var(--rule-firm); }}
tbody th {{ font-weight: 400; color: var(--ink); }}
.num {{ text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums;
  white-space: nowrap; }}
.muted {{ color: var(--muted); }}

/* Cue frequency bars */
.cues table {{ table-layout: auto; }}
.cues th[scope="row"] {{ width: 1%; white-space: nowrap; }}
td.bar {{ width: 100%; padding-right: 0.9rem; }}
td.bar span {{ display: block; height: 0.55rem; width: var(--w);
  background: var(--bar); border-radius: 0 3px 3px 0; min-width: 2px; }}
.cue-grid {{ display: grid; gap: 2.5rem;
  grid-template-columns: repeat(auto-fit, minmax(21rem, 1fr)); }}

/* Song table */
.controls {{ display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: center; }}
input[type="search"], select {{ font: inherit; font-size: 0.88rem;
  padding: 0.4rem 0.6rem; background: var(--surface); color: var(--ink);
  border: 1px solid var(--rule-firm); border-radius: 2px; }}
input[type="search"] {{ flex: 1 1 16rem; min-width: 0; }}
:focus-visible {{ outline: 2px solid var(--midi); outline-offset: 2px; }}
.pill {{ font-family: var(--mono); font-size: 0.7rem; letter-spacing: 0.06em;
  text-transform: uppercase; padding: 0.1rem 0.45rem; border-radius: 2px;
  border: 1px solid currentColor; white-space: nowrap; }}
.pill.midi {{ color: var(--midi); }}
.pill.milo {{ color: var(--milo); }}
.pill.none {{ color: var(--muted); }}
.note {{ font-size: 0.85rem; color: var(--muted); }}
tfoot td {{ border: 0; padding-top: 0.8rem; color: var(--muted); font-size: 0.85rem; }}
</style>

<main>
<header>
  <p class="eyebrow">yarg-lighting &middot; venue_scan</p>
  <h1>What the song library actually lights</h1>
  <p class="lede">Every song in the library, classified by where its stage
  lighting comes from: authored in the chart's <code>VENUE</code> track,
  authored in a Rock&nbsp;Band Milo, or absent &mdash; in which case YARG
  synthesises it at play time.</p>
</header>

<section>
  <h2>Headline</h2>
  <div class="stats">
    <div class="stat"><span class="v">{data['total']:,}</span><span class="k">songs scanned</span></div>
    <div class="stat"><span class="v">{data['authored_lighting']:,}</span><span class="k">have authored lighting</span></div>
    <div class="stat"><span class="v">{pct(data['autogen_lighting'])}%</span><span class="k">get synthesised lighting</span></div>
    <div class="stat"><span class="v">{pct(data['autogen_fog'])}%</span><span class="k">get synthesised fog</span></div>
  </div>
  <p>Lighting and fog are gated separately. Fog generation runs even on fully
  authored venues, so most of the library gets fog it was never charted with.</p>
</section>

<section>
  <h2>Where lighting comes from</h2>
  <div class="coverage" role="img" aria-label="Venue source split: MIDI {src['midi']:,}, Milo {src['milo']:,}, none {src['none']:,}">
    <div class="cv-midi" style="flex:{max(src['midi'], 1)}">MIDI {pct(src['midi'])}%</div>
    <div class="cv-milo" style="flex:{max(src['milo'], 1)}">Milo</div>
    <div class="cv-none" style="flex:{max(src['none'], 1)}">None {pct(src['none'])}%</div>
  </div>
  <div class="legend">
    <span><i class="swatch" style="background:var(--midi)"></i> MIDI <code>VENUE</code> track &mdash; {src['midi']:,}</span>
    <span><i class="swatch" style="background:var(--milo)"></i> Milo animation data &mdash; {src['milo']:,}</span>
    <span><i class="swatch" style="background:var(--none)"></i> Nothing authored &mdash; {src['none']:,}</span>
  </div>
  <div class="scroll"><table>
    <tbody>
      <tr><th scope="row">Ship a Milo file</th><td class="num">{data['has_milo']:,}</td>
        <td class="muted">but only {data['milo_with_anim']:,} of those Milos contain a <code>song.anim</code> member &mdash; the rest are stubs with no animation data</td></tr>
      <tr><th scope="row">Take their venue from a Milo</th><td class="num">{src['milo']:,}</td>
        <td class="muted">the Milo path is effectively dead for this library: the one Milo with real data is shadowed by its own MIDI venue</td></tr>
      <tr><th scope="row">Authored venue, no lighting cues</th><td class="num">{data['cuts_no_lighting']:,}</td>
        <td class="muted">never load their Milo, and still receive synthesised lighting</td></tr>
      <tr><th scope="row">VENUE track truncated by YARG</th><td class="num">{data['truncated']:,}</td>
        <td class="muted">an unmatched note-off aborts the rest of the track, in the game as well as here</td></tr>
    </tbody>
  </table></div>
</section>

<section>
  <h2>Which cues the bridge must render well</h2>
  <p>Frequency across every authored venue in the library. This is the render
  priority list.</p>
  <div class="cue-grid">{''.join(cue_sections)}</div>
</section>

<section>
  <h2>By pack</h2>
  <div class="scroll"><table>
    <thead><tr><th scope="col">Pack</th><th scope="col" class="num">Songs</th>
      <th scope="col" class="num">Authored</th><th scope="col" class="num">Share</th>
      <th scope="col" class="num">Milo</th></tr></thead>
    <tbody>{pack_rows}</tbody>
  </table></div>
</section>

<section>
  <h2>Fixture candidates</h2>
  <p>The {len(data['table'])} richest authored venues, ranked by how much
  lighting, stage, and performer content they carry. Good demo songs and good
  regression fixtures.</p>
  <div class="controls">
    <input type="search" id="q" placeholder="Filter by artist, title, or pack" aria-label="Filter songs">
    <select id="src" aria-label="Filter by venue source">
      <option value="">All sources</option>
      <option value="midi">MIDI only</option>
      <option value="milo">Milo only</option>
    </select>
  </div>
  <div class="scroll"><table>
    <thead><tr>
      <th scope="col">Artist &middot; Title</th><th scope="col">Source</th>
      <th scope="col" class="num">Light</th><th scope="col" class="num">Post</th>
      <th scope="col" class="num">Camera</th><th scope="col" class="num">Perf</th>
      <th scope="col" class="num">Stage</th>
    </tr></thead>
    <tbody id="rows"></tbody>
    <tfoot><tr><td colspan="7" id="count"></td></tr></tfoot>
  </table></div>
</section>

<section>
  <h2>Coverage</h2>
  <div class="cue-grid">
    <div class="scroll"><table>
      <thead><tr><th scope="col">Packaging</th><th scope="col" class="num">Songs</th></tr></thead>
      <tbody>{kind_rows}</tbody>
    </table></div>
    <div class="scroll"><table>
      <thead><tr><th scope="col">Chart format</th><th scope="col" class="num">Songs</th></tr></thead>
      <tbody>{format_rows}</tbody>
    </table></div>
  </div>
  {error_note}
</section>
</main>

<script>
const SONGS = {json.dumps(data['table'], ensure_ascii=False, separators=(',', ':'))};
const rows = document.getElementById('rows');
const count = document.getElementById('count');
const q = document.getElementById('q');
const src = document.getElementById('src');

function draw() {{
  const needle = q.value.trim().toLowerCase();
  const source = src.value;
  const shown = SONGS.filter(s =>
    (!source || s.s === source) &&
    (!needle || (s.a + ' ' + s.t + ' ' + s.p).toLowerCase().includes(needle))
  );
  rows.innerHTML = shown.map(s => `
    <tr>
      <th scope="row">${{esc(s.a)}}${{s.a && s.t ? ' &middot; ' : ''}}<span class="muted">${{esc(s.t)}}</span></th>
      <td><span class="pill ${{s.s}}">${{s.s}}</span></td>
      <td class="num">${{s.l}}</td><td class="num">${{s.pp}}</td>
      <td class="num">${{s.c}}</td><td class="num">${{s.pe}}</td><td class="num">${{s.st}}</td>
    </tr>`).join('');
  count.textContent = `${{shown.length}} of ${{SONGS.length}} shown`;
}}

function esc(text) {{
  return String(text).replace(/[&<>"]/g, c =>
    ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}})[c]);
}}

q.addEventListener('input', draw);
src.addEventListener('change', draw);
draw();
</script>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=Path("venue-index.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("venue.html"))
    args = parser.parse_args(argv)

    data = summarise(load_index(args.index))
    args.out.write_text(render(data), encoding="utf-8")
    print(f"wrote {args.out} ({args.out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
