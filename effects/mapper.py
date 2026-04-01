"""LED zone-to-pixel mapper.

Maps 4 Stage Kit zones (8 bitmask LEDs each) to a 120 LED strip with
interleaved zone positions and fill LEDs.

Layout: zones are interleaved every 3 LEDs across the strip.
  Positions 0, 12, 24, 36, 48, 60, 72, 84   → Zone 1 (Red)    bits 0-7
  Positions 3, 15, 27, 39, 51, 63, 75, 87   → Zone 2 (Green)  bits 0-7
  Positions 6, 18, 30, 42, 54, 66, 78, 90   → Zone 3 (Blue)   bits 0-7
  Positions 9, 21, 33, 45, 57, 69, 81, 93   → Zone 4 (Yellow) bits 0-7
  Positions 96-119                           → Mirror/repeat of 0-23

All other positions are fill LEDs that take the color of the nearest active
zone LED.

Strobe is a full-strip brightness modulation overlay.
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

# How many bitmask LEDs per zone
LEDS_PER_ZONE = 8

# Spacing between zone LED positions (3 LEDs apart in the interleave pattern)
ZONE_SPACING = 12  # Each zone's LEDs are 12 apart (4 zones × 3 positions per zone)
ZONE_OFFSETS = [0, 3, 6, 9]  # Starting offset for each zone within the pattern

# The first 96 positions (0-95) hold the 4×8 zone + fill pattern
# Positions 96-119 mirror positions 0-23 (wraps around for visual continuity)
MAPPED_REGION = 96  # Zone LEDs live in positions 0-95


def build_zone_positions() -> list[list[int]]:
    """Returns list of 4 zones, each containing 8 LED positions."""
    zones = []
    for zone_idx in range(4):
        offset = ZONE_OFFSETS[zone_idx]
        positions = [offset + i * ZONE_SPACING for i in range(LEDS_PER_ZONE)]
        zones.append(positions)
    return zones


# Pre-computed zone positions
ZONE_POSITIONS = build_zone_positions()


class LEDMapper:
    """Maps Stage Kit zone bitmask state to a 120-pixel RGB buffer."""

    def __init__(self, led_count: int = LED_COUNT):
        self.led_count = led_count
        self.pixels = bytearray(led_count * 3)  # RGB buffer

        # Build a lookup: for each LED position, what zone and bit does it belong to?
        # None means it's a fill LED.
        self._zone_map: list[tuple[int, int] | None] = [None] * led_count
        for zone_idx, positions in enumerate(ZONE_POSITIONS):
            for bit_idx, pos in enumerate(positions):
                if pos < led_count:
                    self._zone_map[pos] = (zone_idx, bit_idx)

        # Pre-compute fill LED neighbors for fast rendering
        # Each fill LED gets the color of its nearest zone LED (any zone).
        self._fill_source = self._compute_fill_sources()

    def _compute_fill_sources(self) -> list[int | None]:
        """For each LED position, find the nearest zone LED position.
        Zone LEDs point to themselves. Returns list indexed by position."""
        sources: list[int | None] = [None] * self.led_count
        # Collect all zone LED positions
        zone_positions = set()
        for positions in ZONE_POSITIONS:
            for p in positions:
                if p < self.led_count:
                    zone_positions.add(p)

        for i in range(self.led_count):
            if i in zone_positions:
                sources[i] = i  # zone LED is its own source
            else:
                # Find nearest zone LED
                best_dist = self.led_count
                best_pos = None
                for zp in zone_positions:
                    d = abs(i - zp)
                    if d < best_dist:
                        best_dist = d
                        best_pos = zp
                sources[i] = best_pos
        return sources

    def render(self, zone_bitmasks: list[int]) -> bytes:
        """Render 120 pixels from 4 zone bitmasks.

        Args:
            zone_bitmasks: [red_mask, green_mask, blue_mask, yellow_mask]
                          Each is a uint8 bitmask (bits 0-7 = LEDs 0-7).

        Returns:
            Flat RGB bytes (360 bytes for 120 LEDs).
        """
        # First pass: set zone LED colors based on bitmasks
        zone_colors_at: dict[int, tuple[int, int, int]] = {}

        for zone_idx in range(4):
            mask = zone_bitmasks[zone_idx]
            color = ZONE_COLORS[ZONE_NAMES[zone_idx]]

            for bit_idx, pos in enumerate(ZONE_POSITIONS[zone_idx]):
                if pos >= self.led_count:
                    continue
                if mask & (1 << bit_idx):
                    zone_colors_at[pos] = color
                else:
                    zone_colors_at[pos] = (0, 0, 0)

        # Second pass: set all pixels
        for i in range(min(self.led_count, MAPPED_REGION)):
            source = self._fill_source[i]
            if source is not None and source in zone_colors_at:
                r, g, b = zone_colors_at[source]
            else:
                r, g, b = 0, 0, 0
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
