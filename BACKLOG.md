# Backlog

Per the mission-control protocol, this is the full backlog; the dashboard in
`~/mission-control/README.md` carries only the headline plus a link here.

Item status: `OPEN` · `IN PROGRESS` · `BLOCKED` · `DONE` · `DEFERRED`.

## Next up (set 2026-07-28)

These three came from watching the rig, so they take priority over the remaining
reliability workstreams. In order:

1. **[WLED desync after hours of DDP](#open--wled-drifts-out-of-sync-after-hours-of-ddp-only-a-physical-power-cycle-clears-it)**
   — first, because it's the only one that breaks a show rather than merely
   looking wrong, and because the diagnosis is *evidence gathering*, not a code
   change. Do not touch lighting code for it until "desync" is pinned down to
   constant latency, progressive drift, or dropped frames. Start with a
   long-running replay loop rather than another 4-hour game session.
2. **[Spotlight cues are too narrow](#open--spotlight-cues-light-too-narrow-a-slice-of-the-strip)**
   — a one-number change with an obvious answer, worth landing while the desync
   evidence accumulates.
3. ~~Newer layers ignore the selected palette~~ — **done 2026-07-28** on
   `feat/palette-strictness`; see
   [the write-up](#done-2026-07-28--newer-reactivity-layers-ignore-the-selected-palette).
   Awaits a look on the real strip.

Then back to the reliability push: W3 alignment trim, W4 read-only state/MQTT,
W5 firmware qualification. Note item 1 above is an argument *for* W5 but also a
reason to gather evidence before it, since a firmware upgrade could mask the
cause rather than fix it.

Blocking neither: `feat/replay-harness` is built and awaiting merge, and items 1
and 2 both get easier once it lands (a recorded passage replayed before/after
beats remembering last night's show).

---

## OPEN — VenueSize is hardcoded to Small, so Phase 8 density branching is dead code

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

## OPEN — Venue fixture coverage now has a measured answer

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

## OPEN — Render priority is now measured, not assumed

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

## OPEN — Event-driven cue patterns still run on asyncio, so replay can't judge them

**Found:** 2026-07-27, while building the replay harness.

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

---

## DONE (2026-07-28) — Newer reactivity layers ignore the selected palette

**Raised:** 2026-07-27 by the operator, after watching the vocal ribbon.
**Built:** 2026-07-28 on `feat/palette-strictness`. Still wants a look on the
real strip before it is called finished.

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

**Not done:** nobody has looked at this on the strip. Default-on changes the
live look, including under `default` (its ring is the four-colour fallback,
close to but not the rainbow). Rollback is the slider to 0.0, or `:1.0.0`.

---

## OPEN — WLED drifts out of sync after hours of DDP; only a physical power-cycle clears it

**Observed:** 2026-07-27 by the operator. Left paused on a song for **4+ hours**;
the strip ended up visibly out of sync with the game. Turning WLED off from the
container did **not** clear it — only pulling the controller's power did.

That last detail is the important one: **the bad state lives on the ESP32, not in
the bridge.** A container-side power-off is a JSON API call; it doesn't reset the
device. So this is very unlikely to be a cue-engine or render-thread bug, and
correspondingly unlikely to be fixed by anything in this repo.

What we know about the conditions:

- Pausing does **not** stop the packet stream. YARG keeps sending ~90
  datagrams/s with `paused=2`, so `IDLE_TIMEOUT` never fires and the bridge keeps
  rendering and sending DDP the whole time.
- At 60 FPS that is roughly **860,000 DDP frames** and ~310 MB of UDP over four
  hours, sustained, to a WiFi-attached ESP32.
- Live baseline is WLED 0.14.4, APA102/type 51 on data GPIO 18 / clock GPIO 5 at
  5 MHz, 120 pixels (`evidence/wled-live-baseline-2026-07-27.md`).

Candidate causes, in rough order of suspicion — **none confirmed**:

1. ESP32-side accumulation over a long realtime session (heap fragmentation, a
   growing UDP backlog, or a WiFi power-save state) adding latency that never
   drains.
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

## OPEN — Spotlight cues light too narrow a slice of the strip

**Raised:** 2026-07-27 by the operator: the spotlight cues light one small
section, and should cover roughly **2–3 sections**.

The strip is 8 cells of 12 LEDs. Current widths in `effects/cue_engine.py`:

- `BLACKOUT_SPOTLIGHT` — `spotlight_region=0.18`, about **1.4 cells** (~22 LEDs).
  This is the narrow one.
- `SILHOUETTES_SPOTLIGHT` — `spotlight_region=0.40`, about 3.2 cells, already in
  the requested range.

So the change is mostly `BLACKOUT_SPOTLIGHT`: 2–3 cells is `spotlight_region`
≈ **0.25–0.375**. Worth checking the two cues read as a deliberate pair
afterwards (a tight spot vs. a wider one) rather than collapsing into the same
look. `SILHOUETTES_SPOTLIGHT` may want a small nudge for contrast.

Cheap to try, and the replay harness can show the before/after on the same
recorded passage instead of relying on memory of last night's show.

---

## Reliability-push headline items

The original five-workstream push. **"Next up" above now takes priority over
items 3–5 here** — those three came from the rig's actual behaviour, which beats
a plan made before it was watched this closely.

See `VISION.md` "Current focus — next push: reliability, replay, alignment, and
state" for the full description of each:

1. `DONE` (2026-07-27) — Release the live baseline: merged
   `fix/bridge-review-findings` to `main` (a fast-forward), tagged `v1.0.0`,
   added the CI test gate, and reconciled README/VISION with the deployed
   feature set. The four commits on top of the live commit `117952b` contained
   no runtime changes — only docs and the offline `venue_scan/` package — so the
   release was a re-tag rather than a behaviour change.
   *Still outstanding:* the operator flips the Unraid template from the stale
   branch tag to `:latest` (rollback `:1.0.0`) and recreates the container.
2. `IN PROGRESS` — Timestamped YARG datagram capture/replay harness with a
   headless DDP receiver in CI. Built on `feat/replay-harness`, awaiting merge.
3. `OPEN` — Persisted operator lighting-alignment trim plus a calibration
   pattern. Default 0 ms.
4. `OPEN` — Expanded read-only game/bridge state, optionally over MQTT.
5. `OPEN` — WLED 0.14.4 firmware upgrade qualification with backups, an exact
   rollback image, and proven recovery.
6. `DEFERRED` — Multi-WLED logical canvas and performer-region routing; blocked
   on having enough equivalent hardware to test geometry and partial failure.
