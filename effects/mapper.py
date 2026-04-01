"""LED zone-to-pixel mapper.

Maps 4 Stage Kit zones (8 bitmask LEDs each) to a 120 LED strip.

The strip is divided into 8 cells of 12 LEDs each (positions 0-95).
Positions 96-119 mirror positions 0-23 for visual wrap-around.

Each cell contains one bit position per zone:
  Cell 0: positions 0-11  → Red bit 0 @0, Green bit 0 @3, Blue bit 0 @6, Yellow bit 0 @9
  Cell 1: positions 12-23 → Red bit 1 @12, Green bit 1 @15, Blue bit 1 @18, Yellow bit 1 @21
  ...etc

Within each cell, each zone "owns" a 3-LED segment:
  Red:    cell_start + 0..2
  Green:  cell_start + 3..5
  Blue:   cell_start + 6..8
  Yellow: cell_start + 9..11

When a zone's bit is ON, its 3-LED segment lights up.
When a zone is ALL (0xFF), the entire strip region for that zone lights solid
by also bleeding into adjacent unlit segments for maximum fill.
"""

from config import LED_COUNT

# Zone colors (R, G, B)
ZONE_COLORS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
}

# Zone ordering matches Stage Kit command IDs
ZONE_NAMES = ["red", "green", "blue", "yellow"]

LEDS_PER_ZONE = 8
NUM_CELLS = 8
CELL_SIZE = 12  # LEDs per cell
ZONE_SEGMENT = 3  # LEDs per zone within a cell
ZONE_OFFSETS = [0, 3, 6, 9]  # Starting offset for each zone within a cell

MAPPED_REGION = NUM_CELLS * CELL_SIZE  # 96

OFF = (0, 0, 0)


class LEDMapper:
    """Maps Stage Kit zone bitmask state to a 120-pixel RGB buffer."""

    def __init__(self, led_count: int = LED_COUNT):
        self.led_count = led_count
        self.pixels = bytearray(led_count * 3)

    def render(self, zone_bitmasks: list[int]) -> bytes:
        """Render pixels from 4 zone bitmasks.

        Each zone bit owns a 3-LED segment within its cell. When a bit is on,
        its segment lights up. Solid zones (ALL bits on) also fill any
        neighboring segments that are dark, for maximum strip coverage.
        """
        # First pass: determine which color each LED position gets
        # Start with all black
        colors = [OFF] * MAPPED_REGION

        # For each cell, set zone segments based on bitmasks
        for cell in range(NUM_CELLS):
            cell_start = cell * CELL_SIZE
            for zone_idx in range(4):
                mask = zone_bitmasks[zone_idx]
                bit_on = bool(mask & (1 << cell))
                if bit_on:
                    color = ZONE_COLORS[ZONE_NAMES[zone_idx]]
                    seg_start = cell_start + ZONE_OFFSETS[zone_idx]
                    for j in range(ZONE_SEGMENT):
                        pos = seg_start + j
                        if pos < MAPPED_REGION:
                            colors[pos] = color

        # Second pass: solid zones (ALL=0xFF) fill remaining dark LEDs
        # When multiple zones are solid, alternate their colors across gaps
        solid_zones = []
        for zone_idx in range(4):
            if zone_bitmasks[zone_idx] == 0xFF:
                solid_zones.append(zone_idx)

        if solid_zones:
            gap_idx = 0
            for i in range(MAPPED_REGION):
                if colors[i] == OFF:
                    fill_zone = solid_zones[gap_idx % len(solid_zones)]
                    colors[i] = ZONE_COLORS[ZONE_NAMES[fill_zone]]
                    gap_idx += 1

        # Write to pixel buffer
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
