"""LED zone-to-pixel mapper with per-pixel effects.

Maps 4 Stage Kit zones (8 bitmask LEDs each) to a 120 LED strip with
post-processing effects inspired by LedFx/WLED.

The strip is divided into 8 cells of 12 LEDs each (positions 0-95).
Positions 96-119 mirror positions 0-23 for visual wrap-around.

Effects layer (applied after base zone→pixel mapping):
  - Decay trails: pixels fade to black over N frames instead of instant off
  - Sine breathing: brightness modulates on a sine wave synced to BPM
  - Sparkle overlay: random pixels flash white on beat events
  - Additive blending: overlapping zone colors blend additively
  - Gradient wipe: fills sweep pixel-by-pixel instead of cell-snapping
  - Glitch overlay: random cell segments briefly invert color
"""

import math
import random
import time

from config import LED_COUNT

# Zone ordering matches Stage Kit command IDs
ZONE_NAMES = ["red", "green", "blue", "yellow"]

LEDS_PER_ZONE = 8
NUM_CELLS = 8
CELL_SIZE = 12  # LEDs per cell

MAPPED_REGION = NUM_CELLS * CELL_SIZE  # 96

# Number of pixels on each side of a color boundary to blend over
BLEND_WIDTH = 2

OFF = (0, 0, 0)
WHITE = (255, 255, 255)


def _lerp_color(c1: tuple, c2: tuple, t: float) -> tuple:
    """Linearly interpolate between two RGB colors (t=0 → c1, t=1 → c2)."""
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _add_colors(c1: tuple, c2: tuple) -> tuple:
    """Additive blend of two RGB colors, clamping at 255."""
    return (
        min(255, c1[0] + c2[0]),
        min(255, c1[1] + c2[1]),
        min(255, c1[2] + c2[2]),
    )


def _scale_color(c: tuple, s: float) -> tuple:
    """Scale an RGB color by a factor."""
    return (int(c[0] * s), int(c[1] * s), int(c[2] * s))


