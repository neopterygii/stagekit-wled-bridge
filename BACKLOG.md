# Backlog

Per the mission-control protocol, this is the full backlog; the dashboard in
`~/mission-control/README.md` carries only the headline plus a link here.

Item status: `OPEN` · `IN PROGRESS` · `BLOCKED` · `DONE` · `DEFERRED`.

Item kind, which follows the status on each heading:

| Kind | Means |
|---|---|
| `bugfix` | The bridge does the wrong thing in production today. |
| `feature` | A capability that does not exist yet. |
| `perf` | Same behaviour, less latency or less work per frame. |
| `refactor` | Same behaviour, better structure or testability. |
| `evidence` | A measurement or investigation. May end in no code at all. |
| `ops` | Firmware, backup, recovery — the rig rather than the repo. |

Kind is about the work, not its importance: an `evidence` item can outrank a
`bugfix`, and twice already has.

## Next up (set 2026-07-29)

**Deployment is not tracked here.** The operator updates the container template
and judges changes on the rig on their own time; this backlog covers the work in
the repo. Query the running container when an item needs live evidence
(`AGENTS.md` has the triage commands) — do not add "deploy" or "flip the
template" as a backlog step.

In order:

1. **The two state bugs** (`bugfix`) — both are cases of the bridge believing
   something about the world that is not true, and both mislead the operator in
   the room, which is the one thing the dashboard exists to prevent:
   - [cached WLED power state never resyncs after a device reboot](#open-bugfix--the-bridges-cached-wled-power-state-never-resyncs-after-a-device-reboot)
     — the periodic probe discards its own answer, so a device powered on by
     anything but the bridge stays lit indefinitely at a measured ~1622 mA.
   - [stale lit cue held after disconnect/power-off](#open-bugfix--bridge-keeps-a-stale-lit-cue-after-disconnectpower-off)
     — the dashboard shows an active cue and a lit preview while the strip is
     dark.

   Small, adjacent, and both about state that is never reset — reasonable as
   **one branch**, though they touch different code (`WLEDPower._wled_on` in the
   watchdog vs. cue/zone state in `CueEngine`) and split cleanly if preferred.

Then back to the reliability push: W3 alignment trim (`feature`), W4 read-only
state/MQTT (`feature`), W5 firmware qualification (`ops`). The desync item is an
argument *for* W5 but also a reason to gather evidence before it, since a
firmware upgrade could mask the cause rather than fix it.

### Recently closed

- ~~Spotlights should pulse to the beat, not chase~~ — built 2026-07-29;
  [write-up](#done-2026-07-29-feature--spotlights-should-pulse-to-the-beat-not-chase).
  Operator-reported. Only `spotlight_cues`' digest moved.
- ~~Beat-locked cues lurch or freeze at cue change~~ — built 2026-07-29;
  [write-up](#done-2026-07-29-bugfix--beat-locked-cues-lurch-or-freeze-for-up-to-a-second-at-cue-change).
  Reported as "searchlights has a rough start"; it was every beat-locked cue, up
  to 20× overspeed or a **full second frozen**, and worst on the calmest cues.
- ~~The keyframe tempo fallback steps off the beat~~ — built 2026-07-29;
  [write-up](#done-2026-07-29-bugfix--the-keyframe-tempo-fallback-steps-off-the-beat).
  Found by sanity-checking the transitions for the item above, not from the rig —
  the same defect in the other pattern type.
- ~~Event-driven cue patterns still run on asyncio~~ — built 2026-07-29;
  [write-up](#done-2026-07-29-refactor--event-driven-cue-patterns-still-run-on-asyncio-so-replay-cant-judge-them).
  The harness now covers the library's most common cues. Only `authored_venue`'s
  digest moved; `DEFAULT` gained its first replay coverage of any kind.
- ~~WLED desync — "only a power cycle clears it"~~ — root-caused and fixed
  2026-07-28 (a realtime-timeout misconfiguration on the controller, which was
  also blocking OTA). [Drift during an active stream](#open-evidence--desync-not-reproduced-since-the-realtime-timeout-fix-testing-is-parked-not-finished)
  is unproven and testing is **parked, not finished**.
- ~~Spotlight cues are too narrow~~ — built 2026-07-29;
  [write-up](#done-2026-07-29-feature--spotlight-cues-light-too-narrow-a-slice-of-the-strip).
  Not the one-number change that entry claimed: the operator wanted *more*
  spotlights, not a wider one.
- ~~Newer layers ignore the selected palette~~ — verified on the rig and merged
  2026-07-29; [write-up](#done-2026-07-28-feature--newer-reactivity-layers-ignore-the-selected-palette).
- ~~The 250 ms cue crossfade~~ — built 2026-07-29;
  [write-up](#done-2026-07-29-perf--cue-changes-are-crossfaded-over-250-ms-and-that-is-the-visible-lag).
  It was the largest latency term in the pipeline and the one no hardware change
  touches.

Blocking none of the above: the replay harness is **merged to `main`
(2026-07-29)**, so anything here can lean on it — a recorded passage replayed
before/after beats remembering last night's show.

---

## OPEN `evidence` — VenueSize is hardcoded to Small, so Phase 8 density branching is dead code

**Found:** 2026-07-27, while tracing venue sources for the library inventory.

YARG never varies the venue size it sends. In
`YARG/Assets/Script/Integration/Data Stream/DataStreamGameplayMonitor.cs:85`:

```csharp
// This should be read from the venue itself eventually, but for now, we'll just randomize it.
DataStreamController.MLCVenueSize = (DataStreamController.VenueType)Random.Range(1, 2);
```

Unity's `int` overload of `Random.Range` is max-exclusive, so this always
returns `1` — `VenueType.Small`. Never `Large`, never `None`. The comment says
"randomize", but it cannot.

**Consequence:** the Phase 8 venue-size density branching
(`tests/test_venue_size.py`) can only ever take the Small path. The strip is
permanently thinned to single-head chase steps with halved sparkle, and the
Large fill-out path never runs on real input. `VISION.md` describes the feature
as branching on small/large, which does not match what arrives on the wire.

**Corroborated live, 2026-07-27.** The running Tower container's
`/api/status` reported `"venue_size": "Small"` (`venue_size_id: 1`) while
attached to a live YARG session mid-song. That is consistent with the hardcode
but is still a single observation of one song, so it cannot by itself prove
`Large` is unreachable. A capture across several songs — which the replay
harness (item 2) will produce — is what settles it. Do not change behaviour on
the strength of the source read alone; that is the whole point of `AGENTS.md`'s
"check the running system first" rule.

Options once confirmed:

1. Treat Small as the only real input and simplify/retire the Large path,
   keeping the density transform as an operator-selectable taste control rather
   than a venue-driven one.
2. Keep the code and drive it from an operator setting until YARG populates the
   field for real.
3. Upstream a fix to YARG so venue size is read from the venue.

Do not delete the Large path before the capture confirms it — YARG nightly may
fix this at any time, and the transform itself is tested and correct.

---

## OPEN `evidence` — Venue fixture coverage now has a measured answer

**Found:** 2026-07-27, from the `venue_scan` library inventory
(`~/yarg-lighting/evidence/venue-inventory-2026-07-27.md`).

The replay/capture harness (VISION "Next push" item 2) wants fixtures covering
MIDI venue, Milo venue, and auto-generation. Two of those are now settled:

- **MIDI venue fixtures are abundant** — 16,961 songs, and the inventory ranks
  them by richness.
- **A Milo-only fixture does not exist in this library.** Of 460 songs shipping
  a Milo, 459 of those Milos are stubs with no `song.anim` member. The single
  Milo with real animation data (Hazbin Hotel Cast — "Happy Day In Hell") is
  shadowed by its own non-empty MIDI venue, so YARG's `IsEmpty` gate blocks the
  load. Exercise the Milo path directly from that file rather than waiting for a
  song that triggers it naturally.
- **Auto-generation fixtures are the majority case** — 18,626 songs, including
  every one of the 13,169 `.chart` songs.

**Also worth encoding in the harness:** 96.1% of the library receives
synthesised fog, because fog generation runs even on fully authored venues. Fog
is not an edge case; it is the default.

---

## OPEN `evidence` — Render priority is now measured, not assumed

**Found:** 2026-07-27, same inventory.

Cue frequency across every authored venue in the library:

- `next` (keyframe advance) — 1,400,989 events across 7,966 songs, roughly 10×
  the next-most-common cue. Legacy note numbers can express *only* keyframes
  (48/49/50), never a lighting cue, which is why this dominates.
- Then `flare_fast`, `blackout_fast`, `dischord`, `warm_manual`, `stomp`,
  `chorus`, `cool_manual`.
- Spotlights are heavy: ~86k guitar, ~79k drums, ~78k vocals.
- `bonus_fx` (13,871) outnumbers `fog_on` (9,721).

Worth checking the cue engine's handling of `next` specifically — at that volume
its behaviour dominates the look far more than any individual cue.

---

## DONE 2026-07-29 `refactor` — Event-driven cue patterns still run on asyncio, so replay can't judge them

**Found:** 2026-07-27, while building the replay harness. **Built 2026-07-29.**

Time-driven zone patterns were migrated to the deterministic `engine.tick()`
path, but the **event**-driven ones were not. Five cues launch asyncio
coroutines that step on `_beat_event` / `_keyframe_event`
(`_start_listen_pattern`, and `_start_beat_pattern`/`_start_multi_zone_chase`
when `listen=` is set):

- `DEFAULT`, `WARM_MANUAL`, `COOL_MANUAL` — keyframe-stepped
- `STOMP` — keyframe-stepped
- `DISCHORD` — `beat_any`

**Why this matters more than five cues suggests.** Per the `venue_scan`
inventory, `next` keyframes are the most common cue in the library by roughly
**10×** — legacy note numbers can express *only* keyframes — and
`warm_manual`, `dischord` and `stomp` all sit in the top eight. So the replay
harness currently cannot render, hash, or regression-test the motion of the
library's most common cues. It covers everything else end to end.

`tests/test_replay.py::test_event_driven_cues_do_not_advance_under_replay` pins
the limitation deliberately and fails once it's fixed, which is the prompt to
delete it and assert the real motion.

**Already fixed in passing:** these launches used `asyncio.ensure_future`, whose
`get_event_loop()` fallback *raises* `RuntimeError` when no loop is running
(and is deprecated in 3.12, removed later). Off the event loop that took the
whole cue dispatch down instead of just losing the animation. They now go
through `CueEngine._spawn`, which uses `get_running_loop()`/`create_task` and
degrades to "this cue doesn't animate" when there's no loop. Production always
has one, so live behaviour is unchanged — but the bridge is no longer one Python
upgrade away from a hard failure here.

**The work:** move the `listen=` patterns onto `_TimePattern`-style ticking
driven by beat/keyframe *counters* rather than awaited events. The engine
already tracks beat edges and keyframes, so the state a coroutine keeps in its
local `idx` can live in the pattern object instead. Do it as its own change with
its own look-check on the real strip — it alters the most common cues in the
library, so it should not ride along with unrelated work.

### What was built

`_CounterPattern` alongside `_TimePattern`: the packet path only *counts*
(`_keyframe_count`, and the existing `_beat_count`) and `tick()` reads the
counter, so the step index is a pure function of (counter, wall clock). The
three coroutines, `_spawn`, `_active_tasks`, both `asyncio.Event`s and both
`_last_*_type` fields are gone, and with them the `asyncio` import — there is no
longer a second, untestable way to run a pattern.

**The shape came from LedFx**, which has no event-driven pattern coroutines at
all. Its beat queries are `@lru_cache` methods invalidated once per audio frame
(`ledfx/effects/audio.py:1355-1363`), and discrete state advances by
edge-detecting a sampled phase — `if self.beat < self.last_beat:` then
`frame_c = (frame_c + 1) % framecount` (`ledfx/effects/keybeat2d.py:534-555`).
We read a counter instead of detecting wraps, because we have one.

Three decisions worth keeping:

- **Block rendering, not glide.** These write `self.zones` and are painted as
  cell blocks, exactly as the coroutines were; they claim no zones and emit no
  motion heads. Gliding needs to know when the *next* step lands. Tempo supplies
  that; an external chart keyframe stream does not, and interpolating toward an
  unarrived keyframe would render a step behind the chart. LedFx can interpolate
  its key frames (`frame_progress = self.beat / self.beat_incs[...]`) precisely
  because its beat oscillator predicts the next beat. This is why the migration
  moved only one golden digest.
- **The keyframe fallback is now uniform.** `WARM_MANUAL`/`COOL_MANUAL` already
  stepped on tempo once a keyframe was overdue; `DEFAULT` and `STOMP` awaited
  forever and **froze solid** on charts with no keyframes. All four now fall
  back, matching LedFx's oscillator, which coasts on the last known period and
  cannot freeze. Note the real cadence is one step per **2 ×** the nominal step
  interval — the asyncio `wait_for` re-armed its 2× timeout after firing, so
  "steps on BPM cadence" was never what the old comment claimed. Preserved as
  measured, as `KEYFRAME_FALLBACK_BEATS`.
- **`DISCHORD` reuses `_beat_count`**, so `_advance_clock`'s
  `max(1, round(elapsed))` bridges a dropped beat packet and the chase stays in
  musical phase rather than falling a step behind — the rule the time-driven PLL
  already follows, and further than LedFx goes (its `beat_counter` increments per
  detected beat while only its *position* coasts).

Two details that would have changed the look if missed, both now under test:

- **`apply_delay`.** The coroutine shapes disagreed: `_run_beat_pattern` and
  `_run_multi_zone_chase` applied step 0 *then* awaited, while
  `_run_listen_pattern` awaited *then* applied. So `DEFAULT` holds the `BLUE=ALL`
  its own cue sets until the chart's first keyframe. Inverting this starts the
  cue dark instead of blue.
- **`DEFAULT` is exempt from the venue density transform**, as it always was —
  `_start_listen_pattern` never applied it. Its steps are washes (`NONE`/`ALL`),
  not a chase, so thinning them would dim the cue rather than sparsen a pattern.

`DEFAULT` was the one cue with no rate of its own to inherit (`_start_listen_
pattern` took no `cycles_per_beat`), so its fallback rate — 1.0 over a 2-step
pattern, alternating once per beat — is **the only invented number in the
change** and the thing to judge on the strip.

**Cost: none for the mechanism.** Median ms/frame, base vs. this branch, on the
fixtures whose look is unchanged: `warm_beats` 0.489 → 0.494, `rapid_cue_changes`
0.632 → 0.633, `auto_generated_venue` 0.681 → 0.681, `spotlight_cues` 0.329 →
0.328 — all inside the noise. `authored_venue` goes 0.375 → 0.450 ms/frame, which
is not the counter machinery: it is the cost of *actually rendering* four cues
that previously rendered nothing at all.

**Verification:** 586 tests green, up from 553. 25 new in
`tests/test_counter_patterns.py`, plus new replay coverage.
`test_event_driven_cues_do_not_advance_under_replay` was deleted and replaced
with `test_event_driven_cues_advance_under_replay`, as its own docstring
instructed. Two fixtures were added: `default_keyframes` (DEFAULT had **no
replay coverage at all** — the cue tied to the library's most common event was
the one the harness could not see) and `keyframe_starved` (the fallback).

**Exactly one pre-existing golden digest moved: `authored_venue`**, the only
fixture using `WARM_MANUAL`/`COOL_MANUAL`/`STOMP`/`DISCHORD`. The other ten
holding still is the evidence that block rendering preserved the look. Its
entries in `PRE_FADE_DIGESTS` and `PRE_STRICTNESS_DIGESTS` were re-derived too:
those tables pin that a *settings* rollback is bit-exact, and this was an engine
change, so that fixture's historical capture — four cues sitting motionless
because their coroutines never ran — no longer describes anything reachable. The
other ten values in each table are untouched and are what carry the claim.

**Rollback is the branch/tag, not a knob.** Palette strictness and the cue fade
both shipped a slider plus a pre-change digest table; this one deliberately does
not, because the knob would have to keep the asyncio path alive, and removing
that path is the entire point.

**What to judge on the rig:** the five cues should look unchanged on a keyframed
chart. Two things will differ — `DEFAULT` and `STOMP` now move instead of
freezing on a chart with no keyframes, and `DEFAULT`'s fallback runs at the
invented 1.0 cycles/beat.

---

## DONE (2026-07-28) `feature` — Newer reactivity layers ignore the selected palette

**Raised:** 2026-07-27 by the operator, after watching the vocal ribbon.
**Built:** 2026-07-28. **Verified on the rig and merged to `main` 2026-07-29.**

Several Phase 4–5 features paint from **fixed hue tables of their own** rather
than from the active palette, so they look correct under `default` (RGBY) and
foreign under any other palette — pick Lava or Frost and the vocal ribbon still
emits full-spectrum chroma.

Confirmed palette-independent sources in `effects/mapper.py`:

- **Vocal ribbon** — hue is the pitch *class* sampled from a fixed chroma ramp
  (`VOCAL_CHROMA`, ~line 47). Deliberate: same note → same colour is the whole
  idea. But it means an octave of singing sweeps hues the palette never contains.
- **Performer highlight bias** — a fixed hue per `Performer` bit
  (`PERFORMER_BIAS_STRENGTH` block, ~line 128).
- **Camera-cut bias** — reuses the same performer hue table (~line 163).
- **Song-section bias** — a fixed per-section hue (`SECTION_BIAS_STRENGTH`,
  ~line 147).

The tension is real, not a bug: pitch-class colour and per-performer colour both
*mean* something, and remapping them into a 4-colour palette would throw that
meaning away. Options, cheapest first:

1. **Palette-relative hues.** Map each fixed hue onto the nearest palette entry,
   or onto the palette's own hue span, keeping the *relative* ordering (so notes
   still differ from each other, within the palette's family).
2. **A "palette strictness" operator toggle.** Off = today's expressive hues;
   on = every layer constrained to the palette. Cheap, honest, and lets the
   operator decide per show.
3. **Leave the ribbon alone, fix the biases.** The three *bias* layers are
   subtle tints where palette-relative hue costs nothing; the ribbon is the only
   one whose meaning depends on absolute hue.

First step is the inventory above turned into a test: assert each layer's output
hues fall inside the active palette when strictness is on. Do that before
changing any of the maths.

### What was built

Option 1 (palette-relative hues) as the mechanism, option 2 (a strictness knob)
as the control, and the operator's amendment that the layers **need not land on
one of the four colours — only inside the palette's theme**. That amendment is
what made option 1 workable, and it reframed the whole item: our four colours
were *truncated* from richer LedFx/WLED gradients because four zones is all the
Stage Kit model holds, so the fix is to go back to the sources and recover the
family, not to synthesise one from four swatches.

- `tools/extract_palette_ramps.py` parses the three vendored upstream formats
  (LedFx CSS gradients, WLED `_gp[]` quads, WLED `TProgmemRGBPalette16` with
  `CRGB::` names resolved against the vendored FastLED enum) and emits literals
  pasted into `settings.PALETTES[*]["ramp"]`. Runtime never reads the clones.
  `--check` audits each ramp against that palette's four colours **in both
  directions**; six palettes pass, six fall back to a ring built from their own
  four. Party has no green in it and our Party palette does; LedFx Frost runs on
  into purple and pink and ours is ice. Those are mislabels in the
  `description` strings, now stated in `settings.py`.
- `effects/palette_map.py` builds the ring and does the remap. Three decisions
  worth keeping: the ring closes with a short **return arc rather than a
  mirror** (mirroring maps pitch class p and 12-p to one colour); stops are
  spaced by **perceptual distance, not the upstream's own positions** (lava_gp
  spends its first quarter deepening a single red, which swallowed three pitch
  classes); and layers whose meaning is a *parameter* rather than a *colour* —
  the ribbon, cue gradients — index the ring by that parameter instead of by
  hue, because hue-indexing collapses wherever a ramp holds one hue at several
  brightnesses.
- Scope went beyond the four layers listed above to the **cue gradient
  recolour** (which repaints every lit pixel's hue, a louder violation than any
  bias), the star-power tint and the spotlight-only colour.
- `palette_strictness` 0.0–1.0, **default 1.0**, with a dashboard slider,
  `POST /api/settings`, persistence, and a pin in `replay/player.py`.

**Cost:** +0.037 ms/frame with every remapped layer active — 0.2% of the 60 FPS
budget. The remap runs on a handful of colours per frame, not per pixel; cue
gradients are remapped LUT-wise once and cached.

**Verification:** 457 tests green, up from 357 on the base branch — 91 new in
the two palette files plus 9 pinning the rollback path.
`tests/test_palette_strictness.py` asserts
family membership per layer over four differently shaped palettes, plus that an
octave still sweeps the palette and that pitch classes do not collapse.
`tests/test_replay.py::test_zero_strictness_reproduces_the_pre_palette_look`
pins all nine pre-change golden digests at strictness 0.0, so the rollback path
stays bit-exact under test rather than on trust. Three of the nine goldens moved
at strictness 1.0 — exactly the fixtures that drive a remapped layer.

**Looked at on the strip 2026-07-29 and kept.** Default-on does change the live
look, including under `default` (its ring is the four-colour fallback, close to
but not the rainbow). Rollback remains the slider to 0.0, or `:1.0.0`.

---

## OPEN `evidence` — WLED drifts out of sync after hours of DDP; only a physical power-cycle clears it

**Observed:** 2026-07-27 by the operator. Left paused on a song for **4+ hours**;
the strip ended up visibly out of sync with the game. Turning WLED off from the
container did **not** clear it — only pulling the controller's power did.

That last detail is the important one: **the bad state lives on the ESP32, not in
the bridge.** A container-side power-off is a JSON API call; it doesn't reset the
device. So this is very unlikely to be a cue-engine or render-thread bug, and
correspondingly unlikely to be fixed by anything in this repo.

### Root cause found for the stuck half — 2026-07-28

**The controller is latched in realtime mode permanently.** Full write-up with
the source chain and the live readings:
`~/yarg-lighting/evidence/wled-realtime-latch-2026-07-28.md`.

`if.live.timeout` on `192.168.0.53` is **650**, which `cfg.cpp` scales to
`realtimeTimeoutMs = 65000` — and 65000 is precisely the sentinel value
`realtimeLock()` maps to `realtimeTimeout = UINT32_MAX`, i.e. *never time out*.
The WLED settings page presents the field as a plain `max="65000"` number input
with no hint that the top of its own range means "never", so this is a very easy
setting to land on by accident while trying to stop the strip dropping out of
realtime mid-song.

Because `wled.cpp:96` gates the main loop on not being in realtime, a latched
device permanently skips `strip.service()` — so no state change reaches the
pixels (that's why `{"on": false}` did nothing), the strip holds its last DDP
frame indefinitely, and **`ArduinoOTA.handle()` never runs, so OTA is blocked**.
That last one was not previously known and directly affects W5 below: the
controller cannot be OTA-upgraded while latched, and it latches on the first DDP
frame after every boot.

Confirmed live on 2026-07-28: twelve hours after the bridge's last
`WLED: powered OFF (idle)`, with `ddp_frames_sent` static across repeated
samples, the device still reported `"live": true, "lm": "DDP"` while
`"on": false`.

**Fix (device config, not code): `DONE` 2026-07-28.** Timeout set to `2500`
(`if.live.timeout: 25`) and the controller rebooted — a config change alone does
not clear an already-latched `realtimeTimeout`, because `realtimeLock()` only
recomputes it when it is not already `UINT32_MAX`. Reversible by setting `65000`
back.

Verified by driving all-black DDP for 3 s and watching the device: `live` went
`false → true (lm DDP, 58 fps) → false` ~3 s after the stream stopped, with
strip FPS returning to 38. `strip.service()` runs again, so state changes reach
the pixels and OTA is reachable between streams.

A pre-change backup is at `~/wled-backups/192.168.0.53/2026-07-28/`. Note it is
**not a complete restore image**: WLED never serves `wsec.json` over HTTP, so
the WiFi/OTA passwords are absent (only their lengths are recorded). See W5
below.

**Still open:** progressive drift *during* an active stream. The evidence rules
out the "growing UDP backlog" hypothesis — DDP lands on an AsyncUDP callback
that overwrites the realtime pixel buffer rather than queueing — but heap
fragmentation and WiFi power-save are untested. That needs the soak run, which
is only meaningful once the latch is fixed, because until then the device never
returns to a known state between tests.

**Instrumentation is built.** Two tools, because the proxies and the actual
symptom are different measurements:

`tools/wled_soak.py` samples `/json/info` (heap, uptime, RSSI, live/lm, device
FPS, HTTP reply time) and the bridge's `/api/status` (render gaps, DDP send
timing, stalls, counters) onto one timeline. Its `analyse` subcommand reports
trends, flags counter resets that void deltas, and detects the realtime latch.

`tools/wled_lag.py` measures the symptom itself — how far the strip is behind
the bridge. It works because the two ends are directly comparable on this rig:
the bridge publishes what it just rendered at `/api/status` `preview.strip`
(60 cells, a 2:1 downsample of the strip), WLED publishes what it is displaying
at `/json/live` (120 hex triples out of the live buffer DDP writes into), and
with `bri: 128` and `if.live["no-gc"]: true` the device is just a linear scale
of the bridge. Each side is reduced to one scalar per sample (total frame
brightness) and the lag is the shift that maximises cross-correlation;
normalising each series cancels the brightness scale and the downsample.

**Read only the deltas, not the absolute number.** The two endpoints are polled
over HTTP with very different round trips (Tower is sub-millisecond, the ESP32
is ~30 ms), and the probe attributes each reading to the midpoint of its own
request, so there is an uncorrected systematic offset — the first baseline came
back *negative* (`-60 ms`, correlation 0.66), which is physically impossible and
is exactly that offset showing. Constant latency vs. progressive drift is
answered by whether the number *moves* across a long soak, which is unaffected.

```bash
.venv/bin/python -m tools.wled_soak log --out soak.jsonl --interval 30 --duration 6h
.venv/bin/python -m tools.wled_soak analyse soak.jsonl
.venv/bin/python -m tools.wled_lag probe --seconds 45 --out lag-t0.json
```

What we know about the conditions:

- Pausing does **not** stop the packet stream. YARG keeps sending ~90
  datagrams/s with `paused=2`, so `IDLE_TIMEOUT` never fires and the bridge keeps
  rendering and sending DDP the whole time.
- At 60 FPS that is roughly **860,000 DDP frames** and ~310 MB of UDP over four
  hours, sustained, to a WiFi-attached ESP32.
- Live baseline is WLED 0.14.4, APA102/type 51 on data GPIO 18 / clock GPIO 5 at
  5 MHz, 120 pixels (`evidence/wled-live-baseline-2026-07-27.md`).

Candidate causes, in rough order of suspicion — **none confirmed** when first
written; see the root-cause section above for what has since been settled:

1. ESP32-side accumulation over a long realtime session (heap fragmentation, a
   growing UDP backlog, or a WiFi power-save state) adding latency that never
   drains. *(The UDP-backlog part of this is now ruled out.)*
2. APA102 clock/data timing degrading at 5 MHz once the device has been warm and
   busy for hours.
3. Something in WLED's realtime/DDP timeout handling that behaves differently
   after a very long uninterrupted stream.

**"Desync" needs pinning down before anything else.** It could be constant added
latency (lights consistently late), progressive drift (getting later), or dropped
frames making motion stutter. Those have different causes. Cheapest evidence:

- Reproduce with the replay CLI rather than a 4-hour game session — replay a
  capture on a loop for hours and watch, which is exactly what the harness is
  for.
- Read `/json/info` (heap, uptime, WiFi RSSI) periodically across the run and see
  what moves. The bridge already fetches WiFi info; extend that to log heap.
- Compare bridge-side timing over the same window (`render.gap_ms_max`,
  `ddp.send_us_max`, `stalls`) to rule the host out — if bridge telemetry stays
  flat while the strip drifts, the device is proven guilty.
- Consider whether pausing should stop DDP output entirely after some period.
  A paused game does not need 60 FPS of unchanged frames, and not sending is the
  most reliable way to avoid whatever the long-stream state is.

Related: this is a strong argument *for* the firmware qualification item below —
but note it also means a firmware upgrade could mask the cause rather than fix
it. Capture the evidence first.

---

## OPEN `bugfix` — The bridge's cached WLED power state never resyncs after a device reboot

**Found:** 2026-07-28, immediately after the realtime-latch fix, when the
rebooted controller came up `on: true` and stayed lit with the bridge idle.

`WLEDPower._wled_on` is only ever assigned in four places (`main.py`): the `False`
initialiser, `_power_on()`, `_power_off()`, and the **one-shot** probe at the top
of `_watchdog_run()`. The periodic reachability check discards its own answer:

```python
if check_counter >= 6:
    check_counter = 0
    if not self._wled_on:
        await asyncio.to_thread(self._api.is_on)   # result thrown away
    await asyncio.to_thread(self._api.fetch_wifi_info)

if not self._wled_on:
    continue
```

So if the device is powered on by anything other than the bridge — a reboot, the
WLED UI, a button press — the bridge goes on believing it is off, skips the idle
branch via that `continue`, and **never turns it off**. The strip stays lit
indefinitely (measured ~1622 mA) until YARG next connects, which powers it on
"again" and finally starts the idle timer.

This was invisible before the latch fix, because a latched controller never
rendered its own on-state: a reboot left the strip dark whatever WLED thought.
Fixing the latch made the divergence visible rather than causing it.

**The work:** assign the periodic `is_on()` result to `_wled_on`, so a device
that came up on gets adopted and idled off normally. Guard against racing
`_power_on_pending` / `_power_off_pending`, and note that adopting `True` should
also seed `_last_activity` or the strip will be switched off within one watchdog
tick of the bridge starting. Small, but it touches the code path the
PID-exhaustion bug lived in, so it wants its own change and a look at the rig.

**The mirror case, added 2026-07-29 while re-reading the code — do not fix only
one direction.** The periodic probe is *gated* on `not self._wled_on`, so the
opposite divergence is not merely uncorrected, it is never even looked for:

- bridge believes **ON**, device actually **OFF** (someone used the WLED UI, or a
  power blip) → no probe is ever issued, because the gate is false;
- `on_activity()` (`main.py:223`) only sets `_power_on_pending` when
  `not self._wled_on`, so **a song can start against a dark strip** and the
  bridge will not try to power it on;
- `_check_dark_while_active()` is the guard written for exactly that symptom, but
  it returns early when `self._wled_on` is true (`main.py:203`) — so it cannot
  see the one case it exists for. It catches a *failed* power-on, not a false
  belief that power is already on.

So the fix is "adopt the device's answer, both directions, unconditionally",
not "assign the result inside the existing `if`". Dropping the gate also costs
one extra `/json/state` request per 30 s, which is the same call
`fetch_wifi_info` already makes unconditionally on that tick.

**Evidence to get first** (this is what the rig is for, and it is cheap):
compare the bridge's belief against the device's truth on the live container —
`GET /api/status` (power block) versus `http://192.168.0.53/json/state`'s `on`.
If they already disagree while the bridge is idle, that is the bug reproduced
without having to stage anything.

---

## DONE 2026-07-29 `feature` — Spotlight cues light too narrow a slice of the strip

**Raised:** 2026-07-27 by the operator: the spotlight cues light one small
section, and should cover roughly **2–3 sections**.

**This entry's original write-up was wrong on both counts, and that is the
main lesson in it.**

- It read the request as *one wider window* and called the fix "mostly one
  number". On 2026-07-29 the operator clarified: they want **2–3 separate
  spotlights, each about the size of the current one** — a stage lit by a few
  lamps, not one bigger lamp. "Sections" meant *spotlights*, not cells.
- Its arithmetic used a 12-LED cell (96px strip). `CELL_SIZE` is
  `LED_COUNT // 8` = **15**, so the mapped strip is 120px and every figure
  quoted was 25% low. `BLACKOUT_SPOTLIGHT` at 0.18 was 20px / 1.33 cells, not
  "~22 LEDs / 1.4 cells".

Both errors came from reasoning off the entry instead of the code. The widths
now have tests; they did not before, which is how this drifted unnoticed.

**What was built.** `_center_window` was generalised to
`LEDMapper._spot_windows(count, fraction)` — `count` evenly tiled windows,
`fraction` the width of **one** spot. At `count=1` it is bit-identical to
`_center_window` for every fraction, so no other caller moved. Two new effect
keys (`spotlight_count`, default 1; `spotlight_chase`, default 0.0) leave every
non-spotlight cue rendering exactly as before.

| cue | before | after |
|---|---|---|
| `BLACKOUT_SPOTLIGHT` | 1 × 20px (px 50–70) | 3 × 20px (10–30, 50–70, 90–110) |
| `SILHOUETTES_SPOTLIGHT` | 1 × 48px (px 36–84) | 3 × 30px (5–35, 45–75, 85–115) |

`BLACKOUT_SPOTLIGHT`'s per-spot width is *unchanged*: `spotlight_region=0.18`
already yielded exactly 20px (`int(120×0.18/2)=10`), so only the count changed
— "about the size the current one is" holds by construction, not by rounding.
The two cues stay a deliberate pair: tight 20px spots vs 30px pools.

All three spots stay lit at every instant; a chase pumps one to full while the
others hold at `SPOT_DIM_FLOOR` (0.35), advancing one spot per beat. **The
chase is driven from the engine's free-running `beat_clock`**, the way
`gradient_roll` already is — *not* from `_start_beat_pattern` /
`_start_listen_pattern`. That was deliberate: those are the event-driven
mechanism behind the five cues this backlog records as un-replayable, and these
cues must not join that set. `beat_clock` is 0.0 before the first beat, which
parks the emphasis on spot 0 rather than blanking the cue.

> **Superseded the same day.** The operator asked for the spots to **pulse to
> the beat instead of chase**; see the entry below. The geometry above stands
> unchanged — only the per-spot level does. `spotlight_chase` and
> `SPOT_DIM_FLOOR` no longer exist.

**Cost:** +0.030 ms/frame on `SILHOUETTES_SPOTLIGHT` (the new level-mask pass
vs. the old `_mask_outside`). Both spotlight cues still render cheaper than a
plain wash.

**Live-look change:** `BLACKOUT_SPOTLIGHT` now lights 50% of the strip where it
lit 17%. That is a visible change to how dark a "blackout" cue leaves the
stage, and is the thing to judge on the rig.

`blackout_spotlight` was also added to `status_server.TEST_PATTERNS` — it was
missing, so the operator could not trigger from the dashboard the very cue they
reported.

**Tests:** new `tests/test_spotlight.py` (15) pins the geometry, the chase
levels/advance/wrap, that the chase reads the injected clock rather than the
wall clock, and that the cue still lights before any beat arrives. A
`spotlight_cues` replay fixture plus golden digest covers it end to end. The
nine pre-existing golden digests did **not** move, confirming no prior fixture
exercised either cue.

**Rollback:** set `SPOTLIGHT_COUNT` to 1 and `SPOTLIGHT_WIDTH_WIDE` back to
0.40; the default-1 path is the pre-change renderer exactly.

---

## DONE 2026-07-29 `feature` — Spotlights should pulse to the beat, not chase

**Raised:** 2026-07-29 by the operator, on the multi-spot cues delivered earlier
the same day (entry above): *"spotlights should pulse to the beat instead of
chase."*

The chase walked the emphasis spot→spot, one spot per beat. Two things were
wrong with it, and both are reasons to prefer a pulse rather than to retune one:

- it reads as the **lamps switching around** rather than as the stage responding
  to the music — the movement is in space, where a real three-lamp rig's is not;
- at three spots it imposes a **3-beat cycle** on 4/4 music, so the emphasis
  landed on a different beat of the bar every bar and never agreed with the song.

**What was built.** The per-spot level is now one shared beat envelope: full the
instant a beat lands, easing back over the beat to a `1 - depth` trough.
`spotlight_chase` (spots per beat) became `spotlight_pulse` (depth), and the
`beat_clock` read became a `beat_phase` read.

| | before | after |
|---|---|---|
| driver | `beat_clock` (free-running count) | `beat_phase` (0→1 across one beat) |
| spot levels | one at 1.0, rest at 0.35 | all at `1 - 0.65·(1 - (1-phase)²)` |
| period | 3 beats (one trip round the spots) | 1 beat |
| geometry | unchanged | unchanged |

The envelope is deliberately **the same squared decay as the global `beat_pulse`
pump**, so where a cue has both they read as one gesture rather than two
rhythms. `SPOTLIGHT_PULSE_DEPTH` is 0.65, leaving a 0.35 trough — exactly the
level the un-emphasised spots of the chase sat at, so "2–3 spotlights, all
visible at every instant" still holds at the dimmest point of the beat.

**Two edge cases rest at full, not dark**, which is the part worth keeping:

- **before the first beat** `beat_phase` is 0.0, which is the peak — the cue
  lights fully, as it must for a song that never sends a beat;
- **after the beats stop** `beat_phase` saturates at 1.0 and stays there, which
  read alone would park a finished or disconnected song on a stage dimmed to the
  trough. A new `CueEngine.beats_live()` (the `BEAT_LOCK_TIMEOUT` test that
  `tick()` already used inline, promoted to a method and published as a
  `beats_live` effect key) separates that from a single dropped beat packet,
  which *does* rest at the trough — a beat's worth of no motion, which is what
  `beat_phase`'s saturation is for.

**Tests:** `tests/test_spotlight.py` reworked to 17. The chase tests became
pulse tests: peak on the beat, monotonic decay across it (a pulse that
brightened mid-beat would read as two hits), the trough's value, all spots in
lockstep, and both rest-at-full edge cases. Levels are measured against the same
cue rendered at its peak rather than a hardcoded colour, so the assertions read
the envelope alone — `SILHOUETTES_SPOTLIGHT` also breathes.

**Digest:** `spotlight_cues` moved, and **only** `spotlight_cues` — it is the
only fixture using either cue, which is the confirmation the change is contained.

**Rollback:** set `SPOTLIGHT_PULSE_DEPTH` to 0.0. The spots then hold at full
and the cues are static geometry; the chase itself is gone, so restoring *it*
means reverting the commit.

---

## DONE 2026-07-29 `bugfix` — Beat-locked cues lurch or freeze for up to a second at cue change

**Raised:** 2026-07-29 by the operator, about `SEARCHLIGHTS`: *"a rough start
before smoothing out."* It was neither specific to that cue nor cosmetic.

**Cause.** `_TimePattern` launched with `pos = 0.0` — step 0 — no matter where
the song was. The PLL's lock target is a function of the beat clock, so a cue
starting at step 0 mid-song began with an arbitrary phase error of up to half the
pattern, which the loop then ate at `PLL_TAU` (0.1 s). Because the correction is
clamped forward-only (motion must never run backwards), the two signs of that
error failed differently and both were visible:

- **positive error → a sprint.** The first frame advanced up to 20× the steady
  rate, decaying over ~0.3 s.
- **negative error → a dead stop.** `advance` clamped to 0 and the chase froze
  until the target came round — up to **1.0 s** with no motion at all.

Measured by sweeping every launch phase and both beat parities (the sign depends
on beat parity, which is why it looked intermittent):

| cue | worst overspeed | worst freeze |
|---|---|---|
| `SEARCHLIGHTS` | 5.7× for ~0.27 s | 417 ms |
| `CHORUS` | 10.7× | 917 ms |
| `HARMONY` | 20.7× | 1000 ms |
| `BIG_ROCK_ENDING` | 5.7× | 417 ms |

Note the ordering: **the transient scaled inversely with the cue's speed**, so
the calmest cues — the ones where a stutter is most obvious — were hit hardest.
`SEARCHLIGHTS` was where the operator noticed it, not where it was worst.

**Fix.** Seed `pos` from the beat clock at launch (`_launch_beat_clock` →
`_TimePattern.beat_target`), so the loop starts with zero error. Every
beat-locked cue now launches at exactly its steady rate, at every launch phase
and beat parity — verified by the same sweep that measured the problem. This
leaves the PLL doing what it is for: tracking tempo and beat drift *while* a cue
runs, not absorbing a launch artefact.

A cue's patterns are seeded from one beat clock, so several launched together —
a cue's two counter-rotating scanners, at different rates — start in the phase
relationship they would have had if the cue had been running all along.

**Unseeded on purpose:** a cold start (no beat yet) and a launch after the beats
have gone stale both keep `pos = 0.0`. Neither has a phase to lock to, and
seeding from a coasted `beat_clock` would be inventing a position. `FRENZY`'s
`reverse_on_beat` patterns are not beat-locked at all and keep the free-run
scheduler.

**A near-miss worth recording.** The first version of this also wrapped the PLL
target into `[0, n)` inside `beat_target`, which is algebraically a no-op — the
error is reduced mod `n` regardless — but not a *bit-identical* one. It moved
`rapid_cue_changes`' digest on float rounding alone. Caught by attributing every
moved digest to a specific change before touching the tables; `beat_target` now
returns the unwrapped value and callers wrap. **The digest tables are the tool
that found this**, which is the argument for the rule at the top of them.

**Tests:** `tests/test_beat_lock.py` +5. The headline one sweeps all eight
time-driven beat-locked cues × 4 beat parities × 10 launch phases and asserts
every frame of the first second moves at the pattern's steady rate ±2%; it fails
loudly with the seeding reverted. The rest pin the mechanism and the three
deliberately-unseeded cases.

**Digest:** six fixtures moved — every one that changes to a beat-locked cue
mid-stream. The six that held still are the invariant: four launch their only
beat-locked cue before any beat arrives (a cold start is bit-identical) and two
use only counter-stepped cues, which this does not touch.

**Rollback:** make `_launch_beat_clock` return `None` unconditionally.

---

## DONE 2026-07-29 `bugfix` — The keyframe tempo fallback steps off the beat

**Found 2026-07-29** by the transition sanity-check that produced the two entries
above, not reported from the rig. It is the same defect as the launch-phase one,
in the other pattern type.

`_CounterPattern`'s tempo fallback exists so a keyframe-stepped cue does not
freeze on a chart that sends no keyframes. It steps on a timer seeded at cue
launch (`next_fallback_time = now + interval`), so **its phase is whatever the
cue's launch instant happened to be** and it never aligns to the beat:

| cue launched after a beat | wash flips at beat phase |
|---|---|
| 0.0 s | 0.00 (on the beat) |
| 0.1 s | 0.20 |
| 0.2 s | 0.40 |
| 0.3 s | 0.60 |
| 0.4 s | 0.80 |

Stable, not drifting — just arbitrarily offset. On `DEFAULT` the step is a
full-strip blue↔red swap with no fade (a 510/pixel frame-to-frame delta, the
largest of any cue), so landing it squarely between beats is very visible.
Affects the four cues with a keyframe fallback: `DEFAULT`, `WARM_MANUAL`,
`COOL_MANUAL`, `STOMP`.

Where the fallback is *running*, there is by definition no authored rhythm to
respect, so quantising it to the beat clock is strictly better than an arbitrary
phase.

**What was built.** The fallback now steps on an **absolute grid** of
`KEYFRAME_FALLBACK_BEATS`-spaced positions on the beat clock, rather than on a
wall-clock deadline seeded at launch. `fallback_beats()` (the interval in beats,
tempo-independent) joins the existing `fallback_interval()` (the same in
seconds), and `grid_steps_due()` is a pure function of the beat clock and the
last arm point — so it stays replay-deterministic, like everything else on this
seam.

Two constraints pull against each other here and both are pinned by tests:

- **steps land on the beat** — the grid is absolute (multiples of the interval on
  the beat clock), *not* offsets from the arm point. Offsets from the arm point
  would have reproduced the original bug exactly.
- **the overdue allowance is still honoured in full** — the step lands on the
  first grid boundary at or after `arm + interval`. Arming 0.9 beats before a
  boundary must not fire 0.1 beats later, so a fallback step can be *late* by up
  to one interval but never early. Firing early would mean a cue stepping almost
  immediately after the keyframe that armed it.

**A chart-driven step is still never quantised.** A real keyframe re-arms rather
than snapping: a chart's keyframes *are* the rhythm and may be deliberately
syncopated, so pulling them onto the beat would flatten the chart. This is the
invariant most at risk from a future change here, so it has its own test that
drives keyframes at 0.3 s against a 0.5 s beat and asserts the steps land off the
beat.

**The one case with no grid** is when no beat has *ever* arrived: `beat_clock` is
pinned at 0.0 and frozen, so there is nothing to align to and the wall-clock
timer carries the cue as before. Note this is a deliberately weaker test than the
launch-phase seeding's (`_fallback_beat_clock` vs `_launch_beat_clock`): a
**stale** beat clock is fine here, because it free-runs at tempo and so still
says where the beats would be, which is exactly what a fallback stepping on
tempo wants. Seeding a *position* from a coasted clock invents information;
continuing a grid on one does not.

**Digest:** exactly one moved — `keyframe_starved`, the fixture built for the
starved path. The earlier draft of this entry predicted `default_keyframes` would
move too; **it did not, and that is the better result.** It sends a keyframe on
every packet, so its fallback never runs. `default_keyframes` holding still is
the end-to-end proof that the grid has not leaked into the chart-driven path.

**Tests:** `tests/test_counter_patterns.py` +7 (32 total). The headline one sweeps
all four fallback cues × 10 launch phases and asserts every step lands within one
frame of a beat; it fails with the fix reverted. The rest pin the allowance, the
no-quantising-the-chart invariant, re-arming on the grid path, no burst after a
long gap, the no-beats-ever path, and that stale beats still use the grid. Two of
them assert they are *on* the grid path first, so they cannot silently drift onto
the wall-clock path and pass for the wrong reason.

**Not addressed:** the grid is aligned to the beat, not to the **bar**. All four
cues work out to a 1.0- or 2.0-beat interval, and a 2.0-beat grid lands on every
other beat-clock position, which is not necessarily a downbeat — `beat_clock`
counts from the first beat seen, not from a bar boundary. Landing on a beat was
the defect; landing on the *right* beat of the bar is a further refinement, and
`bar_phase`/`_bar_beat` already carry what it would need.

**Rollback:** make `_fallback_beat_clock` return `None` unconditionally — every
pattern then takes the wall-clock path, which is the pre-change behaviour.

---

## Reliability-push headline items

The original five-workstream push. **"Next up" above now takes priority over
items 3–5 here** — the rig-observed items came from the rig's actual behaviour,
which beats a plan made before it was watched this closely, and the two `bugfix`
items and the event-driven-cue `refactor` now sit ahead of these too.

See `VISION.md` "Current focus — next push: reliability, replay, alignment, and
state" for the full description of each:

1. `DONE` (2026-07-27) — Release the live baseline: merged
   `fix/bridge-review-findings` to `main` (a fast-forward), tagged `v1.0.0`,
   added the CI test gate, and reconciled README/VISION with the deployed
   feature set. The four commits on top of the live commit `117952b` contained
   no runtime changes — only docs and the offline `venue_scan/` package — so the
   release was a re-tag rather than a behaviour change.
2. `DONE` (2026-07-29) — Timestamped YARG datagram capture/replay harness with a
   headless DDP receiver in CI. Merged to `main` together with palette
   strictness and the WLED desync tooling.
3. `OPEN` `feature` — Persisted operator lighting-alignment trim plus a
   calibration pattern. Default 0 ms.
4. `OPEN` `feature` — Expanded read-only game/bridge state, optionally over MQTT.
5. `OPEN` `ops` — WLED 0.14.4 firmware upgrade qualification with backups, an exact
   rollback image, and proven recovery. **Blocked in practice until the realtime
   latch above is fixed:** a latched controller never runs `ArduinoOTA.handle()`,
   so it cannot be OTA-flashed at all, and it latches on the first DDP frame
   after every boot.
   Also revised on 2026-07-28: step 1 of the plan ("export `cfg.json`,
   `presets.json`, …") is **not sufficient for restore**. WLED holds secrets in
   `wsec.json`, which it never serves over HTTP, so an HTTP export comes back
   with the WiFi and OTA passwords stripped — only their lengths survive. A
   complete backup needs `wsec.json` off the flash over USB/serial, the same
   physical access the plan already wants for recovery. The device has no
   presets, so there is nothing to lose there.
6. `DEFERRED` `feature` — Multi-WLED logical canvas and performer-region routing; blocked
   on having enough equivalent hardware to test geometry and partial failure.

---

## OPEN `evidence` — Classify every rig finding by whether the wired QuinLED will fix it

**Raised:** 2026-07-28 by the operator, who expects to move to a QuinLED quad
with ethernet once it ships.

Findings about the lighting rig fall into four classes, and conflating them
leads to buying hardware that fixes nothing:

1. **Transport** (WiFi RSSI, association, retries, airtime contention) — a
   wired board fixes these outright.
2. **This specific board** (Athom, APA102 at 5 MHz on GPIO 18/5, its power
   design) — new hardware very likely fixes these, but check the replacement
   actually supports **clocked** LEDs (data *and* clock) on the channel you
   intend; digital LED boards differ on this and it is not safe to assume.
3. **ESP32/WLED firmware architecture** (a single-threaded sketch where
   `strip.service()` and the async web server contend) — a wired board does
   **not** fix these. Same firmware, same loop.
4. **WLED configuration** (e.g. the realtime-timeout latch above) — these
   **follow you to the new board**. The realtime timeout must be set on the new
   controller before streaming to it, or it will latch identically.

The classification table lives in
`~/yarg-lighting/evidence/wled-realtime-latch-2026-07-28.md`. Keep it current as
findings land; it is the input to the buying decision, not a retrospective.

**The measurement that decides class 1 vs class 3** is in `tools/wled_soak.py`:
sample TCP connect time and HTTP reply time against the device together. TCP
handshakes complete in lwIP/AsyncTCP, below the Arduino `loop()`; an HTTP
response needs `loop()` to run. Both rising together means the network path is
at fault (ethernet helps); HTTP rising while TCP stays flat means the main loop
is blocking (ethernet does not help). `analyse` prints the verdict.

Note RSSI alone cannot settle it — it reports link strength, not congestion or
airtime, so a flat RSSI correlation does not clear WiFi.

---

## DONE 2026-07-29 `perf` — Cue changes are crossfaded over 250 ms, and that is the visible "lag"

**Found:** 2026-07-28, measuring step latency end to end after the operator
described the symptom precisely: *"changing patterns where I could see the
colour change lagging on the strip."*

`main.py:416` sets `self._FADE_DURATION = 0.25`. On every cue change the render
thread snapshots the last sent frame into `_fade_from` and linearly blends the
incoming frames against it for a quarter of a second.

**Measured, all with the same detection method so the overheads cancel:**

| Path | Median | Range |
|---|---|---|
| Direct DDP, bridge bypassed | **22.8 ms** | 13.5–84.8 |
| Via the bridge (cue-change step) | **145–164 ms** | 29–364 |
| Downstream only (bridge's own frame → strip) | **31.7 ms** | — |

The network and the device cost ~23–32 ms. The bridge adds ~120 ms, and the
crossfade is where it goes. Confirmed by the fact that the added latency does
**not** scale with tempo — 171 / 179 / 178 ms measured at 240 / 120 / 60 BPM. It
is wall-clock, not beat-locked.

**This is a design choice, not a defect** — the crossfade exists so cue changes
don't snap harshly, and the code says so. But 250 ms is long enough to read as
hesitation on cues that should land instantly (`BLACKOUT_FAST`, `FLARE_FAST`),
and it is the single largest latency term in the whole pipeline by a factor of
five.

**The part worth checking next:** when cues change faster than 250 ms, each new
fade restarts from `_last_sent`, which is itself mid-fade. Successive changes
would then never reach their target colour and the strip would show a
perpetually smeared blend — which would look exactly like "hitches and glitches"
rather than clean transitions. Whether real songs change cues that fast is a
question for a capture of an actual dense song, not for synthetic patterns.

**Options:**

1. Shorten `_FADE_DURATION` (e.g. 0.08–0.12 s) — one constant, immediately
   testable by eye.
2. Make it per-cue: instant for `BLACKOUT_*`/`FLARE_*`, longer for the
   atmospheric cues where a soft change is the point.
3. Make it an operator setting, defaulting to today's value so nothing changes
   without a deliberate choice.
4. Skip or shorten the fade when a new cue arrives while one is still running,
   so dense passages don't compound.

**Classification for the hardware decision: this is bridge/code class.** An
ethernet QuinLED will not change it by one millisecond. See the classification
item below.

### What was built

Options 1, 2 and 3 together; option 4 was **measured and deliberately not
acted on** — see below.

- `settings.cue_fade_ms`, default **120**, 0–1000, with a dashboard slider,
  `POST /api/settings`, persistence, and a pin in `replay/player.py`.
- `settings.CUE_FADE_MS_OVERRIDES` gives per-cue durations, because **YARG
  already tells us which transitions are meant to be abrupt** and the bridge
  was throwing that away: `BLACKOUT_FAST` and `BLACKOUT_SLOW` are separate cue
  bytes and both were fading over the same 250 ms. `BLACKOUT_FAST`,
  `FLARE_FAST`, `FRENZY` and the four `STROBE_*` cues snap (0 ms);
  `BLACKOUT_SLOW` and `FLARE_SLOW` hold 250 ms whatever the base is; everything
  else uses the base. `STROBE_OFF` deliberately stays on the base — it is a
  *return* to a lit look, where softness is still wanted.
- The duration is **latched at cue-change time** rather than read per frame, so
  dragging the slider mid-fade cannot rescale a fade in flight and make it
  jump. A zero duration leaves `_fade_until` at 0.0 so the blend is skipped
  entirely rather than run at zero length — there is no division to guard.
- `CueEngine.get_effects()` now publishes `fx["cue"]` alongside
  `cue_change_at`; the render thread needs the incoming byte, not just the
  timestamp. That is the whole engine-side change.

**Cost: none measurable.** 0.628 → 0.626 ms/frame across the
`rapid_cue_changes` fixture, which is a worst case (the strip is fading
essentially all the time). Fewer faded frames means fewer runs of the per-byte
blend loop, but the saving is inside the noise; do not claim it as a win.

### The compounding question (option 4) — measured, real, and left alone

Each new fade starts from `_last_sent`, which during a fade is itself a blend,
so a stream changing cue faster than the fade could in principle never reach
any cue's real colour. That was written up here as "the part worth checking
next". It is now measured, against a new `rapid_cue_changes` replay fixture
(80 ms dwell per cue, built from base-fade cues, since the snapping ones would
prove nothing), compared frame-by-frame against the same stream replayed with
the fade disabled:

| base fade | mean per-channel deviation from the un-faded target | frames landing exactly on a cue's colour (of 90) |
|---|---|---|
| 250 ms (old) | 66.4 | **0** |
| 120 ms (new default) | 55.7 | **0** |
| 40 ms | 25.3 | 43 |

So the compounding is **real**: at an 80 ms dwell the strip never once shows
any cue's actual colour at either 250 ms or the new 120 ms. Shortening the fade
reduces the smear by ~16% but does not remove it. When the changes stop the
strip lands exactly on target, so it recovers rather than drifting permanently.

**What this does not establish is whether real songs change cue that fast.**
The fixture is synthetic and was built to force the condition. Answering the
exposure needs a capture of a genuinely dense song — which is exactly what this
entry originally said, and it is still true, so the restart behaviour was not
changed on synthetic evidence. `tests/test_cue_fade.py::
test_fades_do_compound_when_the_dwell_is_shorter_than_the_fade` pins the
limitation the same way the event-driven-cue test does: fixing it fails that
test and prompts an update, rather than passing silently.

**Verification:** 553 tests green, up from 524 — 17 new in
`tests/test_cue_fade.py` plus the new fixture across the digest tables. Seven
of the ten pre-existing golden digests moved (the three that did not —
`authored_venue`, `dropped_beats`, `warm_beats` — are the fixtures that never
change cue mid-stream, which is the expected signal).
`tests/test_replay.py::test_pre_fade_settings_reproduce_the_old_look` pins the
rollback: at `cue_fade_ms=250` with an empty override table, all ten reproduce
their previous digests **exactly**, so "bit-exact rollback" is under test rather
than asserted.

**How to confirm it on the strip, whenever that happens:** `tools/wled_lag.py
step` before and after, expecting the via-bridge median to fall from 145–164 ms
toward ~55–80 ms against an unchanged ~23 ms direct floor. Compare like cues —
the snapping ones now measure differently from the base-fade ones.

**Rollback is two things, and the slider alone is not enough:** `cue_fade_ms`
to 250 restores the base, but exact pre-change behaviour also needs
`CUE_FADE_MS_OVERRIDES = {}` in `settings.py`, because the per-cue table is
code-level. Same shape as the spotlight rollback.

---

## OPEN `bugfix` — Bridge keeps a stale lit cue after disconnect/power-off

**Found:** 2026-07-28 by the operator, watching the dashboard during the latency
work: *"the bridge is still on a cue, even though the device is off and the
strip is dark."*

Confirmed. With no YARG sender and WLED powered off through `/api/power`:

```
connected=False   cue=SCORE (id 31)   zones_raw=[136, 0, 0, 34]   preview sum=980
```

The render thread keeps producing a lit frame from the last cue; it is simply
not sent. So the dashboard shows an active cue and a lit strip preview while the
real strip is dark, and a later power-on resumes that stale frame instead of
starting clean.

`CueEngine.on_cue()` only reacts to a *change* of cue byte, and nothing resets
cue/zone state on disconnect or power-off. Low severity — it misleads rather
than breaks — but it directly contradicts what the operator sees in the room,
which is the one thing the dashboard exists to avoid.

**The work:** reset cue/zone state (and the preview) when the connection drops
or WLED is powered off, or mark the status explicitly as "holding last cue, not
sending" so the dashboard cannot be read as live output.

**Confirmed against the code 2026-07-29, and the two options are not equivalent —
decide before building.**

Mechanically: `_current_cue` is written only in `__init__` and `on_cue()`
(`cue_engine.py:1365`), both on the packet path, so nothing off that path can
reset it. `connected` is *not* part of the problem — it is computed live from
`_last_packet_time` against `CONNECTED_TIMEOUT` (3.0 s, `status_server.py:110`)
and is already correct. What is stale is `cue`/`zones`/preview, which
`on_render()` (`status_server.py:130`) copies from the engine every frame.

- **Reset the state.** Honest dashboard, and a later power-on starts clean rather
  than resuming a stale frame. But holding the last cue through a brief gap is
  *deliberate* — YARG sends at ~88 Hz and a blackout on one dropped datagram
  would be much worse than a stale cue. So a reset needs its own timeout, which
  means picking a second threshold distinct from `CONNECTED_TIMEOUT` and deciding
  what "reset" paints (NO_CUE is a blackout; that is a visible behaviour change
  on the strip, not just on the dashboard).
- **Label it as held.** No change to rendered light at all — purely a status/UI
  truth fix, so no golden digest can move and there is nothing to judge on the
  rig. Strictly smaller, and it does not require inventing a threshold.

**Recommendation: label it as held.** The reported complaint is that the
dashboard contradicts the room, and labelling fixes exactly that without
touching what the strip does. A reset is a lighting-behaviour change wearing a
bug fix's clothes, and it should be its own decision if wanted. Note the
render/DDP path is *already* gated correctly — the frame is produced and simply
not sent — so nothing is reaching the strip either way.

Not to be confused with the render thread continuing to tick: that is correct and
should stay. The bug is only that the dashboard presents an unsent frame as
output.

---

## OPEN `evidence` — Desync: not reproduced since the realtime-timeout fix; testing is parked, not finished

**Status 2026-07-28 (end of day):** two instrumented runs since the latch fix,
neither reproduced the failure. Parked at the operator's request — the tooling
stays ready and is called back up the next time the symptom appears.

| Run | Condition | Result |
|---|---|---|
| 1 | 3.5 h continuous DDP, synthetic stimulus | step latency 145 ms → 164 ms. Flat. No reproduction. |
| 2 | Real song, then **2h51m paused** with the stream still running (585,366 DDP frames) | lag −60/−40 ms before → **−50/−50 ms after**. Operator independently: "looks pretty good still". No reproduction. |

Run 2 is the meaningful one: it is the operator's exact trigger, and their visual
verdict and the instrument agreed **without either seeing the other first**.

**Not settled.** The original failure took **4+ hours**; run 2's pause was
2h51m. A longer pause is the remaining test and costs nothing but time.

**Next time it appears — do not power cycle first.** The bad state is the
evidence. `tools/RUNBOOK.md` has the capture sequence, the healthy reference
values to compare against, and which probe to trust under which conditions.

### Secondary finding: device RSSI degrades across a session

Observed in both runs: −34 dBm fresh, drifting ~**−1.38 dB/hour**, restored by a
power cycle. It is the only metric seen so far that accumulates over a session
and clears only on power cycle — the same shape as the reported symptom.

**But at the magnitude observed it is benign.** Across run 2's −34 → −39 dBm
drift, heap, device FPS, TCP connect and HTTP reply were all flat, and neither
the lag measurement nor the operator noticed anything. Worth continuing to watch;
not worth acting on. It sits in the class a wired board would eliminate, but it
is now well below the 250 ms cue crossfade in priority — that costs ~120 ms on
every cue change and no hardware change will touch it.
