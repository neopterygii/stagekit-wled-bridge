"""Read-only lighting monitor for a YARG party: spot anomalies live, report them at the end.

Watches the whole chain YARG -> bridge -> WLED while people play, and says so the moment something
looks wrong, so a fix can be planned from evidence instead of from "the lights did something weird
during that one song". Modelled on ~/yarg/tools/x32_party.py (the vocal-level party monitor).

    .venv/bin/python -m tools.party_monitor run --out out/sessions/<date>-party --songs mac-mini
    .venv/bin/python -m tools.party_monitor report out/sessions/<date>-party

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

from tools.wled_soak import sample_wled

BRIDGE = "192.168.0.230:36180"
BRIDGE_IP = BRIDGE.split(":")[0]
WLED = "192.168.0.53"
CONTAINER = "stagekit-wled-bridge"

# ---------------------------------------------------------------- tuning (all seconds unless noted)

SILENCE_S = 5.0            # no SSE event this long = bridge down
DARK_S = 5.0               # playing but no DDP frames leaving
BLACK_S = 4.0              # strip black under a cue that should be lit
FROZEN_S = 30.0            # lit strip identical while the song plays
STROBE_STUCK_S = 45.0
STROBE_OUTSIDE_S = 3.0     # strobe still on outside gameplay
BEAT_STALL_S = 10.0
LOW_PPS = 30.0             # YARG sends ~90/s in gameplay
LOW_PPS_S = 10.0
YARG_LOST_S = 10.0
LATCH_S = 15.0             # WLED still live this long after the bridge stopped (timeout is 2.5 s)
IDLE_FAIL_EXTRA_S = 60.0   # grace + this, still live with nobody sending
FLICKER_WINDOW_S = 2.0
FLICKER_SWINGS = 8         # minimum frame-to-frame alternations of FLICKER_DELTA in the window
FLICKER_DELTA = 0.35       # fraction of the window's own max brightness
BLACK_LEVEL = 3.0          # mean channel value (0-255) below which the strip reads black
STUTTER_GAP_X = 2.5        # render gap max over target
HEAP_LOW = 40000
RSSI_LOW = -75
HTTP_SLOW_MS = 500
PIDS_FRAC = 0.8
EVIDENCE_S = 20.0
EVIDENCE_PER_KIND = 5
REPEAT_PRINT_S = 60.0      # a kind re-printed at most this often; every episode is still logged

DARK_CUES = {"BLACKOUT_FAST", "BLACKOUT_SLOW", "BLACKOUT_SPOTLIGHT", "SILHOUETTES",
             "SILHOUETTES_SPOTLIGHT", "NO_CUE", "MENU", "SCORE"}
FLASHY_CUES = {"STROBE_FASTEST", "STROBE_FAST", "STROBE_MEDIUM", "STROBE_SLOW", "FRENZY", "STOMP",
               "FLARE_FAST", "FLARE_SLOW", "BIG_ROCK_ENDING", "BLACKOUT_FAST", "DISCHORD"}

# kind -> (level, class, what it most likely means / where a fix would go)
KINDS = {
    "bridge_down": ("ALERT", "bridge", "bridge status stream silent: container stopped or wedged"),
    "render_dead": ("ALERT", "bridge", "render thread dead; nothing reaches the strip until restart"),
    "dark_playing": ("ALERT", "bridge", "song playing but no DDP frames leaving the bridge "
                     "(output gate shut; the PID-exhaustion bug looked exactly like this)"),
    "parser_garbage": ("ALERT", "yarg", "datagram fields out of range: YARG layout probably "
                       "changed (nightly?) and the parser reads the wrong offsets"),
    "yarg_lost": ("ALERT", "yarg", "YARG stopped sending mid-song (crash, or the broadcast "
                  "left via the wrong interface)"),
    "wled_down": ("ALERT", "transport", "WLED not answering HTTP"),
    "wled_not_live": ("ALERT", "transport", "bridge is sending DDP but WLED is not in realtime mode: "
                      "frames are not arriving"),
    "second_sender": ("ALERT", "config", "another host is writing WLED's realtime buffer "
                      "(YALCY's WLED output?); frames interleave and look like desync"),
    "wled_latched": ("ALERT", "config", "WLED stuck in realtime after the bridge stopped: "
                     "realtime timeout latch; capture before power cycling"),
    "pids_high": ("ALERT", "bridge", "container pid count near its limit: unreaped children again"),
    "black_strip": ("WARN", "bridge", "strip black under a cue that should light it"),
    "frozen_strip": ("WARN", "bridge", "lit strip not changing while the song plays"),
    "flicker": ("WARN", "bridge", "rapid brightness swings under a cue that should not flash"),
    "strobe_stuck": ("WARN", "bridge", "strobe on far longer than a chart would hold it"),
    "strobe_outside": ("WARN", "bridge", "strobe still running outside gameplay"),
    "beat_stalled": ("WARN", "yarg", "beat counter stopped while the song plays; beat-locked "
                     "cues freeze"),
    "render_stutter": ("WARN", "bridge", "render thread missed frames (stalls/skips or long gaps)"),
    "ddp_errors": ("WARN", "transport", "DDP sends failing at the socket"),
    "low_pps": ("WARN", "transport", "datagrams arriving well below YARG's ~90/s"),
    "idle_failed": ("WARN", "bridge", "nobody sending, yet the bridge never handed WLED back to idle"),
    "held_orphan": ("WARN", "bridge", "cue still held long after the sender left"),
    "wled_heap_low": ("WARN", "device", "WLED free heap low"),
    "wled_weak_wifi": ("WARN", "transport", "WLED WiFi signal weak"),
    "wled_slow_http": ("WARN", "device", "WLED slow to answer HTTP (main loop busy?)"),
    "bridge_restart": ("NOTE", "bridge", "bridge restarted (counters reset or image changed)"),
    "wled_reboot": ("NOTE", "device", "WLED rebooted; the bridge's cached power state may be stale"),
    "no_cues": ("NOTE", "chart", "a whole song without a lighting cue"),
    "long_blackout": ("NOTE", "chart", "blackout cue held a long time; check it is authored"),
}

# What a fix would most likely touch, per class; the report's candidate-update section.
CLASS_HINT = {
    "bridge": "bridge code: replay the evidence through replay/ and pin the behaviour with a test",
    "config": "rig configuration: WLED settings or a second app's output, not repo code",
    "device": "this WLED board: watch it across sessions; weighs on the QuinLED decision",
    "transport": "network path: WiFi/broadcast; goes away with ethernet if it is transport",
    "yarg": "YARG side: confirm against YARG source/version before touching the parser",
    "chart": "song content: run venue_scan on the song before calling it a bug",
}


# ---------------------------------------------------------------- pure helpers (tested)

def strip_level(strip):
    """Mean channel value 0-255 of a flat RGB list, or None for no data."""
    return sum(strip) / len(strip) if strip else None


def count_alternations(levels, delta):
    """Frame-to-frame up/down/up steps of at least `delta`: the flicker signature.

    A beat pulse rises once and decays over several frames, so it alternates about once a beat;
    a fade or sweep moves one way. Flicker flips direction on consecutive frames.
    """
    d = [b - a for a, b in zip(levels, levels[1:])]
    return sum(1 for x, y in zip(d, d[1:]) if abs(x) >= delta and abs(y) >= delta and (x > 0) != (y > 0))


def flicker_threshold(bpm, window_s=FLICKER_WINDOW_S):
    """Alternations needed in the window: well above what one pulse per beat would make."""
    beats = max(bpm, 0) / 60 * window_s
    return max(FLICKER_SWINGS, int(2 * beats) + 2)


def _get(d, *path, default=None):
    for p in path:
        if not isinstance(d, dict):
            return default
        d = d.get(p)
    return default if d is None else d


class Episode:
    __slots__ = ("since", "open", "msg", "pending")

    def __init__(self):
        self.since = None      # when the condition first held
        self.open = False      # announced
        self.msg = ""
        self.pending = None


class Detector:
    """Turns bridge snapshots, WLED samples and host readings into episodes.

    Pure: all input carries its own timestamp and every output is returned, so tests feed it
    synthetic timelines. An episode opens once its condition has held for its minimum duration,
    and closes when the condition stops.
    """

    def __init__(self, bridge_ip=BRIDGE_IP):
        self.bridge_ip = bridge_ip
        self.eps = {}
        self.last_printed = {}
        self.out = []
        self.song = None
        self.songs = 0
        self.pending_title = None
        self.prev = None           # previous bridge snapshot
        self.last_event_t = None
        self.frames_t = None       # last time ddp frames advanced
        self.frames = None
        self.beat_t = None
        self.beat_key = None
        self.strip_t = None
        self.strip_sig = None
        self.levels = deque()      # (t, level) for flicker
        self.stats_prev = None
        self.wled_prev = None
        self.disconnected_t = None
        self.connected_ever = False
        self.last_scene = None
        self._lost_mid_song = False
        self.started_t = None

    # -- episode bookkeeping

    def _emit(self, t, level, kind, msg, **extra):
        cls = KINDS[kind][1] if kind in KINDS else extra.pop("cls", "")
        ev = {"t": round(t, 2), "level": level, "kind": kind, "cls": cls, "msg": msg,
              "song": self.song["label"] if self.song else None}
        ev.update(extra)
        self.out.append(ev)
        return ev

    def cond(self, t, kind, active, min_s=0.0, msg=""):
        ep = self.eps.setdefault(kind, Episode())
        level = KINDS[kind][0]
        if active:
            if ep.since is None:
                ep.since = t
            ep.msg = msg or ep.msg
            if not ep.open and t - ep.since >= min_s:
                ep.open = True
                quiet = t - self.last_printed.get(kind, -1e9) < REPEAT_PRINT_S
                if not quiet:
                    self.last_printed[kind] = t
                self._emit(t, level, kind, ep.msg or KINDS[kind][2], phase="start", quiet=quiet,
                           evidence=True)
                if self.song is not None:
                    self.song["anomalies"][kind] += 1
        else:
            if ep.open:
                self._emit(t, "CLEAR" if level == "ALERT" else "END", kind,
                           f"{kind} cleared after {t - ep.since:.0f}s", phase="end",
                           duration=round(t - ep.since, 1), quiet=level != "ALERT")
            ep.since, ep.open = None, False

    def once(self, t, kind, msg):
        """A point event (no duration): restarts, reboots."""
        self._emit(t, KINDS[kind][0], kind, msg, phase="point", evidence=KINDS[kind][0] != "NOTE")
        if self.song is not None:
            self.song["anomalies"][kind] += 1

    def drain(self):
        out, self.out = self.out, []
        return out

    # -- songs

    def title(self, t, title):
        self.pending_title = title

    def _song_start(self, t, snap):
        self.songs += 1
        label = self.pending_title or f"song {self.songs}"
        self.pending_title = None
        self.song = {"label": label, "start": t, "cues": Counter(), "changes": 0, "cue": None,
                     "auto_gen": bool(snap.get("auto_gen")), "anomalies": Counter(),
                     "black_s": 0.0, "lit_s": 0.0, "frames0": _get(snap, "render", "ddp", "frames_sent", default=0),
                     "max_strobe_s": 0.0, "strobe_since": None}
        self._emit(t, "NOTE", "song_start", f"song start: {label}", cls="")

    def _song_end(self, t, snap):
        s, self.song = self.song, None
        dur = t - s["start"]
        frames = (_get(snap, "render", "ddp", "frames_sent", default=0) or 0) - (s["frames0"] or 0)
        cues = ", ".join(f"{c}x{n}" for c, n in s["cues"].most_common(6)) or "none"
        anomalies = ", ".join(f"{k}x{n}" for k, n in sorted(s["anomalies"].items())) or "none"
        if dur >= 30 and set(s["cues"]) <= {"NO_CUE", "MENU", "SCORE"}:
            self._emit(t, "NOTE", "no_cues", f"{s['label']}: no lighting cue in {dur / 60:.1f} min", phase="point")
            s["anomalies"]["no_cues"] += 1
            anomalies = ", ".join(f"{k}x{n}" for k, n in sorted(s["anomalies"].items()))
        lit = s["lit_s"] + s["black_s"]
        self._emit(t, "SONG", "song", (
            f"{s['label']} | {dur / 60:.1f} min | {'AUTO-GEN' if s['auto_gen'] else 'authored'} | "
            f"{s['changes']} cue changes: {cues} | out {frames / dur if dur else 0:.0f} fps | "
            f"black {100 * s['black_s'] / lit if lit else 0:.0f}% | anomalies: {anomalies}"),
            cls="", label=s["label"], duration=round(dur, 1), auto_gen=s["auto_gen"],
            cues=dict(s["cues"]), anomalies=dict(s["anomalies"]))

    # -- inputs

    def bridge(self, t, snap):
        prev = self.prev
        self.prev = snap
        dt = t - self.last_event_t if self.last_event_t is not None else 0.0
        self.last_event_t = t
        self.cond(t, "bridge_down", False)

        scene = snap.get("scene")
        connected = bool(snap.get("connected"))
        paused = bool(snap.get("paused"))
        cue = snap.get("cue") or ""
        playing = connected and scene == "Gameplay" and not paused
        render = snap.get("render") or {}
        ddp = render.get("ddp") or {}
        frames = ddp.get("frames_sent", snap.get("ddp_frames_sent"))
        strip = _get(snap, "preview", "strip", default=[])
        level = strip_level(strip) if _get(snap, "preview", "source") == "live" else None

        # restarts
        if prev is not None:
            if (snap.get("packets_received") or 0) < (prev.get("packets_received") or 0) \
                    or _get(snap, "version", "commit") != _get(prev, "version", "commit"):
                self.once(t, "bridge_restart", f"bridge restarted, now {_get(snap, 'version', 'label', default='?')}")
                self.stats_prev = None

        # songs
        if scene == "Gameplay" and self.last_scene != "Gameplay" and connected:
            self._song_start(t, snap)
        elif self.song is not None and (scene != "Gameplay" or not connected):
            self._song_end(t, snap)
        self.last_scene = scene if connected else None
        if self.song is not None:
            s = self.song
            if cue != s["cue"]:
                s["cues"][cue] += 1
                s["changes"] += s["cue"] is not None
                s["cue"] = cue
            if level is not None and playing:
                s["black_s" if level < BLACK_LEVEL else "lit_s"] += dt

        # sender
        if connected:
            self.connected_ever = True
            self.disconnected_t = None
        elif self.disconnected_t is None:
            self.disconnected_t = t
        gone_s = t - self.disconnected_t if self.disconnected_t is not None else 0.0
        mid_song = prev is not None and prev.get("scene") == "Gameplay" and not prev.get("paused")
        if not connected and self.connected_ever and mid_song and gone_s == 0.0:
            self._lost_mid_song = True
        if connected:
            self._lost_mid_song = False
        self.cond(t, "yarg_lost", not connected and self._lost_mid_song,
                  YARG_LOST_S)

        # render + output
        self.cond(t, "render_dead", render and (not render.get("alive", True) or bool(render.get("fatal_error"))),
                  0, f"render thread dead: {render.get('fatal_error') or 'not alive'}")
        if frames is not None and frames != self.frames:
            self.frames, self.frames_t = frames, t
        stalled = self.frames_t is None or t - self.frames_t
        self.cond(t, "dark_playing", playing and (stalled is True or stalled >= DARK_S), 0,
                  f"playing {cue} but no DDP frames for {DARK_S:.0f}s "
                  f"(output_live={snap.get('output_live')}, wled_power={_get(snap, 'wled_power', 'mode')})")
        self.cond(t, "low_pps", playing and (snap.get("packets_per_sec") or 0) < LOW_PPS, LOW_PPS_S,
                  f"only {snap.get('packets_per_sec')} datagrams/s in gameplay")

        stats = (render.get("stalls") or 0, render.get("skipped") or 0, ddp.get("send_errors") or 0)
        if self.stats_prev is not None:
            d_stall, d_skip, d_err = (a - b for a, b in zip(stats, self.stats_prev))
            gap, target = render.get("gap_ms_max") or 0, render.get("target_ms") or 16.7
            self.cond(t, "render_stutter", playing and (d_stall > 0 or d_skip > 0 or gap > STUTTER_GAP_X * target),
                      0, f"render: +{d_stall} stalls +{d_skip} skipped, gap max {gap} ms "
                      f"(target {target}), work max {render.get('work_ms_max')} ms")
            self.cond(t, "ddp_errors", d_err > 0, 0, f"+{d_err} DDP send errors")
        self.stats_prev = stats

        # parser sanity: values the protocol cannot produce
        bpm = snap.get("bpm") or 0
        bad = []
        if cue.startswith("UNKNOWN"):
            bad.append(f"cue {cue}")
        if (snap.get("scene_id") or 0) > 4:
            bad.append(f"scene id {snap.get('scene_id')}")
        if playing and not 30 <= bpm <= 400:
            bad.append(f"bpm {bpm}")
        self.cond(t, "parser_garbage", connected and bool(bad), 1.0,
                  "datagram reads garbage: " + ", ".join(bad))

        # beat
        beat_key = (snap.get("last_beat"), snap.get("bar_beat"), snap.get("beat_phase"))
        if beat_key != self.beat_key:
            self.beat_key, self.beat_t = beat_key, t
        self.cond(t, "beat_stalled", playing and self.beat_t is not None and t - self.beat_t >= BEAT_STALL_S, 0,
                  f"beat stuck at bar_beat={snap.get('bar_beat')} for {BEAT_STALL_S:.0f}s at {bpm} bpm")

        # what the strip shows
        live = level is not None and snap.get("output_live")
        self.cond(t, "black_strip", playing and live and level < BLACK_LEVEL and cue not in DARK_CUES
                  and not cue.startswith("STROBE"), BLACK_S, f"strip black under {cue}")
        sig = tuple(v // 8 for v in strip) if live else None
        if sig != self.strip_sig:
            self.strip_sig, self.strip_t = sig, t
        self.cond(t, "frozen_strip", playing and live and level >= BLACK_LEVEL and t - self.strip_t >= FROZEN_S,
                  0, f"strip unchanged for {FROZEN_S:.0f}s under {cue}")
        self.cond(t, "long_blackout", playing and live and level < BLACK_LEVEL and cue.startswith("BLACKOUT"),
                  20.0, f"{cue} dark for 20s+")

        if live and playing:
            self.levels.append((t, level))
        else:
            self.levels.clear()
        while self.levels and t - self.levels[0][0] > FLICKER_WINDOW_S:
            self.levels.popleft()
        flicker = False
        if playing and (snap.get("strobe_hz") or 0) == 0 and cue not in FLASHY_CUES and len(self.levels) >= 8:
            vals = [v for _, v in self.levels]
            peak = max(vals)
            if peak >= 20:
                flicker = count_alternations(vals, FLICKER_DELTA * peak) >= flicker_threshold(bpm)
        self.cond(t, "flicker", flicker, 0, f"unexpected flicker under {cue} (strobe off)")

        strobe = (snap.get("strobe_hz") or 0) > 0
        self.cond(t, "strobe_stuck", strobe and playing, STROBE_STUCK_S,
                  f"strobe {snap.get('strobe_hz')} Hz on for {STROBE_STUCK_S:.0f}s+ under {cue}")
        self.cond(t, "strobe_outside", strobe and scene != "Gameplay", STROBE_OUTSIDE_S,
                  f"strobe {snap.get('strobe_hz')} Hz in {scene}")

        # handing back to idle
        grace = _get(snap, "wled_power", "grace", default=15) or 15
        self.cond(t, "idle_failed", self.connected_ever and gone_s > 0 and snap.get("output_live")
                  and gone_s >= grace + IDLE_FAIL_EXTRA_S, 0,
                  f"no sender for {gone_s:.0f}s, output still live")
        self.cond(t, "held_orphan", bool(snap.get("cue_held")) and gone_s >= 60, 0,
                  f"{cue} still held {gone_s:.0f}s after the sender left")

    def silence(self, t):
        """Called periodically; flags the bridge stream having gone quiet."""
        if self.started_t is None:
            self.started_t = t
        last = self.last_event_t if self.last_event_t is not None else self.started_t
        self.cond(t, "bridge_down", t - last >= SILENCE_S, 0,
                  f"no status from the bridge for {SILENCE_S:.0f}s+")

    def wled(self, t, w):
        prev = self.wled_prev  # last good sample: a reboot usually shows as down, then back
        down = "error" in w
        self.cond(t, "wled_down", down, 0, f"WLED: {w.get('error')} (tcp {w.get('tcp_ms')} ms)")
        if down:
            return
        self.wled_prev = w
        if prev and (w.get("uptime") or 0) < (prev.get("uptime") or 0):
            self.once(t, "wled_reboot", f"WLED rebooted (uptime {prev.get('uptime')} -> {w.get('uptime')} s)")
        lip = w.get("lip") or ""
        self.cond(t, "second_sender", bool(w.get("live")) and lip not in ("", self.bridge_ip), 0,
                  f"WLED realtime source is {lip} ({w.get('lm')}), not the bridge")
        bridge_sending = self.frames_t is not None and t - self.frames_t < 2.0
        self.cond(t, "wled_not_live", bridge_sending and not w.get("live"), 15.0,
                  "bridge sending DDP, WLED live=false")
        self.cond(t, "wled_latched", bool(w.get("live")) and lip == self.bridge_ip and self.frames_t is not None
                  and t - self.frames_t >= LATCH_S, 0,
                  f"WLED live via {w.get('lm')} {t - (self.frames_t or t):.0f}s after the bridge's last frame")
        self.cond(t, "wled_heap_low", (w.get("freeheap") or 1e9) < HEAP_LOW, 0, f"free heap {w.get('freeheap')}")
        self.cond(t, "wled_weak_wifi", (w.get("rssi") or 0) < RSSI_LOW, 30.0, f"RSSI {w.get('rssi')} dBm")
        self.cond(t, "wled_slow_http", (w.get("http_ms") or 0) > HTTP_SLOW_MS, 0,
                  f"HTTP {w.get('http_ms')} ms vs TCP connect {w.get('tcp_ms')} ms")

    def pids(self, t, current, maximum):
        self.cond(t, "pids_high", maximum > 0 and current >= PIDS_FRAC * maximum, 0,
                  f"container pids {current}/{maximum}")


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
    det = Detector(bridge_ip=a.bridge.split(":")[0])
    q, stop = queue.Queue(), threading.Event()
    threads = [threading.Thread(target=sse_reader, args=(f"http://{a.bridge}/events", q, stop), daemon=True),
               threading.Thread(target=wled_reader, args=(a.wled, a.wled_every, q, stop), daemon=True)]
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
    print(f"NOTE watching bridge {a.bridge}, WLED {a.wled}, songs {a.songs or 'from scene changes'}; "
          f"logging to {a.out}", flush=True)
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
