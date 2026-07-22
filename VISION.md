# VISION — translating YARG's lighting data onto an LED strip, truthfully and elegantly

This is the design north star for the bridge: how every signal YARG broadcasts
*should* become light on the strip, and which techniques from the reference
projects get us there. It is a roadmap, not a spec — the current code implements
a subset (see **Status** at the end).

## The one constraint that shapes everything: DDP realtime

The bridge streams **host-computed raw RGB** to WLED over **DDP**. In realtime
mode WLED displays exactly the pixels we send and its **on-device engine is
bypassed** — segments, palette morphing, blend modes, `fadeToBlackBy`,
transitions: none of it runs. Our device is on firmware **0.14.4** (ESP32, 120
LEDs, single segment, RGB), but the firmware version barely matters while we use
DDP.

**Consequence:** every "borrow this from WLED" idea below must be **reimplemented
host-side in `effects/mapper.py`** — we cannot delegate it to the controller. A
future *hybrid* path (drive WLED segments/palettes via its JSON API instead of
DDP) is possible and 0.14.4 supports it, but it is a different architecture and
out of scope here. Assume host-side rendering throughout.

## Where the truth lives

- **The datagram** is built by YARG's `DataStreamController.SerializeAndSend()`
  (`DATAGRAM_VERSION = 4`), broadcast to `255.255.255.255:36107` at ~88 Hz.
  Layout is append-only across versions; we parse **by length**, not by version.
- **YALCY** is the canonical receiver/decoder and the reference for what each cue
  *means*. **photonics-dmx**, **LedFx**, and **WLED** are technique sources.
- `protocol/yarg_packet.py` is our decoder; `effects/cue_engine.py` turns signals
  into zone/effect state; `effects/mapper.py` renders pixels; `main.py` wires the
  UDP→engine→render-thread→DDP flow.

## Signal inventory → ideal strip translation

Every field, and what it should drive. **Bold** = implemented today.

| Signal (offset) | Meaning | Ideal translation |
|---|---|---|
| **LightingCue (34)** | the authored "look" | **the primary wash/chase per cue** (33 cues, `cue_engine._launch_cue`) |
| **StrobeState (37)** | strobe speed | **software strobe** (black-frame gate); *should* lock rate to BPM (YALCY `StrobeDmxFromBpm`) |
| **Beat (38)** | Measure/Strong/Weak pulse | **beat flash/sparkle/glitch**; *should* also feed a continuous beat/bar oscillator for smooth motion |
| **Keyframe (39)** | chart-driven step (First/Next/Prev) | **steps the manual cues** (`listen="keyframe"`) |
| **BPM (9)** | tempo | **pattern speed**; basis for the oscillator and tempo-locked strobe |
| **BonusEffect (40)** | one-shot big moment | **white celebration burst** (`bonus_t`) |
| **Paused (7) / Scene (6)** | game state | **pause dim + motion freeze**; scene shown on status page |
| **Star power (47+, v4)** | per-player overdrive amount + active | **charging cool tint → active "surge"** (lift + cool blend + shimmer) |
| **Camera cut (44–46, v3)** | who the camera is on + priority | *parsed & shown on status page*; future: subtle subject color/region bias + a cut accent |
| FogState (36) | haze on/off | lower contrast / add a soft blur-glow floor while foggy |
| **PostProcessing (35)** | 40+ film grades | **apply the color-tint ones** (Desaturated_Blue, Contrast_Red, SepiaTone, B&W…) as a global palette modifier; camera-only grades pass through |
| **Note bitmasks (14–17)** | per-fret/pad hits | **rising-edge accents with a note-hold min (1/32 note)** so transient hits are visible, painted per-instrument (YALCY DMX) |
| **Vocal/Harmony pitch (18–33)** | MIDI pitch, 0=none | **pitch→ribbon blob**: position by absolute pitch, hue by pitch class (chroma) |
| **Spotlight / Singalong (42,43)** | performer bitmask | **hue bias** toward the highlighted performer(s) |
| VenueSize (8) | small/large | density branching (sparser vs denser patterns), as YALCY does per-cue |
| SongSection (13) | Verse/Chorus/… | slow palette/energy bias per section |
| AutoGenVenueTrack (41) | chart has no authored venue | shown on status page (AUTO); could soften/idealize the look |

