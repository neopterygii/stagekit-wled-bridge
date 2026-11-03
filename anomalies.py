"""Lighting anomaly detection: is the strip doing what the show intends?

Pure and stdlib-only, so it runs in two places from one source:

  * inside the bridge (`AnomalyLog`, fed by the status loop at 10 Hz plus a per-frame brightness
    series from the render thread), where it is always on and sees every rendered frame;
  * in `tools/party_monitor.py`, an external watcher that relays the bridge's findings during a
    party and covers what the bridge cannot say about itself (bridge down, pid exhaustion), or
    does the whole job against an older image without `/api/anomalies`.

`Detector` turns timestamped inputs into *episodes*: a condition that must hold for a minimum time
before it opens (one event) and closes when it stops (a second event). Every kind has a level
(ALERT broken now / WARN looks unintended / NOTE context) and a cause class, following the split
rig findings use: bridge (code), config, device (this WLED board), transport (network), yarg (the
sender), chart (the song's own content).
"""

import json
import os
import time
from collections import Counter, deque
from datetime import datetime

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
    "bridge_start": ("NOTE", "bridge", "bridge process started"),
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

    def __init__(self, bridge_ip="", kinds=None):
        """`bridge_ip` is the address WLED sees the bridge's DDP come from (`lip`); `kinds`, if
        given, limits which episodes this detector may open (songs are tracked regardless)."""
        self.bridge_ip = bridge_ip
        self.kinds = set(kinds) if kinds is not None else None
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

    def enabled(self, kind):
        return self.kinds is None or kind in self.kinds

    def cond(self, t, kind, active, min_s=0.0, msg=""):
        if not self.enabled(kind):
            return
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
        if not self.enabled(kind):
            return
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

    def bridge(self, t, snap, levels=None, frame=None):
        """One status snapshot.

        `levels` and `frame` come from inside the bridge: the mean channel value of every frame
        sent since the last call, and the last frame's bytes. Without them (the external watcher)
        the dashboard preview strip stands in, which only exists while someone watches it and is
        a 10 Hz sample of a 60 fps render.
        """
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
        if levels is not None:
            strip = frame or b""
            level = levels[-1] if levels else None
            frame_levels = levels
        else:
            strip = _get(snap, "preview", "strip", default=[])
            level = strip_level(strip) if _get(snap, "preview", "source") == "live" else None
            frame_levels = [level] if level is not None else []

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

        # beat: YARG's own beat datagrams. The engine's beat_phase free-runs from BPM, so it keeps
        # moving when YARG stops sending beats; only a count of received beats can stall.
        beat_key = snap.get("beats_received")
        if beat_key != self.beat_key:
            self.beat_key, self.beat_t = beat_key, t
        self.cond(t, "beat_stalled", beat_key is not None and playing and self.beat_t is not None
                  and t - self.beat_t >= BEAT_STALL_S, 0,
                  f"no beat from YARG for {BEAT_STALL_S:.0f}s at {bpm} bpm")

        # what the strip shows
        live = level is not None and snap.get("output_live")
        self.cond(t, "black_strip", playing and live and level < BLACK_LEVEL and cue not in DARK_CUES
                  and not cue.startswith("STROBE"), BLACK_S, f"strip black under {cue}")
        sig = bytes(v >> 3 for v in strip) if live else None
        if sig != self.strip_sig:
            self.strip_sig, self.strip_t = sig, t
        self.cond(t, "frozen_strip", playing and live and level >= BLACK_LEVEL and t - self.strip_t >= FROZEN_S,
                  0, f"strip unchanged for {FROZEN_S:.0f}s under {cue}")
        self.cond(t, "long_blackout", playing and live and level < BLACK_LEVEL and cue.startswith("BLACKOUT"),
                  20.0, f"{cue} dark for 20s+")

        if live and playing:
            self.levels.extend((t, v) for v in frame_levels)
        else:
            self.levels.clear()
        while self.levels and t - self.levels[0][0] > FLICKER_WINDOW_S:
            self.levels.popleft()
        flicker = False
        if playing and live and (snap.get("strobe_hz") or 0) == 0 and cue not in FLASHY_CUES and len(self.levels) >= 8:
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
        # Only while someone plays (or did within 5 min): a strip unplugged between parties is
        # not an alert worth pinning to the dashboard all week.
        recent = self.connected_ever and (self.disconnected_t is None or t - self.disconnected_t < 300)
        self.cond(t, "wled_down", down and recent, 0, f"WLED: {w.get('error')} (tcp {w.get('tcp_ms')} ms)")
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


# Kinds only an outside observer can raise: the bridge cannot report its own silence, and a
# watcher's view of a restart is the only one that survives it.
EXTERNAL_KINDS = {"bridge_down", "bridge_restart", "pids_high"}