class LEDMapper:
    """Maps Stage Kit zone bitmask state to a 120-pixel RGB buffer with effects."""

    def __init__(self, led_count: int = LED_COUNT):
        self.led_count = led_count
        self.pixels = bytearray(led_count * 3)

        # Decay trails state: per-pixel RGB that fades over frames
        self._trail_buf = [OFF] * MAPPED_REGION

        # Sparkle state: countdown per pixel (0 = no sparkle)
        self._sparkle_frames = [0] * MAPPED_REGION

        # Glitch state: per-cell invert countdown
        self._glitch_frames = [0] * NUM_CELLS

        # Timing for breathing
        self._start_time = time.monotonic()

    def render(self, zone_bitmasks: list[int], zone_colors: dict | None = None,
               effects: dict | None = None) -> bytes:
        """Render pixels from 4 zone bitmasks with optional effects.

        Args:
            zone_bitmasks: 4 bitmasks [red, green, blue, yellow].
            zone_colors: Color mapping from settings palette.
            effects: Dict of active effects from cue engine:
                - "trails": int — decay trail length in frames (0=off)
                - "breathing": float — breathing rate as multiplier of BPM (0=off)
                - "sparkle": float — sparkle density 0.0-1.0 (0=off)
                - "sparkle_continuous": bool — sparkle every frame vs beat-triggered
                - "beat_flash": bool — beat just occurred, trigger sparkles
                - "additive": bool — use additive blending for overlapping zones
                - "glitch": float — glitch probability per beat 0.0-1.0 (0=off)
                - "glitch_trigger": bool — beat occurred, maybe trigger glitch
                - "bpm": float — current BPM for breathing sync
                - "initial_flash": int — frames remaining of initial white flash
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
        use_additive = effects.get("additive", False)
        glitch_prob = effects.get("glitch", 0.0)
        glitch_trigger = effects.get("glitch_trigger", False)
        bpm = effects.get("bpm", 120.0)
        initial_flash = effects.get("initial_flash", 0)

        # ── Base zone→pixel mapping ──────────────────────────────
        colors = [OFF] * MAPPED_REGION

        if use_additive:
            # Additive: each zone contributes its color to every pixel in its cells
            for cell in range(NUM_CELLS):
                cell_start = cell * CELL_SIZE
                for zone_idx in range(4):
                    if zone_bitmasks[zone_idx] & (1 << cell):
                        color = zone_colors[ZONE_NAMES[zone_idx]]
                        for j in range(CELL_SIZE):
                            pos = cell_start + j
                            if pos < MAPPED_REGION:
                                colors[pos] = _add_colors(colors[pos], color)
        else:
            # Standard: divide cell evenly among active zones
            for cell in range(NUM_CELLS):
                cell_start = cell * CELL_SIZE
                active = []
                for zone_idx in range(4):
                    if zone_bitmasks[zone_idx] & (1 << cell):
                        active.append(zone_idx)

                if not active:
                    continue

                n = len(active)
                leds_per = CELL_SIZE // n
                remainder = CELL_SIZE % n
                pos = cell_start
                for i, zone_idx in enumerate(active):
                    color = zone_colors[ZONE_NAMES[zone_idx]]
                    count = leds_per + (1 if i < remainder else 0)
                    for _ in range(count):
                        if pos < MAPPED_REGION:
                            colors[pos] = color
                        pos += 1

        # Second pass: solid zones (ALL=0xFF) fill completely dark cells
        solid_zones = [z for z in range(4) if zone_bitmasks[z] == 0xFF]
        if solid_zones:
            for cell in range(NUM_CELLS):
                cell_start = cell * CELL_SIZE
                if all(colors[cell_start + j] == OFF for j in range(CELL_SIZE)):
                    n = len(solid_zones)
                    leds_per = CELL_SIZE // n
                    remainder = CELL_SIZE % n
                    pos = cell_start
                    for i, zone_idx in enumerate(solid_zones):
                        color = zone_colors[ZONE_NAMES[zone_idx]]
                        count = leds_per + (1 if i < remainder else 0)
                        for _ in range(count):
                            if pos < MAPPED_REGION:
                                colors[pos] = color
                            pos += 1

        # ── Effect: Decay trails ─────────────────────────────────
        if trail_len > 0:
            decay = 1.0 - 1.0 / max(trail_len, 1)
            for i in range(MAPPED_REGION):
                if colors[i] != OFF:
                    # New color — overwrite trail
                    self._trail_buf[i] = colors[i]
                else:
                    # No new color — decay the trail
                    tr, tg, tb = self._trail_buf[i]
                    if tr > 0 or tg > 0 or tb > 0:
                        self._trail_buf[i] = (
                            int(tr * decay),
                            int(tg * decay),
                            int(tb * decay),
                        )
                        # Use trail color if it's brighter than threshold
                        if self._trail_buf[i][0] + self._trail_buf[i][1] + self._trail_buf[i][2] > 6:
                            colors[i] = self._trail_buf[i]
                        else:
                            self._trail_buf[i] = OFF
        else:
            # No trails — clear buffer
            for i in range(MAPPED_REGION):
                self._trail_buf[i] = colors[i]

        # ── Effect: Gradient blending ────────────────────────────
        blended = list(colors)
        for i in range(1, MAPPED_REGION):
            if colors[i] != colors[i - 1] and colors[i] != OFF and colors[i - 1] != OFF:
                for offset in range(1, BLEND_WIDTH + 1):
                    t = 1.0 - offset / (BLEND_WIDTH + 1)
                    left = i - offset
                    if 0 <= left < MAPPED_REGION and colors[left] == colors[i - 1]:
                        blended[left] = _lerp_color(colors[i - 1], colors[i], t * 0.5)
                    right = i - 1 + offset
                    if 0 <= right < MAPPED_REGION and colors[right] == colors[i]:
                        blended[right] = _lerp_color(colors[i], colors[i - 1], t * 0.5)
        colors = blended

        # ── Effect: Sine breathing ───────────────────────────────
        if breathing_rate > 0.0:
            elapsed = time.monotonic() - self._start_time
            beats_per_sec = max(bpm, 30.0) / 60.0
            phase = elapsed * beats_per_sec * breathing_rate * 2.0 * math.pi
            # Sine wave: 0.35 to 1.0 range (never fully dark)
            breath = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(phase))
            colors = [_scale_color(c, breath) if c != OFF else OFF for c in colors]

        # ── Effect: Sparkle overlay ──────────────────────────────
        if sparkle_density > 0.0:
            # Trigger new sparkles on beat or continuously
            if beat_flash or sparkle_continuous:
                for i in range(MAPPED_REGION):
                    if colors[i] != OFF and random.random() < sparkle_density:
                        self._sparkle_frames[i] = 3  # 3-frame sparkle

            # Apply active sparkles
            for i in range(MAPPED_REGION):
                if self._sparkle_frames[i] > 0:
                    # Blend toward white based on remaining frames
                    t = self._sparkle_frames[i] / 3.0
                    if colors[i] != OFF:
                        colors[i] = _lerp_color(colors[i], WHITE, t * 0.7)
                    else:
                        colors[i] = _scale_color(WHITE, t * 0.4)
                    self._sparkle_frames[i] -= 1

        # ── Effect: Glitch overlay ───────────────────────────────
        if glitch_prob > 0.0:
            # Trigger new glitches on beat
            if glitch_trigger:
                for cell in range(NUM_CELLS):
                    if random.random() < glitch_prob:
                        self._glitch_frames[cell] = 3  # 3-frame glitch

            # Apply active glitches: invert cell colors
            for cell in range(NUM_CELLS):
                if self._glitch_frames[cell] > 0:
                    cell_start = cell * CELL_SIZE
                    for j in range(CELL_SIZE):
                        pos = cell_start + j
                        if pos < MAPPED_REGION and colors[pos] != OFF:
                            r, g, b = colors[pos]
                            colors[pos] = (255 - r, 255 - g, 255 - b)
                    self._glitch_frames[cell] -= 1

        # ── Effect: Initial flash ────────────────────────────────
        if initial_flash > 0:
            flash_t = min(initial_flash / 3.0, 1.0)
            colors = [_lerp_color(c, WHITE, flash_t) if c != OFF else
                      _scale_color(WHITE, flash_t) for c in colors]

        # ── Write to pixel buffer ────────────────────────────────
        for i in range(min(self.led_count, MAPPED_REGION)):
            r, g, b = colors[i]
            off = i * 3
            self.pixels[off] = r
            self.pixels[off + 1] = g
            self.pixels[off + 2] = b

        # Positions 96-119 mirror positions 0-23
        for i in range(MAPPED_REGION, self.led_count):
            mirror = i - MAPPED_REGION
            off_src = mirror * 3
            off_dst = i * 3
            self.pixels[off_dst] = self.pixels[off_src]
            self.pixels[off_dst + 1] = self.pixels[off_src + 1]
            self.pixels[off_dst + 2] = self.pixels[off_src + 2]

        return bytes(self.pixels)
