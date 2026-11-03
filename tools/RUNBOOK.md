# Desync investigation runbook

For when the strip looks out of sync again. Everything here already exists and
was exercised on 2026-07-28; this is how to call it back up without rebuilding
the reasoning.

Background: `evidence/desync-latency-investigation-2026-07-28.md` and
`evidence/wled-realtime-latch-2026-07-28.md`.

**The controller runs WLED 16.0.1 since ~2026-11**, which has no `/json/live`.
The tools now read the device through `tools/wled_peek.py` — the websocket
live peek, falling back to `/json/live` on firmware that still has it — and
print which one they used. Numbers below dated 2026-07-28 were taken through
`/json/live` on 0.14.4 and carry a different systematic offset: compare
16.x readings with 16.x readings.

**Check for a second sender first.** `curl -s http://192.168.0.53/json/info`
→ `live`/`lm`/`lip`: `lip` is whoever last wrote the realtime buffer. On
2026-11-02 it was the YARG PC (`192.168.0.15`) — YALCY's WLED output, sending a
10 Hz keep-alive alongside the bridge. Two senders interleave frames on the
strip, and that looks exactly like a desync.

## During a party: anomaly detection

The bridge detects anomalies itself (`anomalies.py`, the dashboard's **Health** card): every
rendered frame is judged, and each new episode leaves a replayable capture of the 30 s before it in
`captures/anomalies/` (on Tower: `/mnt/user/appdata/stagekit-wled-bridge/captures/anomalies/`),
with the night's events in `/data/anomalies/<day>.jsonl`.

`tools/party_start.sh` adds the outside view for a party: `tools/party_monitor.py` relays the
bridge's `/api/anomalies` feed with song titles from the Mac, and raises what the bridge cannot
say about itself (bridge down, restarts, pid exhaustion). Against an image without
`/api/anomalies` it runs every detector itself, from the 10 Hz status stream. Afterwards
`python -m tools.party_monitor report out/sessions/<date>-party` writes `report.md`, grouped by
cause class, which is what the update plan is drafted from. The `party-lights` skill drives
start / status / end. An ALERT means: come here and capture first.

## If it is happening right now — do this first, before anything else

**Do not power cycle yet.** The bad state is the evidence. Capture it:

```bash
cd ~/yarg-lighting/stagekit-wled-bridge

# 1. Is the controller latched in realtime? (the known failure mode)
curl -s http://192.168.0.53/json/info | .venv/bin/python -m json.tool | grep -E '"live"|"lm"|"fps"|freeheap|uptime'
curl -s http://192.168.0.53/json/cfg  | .venv/bin/python -c \
  'import sys,json; t=json.load(sys.stdin)["if"]["live"]["timeout"]; print("realtime timeout:", t*100, "ms", "<-- NEVER TIMES OUT" if t*100==65000 else "(ok)")'

# 2. Is the strip showing what the bridge sent?  (works even on a static strip;
#    refuses if the bridge isn't rendering, and names a second sender if any)
.venv/bin/python tools/frame_match.py

# 3. How far behind is it?  Needs the song *playing* — aperiodic motion.
.venv/bin/python -m tools.wled_lag probe --seconds 55 --max-lag 3.0
```

Compare the lag against the 2026-07-28 healthy baseline: **−40 to −60 ms**,
correlation > 0.9, no ambiguity warning. (The sign is negative because of an
uncorrected systematic offset — only the delta matters.)

Then power cycle and re-measure. A large difference across the power cycle is
the reproduction.

## Long-run soak

```bash
# Sample device + bridge onto one timeline. Survives a dropped session.
.venv/bin/python -m tools.wled_soak log --out soak.jsonl --interval 60 --duration 6h

# Read it back — trends, per-hour degradation, transport-vs-firmware verdict,
# realtime-latch detection, counter resets that would void deltas.
.venv/bin/python -m tools.wled_soak analyse soak.jsonl
```

## Latency, decomposed

Answers "is it the bridge, the network, or the device". Healthy values measured
2026-07-28 in brackets.

```bash
# Whole path, via the bridge. Injects cues, so NOT during a real YARG session.
# The 145-164 ms reading predates the cue-fade change; expect ~55-80 ms once
# that is deployed, and note the number now depends on which cue is injected.
.venv/bin/python -m tools.wled_lag step --cycles 12                 # [145-164 ms]

# Bridge removed. Stop the bridge's DDP output first, or both fight for the
# same realtime buffer and the last writer wins. The probe refuses to start
# while WLED is still in realtime mode and names the sender (`lip`); it also
# switches WLED on for the run if it is off (the peek reads black while off)
# and restores it afterwards.
curl -s -X POST http://192.168.0.230:36180/api/power -d '{"action":"off"}'
.venv/bin/python -m tools.wled_lag step --direct --cycles 12        # [22.8 ms on 0.14.4]
curl -s -X POST http://192.168.0.230:36180/api/power -d '{"action":"on"}'
```

Through the websocket peek every step latency includes up to one push interval
(~44 ms measured, so ~22 ms on average) of waiting for the next frame. The
summary prints the observed interval.

The gap between those two is the bridge's own contribution — ~120 ms of it was
the cue crossfade, which was a flat 250 ms until 2026-07-29.

**That is now `settings.cue_fade_ms` (default 120 ms) with per-cue overrides in
`settings.CUE_FADE_MS_OVERRIDES`**, so this measurement depends on which cue
`wled_lag step` injects: the cues YARG names "fast" (`BLACKOUT_FAST`,
`FLARE_FAST`, `FRENZY`, `STROBE_*`) now snap with no fade at all, and
`BLACKOUT_SLOW`/`FLARE_SLOW` still take 250 ms. Compare like against like when
reading a before/after, and record which cue was used.

## Reproducing the original failure

The trigger is **a long pause with the stream still running**. Pausing does not
stop YARG; it keeps sending ~90 datagrams/s and the bridge keeps pushing ~60
FPS, about 220,000 frames an hour.

1. Power cycle the controller. Play a song. Measure lag — this is the baseline,
   and confirm by eye that it looks right.
2. Pause and leave it. **4+ hours**; 2h51m was not enough to reproduce on
   2026-07-28.
3. While paused, lag is not measurable (a static strip carries no timing
   information) — the probe will refuse rather than guess. Track state instead:
   `tools/wled_soak.py` plus `tools/frame_match.py`.
4. Unpause. Measure lag immediately and compare against step 1.

## Which probe to trust when

| Probe | Needs | Fails silently when |
|---|---|---|
| `wled_lag probe` | aperiodic motion on the strip | the cue is repetitive — flags `[AMBIGUOUS]`, retry for a clean window |
| `wled_lag step` | to own the cue | another sender is feeding the bridge — every step reads "not seen" |
| `wled_lag step --direct` | no other DDP sender | — (refuses, naming `lip`) |
| `frame_match.py` | the bridge rendering; works on a static strip | — (refuses on an empty preview) |
| any of them, via the peek | WLED on, nobody else using its live view | — (refuses: WLED off reads black; WLED's UI live preview takes the single live-view slot) |

If no device read ever succeeds, the step summary now prints the read error
(off, stale peek, 501) instead of "never visibly reacted".
| `wled_soak` | nothing | — |

Absolute lag values carry a systematic offset and can come back negative, which
is physically impossible. **Only compare deltas.**

Polling the ESP32 perturbs it: `/json/live` at ~20 Hz measurably loads the
device. The websocket peek is a ≤25 Hz push with no HTTP handling, which should
be lighter, but that is unmeasured. Keep probes short and occasional, and
prefer a probe-free window when asking "does it look bad to you".

## Known-good reference values (2026-07-28, after the realtime-timeout fix)

| | Healthy |
|---|---|
| lag, via bridge, song playing | −40 to −60 ms, corr > 0.9 |
| step latency, via bridge | 145–164 ms median at the old flat 250 ms crossfade; expect ~55–80 ms on a base-fade cue once `cue_fade_ms=120` is deployed, and ~25–35 ms on a snapping cue |
| step latency, direct DDP | ~23 ms median (0.14.4, `/json/live`); **29 ms** median, n=12, via the peek on 16.0.1 (2026-11-02; may have overlapped YALCY starting — retake in a clean window) |
| frame match | mean per-channel diff ~0.1, worst ~1 |
| device RSSI | −34 fresh; drifts ~−1.4 dB/h, benign to at least −40 |
| device heap | ~181,000, flat |
| device strip FPS | 44–58 against a 60 FPS send |
| bridge | 60.00 DDP/s, 0 send errors, gap ≈ 17 ms vs 16.7 target |
| WLED realtime timeout | **2500 ms** — 65000 means *never*, and latches the device |
