"""Read-only lighting monitor for a YARG party: spot anomalies live, report them at the end.

Watches the whole chain YARG -> bridge -> WLED while people play, and says so the moment something
looks wrong, so a fix can be planned from evidence instead of from "the lights did something weird
during that one song". Modelled on ~/yarg/tools/x32_party.py (the vocal-level party monitor).

    .venv/bin/python -m tools.party_monitor run --out out/sessions/<date>-party --songs mac-mini
    .venv/bin/python -m tools.party_monitor report out/sessions/<date>-party

Two modes, picked at start:
  * relay (bridge has `/api/anomalies`, v1.2+): the bridge runs the detectors itself, on every
    rendered frame, and keeps replayable evidence. This relays its findings, adds song titles, and
    raises only what the bridge cannot say about itself: bridge down, restarts, pid exhaustion.
  * local (older image): every detector runs here, from the 10 Hz status stream.

What it reads (it never writes to the bridge, WLED or YARG):
  * the bridge's `/events` SSE stream: the dashboard's own 10 Hz status snapshot (one more
    subscriber, no extra polling)
  * WLED `/json/info` every --wled-every s (default 10). Polling the ESP32 perturbs it, so keep it
    slow; see tools/RUNBOOK.md
  * the container's cgroup pid count over `ssh unraid` every 5 min (the PID-exhaustion bug)
  * optionally YARG's log on the rig over SSH (`--songs mac-mini`), for song titles

What it says, on stdout (so it can be watched for notifications):
  ALERT  the lights are broken right now: dark while playing, bridge/WLED down, latched, a second
         sender, the datagram parser reading garbage
  WARN   an anomaly worth a backlog look: unexpected flicker, a frozen or black strip under a lit
         cue, a stuck strobe, render stutter, packet loss
  SONG   one line per finished song: cues seen, auto-generated or authored, anomalies in it
  CLEAR  an ALERT condition went away (with how long it lasted)
NOTE lines (restarts, reboots, songs that never sent a cue) go to the log and the page only.

Every line carries a cause class, following the four-way split the rig findings use
(isolate-transport-vs-code-stutter): bridge (code), config, device (this WLED board), transport
(network), plus yarg (the sender) and chart (the song's own content).

Files in --out:
  events.jsonl      every episode start/end, SONG summary and NOTE, one JSON object per line
  samples.csv       one row per second: cue, scene, pps, fps, strip brightness, WLED fields
  evidence/*.jsonl  the 20 s of bridge snapshots before each new episode (capped per kind)
  report.md         written by `report`: findings grouped by class plus candidate updates

Phone page: `--web 8733` serves a read-only view on this machine's Tailscale address only.
"""

import argparse
import csv
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from datetime import datetime

from anomalies import (CLASS_HINT, EVIDENCE_PER_KIND, EVIDENCE_S, EXTERNAL_KINDS, KINDS,  # noqa: F401
                       Detector, _get, strip_level)
from tools.wled_soak import sample_wled

BRIDGE = "192.168.0.230:36180"
BRIDGE_IP = BRIDGE.split(":")[0]
WLED = "192.168.0.53"
CONTAINER = "stagekit-wled-bridge"

# ---------------------------------------------------------------- I/O

def sse_reader(url, q, stop):
    """Background thread: push (t, snapshot) from the bridge's SSE stream; reconnects forever."""
    while not stop.is_set():
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                for raw in resp:
                    if stop.is_set():
                        return
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data:"):
                        try:
                            q.put(("bridge", time.time(), json.loads(line[5:])))
                        except json.JSONDecodeError:
                            pass
        except (urllib.error.URLError, OSError) as e:
            q.put(("info", time.time(), f"bridge stream: {e}"))
        stop.wait(2)


def relay_reader(bridge, seq, q, stop):
    """Background thread: the bridge's own anomaly events, polled by sequence number."""
    while not stop.is_set():
        try:
            with urllib.request.urlopen(f"http://{bridge}/api/anomalies?since={seq}", timeout=5) as resp:
                data = json.loads(resp.read())
            for ev in data.get("events", []):
                seq = max(seq, ev.get("seq", 0))
                q.put(("relay", time.time(), ev))
        except (urllib.error.URLError, OSError, ValueError):
            pass  # bridge_down comes from the SSE stream going quiet
        stop.wait(2)


