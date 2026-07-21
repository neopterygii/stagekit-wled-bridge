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
| PostProcessing (35) | 40+ film grades | apply the *color-tint* ones (Desaturated_Blue, Contrast_Red, SepiaTone, B&W…) as a global palette modifier; ignore camera-only grades |
| Note bitmasks (14–17) | per-fret/pad hits | rising-edge accents with a **note-hold** min (1/32 note) so transient hits are visible (YALCY DMX) |
| Vocal/Harmony pitch (18–33) | MIDI pitch, 0=none | map pitch→gradient position for a vocal shimmer / pitch ribbon |
| Spotlight / Singalong (42,43) | performer bitmask | bias a region/hue toward the highlighted performer(s) |
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
- [x] **2. Continuous sub-pixel scanner rendering** *(implemented on
  `feat/sub-pixel-scanner`, not yet merged)* — motion cues paint a soft
  triangular profile at a continuous float position; peak and width stay
  constant while the head glides pixel-by-pixel. See "Current focus" below.
- [ ] **3. Layer/slot compositor** — restructure the mapper so overlays
  (wash / motion / strobe / star-power) compose cleanly.
- [ ] **4. Note-hold + performer/vocal reactivity + post-processing colour
  tints** — use the already-parsed-but-unused signals (notes 14–17, vocals
  18–33, spotlight/singalong 42–43, post-processing 35).
- [ ] **5. Camera-cut lighting** — subtle subject colour/region bias + a cut
  accent (data already parsed).
- [ ] **6. Blur/mirror post-process polish** — the LedFx filter chain.

## Current focus — Phase 2: continuous sub-pixel scanner

**Problem (established empirically 2026-07-21).** Chases aren't fluid. The
scanner is modelled as 8 StageKit cells → 8 fixed 15-LED blocks (at
LED_COUNT=120), and motion is a *linear brightness crossfade between whole
blocks*. Measured:
- Cell-aligned frame: one 15-LED block at full brightness (peak **255**).
- Mid-transition frame: **two** adjacent blocks each at 50% → peak drops to
  **127** and the lit width balloons **15 → 30 LEDs**, then sharpens back.
- Position is quantised to 15-LED block boundaries — it never glides
  pixel-by-pixel.

So a moving scanner *throbs and smears* (peak −50%, width 2×, every handoff)
instead of gliding. Affects **all** chases (timed and beat-locked); MENU just
exposes it (slow, isolated single scanner). Note the beat-lock PLL already
computes a **continuous float `pos`** per pattern, but the render path
immediately quantises it back to `int(step)` + a cell crossfade — the precision
exists and is thrown away.

**The fix (implemented).** A motion renderer paints a soft triangular profile
centred at a *continuous float position* on the pixel array — gliding
pixel-by-pixel with constant peak and width, decoupled from the 8-cell grid
(the LedFx `scan.py` model). The cell/zone model is kept for StageKit-authentic
*static* cues; *motion* cues (scanners/chases/comets) are expressed as
**position + profile + width**, fed by the `pos`/`beat_clock` the engine already
computes.

How it fits together:
- `_TimePattern.motion_heads(cur, nxt, progress)` turns the continuous pattern
  position into gliding "heads" — one per lit StageKit cell, matched between
  steps by the least-travel cyclic rotation and interpolated along the shorter
  way round the ring (so a 7→0 hop moves +1, not −7).
- `CueEngine.tick` builds `motion_sources` (a flat `(zone, cell_pos, level)`
  list) each frame and **zeros the cell levels of motion-owned zones**, so the
  two render models never double-paint.
- `LEDMapper.render` paints each head as a triangular profile (half-width = one
  cell) into a motion layer, accumulating colour additively and coverage as an
  alpha, then alpha-composites that layer over the static base. Adjacent heads
  form a partition of unity (tiled chases fill with no dark seam), crossing
  scanners mix colour, and a lone scanner keeps a soft falloff over any wash.

Verified: the MENU scanner holds peak ≈245 (was 255→127) and width ≈one cell
(was 15→30) while gliding; a beat-locked CHORUS chase glides with <0.02 px/frame
centroid jitter; the tiled BigRockEnding chase has no dark seams. Tests:
`tests/test_scanner.py`. Branch: `feat/sub-pixel-scanner`.

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
- Everything above under "Current focus" and the later roadmap phases is
  **future work**.