class AnomalyLog:
    """The bridge's anomaly feed: a Detector plus numbering, a day log on disk, and evidence.

    Fed from the status loop (one call per ~100 ms snapshot). Events get a monotonically rising
    `seq` so a watcher can poll `since(seq)`. Each day's events are appended to
    `<directory>/<YYYYMMDD>.jsonl` and reloaded at start, so a restart mid-party does not wipe
    the night. `on_evidence(event)` is called for each new episode that wants evidence (capped per
    kind per day); it returns the evidence file's name, or None.
    """

    MAX_EVENTS = 500
    RECENT = 15
    EVIDENCE_TOTAL_PER_DAY = 40

    def __init__(self, directory=None, bridge_ip="", on_evidence=None, clock=time.time):
        self.det = Detector(bridge_ip=bridge_ip)
        self.directory = directory
        self.on_evidence = on_evidence
        self.clock = clock
        self.events = deque(maxlen=self.MAX_EVENTS)
        self.seq = 0
        self.errors = 0
        self.last_error = ""
        self._day = None
        self._evidence = Counter()
        self._counts = Counter()
        self._recent = deque(maxlen=self.RECENT)
        self._wled_seen = None
        self._load_today()

    # -- persistence

    def _path(self, day):
        return os.path.join(self.directory, f"{day}.jsonl") if self.directory else None

    def _load_today(self):
        day = datetime.fromtimestamp(self.clock()).strftime("%Y%m%d")
        self._day = day
        path = self._path(day)
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    self.events.append(ev)
                    self._index(ev)
                    self.seq = max(self.seq, ev.get("seq", 0))
                    if ev.get("evidence_file"):
                        self._evidence[ev["kind"]] += 1
        except OSError:
            pass

    def _rollover(self, day):
        if day != self._day:
            self._day, self._evidence, self._counts = day, Counter(), Counter()
            self._recent.clear()

    def _index(self, ev):
        """Keep the per-day counts and the recent list current, so summary() stays O(1)."""
        if ev.get("phase") in ("start", "point") and ev["level"] in ("ALERT", "WARN"):
            self._counts[ev["level"]] += 1
        if ev["kind"] != "song_start":
            self._recent.append({k: ev.get(k) for k in ("seq", "time", "level", "kind", "cls", "msg",
                                                         "song", "evidence_file")})

    def _write(self, ev):
        day = self._day
        path = self._path(day)
        if not path:
            return
        try:
            os.makedirs(self.directory, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
        except OSError as exc:
            self.last_error = f"cannot write {path}: {exc}"

    # -- feeding

    def record(self, ev):
        """Number, timestamp, persist and keep one event. Also used for events from outside."""
        self._rollover(datetime.fromtimestamp(ev["t"]).strftime("%Y%m%d"))
        self.seq += 1
        ev["seq"] = self.seq
        ev.setdefault("time", datetime.fromtimestamp(ev["t"]).strftime("%H:%M:%S"))
        wants = ev.pop("evidence", False)
        ev.pop("quiet", None)
        if wants and self.on_evidence is not None and self._evidence[ev["kind"]] < EVIDENCE_PER_KIND \
                and sum(self._evidence.values()) < self.EVIDENCE_TOTAL_PER_DAY:
            try:
                name = self.on_evidence(ev)
            except Exception as exc:  # evidence is a bonus; never lose the event over it
                name, self.last_error = None, f"evidence: {exc}"
            if name:
                self._evidence[ev["kind"]] += 1
                ev["evidence_file"] = name
        self.events.append(ev)
        self._index(ev)
        self._write(ev)
        return ev

    def _flush(self):
        for ev in self.det.drain():
            self.record(ev)

    def start(self, label=""):
        self.det.once(self.clock(), "bridge_start", f"bridge started {label}".strip())
        self._flush()

    def tick(self, snap, levels=None, frame=None, wled=None, pids=None):
        """One status-loop step. Never raises: the caller is the dashboard's only feed."""
        t = self.clock()
        try:
            self.det.bridge(t, snap, levels=levels, frame=frame)
            if wled is not None and wled is not self._wled_seen:
                self._wled_seen = wled
                self.det.wled(t, wled)
            if pids is not None:
                self.det.pids(t, *pids)
            self._flush()
        except Exception as exc:
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.det.drain()

    # -- reading

    def open_episodes(self):
        out = []
        for kind, ep in list(self.det.eps.items()):
            if ep.open:
                level, cls, _ = KINDS[kind]
                out.append({"kind": kind, "level": level, "cls": cls, "msg": ep.msg,
                            "since": round(ep.since, 1)})
        order = {"ALERT": 0, "WARN": 1, "NOTE": 2}
        return sorted(out, key=lambda e: (order.get(e["level"], 3), e["since"]))

    def summary(self):
        """Small, for every SSE snapshot: what is wrong now and the last few things that were."""
        self._rollover(datetime.fromtimestamp(self.clock()).strftime("%Y%m%d"))
        return {
            "seq": self.seq,
            "open": self.open_episodes(),
            "today": {"alerts": self._counts["ALERT"], "warnings": self._counts["WARN"]},
            "recent": list(self._recent)[::-1],
            "song": self.det.song["label"] if self.det.song else None,
            "errors": self.errors,
        }

    def since(self, seq=0):
        return [ev for ev in self.events if ev.get("seq", 0) > seq]