def bridge_anomalies(bridge):
    """The bridge's anomaly summary, or None when this image predates `/api/anomalies`."""
    try:
        with urllib.request.urlopen(f"http://{bridge}/api/anomalies?since=999999999", timeout=5) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


RELAY_SKIP = {"song", "song_start"}  # the watcher's own songs carry the Mac's titles


def wled_reader(host, every, q, stop):
    while not stop.is_set():
        q.put(("wled", time.time(), sample_wled(host)))
        stop.wait(every)


def pids_reader(every, q, stop):
    cmd = (f"id=$(docker inspect --format '{{{{.Id}}}}' {CONTAINER}) && "
           "cat /sys/fs/cgroup/docker/$id/pids.current /sys/fs/cgroup/docker/$id/pids.max")
    while not stop.is_set():
        try:
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "unraid", cmd],
                               capture_output=True, text=True, timeout=20)
            vals = r.stdout.split()
            if len(vals) == 2 and vals[1].isdigit():
                q.put(("pids", time.time(), (int(vals[0]), int(vals[1]))))
        except (OSError, subprocess.TimeoutExpired):
            pass
        stop.wait(every)


# YARG keeps writing the log named for the day it started, so follow the newest file.
MAC_LOGS = "~/Library/Application\\ Support/in.yarg.game/nightly/logs"
SONG_CMD = f'f=$(ls -t {MAC_LOGS}/*.log | head -n 1); timeout 900 tail -n0 -F "$f" 2>/dev/null'