### Quirks to respect (from YARG source)
- **No song-time field** and **no drum-fill field** exist. Timing must be
  inferred from BPM + beat pulses; there is no absolute clock or fill-lane flag.
- **Star power is per-player only** — there is no pooled/aggregate value. We
  aggregate host-side (any-active, max-amount-among-active, max-charge-overall).
- **Beat "no-beat" sentinel:** after each send YARG resets the beat byte to `3`,
  which collides with YALCY's "Weak=3". Currently harmless because
  `cue_engine._run_beat_pattern` filters for Measure/Strong before acting, so a
  spurious "weak" only nudges listen-patterns. If we ever make Weak beats
  visually significant, disambiguate first (e.g. treat a repeated 3 with no
  intervening pulse as "no beat").

## Borrowed techniques, mapped to the bridge

Who does what best, and where it lands in our code:

- **Layer / slot compositor (photonics-dmx).** Model a look as concurrent slots —
  *primary wash + secondary overlay + strobe + motion* — each rendered to its own
  buffer and composited with `replace`/`add`/`mix` + opacity. This is the clean
  home for star power (its own overlay layer), strobe (top layer), and beat
  accents, so they stop fighting inside one flat buffer. → refactor `mapper` from
  a fixed pass-chain toward a small layer compositor.
- **Interpolating beat/bar oscillator (LedFx).** Synthesize a continuous phase
  (0–1 per beat, 0–N across the bar) from BPM + beat edges, and drive motion/color
  off the *smooth* phase instead of discrete 8-step hops. → new helper feeding
  `cue_engine.tick`.
- **Eased gradient / palette engine (LedFx + WLED).** Replace raw RGBY zone
  colors with per-cue **palettes** (Warm=red/amber, Cool=blue/teal…) looked up by
  position/phase/pitch, with sigmoid-eased stops and a rolling option for chases.
- **Spatial target language (photonics `LightTarget` → WLED segments).**
  A vocabulary for sub-regions/orderings (all/even/odd/halves/thirds/quarters/
  linear/inverse-linear/pairs/ring/random) — the elegant way to address the strip
  for performer bias, sweeps, and symmetry (mirror/reverse).
- **Note-hold / rising-edge (YALCY DMX).** Hold a one-frame note flash for a
  musically-scaled minimum so instrument hits are actually visible on the strip.
- **Tempo-locked strobe (YALCY).** Strobe *rate* follows BPM rather than fixed Hz.
- **Decaying overlays (LedFx / WLED `fadeToBlackBy`).** Flashes and shimmer fade
  out over wall-clock time instead of hard-cutting. We already do this for
  `bonus_t` and the star-power shimmer; generalize it.
- **Crossfade / palette morph on cue change (WLED).** We already crossfade cue
  changes over 0.25 s in the render thread; extend to morph *palettes*, not just
  blend pixels, once palettes exist.
- **Post-process chain: blur → mirror(max) → brightness → background (LedFx).**
  A light Gaussian blur is the cheapest way to make discrete cue events read as
  smooth, stage-quality light on a dense strip.
- **Previous-cue / CueData context (YALCY + photonics).** Let context-sensitive
  cues (Flare, Silhouettes, Dischord) adapt based on the previous cue and history.

## Phased roadmap (backlog seed)

- [x] **0. v4 signals** *(merged to main)* — parse camera cuts + per-player star
  power; render star power as a *tasteful surge*; camera cuts surfaced on the
  status page (parse-only).
- [x] **1. Beat/bar oscillator + gradient palettes + phase-locked motion**
  *(merged to main)* — continuous beat clock; on-beat brightness pump; eased
  gradient palettes recoloured onto the strip with a beat-locked scroll (demo:
  VERSE); BPM-synced chases hard-locked to the beat via a smooth PLL that coasts
  through dropped beats.
- [x] **2. Continuous sub-pixel scanner rendering** *(merged to main)* — motion
  cues paint a soft triangular profile at a continuous float position; peak and
  width stay constant while the head glides pixel-by-pixel.
- [x] **3. Layer/slot compositor** *(implemented on `feat/layer-compositor`)* —
  independent elements (wash, motion, sparkle, flash, bonus) render into their
  own pre-allocated buffers and are folded together by a `Compositor` with
  explicit blend modes (REPLACE / ADD / MIX / MIX_LIT / MIX_PREMULT); the
  whitening accents compose convexly in one pass, so overlapping overlays
  screen-combine instead of clip-fighting in a shared buffer. Whole-image
  modifiers (breathing, glitch, surge, masks, beat-pulse, brightness) stay as
  ordered transforms. See "Current focus" below.
