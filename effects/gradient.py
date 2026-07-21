"""Gradient palette primitive — an eased colour ramp sampled by position.

Lets a cue colour the strip by position/phase instead of by fixed zone colours
(LedFx's gradient idea, reimplemented host-side — see VISION.md). A Gradient
precomputes a LUT of RESOLUTION entries with smootherstep-eased transitions
between stops, so `color_at()` is an O(1) lookup and blends read smooth rather
than linear-banded. `color_at(t)` wraps t into [0, 1).

Cues reference a gradient by name from GRADIENTS; the mapper recolours lit
pixels from it and scrolls it along the strip with the beat clock, so the
gradient rides the music while zones/patterns still own which pixels are lit
and how bright.
"""

RESOLUTION = 256


def _smootherstep(t: float) -> float:
    """6t^5 - 15t^4 + 10t^3 — zero 1st/2nd derivatives at both ends."""
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


class Gradient:
    """An eased colour ramp precomputed into a lookup table."""

    __slots__ = ("_lut",)

    def __init__(self, stops):
        """stops: list of (r,g,b) evenly spaced, or list of (pos, (r,g,b)).

        A ramp whose first and last colours match scrolls seamlessly.
        """
        self._lut = self._build(self._normalize(stops))

    @staticmethod
    def _normalize(stops):
        if not stops:
            raise ValueError("gradient needs at least one stop")
        placed = []
        for s in stops:
            # (pos, (r,g,b)) form vs bare (r,g,b)
            if len(s) == 2 and isinstance(s[1], (tuple, list)):
                placed.append((float(s[0]), tuple(s[1])))
            else:
                placed.append((None, tuple(s)))
        if any(p is None for p, _ in placed):
            n = len(placed)
            placed = [(i / (n - 1) if n > 1 else 0.0, c)
                      for i, (_, c) in enumerate(placed)]
        placed.sort(key=lambda pc: pc[0])
        # Extend the ends so the ramp covers the whole [0, 1] domain.
        if placed[0][0] > 0.0:
            placed.insert(0, (0.0, placed[0][1]))
        if placed[-1][0] < 1.0:
            placed.append((1.0, placed[-1][1]))
        return placed

    @staticmethod
    def _build(stops):
        lut = []
        si = 0
        last = len(stops) - 1
        for k in range(RESOLUTION):
            t = k / (RESOLUTION - 1)
            while si < last - 1 and t > stops[si + 1][0]:
                si += 1
            p0, c0 = stops[si]
            p1, c1 = stops[si + 1]
            span = p1 - p0
            local = 0.0 if span <= 0.0 else (t - p0) / span
            if local > 1.0:
                local = 1.0
            e = _smootherstep(local)
            lut.append((
                int(c0[0] + (c1[0] - c0[0]) * e),
                int(c0[1] + (c1[1] - c0[1]) * e),
                int(c0[2] + (c1[2] - c0[2]) * e),
            ))
        return lut

    def color_at(self, t: float):
        """Sample the ramp at position t (wrapped into [0, 1)). Returns (r,g,b)."""
        idx = int((t % 1.0) * RESOLUTION)
        if idx >= RESOLUTION:
            idx = RESOLUTION - 1
        return self._lut[idx]


# A few tasteful named ramps for cues to reference. The seamless (cyclic) ones
# — first colour == last colour — are the ones that scroll without a wrap seam.
GRADIENTS = {
    # Cyclic (seamless scroll):
    "rainbow": Gradient([(255, 0, 0), (255, 255, 0), (0, 255, 0),
                         (0, 255, 255), (0, 0, 255), (255, 0, 255), (255, 0, 0)]),
    "warm":    Gradient([(255, 60, 0), (255, 150, 0), (255, 40, 80), (255, 60, 0)]),
    "cool":    Gradient([(0, 80, 255), (0, 220, 200), (80, 0, 255), (0, 80, 255)]),
    # Non-cyclic (nice as a static wash / breathing ramp):
    "fire":    Gradient([(120, 0, 0), (255, 60, 0), (255, 200, 0), (255, 255, 180)]),
    "ocean":   Gradient([(0, 20, 120), (0, 120, 200), (0, 220, 180), (40, 255, 140)]),
    "sunset":  Gradient([(40, 0, 80), (200, 0, 120), (255, 120, 0), (255, 220, 80)]),
}
