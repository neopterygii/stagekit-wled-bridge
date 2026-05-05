"""LED zone-to-pixel mapper with per-pixel effects.

Maps 4 Stage Kit zones (8 bitmask LEDs each) to an LED strip with
post-processing effects inspired by LedFx/WLED.

The strip is divided into 8 cells (LED_COUNT // 8 LEDs each).
Any remainder LEDs mirror from the start for visual wrap-around.

Effects layer (applied after base zone→pixel mapping):
  - Decay trails: pixels fade to black over N frames instead of instant off
  - Sine breathing: brightness modulates on a sine wave synced to BPM
  - Sparkle overlay: random pixels flash white on beat events
  - Additive blending: overlapping zone colors blend additively
  - Gradient wipe: fills sweep pixel-by-pixel instead of cell-snapping
  - Glitch overlay: random cell segments briefly invert color

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

# Zone ordering matches Stage Kit command IDs
ZONE_NAMES = ["red", "green", "blue", "yellow"]

LEDS_PER_ZONE = 8
NUM_CELLS = 8
CELL_SIZE = LED_COUNT // NUM_CELLS  # LEDs per cell (scales with strip length)

MAPPED_REGION = NUM_CELLS * CELL_SIZE

# Number of pixels on each side of a color boundary to blend over (scales with cell size)
BLEND_WIDTH = max(1, CELL_SIZE // 6)

# Byte-level constants for a black pixel
_OFF_R = 0
_OFF_G = 0
_OFF_B = 0


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

        # Decay trails state: flat R,G,B per pixel
        self._trail = bytearray(MAPPED_REGION * 3)

        # Sparkle state: countdown per pixel (0 = no sparkle)
        self._sparkle = bytearray(MAPPED_REGION)

        # Glitch state: per-cell invert countdown
        self._glitch = bytearray(NUM_CELLS)

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

    def render(self, zone_bitmasks: list[int], zone_colors: dict | None = None,
               effects: dict | None = None, brightness: float = 1.0,
               reverse: bool = False,
               zone_cell_levels: list[list[float]] | None = None) -> bytes:
        """Render pixels from 4 zone bitmasks with optional effects.

        Args:
            zone_bitmasks: 4 bitmasks [red, green, blue, yellow] — used to
                detect "solid" background zones (mask 0xFF) and as a
                fallback when zone_cell_levels isn't provided.
            zone_colors: Color mapping from settings palette.
            effects: Dict of active effects from cue engine.
            brightness: Global brightness 0.0-1.0 (baked into output).
            reverse: Mirror the output horizontally.
            zone_cell_levels: 4 zones x 8 cells of float 0.0-1.0 brightness.
                When provided, the engine's tick() has computed sub-cell
                interpolation for smooth scanner motion. When None, levels
                are derived from zone_bitmasks (binary on/off).

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
        bonus_burst = effects.get("bonus_burst", 0)
        reveal_frames = effects.get("reveal_frames", 0)
        reveal_total = effects.get("reveal_total", 0)
        spotlight_region = effects.get("spotlight_region", 0.0)  # 0 = no mask
        spotlight_only = effects.get("spotlight_only", None)     # (r,g,b) or None

        buf = self._buf

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
            sr = spotlight_only[0]
            sg = spotlight_only[1]
            sb = spotlight_only[2]
            half = max(1, int(MAPPED_REGION * spotlight_region / 2.0)) if spotlight_region > 0 else MAPPED_REGION // 2
            mid = MAPPED_REGION // 2
            start = max(0, mid - half)
            end = min(MAPPED_REGION, mid + half)
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
            if downbeat_flash:
                effective_density = min(1.0, sparkle_density * 1.5)
                sparkle_life = 4
            else:
                effective_density = sparkle_density
                sparkle_life = 3
            if beat_flash or sparkle_continuous:
                for i in range(MAPPED_REGION):
                    o = i * 3
                    if (buf[o] | buf[o + 1] | buf[o + 2]) and random.random() < effective_density:
                        sparkle[i] = sparkle_life

            life_div = float(sparkle_life)
            for i in range(MAPPED_REGION):
                if sparkle[i] > 0:
                    o = i * 3
                    t = sparkle[i] / life_div
                    if buf[o] or buf[o + 1] or buf[o + 2]:
                        # Blend toward white
                        t07 = t * 0.7
                        inv = 1.0 - t07
                        buf[o]     = int(buf[o] * inv + 255 * t07)
                        buf[o + 1] = int(buf[o + 1] * inv + 255 * t07)
                        buf[o + 2] = int(buf[o + 2] * inv + 255 * t07)
                    else:
                        v = int(255 * t * 0.4)
                        buf[o] = v
                        buf[o + 1] = v
                        buf[o + 2] = v
                    sparkle[i] -= 1

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
        if initial_flash > 0:
            flash_t = min(initial_flash / 3.0, 1.0)
            inv = 1.0 - flash_t
            white_v = int(255 * flash_t)
            for i in range(MAPPED_REGION):
                o = i * 3
                if buf[o] or buf[o + 1] or buf[o + 2]:
                    buf[o]     = int(buf[o] * inv + 255 * flash_t)
                    buf[o + 1] = int(buf[o + 1] * inv + 255 * flash_t)
                    buf[o + 2] = int(buf[o + 2] * inv + 255 * flash_t)
                else:
                    buf[o] = white_v
                    buf[o + 1] = white_v
                    buf[o + 2] = white_v

        # ── Effect: Bonus burst (YARG bonus_effect) ──────────────
        # White celebration flash that rolls over the strip whenever YARG
        # flags a big-moment bonus. Decays over ~8 frames.
        if bonus_burst > 0:
            burst_t = bonus_burst / 8.0
            burst_w = int(255 * burst_t * 0.85)
            inv = 1.0 - burst_t * 0.7
            for i in range(MAPPED_REGION):
                o = i * 3
                if buf[o] | buf[o + 1] | buf[o + 2]:
                    buf[o]     = min(255, int(buf[o] * inv + burst_w))
                    buf[o + 1] = min(255, int(buf[o + 1] * inv + burst_w))
                    buf[o + 2] = min(255, int(buf[o + 2] * inv + burst_w))
                else:
                    buf[o] = burst_w
                    buf[o + 1] = burst_w
                    buf[o + 2] = burst_w

        # ── Effect: Reveal mask (Intro) ─────────────────────────
        # Pixels light up sequentially from the strip centre outward over
        # reveal_total frames. While reveal_frames > 0, mask everything
        # outside the current radius. When done, no-op.
        if reveal_total > 0 and reveal_frames > 0:
            elapsed = reveal_total - reveal_frames
            progress = elapsed / float(reveal_total)
            if progress < 1.0:
                radius = int((MAPPED_REGION / 2.0) * progress)
                mid = MAPPED_REGION // 2
                lo = mid - radius
                hi = mid + radius
                for i in range(MAPPED_REGION):
                    if i < lo or i >= hi:
                        o = i * 3
                        buf[o] = 0
                        buf[o + 1] = 0
                        buf[o + 2] = 0

        # ── Effect: Spotlight region mask ────────────────────────
        # Used by SILHOUETTES_SPOTLIGHT (spotlight_only is None) — keep
        # only the centre fraction of the strip visible. spotlight_only
        # cues already painted the spotlight directly so they're skipped.
        if spotlight_region > 0.0 and spotlight_only is None:
            half = max(1, int(MAPPED_REGION * spotlight_region / 2.0))
            mid = MAPPED_REGION // 2
            lo = mid - half
            hi = mid + half
            for i in range(MAPPED_REGION):
                if i < lo or i >= hi:
                    o = i * 3
                    buf[o] = 0
                    buf[o + 1] = 0
                    buf[o + 2] = 0

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