- [x] **4. Note-hold + performer/vocal reactivity + post-processing colour
  tints** *(implemented on `feat/note-vocal-reactivity`)* — the four
  parsed-but-unused signal groups now drive light: per-instrument **note-hold**
  accents (rising edge → ≥1/32-note hold → decay, painted in each instrument's
  strip slice), a **vocal pitch ribbon** (per-voice blob, position = absolute
  pitch, hue = pitch class), a **performer highlight bias** (spotlight/singalong
  union tints the wash toward the featured performers' hues), and
  **post-processing colour grades** (B&W/sepia/silver/negative/desaturated/
  contrast tints applied as a global palette modifier; camera-only grades pass
  through). See "Current focus" below.
- [ ] **5. Camera-cut lighting** — subtle subject colour/region bias + a cut
  accent (data already parsed).
- [ ] **6. Blur/mirror post-process polish** — the LedFx filter chain.
- [ ] **7. Status dashboard pass** — evolve the status page (`status_server.py`)
  from a status readout into a live operator dashboard: render/DDP throughput and
  stall stats, per-signal telemetry (cue, BPM, beat clock, strobe, star power,
  camera subject, section), and a live strip / per-layer preview. Mostly
  read-only, driven off the data the tracker and engine already expose.
  *Groundwork landed:* a registry-driven **effect-toggle framework**
  (`settings.EFFECT_TOGGLES` + `apply_effect_toggles()`), with per-layer on/off
  switches already on the dashboard for the four Phase 4 reactivity layers.
  Adding a toggle is one registry row (label/description + the effects key it
  suppresses) — the render loop, persistence, and dashboard switches are
  generic, so new layers get a switch for free.

## Current focus — Phase 4: note / vocal / performer / post-processing reactivity

**Problem.** Four signal groups YARG broadcasts were parsed but thrown away, so
the strip ignored the notes being played, the vocal line, who the venue was
featuring, and the film grade — the light didn't react to the *performance*, only
to the authored cue.

**The work (implemented, `feat/note-vocal-reactivity`).** Each group is consumed
host-side, tastefully, and composited on top of the existing cue look:

- **Note-hold accents** (`on_notes`, notes 14–17). A note bitmask bit going 0→1
  is a fresh fret/pad hit; the engine holds it for at least a 1/32 note
  (tempo-scaled, floored) so a hit living in one ~88 Hz packet still reads, then
  decays. The mapper paints each instrument's decaying accent in its own
  two-cell slice (guitar|bass|drums|keys, left→right) as a convex whitening
  layer folded in with the sparkle/flash/bonus accents — so *where* the accent
  lands tells you *which* instrument played, and stacked hits never clip.
  *Hardened after the first cut:* the accent is **lit-pixels only** (like
  sparkle) so a busy part can't grey out a deliberate blackout, and a
  **~7 Hz anti-strobe cap** (`NOTE_REFRESH_MIN`) turns a fast run into a
  sustained glow instead of a flash-per-hit strobe — the flicker call-out from
  the Phase 4 review.
- **Vocal pitch ribbon** (`on_vocals`, vocals 18–33). Each sounding voice is a
  soft blob whose position tracks absolute MIDI pitch and whose hue is the pitch
  *class* from a cyclic chroma ramp (same note → same colour; an octave apart →
  two same-hue blobs at different places). Painted premultiplied like the
  scanner heads and alpha-over'd, so voices blend without overshooting.
- **Performer highlight bias** (`on_performers`, spotlight/singalong 42–43).
  Their union leans the lit wash toward the featured performers' colours (one
  hue each) via a gentle convex MIX on lit pixels only — a hue lean, never a
  repaint, never lifting a blackout.
- **Post-processing colour grade** (`on_post_processing`, offset 35). Only the
  colour grades act, via one loop over an `(invert, sat, tint)` table
  (`POST_GRADES`): B&W, sepia, silver, photo-negative, desaturated blue/red,
  contrast R/G/B, duotone. Camera-only grades (Bloom/Bright/Posterize/Mirror/
  Grainy/Scanline geometry/Trails) carry no colour meaning on a 1-D strip and
  pass through. Applied last, to lit pixels only.

