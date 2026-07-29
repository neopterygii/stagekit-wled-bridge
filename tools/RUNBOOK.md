# Desync investigation runbook

For when the strip looks out of sync again. Everything here already exists and
was exercised on 2026-07-28; this is how to call it back up without rebuilding
the reasoning.

Background: `evidence/desync-latency-investigation-2026-07-28.md` and
`evidence/wled-realtime-latch-2026-07-28.md`.

## If it is happening right now — do this first, before anything else

**Do not power cycle yet.** The bad state is the evidence. Capture it:

```bash
cd ~/yarg-lighting/stagekit-wled-bridge

# 1. Is the controller latched in realtime? (the known failure mode)
curl -s http://192.168.0.53/json/info | .venv/bin/python -m json.tool | grep -E '"live"|"lm"|"fps"|freeheap|uptime'
curl -s http://192.168.0.53/json/cfg  | .venv/bin/python -c \
  'import sys,json; t=json.load(sys.stdin)["if"]["live"]["timeout"]; print("realtime timeout:", t*100, "ms", "<-- NEVER TIMES OUT" if t*100==65000 else "(ok)")'

# 2. Is the strip showing what the bridge sent?  (works even on a static strip)
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
.venv/bin/python -m tools.wled_lag step --cycles 12                 # [145-164 ms]

# Bridge removed. Stop the bridge's DDP output first, or both fight for the
# same realtime buffer and the last writer wins.
curl -s -X POST http://192.168.0.230:36180/api/power -d '{"action":"off"}'
.venv/bin/python -m tools.wled_lag step --direct --cycles 12        # [22.8 ms]
curl -s -X POST http://192.168.0.230:36180/api/power -d '{"action":"on"}'
```

The gap between those two is the bridge's own contribution — ~120 ms of it is
the 250 ms cue crossfade at `main.py:416`.

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
| `wled_lag step --direct` | bridge DDP stopped | the bridge is still sending |
| `frame_match.py` | nothing; works on a static strip | — (content check, not timing) |
| `wled_soak` | nothing | — |

Absolute lag values carry a systematic offset and can come back negative, which
is physically impossible. **Only compare deltas.**

Polling the ESP32 perturbs it: `/json/live` at ~20 Hz measurably loads the
device. Keep probes short and occasional, and prefer a probe-free window when
asking "does it look bad to you".

## Known-good reference values (2026-07-28, after the realtime-timeout fix)

| | Healthy |
|---|---|
| lag, via bridge, song playing | −40 to −60 ms, corr > 0.9 |
| step latency, via bridge | 145–164 ms median |
| step latency, direct DDP | ~23 ms median |
| frame match | mean per-channel diff ~0.1, worst ~1 |
| device RSSI | −34 fresh; drifts ~−1.4 dB/h, benign to at least −40 |
| device heap | ~181,000, flat |
| device strip FPS | 44–58 against a 60 FPS send |
| bridge | 60.00 DDP/s, 0 send errors, gap ≈ 17 ms vs 16.7 target |
| WLED realtime timeout | **2500 ms** — 65000 means *never*, and latches the device |