def songs_reader(host, q, stop):
    while not stop.is_set():
        try:
            p = subprocess.Popen(["ssh", "-o", "ServerAliveInterval=15", "-o", "BatchMode=yes", host, SONG_CMD],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
            for line in p.stdout:
                if "Loading song " in line:
                    q.put(("title", time.time(), line.split("Loading song ", 1)[1].strip()))
            p.wait()
        except OSError as e:
            q.put(("info", time.time(), f"song follower: {e}"))
        stop.wait(5)


class Session:
    """Writes the session files and keeps the state the phone page shows."""

    def __init__(self, out):
        self.out = out
        os.makedirs(os.path.join(out, "evidence"), exist_ok=True)
        self.events = open(os.path.join(out, "events.jsonl"), "a", buffering=1)
        new = not os.path.exists(os.path.join(out, "samples.csv"))
        self.samples_f = open(os.path.join(out, "samples.csv"), "a", newline="", buffering=1)
        self.samples = csv.writer(self.samples_f)
        if new:
            self.samples.writerow(["time", "scene", "cue", "connected", "paused", "pps", "bpm", "strobe_hz",
                                   "output_live", "frames_sent", "strip_level", "gap_ms_max", "stalls",
                                   "wled_live", "wled_lip", "wled_http_ms", "wled_rssi", "wled_heap"])
        self.ring = deque()
        self.evidence_count = Counter()
        self.last_sample_t = 0.0
        self.lines = deque(maxlen=200)
        self.snap = {}
        self.wled = {}
        self.lock = threading.Lock()
        self.detector = None

    def record(self, t, snap):
        self.ring.append((t, snap))
        while self.ring and t - self.ring[0][0] > EVIDENCE_S:
            self.ring.popleft()
        with self.lock:
            self.snap = snap
        if t - self.last_sample_t >= 1.0:
            self.last_sample_t = t
            r, w = snap.get("render") or {}, self.wled
            lvl = strip_level(_get(snap, "preview", "strip", default=[]))
            self.samples.writerow([
                datetime.fromtimestamp(t).strftime("%H:%M:%S"), snap.get("scene"), snap.get("cue"),
                int(bool(snap.get("connected"))), int(bool(snap.get("paused"))), snap.get("packets_per_sec"),
                snap.get("bpm"), snap.get("strobe_hz"), int(bool(snap.get("output_live"))),
                _get(r, "ddp", "frames_sent"), round(lvl, 1) if lvl is not None else "",
                r.get("gap_ms_max"), r.get("stalls"), w.get("live"), w.get("lip"), w.get("http_ms"),
                w.get("rssi"), w.get("freeheap")])

    def emit(self, ev):
        ev["time"] = datetime.fromtimestamp(ev["t"]).strftime("%H:%M:%S")
        if ev.pop("evidence", False) and self.evidence_count[ev["kind"]] < EVIDENCE_PER_KIND and self.ring:
            self.evidence_count[ev["kind"]] += 1
            name = f"{datetime.fromtimestamp(ev['t']):%H%M%S}-{ev['kind']}.jsonl"
            with open(os.path.join(self.out, "evidence", name), "w") as f:
                for t, s in self.ring:
                    f.write(json.dumps({"t": round(t, 3), "snap": s}) + "\n")
                if self.wled:
                    f.write(json.dumps({"t": round(ev["t"], 3), "wled": self.wled}) + "\n")
            ev["evidence_file"] = f"evidence/{name}"
        self.events.write(json.dumps(ev) + "\n")
        level = ev["level"]
        text = f"{level} {ev['time']} [{ev['cls'] or '-'}] {ev['kind']}: {ev['msg']}" if level != "SONG" \
            else f"SONG {ev['time']} {ev['msg']}"
        with self.lock:
            self.lines.append(text)
        if level in ("ALERT", "WARN", "SONG", "CLEAR") and not ev.get("quiet"):
            print(text, flush=True)


def run(a):
    session = Session(a.out)
    remote = None if a.local else bridge_anomalies(a.bridge)
    det = Detector(bridge_ip=a.bridge.split(":")[0], kinds=EXTERNAL_KINDS if remote else None)
    q, stop = queue.Queue(), threading.Event()
    threads = [threading.Thread(target=sse_reader, args=(f"http://{a.bridge}/events", q, stop), daemon=True)]
    if remote:
        threads.append(threading.Thread(target=relay_reader, args=(a.bridge, remote.get("seq", 0), q, stop),
                                        daemon=True))
    else:
        threads.append(threading.Thread(target=wled_reader, args=(a.wled, a.wled_every, q, stop), daemon=True))
    if a.pids_every > 0:
        threads.append(threading.Thread(target=pids_reader, args=(a.pids_every, q, stop), daemon=True))
    if a.songs:
        threads.append(threading.Thread(target=songs_reader, args=(a.songs, q, stop), daemon=True))
    session.detector = det
    if a.web:
        from tools.party_web import serve
        try:
            print(f"PHONE page {serve(session, a.web, a.web_bind)}", flush=True)
        except (OSError, SystemExit) as e:
            print(f"NOTE phone page not started: {e}", flush=True)
    for th in threads:
        th.start()
    mode = "relaying the bridge's own detectors" if remote else f"detecting locally, WLED {a.wled}"
    print(f"NOTE watching bridge {a.bridge}, {mode}, songs {a.songs or 'from scene changes'}; "
          f"logging to {a.out}", flush=True)
    if remote:
        today = remote.get("today") or {}
        print(f"NOTE bridge has logged {today.get('alerts', 0)} alerts and {today.get('warnings', 0)} "
              f"warnings today before this watch; open now: "
              f"{', '.join(e['kind'] for e in remote.get('open', [])) or 'none'}", flush=True)
    end = time.time() + a.hours * 3600
    try:
        while time.time() < end:
            try:
                kind, t, payload = q.get(timeout=1.0)
            except queue.Empty:
                kind, t, payload = None, time.time(), None
            if kind == "bridge":
                session.record(t, payload)
                det.bridge(t, payload)
            elif kind == "wled":
                session.wled = payload
                det.wled(t, payload)
            elif kind == "pids":
                det.pids(t, *payload)
            elif kind == "title":
                det.title(t, payload)
            elif kind == "relay" and payload.get("kind") not in RELAY_SKIP:
                ev = dict(payload, source="bridge", song=det.song["label"] if det.song else payload.get("song"))
                if ev.get("evidence_file"):
                    ev["evidence_file"] = "bridge:" + ev["evidence_file"]
                if det.song is not None and ev.get("phase") in ("start", "point"):
                    det.song["anomalies"][ev["kind"]] += 1
                session.emit(ev)
            elif kind == "info":
                session.emit({"t": t, "level": "NOTE", "kind": "info", "cls": "", "msg": payload, "song": None})
            det.silence(time.time())
            for ev in det.drain():
                session.emit(ev)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        print(f"NOTE monitor stopped; run: .venv/bin/python -m tools.party_monitor report {a.out}", flush=True)


# ---------------------------------------------------------------- end-of-night report

def report(out):
    path = os.path.join(out, "events.jsonl")
    with open(path) as f:
        evs = [json.loads(line) for line in f if line.strip()]
    songs = [e for e in evs if e["kind"] == "song"]
    starts = [e for e in evs if e.get("phase") in ("start", "point") and e["level"] in ("ALERT", "WARN", "NOTE")]
    ends = {}
    for e in evs:
        if e.get("phase") == "end":
            ends.setdefault(e["kind"], []).append(e.get("duration") or 0)
    by_kind = {}
    for e in starts:
        by_kind.setdefault(e["kind"], []).append(e)

    lines = [f"# Lighting party report — {os.path.basename(os.path.normpath(out))}", ""]
    if evs:
        lines.append(f"{evs[0].get('time', '?')} – {evs[-1].get('time', '?')} · {len(songs)} songs · "
                     f"{sum(1 for e in starts if e['level'] == 'ALERT')} alerts · "
                     f"{sum(1 for e in starts if e['level'] == 'WARN')} warnings")
    lines += ["", "## Songs", "", "| time | song | min | lighting | cue changes | anomalies |", "|---|---|---|---|---|---|"]
    for s in songs:
        an = ", ".join(f"{k}×{n}" for k, n in sorted((s.get("anomalies") or {}).items())) or "—"
        lines.append(f"| {s.get('time')} | {s.get('label')} | {s.get('duration', 0) / 60:.1f} | "
                     f"{'auto-gen' if s.get('auto_gen') else 'authored'} | {sum((s.get('cues') or {}).values())} | {an} |")

    lines += ["", "## Findings by class", ""]
    order = {"ALERT": 0, "WARN": 1, "NOTE": 2}
    classes = {}
    for kind, items in by_kind.items():
        classes.setdefault(items[0]["cls"] or "other", []).append((kind, items))
    if not classes:
        lines += ["Nothing anomalous was detected.", ""]
    for cls in sorted(classes, key=lambda c: min(order[i[0]["level"]] for _, i in classes[c])):
        lines += [f"### {cls}", "", f"_Where a fix goes: {CLASS_HINT.get(cls, '—')}_", ""]
        for kind, items in sorted(classes[cls], key=lambda ki: (order[ki[1][0]["level"]], -len(ki[1]))):
            durs = ends.get(kind, [])
            song_names = sorted({i["song"] for i in items if i.get("song")})
            lines.append(f"- **{items[0]['level']} `{kind}`** ×{len(items)}"
                         + (f", total {sum(durs):.0f}s, longest {max(durs):.0f}s" if durs else "")
                         + f" — {KINDS.get(kind, ('', '', ''))[2]}")
            lines.append(f"  - first {items[0]['time']}: {items[0]['msg']}")
            if song_names:
                lines.append(f"  - songs: {', '.join(song_names[:8])}{' …' if len(song_names) > 8 else ''}")
            files = [i["evidence_file"] for i in items if i.get("evidence_file")]
            if files:
                lines.append(f"  - evidence: {', '.join(files[:3])}")
        lines.append("")

    lines += ["## Candidate updates", "",
              "Draft only: each needs the evidence read and the operator's word on what they saw before",
              "it becomes a BACKLOG.md item. Deploy/template steps are not backlog.", ""]
    n = 0
    for cls, kinds in classes.items():
        for kind, items in kinds:
            if items[0]["level"] == "NOTE" and kind != "no_cues":
                continue
            n += 1
            lines.append(f"{n}. `{kind}` ({cls}, ×{len(items)}): {KINDS.get(kind, ('', '', kind))[2]}. "
                         f"Next step: {CLASS_HINT.get(cls, 'investigate')}.")
    if not n:
        lines.append("None.")
    text = "\n".join(lines) + "\n"
    with open(os.path.join(out, "report.md"), "w") as f:
        f.write(text)
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="watch a party")
    r.add_argument("--out", required=True)
    r.add_argument("--bridge", default=BRIDGE)
    r.add_argument("--wled", default=WLED)
    r.add_argument("--wled-every", type=float, default=10.0)
    r.add_argument("--pids-every", type=float, default=300.0, help="0 disables the ssh unraid check")
    r.add_argument("--songs", metavar="SSH_HOST", help="follow YARG's log on this host for song titles")
    r.add_argument("--hours", type=float, default=8.0)
    r.add_argument("--web", type=int, metavar="PORT")
    r.add_argument("--web-bind")
    r.add_argument("--local", action="store_true", help="detect locally even if the bridge can")
    p = sub.add_parser("report", help="write <out>/report.md from a session")
    p.add_argument("out")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        run(a)
    else:
        print(report(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