Hot-path discipline preserved: new layer/alpha buffers allocated once; inactive
effects skipped by a gate or a `None` signal. Verified: unit tests pin each
feature and its blackout-safe / bounded invariants
(`tests/test_note_hold.py`, `test_vocal_ribbon.py`, `test_performer_bias.py`,
`test_post_processing.py`), and a render-only bench at LED_COUNT=120 shows the
**common in-song frame at parity** with the pre-Phase-4 render (~517 vs ~515
µs/frame here) — the always-on note-accent zero-fill is ~2 µs — while the
pathological all-four-effects-at-once frame is ~896 µs, still ≈3.6% of the 25 ms
budget at 40 FPS.

## Foundation — Phase 3: layer/slot compositor

**Problem.** `LEDMapper.render()` was a flat pass-chain: one shared RGB buffer
that ~13 effects read and overwrote in a fixed order. When several *overlays*
were active at once — star-power lift + beat-pulse + bonus burst + sparkle —
they stacked in code order and each clamped independently, so bright frames
clipped to white in an order-dependent way ("overlays fighting inside one flat
buffer"). Whitening accents were the worst offenders: three sequential
blend-toward-white passes over-whitened a lit pixel until its wash colour was
lost.

**The fix (implemented, `feat/layer-compositor`).** A small compositor
(`effects/compositor.py`): each independent element renders into its own
pre-allocated buffer — a `Layer` — and `Compositor.composite()` folds the active
layers together with an explicit blend mode + opacity (REPLACE / ADD / MIX /
MIX_LIT / MIX_PREMULT). The key property: **MIX is convex**, so stacking two
MIX-toward-white layers screen-combines (`t = 1-(1-t₁)(1-t₂)`) and can never
overshoot 255 — the whitening accents stop clipping and the fold is
order-independent.

How it fits together:
- Layers: **wash** (zone→pixel base, REPLACE) → **motion** (scanner heads,
  MIX_PREMULT — the Phase-2 premultiplied alpha-over, unchanged) → **accents**
  (sparkle per-pixel + initial-flash + bonus, all convex MIX toward white,
  composited in ONE pass).
- Transforms (whole-image, kept as ordered in-place passes because they aren't
  independent colour sources): gradient recolour, decay trails, gradient-boundary
  blend, breathing, glitch, star-power lift/tint + shimmer, reveal/spotlight
  masks, beat-pulse, pause-dim + brightness, reverse, wrap-around mirror.
- Strobe stays the top-level black-frame gate in `main.py` (a possible future
  "top layer").

Hot-path discipline preserved: all layer buffers/alpha arrays allocated once;
inactive layers skipped by a dirty flag. Verified: all Phase-2 scanner invariants
hold (`tests/test_scanner.py`), stacked whitening screen-combines and stays
bounded / order-independent (`tests/test_compositor.py`), and a bench of the
heaviest frame mix at LED_COUNT=120 shows **no regression** (~2.9k render/s,
≈342 µs/frame — a touch faster, three accent passes became one composite).

## Status (implemented today, on main)
- 33 lighting cues → zone/effect patterns; software strobe; beat flash/sparkle/
  glitch; keyframe-stepped manual cues; bonus burst; pause dim/freeze; cue
  crossfade; sub-cell (block-crossfade) interpolation; decay trails; breathing;
  gradient-boundary blend; spotlight/reveal masks.
- **v4:** camera cuts + per-player star power parsed (length-guarded); star power
  rendered as charging tint → active surge; camera subject on the status page.
- **Beat oscillator:** continuous beat clock (coasts through dropped beats);
  on-beat brightness pump (CHORUS, BigRockEnding); phase-locked chase motion
  (smooth PLL + free-run fallback); eased gradient palettes with beat-locked
  scroll (`effects/gradient.py`; demo on VERSE).
- **Phase 2 (merged):** continuous sub-pixel scanner rendering
  (`tests/test_scanner.py`).
- **Phase 3 (`feat/layer-compositor`):** the layer/slot compositor above.
- **Phase 4 (`feat/note-vocal-reactivity`):** note-hold accents, vocal pitch
  ribbon, performer highlight bias, and post-processing colour grades — the four
  parsed-but-unused signal groups now drive light (see "Current focus" above).
- The later roadmap phases (5–7) are **future work**.
