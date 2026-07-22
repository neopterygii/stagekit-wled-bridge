"""LED zone-to-pixel mapper with per-pixel effects.

Maps 4 Stage Kit zones (8 bitmask LEDs each) to an LED strip with
post-processing effects inspired by LedFx/WLED.

The strip is divided into 8 cells (LED_COUNT // 8 LEDs each).
Any remainder LEDs mirror from the start for visual wrap-around.

Render pipeline (VISION Phase 3 — layer/slot compositor, see compositor.py):
  1. Wash        — zone→pixel base + solid-zone fill (the primary look)
  2. Motion      — sub-pixel scanner heads, alpha-over the wash (a layer)
  3. Scene shape — gradient recolour, decay trails, gradient-boundary blend,
                   sine breathing, glitch (in-place transforms on the scene)
  4. Accents     — sparkle + initial-flash + bonus + note-hold, each a convex
                   whitening layer, composited together in ONE pass so they
                   can't clip-fight in a shared buffer (the Phase-3 fix)
  5. Surge       — star-power lift/tint + shimmer, on lit pixels; then the
                   vocal pitch ribbon (colour-by-pitch blobs) alpha-over
  6. Masks/pump  — reveal/spotlight masks, beat-locked brightness pulse
  7. Grade       — performer hue bias + post-processing colour grade
  8. Post-proc   — blur (fog-lifted Gaussian smoothing) → mirror(max) fold
  9. Output      — pause-dim + global brightness, reverse, wrap-around fill

Independent elements (wash, motion, sparkle, flash, bonus) render into their
own pre-allocated buffers and are folded together by the Compositor with an
explicit blend mode + opacity; whole-image modifiers stay as ordered transforms.

Performance notes (Phase 2 optimisation):
  All per-pixel work operates on flat pre-allocated bytearrays using integer
  math and direct index writes.  No tuples, list comprehensions, or bytes()
  copies are created in the hot path.  The output buffer is written in-place
  and returned as a memoryview — the caller must copy if it needs to keep a
  snapshot (the render thread does this via bytes() only when the frame
  actually changed).
"""

import math
import random
import time

from config import LED_COUNT
from effects.compositor import Layer, Compositor, MIX, MIX_PREMULT
from effects.gradient import GRADIENTS
from protocol.yarg_packet import Performer, PostProcessing

# Chroma ramp for the vocal ribbon: pitch-class (0..1 across an octave) → hue.
# The cyclic rainbow makes the octave wrap seamless (C just below the next C is
# the same red), so a rising/falling line sweeps smoothly through the colours.
VOCAL_CHROMA = GRADIENTS["rainbow"]

# Zone ordering matches Stage Kit command IDs
ZONE_NAMES = ["red", "green", "blue", "yellow"]

LEDS_PER_ZONE = 8
NUM_CELLS = 8
CELL_SIZE = LED_COUNT // NUM_CELLS  # LEDs per cell (scales with strip length)

MAPPED_REGION = NUM_CELLS * CELL_SIZE

