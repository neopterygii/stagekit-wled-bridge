"""LED zone-to-pixel mapper.

Maps 4 Stage Kit zones (8 bitmask LEDs each) to a 120 LED strip.

The strip is divided into 8 cells of 12 LEDs each (positions 0-95).
Positions 96-119 mirror positions 0-23 for visual wrap-around.

Each cell contains one bit position per zone:
  Cell 0: positions 0-11  → Red bit 0 @0, Green bit 0 @3, Blue bit 0 @6, Yellow bit 0 @9
  Cell 1: positions 12-23 → Red bit 1 @12, Green bit 1 @15, Blue bit 1 @18, Yellow bit 1 @21
  ...etc

Within each cell, active zones expand to fill the entire cell. When
multiple zones are active in a cell, each gets an equal share. When only
one zone is active in a cell, it fills the whole cell (12 LEDs).

Solid zones (ALL=0xFF) additionally fill any remaining dark cells.

Gradient blending smooths the boundary between adjacent color blocks
for a more modern LED strip look inspired by LedFx.
"""

from config import LED_COUNT

# Zone ordering matches Stage Kit command IDs
ZONE_NAMES = ["red", "green", "blue", "yellow"]

LEDS_PER_ZONE = 8
NUM_CELLS = 8
CELL_SIZE = 12  # LEDs per cell
ZONE_OFFSETS = [0, 3, 6, 9]  # Starting offset for each zone within a cell

MAPPED_REGION = NUM_CELLS * CELL_SIZE  # 96

# Number of pixels on each side of a color boundary to blend over
BLEND_WIDTH = 2

OFF = (0, 0, 0)


def _lerp_color(c1: tuple, c2: tuple, t: float) -> tuple:
    """Linearly interpolate between two RGB colors (t=0 → c1, t=1 → c2)."""
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


class LEDMapper:
    """Maps Stage Kit zone bitmask state to a 120-pixel RGB buffer."""

    def __init__(self, led_count: int = LED_COUNT):
        self.led_count = led_count
        self.pixels = bytearray(led_count * 3)

    def render(self, zone_bitmasks: list[int], zone_colors: dict | None = None) -> bytes:
        """Render pixels from 4 zone bitmasks.

        Active zones within each cell expand to fill it evenly. If only one
        zone is active in a cell, it gets all 12 LEDs. If two are active,
        each gets 6. Solid zones (ALL=0xFF) also fill completely dark cells.
        Adjacent color blocks are gradient-blended for a smooth LedFx-style look.

        Args:
            zone_bitmasks: 4 bitmasks [red, green, blue, yellow].
            zone_colors: Optional color mapping override from settings.
        """
        if zone_colors is None:
            from settings import PALETTES
            zone_colors = PALETTES["default"]["colors"]

        colors = [OFF] * MAPPED_REGION

        for cell in range(NUM_CELLS):
            cell_start = cell * CELL_SIZE
            # Find which zones are active in this cell
            active = []
            for zone_idx in range(4):
                if zone_bitmasks[zone_idx] & (1 << cell):
                    active.append(zone_idx)

            if not active:
                continue

            # Divide the 12 LEDs evenly among active zones
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
        solid_zones = []
        for zone_idx in range(4):
            if zone_bitmasks[zone_idx] == 0xFF:
                solid_zones.append(zone_idx)

        if solid_zones:
            for cell in range(NUM_CELLS):
                cell_start = cell * CELL_SIZE
                # Check if this cell is entirely dark
                cell_dark = all(colors[cell_start + j] == OFF
                                for j in range(CELL_SIZE))
                if cell_dark:
                    # Fill with alternating solid zone colors
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

        # Gradient blending pass: smooth color transitions between adjacent blocks
        blended = list(colors)
        for i in range(1, MAPPED_REGION):
            if colors[i] != colors[i - 1]:
                # Found a color boundary — blend BLEND_WIDTH pixels on each side
                for offset in range(1, BLEND_WIDTH + 1):
                    t = 1.0 - offset / (BLEND_WIDTH + 1)
                    # Blend pixels before the boundary toward the next color
                    left = i - offset
                    if 0 <= left < MAPPED_REGION and colors[left] == colors[i - 1]:
                        blended[left] = _lerp_color(colors[i - 1], colors[i], t * 0.5)
                    # Blend pixels after the boundary toward the previous color
                    right = i - 1 + offset
                    if 0 <= right < MAPPED_REGION and colors[right] == colors[i]:
                        blended[right] = _lerp_color(colors[i], colors[i - 1], t * 0.5)

        # Write to pixel buffer
        for i in range(min(self.led_count, MAPPED_REGION)):
            r, g, b = blended[i]
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