# Number of pixels on each side of a color boundary to blend over (scales with cell size)
BLEND_WIDTH = max(1, CELL_SIZE // 6)

# Post-process blur (VISION Phase 6). The engine emits a 0..1 wet strength; the
# mapper owns the kernel width — BLUR_PASSES iterations of a 3-tap [1,2,1]/4
# Gaussian on a working copy, wet-mixed back over the crisp buffer. Two passes
# give a soft ~2-pixel radius that smooths discrete cue events without smearing
# the strip to mush; edges clamp (replicate) so light bleeds inward, not off.
BLUR_PASSES = 2

# Sub-pixel scanner (VISION Phase 2). Motion cues are painted as soft triangular
# intensity profiles at a *continuous* pixel position instead of crossfading
# whole cell-blocks, so a moving light glides pixel-by-pixel with a constant
# peak and width. The half-width equals one cell: a lone scanner is then a
# ~1-cell bright core with soft shoulders, and adjacent heads in a tiled chase
# (spaced one cell apart) form a partition of unity — their triangles sum to 1,
# so the strip fills with no dark seams between colour segments.
SCAN_HALFWIDTH = max(2, CELL_SIZE)

# Byte-level constants for a black pixel
_OFF_R = 0
_OFF_G = 0
_OFF_B = 0

# ── Star-power "tasteful surge" tuning ───────────────────────────
# Cool overdrive tint (a blue-white). The surge acts ONLY on already-lit
# pixels — it lifts, cools, and shimmers the current wash rather than adding a
# floor to dark pixels, so it never lights up a deliberate blackout. Overdrive
# reads as "the existing lights intensify," which is how it looks on a real rig.
SP_TINT = (150, 200, 255)     # cool blue-white the surge blends lit pixels toward
SP_SURGE_LIFT = 0.30          # max fractional brightness lift on lit pixels
SP_SURGE_BASE = 0.45          # surge floor while active, before amount scales in
SP_TINT_STRENGTH = 0.30       # how far lit pixels blend toward SP_TINT at full
SP_CHARGE_TINT = 0.15         # max cool tint on lit pixels while only charging
SP_SHIMMER_DENSITY = 0.05     # per-pixel cool-shimmer seeding probability/frame
SP_SHIMMER_MULTI = 0.5        # extra shimmer per additional active player

# ── Note-hold accents (VISION Phase 4) ───────────────────────────
# A note/pad hit lights a brief whitening accent in that instrument's slice of
# the strip. The four instruments (guitar, bass, drums, keys) tile the strip in
# order, two of the eight cells each, so you can read *which* instrument played
# by *where* the accent lands. Painted as a convex whitening layer composited
# with the other accents (sparkle/flash/bonus), so stacked hits never clip.
NOTE_ACCENT_MAX = 0.5                          # peak whitening coverage per hit
NOTE_CELLS_PER_INSTRUMENT = NUM_CELLS // 4     # 2 cells each (gtr|bass|drum|keys)

# ── Vocal pitch ribbon (VISION Phase 4) ──────────────────────────
# Each sounding voice (lead + 3 harmonies) is painted as a soft colour blob: its
# position along the strip tracks absolute MIDI pitch (low→left, high→right) and
# its hue is the pitch *class* (note within the octave) sampled from a chroma
# ramp — so the same note is always the same colour, an octave apart lands two
# blobs of one hue at different places. Composited alpha-over the wash so it
# reads as a translucent ribbon riding the vocal line, not a hard repaint.
VOCAL_MIDI_LO = 36.0          # C2 — bottom of the mapped vocal range
VOCAL_MIDI_HI = 84.0          # C6 — top of the mapped vocal range
VOCAL_HALFWIDTH = max(2, (CELL_SIZE * 3) // 2)  # blob half-width (~1.5 cells)
VOCAL_LEVEL = 0.7             # peak coverage of a voice blob

# ── Performer highlight bias (VISION Phase 4) ────────────────────
# Spotlight + singalong flag which performer(s) the venue is featuring; their
# union biases the whole wash a little toward those performers' colours — a
# gentle hue lean on the lit pixels (never a repaint, never lights a blackout).
# One tasteful hue per performer, keyed by the Performer bitmask bit value.
PERFORMER_COLORS = {
    Performer.GUITAR:   (255, 140, 0),    # amber
    Performer.BASS:     (180, 40, 255),   # violet
    Performer.DRUMS:    (255, 50, 50),    # red
    Performer.VOCALS:   (0, 210, 220),    # cyan
    Performer.KEYBOARD: (60, 230, 120),   # green
}
PERFORMER_BIAS_STRENGTH = 0.18   # max blend toward the highlighted hue

# ── Camera-cut lighting (VISION Phase 5) ─────────────────────────
# The venue camera's current subject biases the wash toward that player: a
# gentle brightness lift + hue lean confined to the player's *region* of the
# strip, so you can read who the camera is on. A directed cut adds a brief
# global bloom on top.
#
# A subject maps to a logical player "channel"; a channel maps to a physical
# region. Today every channel tiles ONE strip in CAMERA_CHANNEL_ORDER (drums
# leftmost, vocals rightmost). That order is deliberate: a future multi-strip
# rig — planned as a quad — can bookend with the drum and vocal strips and put
# the other instruments between, by swapping only _camera_region(); the subject→
# channel table, hues, and bias maths stay put.
CAMERA_CHANNEL_ORDER = ("drums", "guitar", "bass", "keys", "vocals")

# Per-channel hues reuse the Performer palette so the two features read the same.
CAMERA_CHANNEL_COLORS = {
    "drums":  PERFORMER_COLORS[Performer.DRUMS],
    "guitar": PERFORMER_COLORS[Performer.GUITAR],
    "bass":   PERFORMER_COLORS[Performer.BASS],
    "keys":   PERFORMER_COLORS[Performer.KEYBOARD],
    "vocals": PERFORMER_COLORS[Performer.VOCALS],
}

# Which channel(s) each camera subject features (CameraCutSubject id → names).
# Whole-stage / crowd / "everyone-but" / random shots carry no single-subject
# bias and are simply absent here (→ no region bias, just any cut accent).
CAMERA_SUBJECT_CHANNELS = {
    7: ("guitar",), 8: ("guitar",), 9: ("guitar",), 10: ("guitar",),
    11: ("drums",), 12: ("drums",), 13: ("drums",), 14: ("drums",), 15: ("drums",),
    16: ("bass",), 17: ("bass",), 18: ("bass",), 19: ("bass",),
    20: ("vocals",), 21: ("vocals",), 22: ("vocals",),
    23: ("keys",), 24: ("keys",), 25: ("keys",), 26: ("keys",),
    27: ("drums", "vocals"), 28: ("bass", "drums"), 29: ("drums", "guitar"),
    30: ("bass", "vocals"), 31: ("bass", "vocals"),
    32: ("guitar", "vocals"), 33: ("guitar", "vocals"),
    34: ("keys", "vocals"), 35: ("keys", "vocals"),
    36: ("bass", "guitar"), 37: ("bass", "guitar"),
    38: ("bass", "keys"), 39: ("bass", "keys"),
    40: ("guitar", "keys"), 41: ("guitar", "keys"),
}

CAMERA_BIAS_STRENGTH = 0.22   # max hue lean toward the on-camera player's colour
CAMERA_BIAS_LIFT = 0.12       # max brightness lift on that player's region
CAMERA_CUT_LIFT = 0.18        # directed-cut bloom: peak brightness lift, lit px

# ── Post-processing colour grades (VISION Phase 4) ───────────────
# YARG's venue post-processing (offset 35) is a film grade. We apply only the
# *colour* ones as a global palette modifier on lit pixels — the spatial/camera
# ones (Bloom, Bright, Posterize, Mirror, Grainy, Scanline geometry, Trails)
# have no colour meaning on a 1-D strip and are left out (pass-through).
#
# Every colour grade is expressed as one tuple (invert, sat, tr, tg, tb) and run
# by a single loop: optionally invert, desaturate toward luma by (1 - sat), then
# scale each channel by its tint multiplier. So SepiaTone is "full desaturate +
# warm channel tint", Desaturated_Blue is "half desaturate + cool tint", etc.
# Applied to lit pixels only, so a grade never lights a deliberate blackout.
_PP = PostProcessing
POST_GRADES = {
    #                        invert  sat    tr     tg     tb
    _PP.PHOTO_NEGATIVE:               (True,  1.0,  1.00,  1.00,  1.00),
    _PP.PHOTO_NEGATIVE_RED_AND_BLACK: (True,  0.0,  1.10,  0.35,  0.35),
    _PP.BLACK_AND_WHITE:              (False, 0.0,  1.00,  1.00,  1.00),
    _PP.CHOPPY_BLACK_AND_WHITE:       (False, 0.0,  1.00,  1.00,  1.00),
    _PP.POLARIZED_BLACK_AND_WHITE:    (False, 0.0,  1.00,  1.00,  1.00),
    _PP.SCANLINES_BLACK_AND_WHITE:    (False, 0.0,  1.00,  1.00,  1.00),
    _PP.SEPIA_TONE:                   (False, 0.0,  1.12,  0.92,  0.62),
    _PP.SILVER_TONE:                  (False, 0.0,  0.98,  1.00,  1.08),
    _PP.POLARIZED_RED_AND_BLUE:       (False, 0.2,  1.15,  0.60,  1.15),
    _PP.DESATURATED_BLUE:             (False, 0.45, 0.82,  0.90,  1.15),
    _PP.DESATURATED_RED:              (False, 0.45, 1.15,  0.85,  0.82),
    _PP.TRAILS_DESATURATED:           (False, 0.50, 1.00,  1.00,  1.00),
    _PP.CONTRAST_RED:                 (False, 0.9,  1.18,  0.82,  0.82),
    _PP.CONTRAST_GREEN:               (False, 0.9,  0.82,  1.18,  0.82),
    _PP.CONTRAST_BLUE:                (False, 0.9,  0.82,  0.82,  1.18),
}


class LEDMapper:
    """Maps Stage Kit zone bitmask state to an RGB pixel buffer with effects.

    All pixel math uses pre-allocated flat bytearrays (3 bytes per pixel)
    to avoid per-frame heap allocations and GC pressure.
    """

    def __init__(self, led_count: int = LED_COUNT):
        self.led_count = led_count
        # Output buffer — written in place each frame
        self._out = bytearray(led_count * 3)

        # Working buffer for the mapped region (R,G,B flat)
        self._buf = bytearray(MAPPED_REGION * 3)

        # Second working buffer for gradient blending pass
        self._blend = bytearray(MAPPED_REGION * 3)

        # Ping-pong buffers for the post-process blur (Phase 6), pre-allocated
        # so the per-frame kernel passes never allocate.
        self._blur_a = bytearray(MAPPED_REGION * 3)
        self._blur_b = bytearray(MAPPED_REGION * 3)

        # ── Compositor layers (VISION Phase 3) ───────────────────
        # Independent elements render into their own buffers and are folded
        # onto the wash by the compositor with explicit blend modes, instead
        # of each mutating one shared buffer in sequence (see compositor.py).
        #
        # Motion (sub-pixel scanner): heads accumulate premultiplied colour +
        # coverage, then alpha-over the static base (MIX_PREMULT).
        self._motion_layer = Layer(MAPPED_REGION, MIX_PREMULT, per_pixel_alpha=True)

        # Vocal ribbon (Phase 4): colour-by-pitch blobs, painted premultiplied
        # like the motion layer and alpha-over'd onto the scene.
        self._vocal_layer = Layer(MAPPED_REGION, MIX_PREMULT, per_pixel_alpha=True)

        # Accent overlays that brighten toward white — sparkle, initial flash,
        # bonus burst. Each is a constant-white buffer blended by a coverage
        # (per-pixel for sparkle, scalar for flash/bonus). Because MIX is convex
        # they screen-combine and never clip, however many fire at once — this
        # is the "stop the overlays fighting" fix. Composited in one pass.
        self._sparkle_layer = Layer(MAPPED_REGION, MIX, per_pixel_alpha=True)
        self._flash_layer = Layer(MAPPED_REGION, MIX)
        self._bonus_layer = Layer(MAPPED_REGION, MIX)
        # Note-hold accent (Phase 4): a whitening layer keyed per instrument
        # region by the engine's decaying note levels (see NOTE_ACCENT_MAX).
        self._note_layer = Layer(MAPPED_REGION, MIX, per_pixel_alpha=True)
        for _lyr in (self._sparkle_layer, self._flash_layer, self._bonus_layer,
                     self._note_layer):
            for _k in range(len(_lyr.buf)):
                _lyr.buf[_k] = 255                      # constant white source
        self._accents = (self._sparkle_layer, self._flash_layer,
                         self._bonus_layer, self._note_layer)

        # Decay trails state: flat R,G,B per pixel
        self._trail = bytearray(MAPPED_REGION * 3)

        # Sparkle state: countdown per pixel (0 = no sparkle)
        self._sparkle = bytearray(MAPPED_REGION)

        # Glitch state: per-cell invert countdown
        self._glitch = bytearray(NUM_CELLS)

        # Star-power shimmer: countdown per pixel (0 = none), decayed like
        # sparkle so cool flecks fade smoothly instead of strobing per frame.
        self._sp_shimmer = bytearray(MAPPED_REGION)

        # Timing for breathing
        self._start_time = time.monotonic()

    @staticmethod
    def _set_px(buf: bytearray, i: int, r: int, g: int, b: int):
        o = i * 3
        buf[o] = r
        buf[o + 1] = g
        buf[o + 2] = b

    @staticmethod
    def _add_px(buf: bytearray, i: int, r: int, g: int, b: int):
        """Additive blend into buf[i], clamping at 255."""
        o = i * 3
        buf[o] = min(255, buf[o] + r)
        buf[o + 1] = min(255, buf[o + 1] + g)
        buf[o + 2] = min(255, buf[o + 2] + b)

    @staticmethod
    def _center_window(fraction: float) -> tuple[int, int]:
        """(lo, hi) pixel bounds of a centered window covering *fraction* of the strip."""
        half = max(1, int(MAPPED_REGION * fraction / 2.0))
        mid = MAPPED_REGION // 2
        return max(0, mid - half), min(MAPPED_REGION, mid + half)

    @staticmethod
    def _camera_region(idx: int) -> tuple[int, int]:
        """(lo, hi) strip region owned by camera channel `idx` today.

        Even tiling of the mapped strip in CAMERA_CHANNEL_ORDER (single-strip
        layout). This is the one place a future multi-strip rig changes: map the
        channel to a whole strip instead of a slice.
        """
        n = len(CAMERA_CHANNEL_ORDER)
        return MAPPED_REGION * idx // n, MAPPED_REGION * (idx + 1) // n

    @staticmethod
    def _mask_outside(buf: bytearray, lo: int, hi: int):
        """Zero all pixels outside [lo, hi)."""
        for i in range(lo):
            o = i * 3
            buf[o] = 0
            buf[o + 1] = 0
            buf[o + 2] = 0
        for i in range(hi, MAPPED_REGION):
            o = i * 3
            buf[o] = 0
            buf[o + 1] = 0
            buf[o + 2] = 0

    def _blur(self, buf: bytearray, wet: float):
        """Wet-mix a 1-D Gaussian blur of the mapped region into ``buf``.

        BLUR_PASSES iterations of a 3-tap [1,2,1]/4 kernel per channel (edges
        clamp) build a soft blur on a ping-pong copy, then each channel is
        lerped back over the crisp buffer by ``wet`` (0 = no change, 1 = fully
        blurred). Runs on all pixels so lit regions bleed into dark neighbours;
        an all-black frame blurs to black, so a blackout is never lifted.
        """
        if wet <= 0.0:
            return
        if wet > 1.0:
            wet = 1.0
        n3 = MAPPED_REGION * 3
        src = self._blur_a
        dst = self._blur_b
        src[:n3] = buf[:n3]
        last = MAPPED_REGION - 1
        for _ in range(BLUR_PASSES):
            for i in range(MAPPED_REGION):
                o = i * 3
                lo = o if i == 0 else o - 3          # clamp at the left edge
                ro = o if i == last else o + 3       # clamp at the right edge
                dst[o]     = (src[lo]     + 2 * src[o]     + src[ro])     >> 2
                dst[o + 1] = (src[lo + 1] + 2 * src[o + 1] + src[ro + 1]) >> 2
                dst[o + 2] = (src[lo + 2] + 2 * src[o + 2] + src[ro + 2]) >> 2
            src, dst = dst, src
        inv = 1.0 - wet
        for k in range(n3):
            buf[k] = int(buf[k] * inv + src[k] * wet)

    def render(self, zone_bitmasks: list[int], zone_colors: dict | None = None,
               effects: dict | None = None, brightness: float = 1.0,
               reverse: bool = False,
               zone_cell_levels: list[list[float]] | None = None,
               motion_sources: list | None = None) -> bytes:
        """Render pixels from 4 zone bitmasks with optional effects.

        Args:
            zone_bitmasks: 4 bitmasks [red, green, blue, yellow] — used to
                detect "solid" background zones (mask 0xFF) and as a
                fallback when zone_cell_levels isn't provided.
            zone_colors: Color mapping from settings palette.
            effects: Dict of active effects from cue engine.
            brightness: Global brightness 0.0-1.0 (baked into output).
            reverse: Mirror the output horizontally.
            zone_cell_levels: 4 zones x 8 cells of float 0.0-1.0 brightness for
                the *static* cell model (washes/spotlights). When None, levels
                are derived from zone_bitmasks (binary on/off).
            motion_sources: list of (zone, cell_pos, level) scanner heads from
                the engine, where cell_pos is a continuous float in [0, 8).
                Painted as soft profiles at a sub-pixel position and composited
                over the static base. Zones a motion pattern owns arrive with
                their cell levels zeroed, so the two models don't double-paint.

        Returns:
            Flat bytes of R,G,B,R,G,B,... for all LEDs with brightness applied.
        """
        if zone_colors is None:
            from settings import PALETTES
            zone_colors = PALETTES["default"]["colors"]

        if effects is None:
            effects = {}

        trail_len = effects.get("trails", 0)
        breathing_rate = effects.get("breathing", 0.0)
        sparkle_density = effects.get("sparkle", 0.0)
        sparkle_continuous = effects.get("sparkle_continuous", False)
        beat_flash = effects.get("beat_flash", False)
        downbeat_flash = effects.get("downbeat_flash", False)
        use_additive = effects.get("additive", False)
        glitch_prob = effects.get("glitch", 0.0)
        glitch_trigger = effects.get("glitch_trigger", False)
        bpm = effects.get("bpm", 120.0)
        initial_flash = effects.get("initial_flash", 0)

        # New YARG-data effects
        paused = effects.get("paused", False)
        bonus_t = effects.get("bonus_t", 0.0)                    # 1.0 → 0.0 decay
        reveal_progress = effects.get("reveal_progress", 1.0)    # < 1.0 = masking
        spotlight_region = effects.get("spotlight_region", 0.0)  # 0 = no mask
        spotlight_only = effects.get("spotlight_only", None)     # (r,g,b) or None
        fps = effects.get("fps", 30.0)  # render FPS, for frame-count effects

        # Note-hold accents (Phase 4): 4 decayed levels [gtr, bass, drum, keys]
        # or None. Painted as a per-instrument whitening region below.
        note_accents = effects.get("note_accents", None)

        # Vocal ribbon (Phase 4): MIDI pitch per voice [lead, h0, h1, h2] or
        # None; 0.0 = silent. Painted as colour-by-pitch blobs below.
        vocal_notes = effects.get("vocal_notes", None)

        # Performer highlight (Phase 4): union of spotlight+singalong bitmasks.
        # Biases lit pixels toward the highlighted performers' colours.
        performers = effects.get("performers", 0)

        # Post-processing grade (Phase 4): venue film grade byte. Only the
        # colour grades in POST_GRADES do anything; others pass through.
        post_grade = POST_GRADES.get(effects.get("post_processing", 0))

        # Camera cut (Phase 5): (subject_id, bias_gain, cut_t) or None. The
        # subject biases its player's strip region/hue (eased in by bias_gain
        # after a cut); cut_t is a brief directed-cut bloom. None = toggled off.
        camera = effects.get("camera", None)

        # Post-process chain (Phase 6). blur is a 0..1 wet strength (0 = off);
        # mirror folds the strip into a left-right symmetric look. Both applied
        # in the Output stage in the order blur → mirror → brightness.
        blur_amount = effects.get("blur", 0.0)
        mirror = effects.get("mirror", False)

        # Star power (v4). sp_active → surge; sp_charge → pre-activation glow.
        sp_active = effects.get("sp_active", False)
        sp_amount = effects.get("sp_amount", 0.0)          # 0..1 (active players)
        sp_charge = effects.get("sp_charge", 0.0)          # 0..1 (all players)
        sp_active_count = effects.get("sp_active_count", 0)

        # Beat oscillator: a gentle on-beat brightness pump. beat_pulse is the
        # depth (0 = off); beat_phase is the engine's continuous 0→1 phase.
        beat_pulse = effects.get("beat_pulse", 0.0)
        beat_phase = effects.get("beat_phase", 1.0)

        # Gradient palette (opt-in): a Gradient LUT that recolours lit pixels by
        # position, scrolled along the strip by the beat clock. gradient_roll is
        # gradient-cycles per beat (0 = static).
        gradient = effects.get("gradient", None)
        gradient_roll = effects.get("gradient_roll", 0.0)
        beat_clock = effects.get("beat_clock", 0.0)

        buf = self._buf

        # Reset accent-layer activity for this frame; their effect blocks below
        # set it True and fill coverage. Composited together before the surge.
        self._sparkle_layer.active = False
        self._flash_layer.active = False
        self._bonus_layer.active = False
        self._note_layer.active = False

        # Resolve zone colors to flat ints once
        zc = [zone_colors[ZONE_NAMES[z]] for z in range(4)]

        # Derive fractional cell levels from bitmasks if engine didn't.
        if zone_cell_levels is None:
            zone_cell_levels = [
                [(1.0 if (zone_bitmasks[z] >> c) & 1 else 0.0) for c in range(8)]
                for z in range(4)
            ]

        # ── Base zone→pixel mapping ──────────────────────────────
        # Zero the working buffer
        for k in range(MAPPED_REGION * 3):
            buf[k] = 0

        # Spotlight-only mode (BLACKOUT_SPOTLIGHT): paint a fixed colour in
        # the centre region and skip the normal zone mapping entirely.
        if spotlight_only is not None:
            sr, sg, sb = spotlight_only
            start, end = self._center_window(spotlight_region)
            for pos in range(start, end):
                self._set_px(buf, pos, sr, sg, sb)

        elif use_additive:
            for cell in range(NUM_CELLS):
                cell_start = cell * CELL_SIZE
                for zone_idx in range(4):
                    level = zone_cell_levels[zone_idx][cell]
                    if level <= 0.0:
                        continue
                    cr, cg, cb = zc[zone_idx]
                    if level < 1.0:
                        cr = int(cr * level)
                        cg = int(cg * level)
                        cb = int(cb * level)
                    for j in range(CELL_SIZE):
                        pos = cell_start + j
                        if pos < MAPPED_REGION:
                            self._add_px(buf, pos, cr, cg, cb)
        else:
            # Non-additive: subdivide cell among "active" zones, where a
            # zone is active when its level exceeds a small threshold.
            # The colour for that zone slice is scaled by the zone's level
            # so a cell mid-fade (level ~0.5) renders dimmer.
            ACTIVE_THRESHOLD = 0.05
            for cell in range(NUM_CELLS):
                cell_start = cell * CELL_SIZE
                active: list[tuple[int, float]] = []
                for zone_idx in range(4):
                    level = zone_cell_levels[zone_idx][cell]
                    if level > ACTIVE_THRESHOLD:
                        active.append((zone_idx, level))

                if not active:
                    continue

                n = len(active)
                leds_per = CELL_SIZE // n
                remainder = CELL_SIZE % n
                pos = cell_start
                for i, (zone_idx, level) in enumerate(active):
                    cr, cg, cb = zc[zone_idx]
                    if level < 1.0:
                        cr = int(cr * level)
                        cg = int(cg * level)
                        cb = int(cb * level)
                    count = leds_per + (1 if i < remainder else 0)
                    for _ in range(count):
                        if pos < MAPPED_REGION:
                            self._set_px(buf, pos, cr, cg, cb)
                        pos += 1

        # Second pass: solid zones (ALL=0xFF) fill completely dark cells
        solid_zones = [z for z in range(4) if zone_bitmasks[z] == 0xFF]
        if solid_zones:
            for cell in range(NUM_CELLS):
                cell_start = cell * CELL_SIZE
                base = cell_start * 3
                cell_off = True
                for j in range(CELL_SIZE):
                    o = base + j * 3
                    if buf[o] | buf[o + 1] | buf[o + 2]:
                        cell_off = False
                        break
                if cell_off:
                    n = len(solid_zones)
                    leds_per = CELL_SIZE // n
                    remainder = CELL_SIZE % n
                    pos = cell_start
                    for i, zone_idx in enumerate(solid_zones):
                        cr, cg, cb = zc[zone_idx]
                        count = leds_per + (1 if i < remainder else 0)
                        for _ in range(count):
                            if pos < MAPPED_REGION:
                                self._set_px(buf, pos, cr, cg, cb)
                            pos += 1

        # ── Sub-pixel scanner motion (VISION Phase 2) ────────────
        # Paint each scanner "head" as a soft triangular profile centred at a
        # continuous pixel position, accumulating colour (additively) and
        # coverage (alpha) into a motion layer, then composite that layer over
        # the static base by its coverage. Because adjacent heads' triangles
        # sum to 1, a tiled chase fills without dark seams while a lone scanner
        # keeps a soft falloff; because heads add within the layer, crossing
        # scanners mix colour. The float centre means motion glides pixel-by-
        # pixel with a constant peak/width instead of the old cell-block throb.
        motion_layer = self._motion_layer
        if motion_sources:
            mbuf = motion_layer.buf
            malpha = motion_layer.alpha
            for k in range(MAPPED_REGION * 3):
                mbuf[k] = 0
            for i in range(MAPPED_REGION):
                malpha[i] = 0.0
            half = SCAN_HALFWIDTH
            inv_half = 1.0 / half
            for zone, cell_pos, level in motion_sources:
                if level <= 0.0:
                    continue
                cr, cg, cb = zc[zone]
                center = ((cell_pos + 0.5) * CELL_SIZE) % MAPPED_REGION
                lo = int(math.ceil(center - half))
                hi = int(math.floor(center + half))
                for px in range(lo, hi + 1):
                    d = px - center
                    if d < 0.0:
                        d = -d
                    w = (1.0 - d * inv_half) * level
                    if w <= 0.0:
                        continue
                    idx = px % MAPPED_REGION
                    o = idx * 3
                    v = mbuf[o] + int(cr * w);     mbuf[o]     = v if v < 255 else 255
                    v = mbuf[o + 1] + int(cg * w); mbuf[o + 1] = v if v < 255 else 255
                    v = mbuf[o + 2] + int(cb * w); mbuf[o + 2] = v if v < 255 else 255
                    a = malpha[idx] + w
                    malpha[idx] = a if a < 1.0 else 1.0
            motion_layer.active = True
        else:
            motion_layer.active = False
        # Alpha-over the motion layer onto the static base (premultiplied).
        Compositor.composite(buf, (motion_layer,))

        # ── Gradient recolour (beat oscillator) ──────────────────
        # Opt-in: recolour every lit pixel from the gradient by its position,
        # scrolled along the strip by the beat clock. Zones/patterns decided
        # which pixels are lit and how bright; the gradient decides hue,
        # preserving each pixel's intensity (its brightest channel). Runs before
        # trails so decay/breathing/sparkle all operate on the gradient colour.
        if gradient is not None:
            offset = (beat_clock * gradient_roll) % 1.0 if gradient_roll else 0.0
            color_at = gradient.color_at
            inv_region = 1.0 / MAPPED_REGION
            for i in range(MAPPED_REGION):
                o = i * 3
                r = buf[o]
                g = buf[o + 1]
                b = buf[o + 2]
                mx = r if r >= g else g
                if b > mx:
                    mx = b
                if mx == 0:
                    continue
                gr, gg, gb = color_at(i * inv_region + offset)
                inten = mx / 255.0
                buf[o] = int(gr * inten)
                buf[o + 1] = int(gg * inten)
                buf[o + 2] = int(gb * inten)

        # ── Effect: Decay trails ─────────────────────────────────
        trail = self._trail
        if trail_len > 0:
            decay = 1.0 - 1.0 / max(trail_len, 1)
            for i in range(MAPPED_REGION):
                o = i * 3
                br = buf[o]
                bg = buf[o + 1]
                bb = buf[o + 2]
                if br | bg | bb:
                    # New color — overwrite trail
                    trail[o] = br
                    trail[o + 1] = bg
                    trail[o + 2] = bb
                else:
                    tr = trail[o]
                    tg = trail[o + 1]
                    tb = trail[o + 2]
                    if tr | tg | tb:
                        tr = int(tr * decay)
                        tg = int(tg * decay)
                        tb = int(tb * decay)
                        if tr + tg + tb > 6:
                            trail[o] = tr
                            trail[o + 1] = tg
                            trail[o + 2] = tb
                            buf[o] = tr
                            buf[o + 1] = tg
                            buf[o + 2] = tb
                        else:
                            trail[o] = 0
                            trail[o + 1] = 0
                            trail[o + 2] = 0
        else:
            # No trails — sync trail buffer with current frame
            trail[:] = buf[:]

        # ── Effect: Gradient blending ────────────────────────────
        blend = self._blend
        blend[:] = buf[:]
        for i in range(1, MAPPED_REGION):
            o = i * 3
            p = (i - 1) * 3
            # Adjacent pixels differ and neither is black
            cur_on = buf[o] | buf[o + 1] | buf[o + 2]
            prev_on = buf[p] | buf[p + 1] | buf[p + 2]
            if cur_on and prev_on and (
                buf[o] != buf[p] or buf[o + 1] != buf[p + 1] or buf[o + 2] != buf[p + 2]
            ):
                for offset in range(1, BLEND_WIDTH + 1):
                    t = 1.0 - offset / (BLEND_WIDTH + 1)
                    t_half = t * 0.5
                    inv_t = 1.0 - t_half
                    left = i - offset
                    if 0 <= left < MAPPED_REGION:
                        lo = left * 3
                        # Check left pixel matches the colour at i-1
                        if buf[lo] == buf[p] and buf[lo+1] == buf[p+1] and buf[lo+2] == buf[p+2]:
                            blend[lo]   = int(buf[p]   * inv_t + buf[o]   * t_half)
                            blend[lo+1] = int(buf[p+1] * inv_t + buf[o+1] * t_half)
                            blend[lo+2] = int(buf[p+2] * inv_t + buf[o+2] * t_half)
                    right = i - 1 + offset
                    if 0 <= right < MAPPED_REGION:
                        ro = right * 3
                        if buf[ro] == buf[o] and buf[ro+1] == buf[o+1] and buf[ro+2] == buf[o+2]:
                            blend[ro]   = int(buf[o]   * inv_t + buf[p]   * t_half)
                            blend[ro+1] = int(buf[o+1] * inv_t + buf[p+1] * t_half)
                            blend[ro+2] = int(buf[o+2] * inv_t + buf[p+2] * t_half)
        # Swap: blend becomes the active buffer
        buf, blend = blend, buf
        self._buf, self._blend = buf, blend

        # ── Effect: Sine breathing ───────────────────────────────
        if breathing_rate > 0.0:
            elapsed = time.monotonic() - self._start_time
            beats_per_sec = max(bpm, 30.0) / 60.0
            phase = elapsed * beats_per_sec * breathing_rate * 2.0 * math.pi
            breath = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(phase))
            for i in range(MAPPED_REGION):
                o = i * 3
                if buf[o] or buf[o + 1] or buf[o + 2]:
                    buf[o]     = int(buf[o] * breath)
                    buf[o + 1] = int(buf[o + 1] * breath)
                    buf[o + 2] = int(buf[o + 2] * breath)

        # ── Effect: Sparkle overlay ──────────────────────────────
        if sparkle_density > 0.0:
            sparkle = self._sparkle
            # Downbeat (MEASURE) gets ~1.5x density and longer-lived
            # sparkles. STRONG beats keep base density. WEAK doesn't
            # trigger sparkles at all (it doesn't set beat_flash).
            # Lifetimes target wall-clock durations so they look the same
            # at any render FPS.
            if downbeat_flash:
                effective_density = min(1.0, sparkle_density * 1.5)
                sparkle_life = max(2, round(fps * 0.10))   # ~100 ms
            else:
                effective_density = sparkle_density
                sparkle_life = max(2, round(fps * 0.075))  # ~75 ms
            if beat_flash or sparkle_continuous:
                for i in range(MAPPED_REGION):
                    o = i * 3
                    if (buf[o] | buf[o + 1] | buf[o + 2]) and random.random() < effective_density:
                        sparkle[i] = sparkle_life

            life_div = float(sparkle_life)
            salpha = self._sparkle_layer.alpha
            for i in range(MAPPED_REGION):
                if sparkle[i] > 0:
                    o = i * 3
                    # Clamp: live sparkles may outlast a shrunk life_div
                    # after a downbeat or an FPS change.
                    t = min(1.0, sparkle[i] / life_div)
                    # Whitening coverage for the accent layer (MIX toward white):
                    # 0.7t on lit pixels, a fainter 0.4t seed on dark ones —
                    # matches the old look, but composited convexly so stacked
                    # accents can't clip. Applied in the accent composite below.
                    if buf[o] or buf[o + 1] or buf[o + 2]:
                        salpha[i] = t * 0.7
                    else:
                        salpha[i] = t * 0.4
                    sparkle[i] -= 1
                else:
                    salpha[i] = 0.0
            self._sparkle_layer.active = True

        # ── Effect: Glitch overlay ───────────────────────────────
        if glitch_prob > 0.0:
            glitch = self._glitch
            if glitch_trigger:
                for cell in range(NUM_CELLS):
                    if random.random() < glitch_prob:
                        glitch[cell] = 3

            for cell in range(NUM_CELLS):
                if glitch[cell] > 0:
                    cell_start = cell * CELL_SIZE
                    for j in range(CELL_SIZE):
                        pos = cell_start + j
                        if pos < MAPPED_REGION:
                            o = pos * 3
                            if buf[o] or buf[o + 1] or buf[o + 2]:
                                buf[o]     = 255 - buf[o]
                                buf[o + 1] = 255 - buf[o + 1]
                                buf[o + 2] = 255 - buf[o + 2]
                    glitch[cell] -= 1

        # ── Effect: Initial flash ────────────────────────────────
        # Whole-strip white flash on the first frames of a cue, as a convex
        # whitening overlay (MIX white by flash_t): on lit pixels it blends
        # toward white, on dark ones it lifts to grey — same as the old pass.
        if initial_flash > 0:
            self._flash_layer.opacity = min(initial_flash / 3.0, 1.0)
            self._flash_layer.active = True

        # ── Effect: Bonus burst (YARG bonus_effect) ──────────────
        # White celebration flash that rolls over the strip whenever YARG
        # flags a big-moment bonus. bonus_t decays 1.0 → 0.0 over the
        # engine's BONUS_BURST_DURATION (wall-clock, FPS-independent). Rendered
        # as a convex whitening overlay (MIX white by 0.85*bonus_t): on dark
        # pixels this reproduces the old grey burst; on lit pixels it whitens
        # without the old add-then-clamp that clipped when it stacked.
        if bonus_t > 0.0:
            self._bonus_layer.opacity = bonus_t * 0.85
            self._bonus_layer.active = True

        # ── Effect: Note-hold accents (Phase 4) ──────────────────
        # Paint each instrument's decaying hit as a whitening accent across its
        # two-cell slice of the strip (guitar|bass|drums|keys, left→right). The
        # four regions tile the whole mapped strip, so every pixel's coverage is
        # (re)written each frame — 0 where that instrument is silent. Convex MIX
        # (below) screen-combines it with the other accents instead of clipping.
        #
        # Lit-pixels only: coverage is applied where the scene is already lit,
        # never on dark pixels — so a busy part can't grey out a deliberate
        # blackout (matches sparkle; the other Phase-4 grades are lit-only too).
        if note_accents is not None:
            nalpha = self._note_layer.alpha
            region = NOTE_CELLS_PER_INSTRUMENT * CELL_SIZE
            any_on = False
            for inst in range(4):
                a = NOTE_ACCENT_MAX * note_accents[inst]
                start = inst * region
                end = start + region
                if end > MAPPED_REGION:
                    end = MAPPED_REGION
                if a > 0.0:
                    any_on = True
                    for i in range(start, end):
                        o = i * 3
                        nalpha[i] = a if (buf[o] | buf[o + 1] | buf[o + 2]) else 0.0
                else:
                    for i in range(start, end):
                        nalpha[i] = 0.0
            self._note_layer.active = any_on

        # ── Composite accent overlays (VISION Phase 3) ───────────
        # Sparkle + flash + bonus fold onto the scene in one convex pass. MIX
        # screen-combines, so any number active together stay bounded and
        # order-independent — the whitening overlays no longer clip-fight in a
        # single shared buffer (the core Phase-3 fix). Runs before the surge so
        # star-power still lifts/tints the final lit picture.
        Compositor.composite(buf, self._accents)

        # ── Effect: Star-power surge (v4) ────────────────────────
        # "Tasteful surge": while a player has overdrive active, lift the wash
        # brighter, blend it toward a cool blue-white, and lay a decaying cool
        # shimmer over it. Before activation, a subtle cool tint grows as the
        # meter charges. Acts only on lit pixels (see SP_TINT note) so it
        # composites over the current cue and never disturbs a blackout.
        # Written pre-brightness so global brightness and pause-dim still scale.
        if sp_active:
            # Surge intensity: a floor while active plus amount on top (amount
            # drains as overdrive is spent, so the floor keeps it from
            # vanishing mid-activation).
            surge = SP_SURGE_BASE + (1.0 - SP_SURGE_BASE) * sp_amount
            lift = 1.0 + SP_SURGE_LIFT * surge
            tint_t = SP_TINT_STRENGTH * surge
            inv_t = 1.0 - tint_t
            tr, tg, tb = SP_TINT
            density = min(0.25, SP_SHIMMER_DENSITY *
                          (1.0 + SP_SHIMMER_MULTI * max(0, sp_active_count - 1)))
            shimmer_life = max(2, round(fps * 0.12))  # ~120 ms
            shimmer = self._sp_shimmer
            for i in range(MAPPED_REGION):
                o = i * 3
                if buf[o] | buf[o + 1] | buf[o + 2]:
                    r = int(buf[o] * lift * inv_t + tr * tint_t)
                    g = int(buf[o + 1] * lift * inv_t + tg * tint_t)
                    b = int(buf[o + 2] * lift * inv_t + tb * tint_t)
                    buf[o] = r if r < 255 else 255
                    buf[o + 1] = g if g < 255 else 255
                    buf[o + 2] = b if b < 255 else 255
                    if random.random() < density:
                        shimmer[i] = shimmer_life
            # Render + decay the cool shimmer flecks (additive toward SP_TINT).
            life_div = float(shimmer_life)
            for i in range(MAPPED_REGION):
                if shimmer[i] > 0:
                    o = i * 3
                    t = min(1.0, shimmer[i] / life_div)
                    v = buf[o] + int(tr * t);       buf[o] = v if v < 255 else 255
                    v = buf[o + 1] + int(tg * t);   buf[o + 1] = v if v < 255 else 255
                    v = buf[o + 2] + int(255 * t);  buf[o + 2] = v if v < 255 else 255
                    shimmer[i] -= 1
        elif sp_charge > 0.0:
            # Charging only: a subtle cool tint on lit pixels, proportional to
            # how full the fullest player's meter is. No brightness lift, no
            # shimmer — it should be barely-there anticipation.
            tint_t = SP_CHARGE_TINT * sp_charge
            inv_t = 1.0 - tint_t
            tr, tg, tb = SP_TINT
            for i in range(MAPPED_REGION):
                o = i * 3
                if buf[o] | buf[o + 1] | buf[o + 2]:
                    buf[o] = int(buf[o] * inv_t + tr * tint_t)
                    buf[o + 1] = int(buf[o + 1] * inv_t + tg * tint_t)
                    buf[o + 2] = int(buf[o + 2] * inv_t + tb * tint_t)

        # ── Effect: Vocal pitch ribbon (Phase 4) ─────────────────
        # Paint each sounding voice as a soft triangular blob: centre = absolute
        # pitch mapped along the strip, colour = pitch class from the chroma
        # ramp. Accumulate premultiplied colour + coverage (like the scanner
        # heads), then alpha-over the scene — so blobs of the same hue add and
        # crossing voices blend, but the ribbon never overshoots 255.
        if vocal_notes is not None:
            active_voices = False
            for pitch in vocal_notes:
                if pitch > 0.0:
                    active_voices = True
                    break
            if active_voices:
                vlayer = self._vocal_layer
                vbuf = vlayer.buf
                valpha = vlayer.alpha
                for k in range(MAPPED_REGION * 3):
                    vbuf[k] = 0
                for i in range(MAPPED_REGION):
                    valpha[i] = 0.0
                half = VOCAL_HALFWIDTH
                inv_half = 1.0 / half
                span = VOCAL_MIDI_HI - VOCAL_MIDI_LO
                for pitch in vocal_notes:
                    if pitch <= 0.0:
                        continue
                    pos01 = (pitch - VOCAL_MIDI_LO) / span
                    if pos01 < 0.0:
                        pos01 = 0.0
                    elif pos01 > 1.0:
                        pos01 = 1.0
                    center = pos01 * (MAPPED_REGION - 1)
                    # Hue from pitch class (note within the octave).
                    cr, cg, cb = VOCAL_CHROMA.color_at((pitch % 12.0) / 12.0)
                    lo = int(math.ceil(center - half))
                    hi = int(math.floor(center + half))
                    if lo < 0:
                        lo = 0
                    if hi > MAPPED_REGION - 1:
                        hi = MAPPED_REGION - 1
                    for px in range(lo, hi + 1):
                        d = px - center
                        if d < 0.0:
                            d = -d
                        w = (1.0 - d * inv_half) * VOCAL_LEVEL
                        if w <= 0.0:
                            continue
                        o = px * 3
                        v = vbuf[o] + int(cr * w);     vbuf[o]     = v if v < 255 else 255
                        v = vbuf[o + 1] + int(cg * w); vbuf[o + 1] = v if v < 255 else 255
                        v = vbuf[o + 2] + int(cb * w); vbuf[o + 2] = v if v < 255 else 255
                        a = valpha[px] + w
                        valpha[px] = a if a < 1.0 else 1.0
                vlayer.active = True
                Compositor.composite(buf, (vlayer,))

        # ── Effect: Performer highlight bias (Phase 4) ───────────
        # Lean the lit pixels toward the highlighted performers' average hue.
        # A gentle convex MIX on already-lit pixels only, so it tints the wash
        # without lifting a blackout or overshooting. Off when nobody's flagged.
        if performers:
            tr = tg = tb = 0
            count = 0
            for bit, (pr, pg, pb) in PERFORMER_COLORS.items():
                if performers & bit:
                    tr += pr
                    tg += pg
                    tb += pb
                    count += 1
            if count:
                tr //= count
                tg //= count
                tb //= count
                t = PERFORMER_BIAS_STRENGTH
                inv_t = 1.0 - t
                tr_t = tr * t
                tg_t = tg * t
                tb_t = tb * t
                for i in range(MAPPED_REGION):
                    o = i * 3
                    if buf[o] | buf[o + 1] | buf[o + 2]:
                        buf[o]     = int(buf[o] * inv_t + tr_t)
                        buf[o + 1] = int(buf[o + 1] * inv_t + tg_t)
                        buf[o + 2] = int(buf[o + 2] * inv_t + tb_t)

        # ── Effect: Camera-cut lighting (Phase 5) ────────────────
        # Bias the wash toward the on-camera player: within that player's region
        # of the strip, lift brightness a touch and lean the hue toward its
        # colour — a gentle convex MIX on lit pixels only (never lifts a
        # blackout), eased in by bias_gain so the band doesn't snap as the
        # director cuts. A directed cut adds a short global bloom (cut_t) on top.
        # Regions are disjoint slices, so multi-channel shots (e.g. GuitarVocals)
        # tint two bands without double-painting.
        if camera is not None:
            subject, bias_gain, cut_t = camera
            channels = CAMERA_SUBJECT_CHANNELS.get(subject)
            if channels and bias_gain > 0.0:
                lift = 1.0 + CAMERA_BIAS_LIFT * bias_gain
                t = CAMERA_BIAS_STRENGTH * bias_gain
                inv_t = 1.0 - t
                for name in channels:
                    cr, cg, cb = CAMERA_CHANNEL_COLORS[name]
                    cr_t, cg_t, cb_t = cr * t, cg * t, cb * t
                    lo, hi = self._camera_region(CAMERA_CHANNEL_ORDER.index(name))
                    for i in range(lo, hi):
                        o = i * 3
                        if buf[o] | buf[o + 1] | buf[o + 2]:
                            r = int((buf[o] * inv_t + cr_t) * lift)
                            g = int((buf[o + 1] * inv_t + cg_t) * lift)
                            b = int((buf[o + 2] * inv_t + cb_t) * lift)
                            buf[o] = r if r < 255 else 255
                            buf[o + 1] = g if g < 255 else 255
                            buf[o + 2] = b if b < 255 else 255
            if cut_t > 0.0:
                env = 1.0 + CAMERA_CUT_LIFT * cut_t
                for i in range(MAPPED_REGION):
                    o = i * 3
                    if buf[o] | buf[o + 1] | buf[o + 2]:
                        r = int(buf[o] * env)
                        g = int(buf[o + 1] * env)
                        b = int(buf[o + 2] * env)
                        buf[o] = r if r < 255 else 255
                        buf[o + 1] = g if g < 255 else 255
                        buf[o + 2] = b if b < 255 else 255

        # ── Effect: Reveal mask (Intro) ─────────────────────────
        # Pixels light up sequentially from the strip centre outward as
        # reveal_progress climbs 0.0 → 1.0 (wall-clock driven by the
        # engine). At 1.0 (done or inactive) this is a no-op.
        if reveal_progress < 1.0:
            radius = max(1, int((MAPPED_REGION / 2.0) * reveal_progress))
            mid = MAPPED_REGION // 2
            self._mask_outside(buf, mid - radius, mid + radius)

        # ── Effect: Spotlight region mask ────────────────────────
        # Used by SILHOUETTES_SPOTLIGHT (spotlight_only is None) — keep
        # only the centre fraction of the strip visible. spotlight_only
        # cues already painted the spotlight directly so they're skipped.
        if spotlight_region > 0.0 and spotlight_only is None:
            lo, hi = self._center_window(spotlight_region)
            self._mask_outside(buf, lo, hi)

        # ── Effect: Beat pulse (beat oscillator) ─────────────────
        # A gentle global brightness pump locked to the beat: peaks the instant
        # a beat lands (beat_phase 0) and eases back over the beat via a squared
        # decay. Driven by the engine's continuous beat_phase, so it stays
        # smooth between the ~88 Hz packets and tempo-locked. Seamless by
        # construction — the envelope is at its floor (1.0) right where
        # beat_phase wraps, so a beat reset never jumps.
        if beat_pulse > 0.0:
            decay = 1.0 - beat_phase
            env = 1.0 + beat_pulse * decay * decay
            if env > 1.0:
                for i in range(MAPPED_REGION):
                    o = i * 3
                    if buf[o] | buf[o + 1] | buf[o + 2]:
                        r = int(buf[o] * env)
                        g = int(buf[o + 1] * env)
                        b = int(buf[o + 2] * env)
                        buf[o] = r if r < 255 else 255
                        buf[o + 1] = g if g < 255 else 255
                        buf[o + 2] = b if b < 255 else 255

        # ── Effect: Post-processing colour grade (Phase 4) ───────
        # Global palette modifier from the venue film grade. One loop handles
        # every grade: optional invert, desaturate toward luma by (1 - sat),
        # then per-channel tint. Lit pixels only — a grade never lifts a
        # blackout. Runs last (after masks/pulse) so it grades the final look.
        if post_grade is not None:
            invert, sat, tr, tg, tb = post_grade
            inv_sat = 1.0 - sat
            for i in range(MAPPED_REGION):
                o = i * 3
                r = buf[o]
                g = buf[o + 1]
                b = buf[o + 2]
                if not (r | g | b):
                    continue
                if invert:
                    r = 255 - r
                    g = 255 - g
                    b = 255 - b
                if inv_sat > 0.0:
                    # luma ≈ 0.30R + 0.59G + 0.11B (integer weights /256)
                    luma = (r * 77 + g * 150 + b * 29) >> 8
                    r = int(r * sat + luma * inv_sat)
                    g = int(g * sat + luma * inv_sat)
                    b = int(b * sat + luma * inv_sat)
                r = int(r * tr)
                g = int(g * tg)
                b = int(b * tb)
                buf[o]     = r if r < 255 else 255
                buf[o + 1] = g if g < 255 else 255
                buf[o + 2] = b if b < 255 else 255

        # ── Post-process chain: blur → mirror(max) → brightness ──
        # LedFx-style filter chain (Phase 6), on the fully-composed frame just
        # before brightness. A light Gaussian blur bleeds light into neighbours
        # so discrete cue events read as smooth stage light; mirror folds the
        # strip symmetric. Blurring all-black stays black, so a blackout is safe.
        if blur_amount > 0.0:
            self._blur(buf, blur_amount)
        if mirror:
            half = MAPPED_REGION // 2
            for i in range(half):
                o = i * 3
                p = (MAPPED_REGION - 1 - i) * 3
                for c in range(3):
                    m = buf[o + c] if buf[o + c] >= buf[p + c] else buf[p + c]
                    buf[o + c] = m
                    buf[p + c] = m

        # ── Apply brightness + write to output buffer ────────────
        # Pause dims everything to 35% so the strip doesn't go fully dark
        # when the player pauses — useful as a "still on, just waiting" cue.
        effective_brightness = brightness * 0.35 if paused else brightness
        out = self._out
        mapped_bytes = MAPPED_REGION * 3
        if effective_brightness >= 1.0:
            out[:mapped_bytes] = buf[:mapped_bytes]
        else:
            for k in range(mapped_bytes):
                out[k] = int(buf[k] * effective_brightness)

        # Reverse pixel order if direction is reversed
        if reverse:
            for i in range(MAPPED_REGION // 2):
                j = MAPPED_REGION - 1 - i
                io, jo = i * 3, j * 3
                out[io], out[jo] = out[jo], out[io]
                out[io + 1], out[jo + 1] = out[jo + 1], out[io + 1]
                out[io + 2], out[jo + 2] = out[jo + 2], out[io + 2]

        # Remainder LEDs mirror from start for visual wrap-around
        mirror_bytes = (self.led_count - MAPPED_REGION) * 3
        if mirror_bytes > 0:
            out[mapped_bytes:mapped_bytes + mirror_bytes] = out[:mirror_bytes]

        return bytes(out)
