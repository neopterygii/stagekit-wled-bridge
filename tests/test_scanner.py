"""Tests for the continuous sub-pixel scanner renderer (VISION Phase 2).

The old renderer moved a scanner by crossfading whole 15-LED cell-blocks, so
at every handoff the peak halved (255→127) and the lit width doubled (15→30)
and the position quantised to cell boundaries. The fix paints a soft profile
at a *continuous* float position: constant peak, constant width, gliding
pixel-by-pixel. These tests pin that behaviour.

Run: python -m pytest tests/test_scanner.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import (  # noqa: E402
    CueEngine, _TimePattern, _ring_delta, RED, BLUE, YELLOW,
)
from effects.mapper import LEDMapper, MAPPED_REGION, CELL_SIZE  # noqa: E402
from protocol.yarg_packet import CueByte  # noqa: E402


def _lum(px):
    n = len(px) // 3
    return [max(px[i * 3], px[i * 3 + 1], px[i * 3 + 2]) for i in range(n)]


# ── _ring_delta: shorter way round the 8-cell ring ───────────────

def test_ring_delta_takes_short_path():
    assert _ring_delta(0, 1) == 1
    assert _ring_delta(7, 0) == 1     # wrap forward, not -7
    assert _ring_delta(0, 7) == -1    # wrap backward
    assert _ring_delta(3, 4) == 1


# ── motion_heads: continuous interpolation of positions ──────────

def _single(n=8, zone=BLUE):
    steps = [[(zone, 1 << i)] for i in range(n)]
    return _TimePattern(steps, bpm_sync=True, param=0.25, now=0.0)


def test_single_head_glides_between_cells():
    p = _single()
    assert p.motion_heads(0, 1, 0.0) == [(BLUE, 0.0, 1.0)]
    assert p.motion_heads(0, 1, 0.5) == [(BLUE, 0.5, 1.0)]
    # Position is continuous, not snapped to a cell.
    (_, pos, lvl), = p.motion_heads(0, 1, 0.25)
    assert abs(pos - 0.25) < 1e-9 and lvl == 1.0


def test_single_head_wraps_forward_smoothly():
    p = _single()
    (_, pos, _), = p.motion_heads(7, 0, 0.5)
    assert abs(pos - 7.5) < 1e-9        # 7 → 8, not 7 → 0 (no backward jump)


def test_paired_heads_stay_matched():
    # CHORUS-style opposing pair: ZERO|FOUR → ONE|FIVE ...
    steps = [[(RED, (1 << i) | (1 << (i + 4)))] for i in range(4)]
    p = _TimePattern(steps, bpm_sync=True, param=0.25, now=0.0)
    heads = sorted(p.motion_heads(0, 1, 0.5))
    assert len(heads) == 2
    assert abs(heads[0][1] - 0.5) < 1e-9
    assert abs(heads[1][1] - 4.5) < 1e-9


# ── engine wiring: motion owns its zones, static keeps the cells ──

def test_motion_cue_zeros_owned_cells_and_emits_heads():
    e = CueEngine(); e.bpm = 120.0
    p = _single()
    e._time_patterns = [p]
    e.zones[BLUE] = 0xFF          # stale bits that must NOT render as blocks
    e.tick(0.0)
    assert e.motion_sources, "expected a scanner head"
    assert all(v == 0.0 for v in e.zone_cell_levels[BLUE]), \
        "motion-owned zone must not also paint cell blocks"


def test_static_cue_has_no_motion_sources():
    e = CueEngine(); e.bpm = 120.0
    e.on_cue(CueByte.VERSE)       # full BLUE wash, no time pattern
    e.tick(0.0)
    assert e.motion_sources == []
    assert all(v == 1.0 for v in e.zone_cell_levels[BLUE])


def test_paused_freezes_heads_without_double_painting():
    e = CueEngine(); e.bpm = 120.0
    e._time_patterns = [_single()]
    e.tick(0.0)
    frozen = list(e.motion_sources)
    e.on_paused(True)
    e.tick(1.0)                   # would have advanced if not frozen
    assert e.motion_sources == frozen           # held still
    assert all(v == 0.0 for v in e.zone_cell_levels[BLUE])  # no blocks


# ── the regression: constant peak + width across a sub-cell glide ─

def test_scanner_peak_and_width_are_constant_while_gliding():
    m = LEDMapper()
    zero = [[0.0] * 8 for _ in range(4)]
    peaks, fwhm = [], []
    pos = 0.0
    while pos < 2.0:              # glide two whole cells, sub-cell steps
        px = m.render([0, 0, 0, 0], effects={}, zone_cell_levels=zero,
                      motion_sources=[(BLUE, pos, 1.0)])
        lum = _lum(px)
        peak = max(lum)
        peaks.append(peak)
        fwhm.append(sum(1 for v in lum if v > peak * 0.5))
        pos += 0.1

    # Peak never collapses toward the old 127 half-brightness handoff.
    assert min(peaks) > 200, f"peak dropped to {min(peaks)} (throb returned)"
    assert max(peaks) - min(peaks) <= 20, "peak should stay ~constant"
    # Width never balloons toward the old 2x (≈30) handoff smear.
    assert max(fwhm) - min(fwhm) <= 3, f"width varied {min(fwhm)}..{max(fwhm)}"
    assert max(fwhm) <= CELL_SIZE + 4, "lit width should stay ~one cell"


def test_scanner_position_glides_monotonically():
    m = LEDMapper()
    zero = [[0.0] * 8 for _ in range(4)]
    centroids = []
    for k in range(6):
        pos = 2.0 + k * 0.1      # mid-strip, clear of the wrap seam
        px = m.render([0, 0, 0, 0], effects={}, zone_cell_levels=zero,
                      motion_sources=[(BLUE, pos, 1.0)])
        lum = _lum(px)
        tot = sum(lum)
        centroids.append(sum(i * lum[i] for i in range(len(lum))) / tot)
    # ~CELL_SIZE * 0.1 px per step, strictly increasing, small even steps.
    steps = [b - a for a, b in zip(centroids, centroids[1:])]
    assert all(s > 0 for s in steps), "scanner should move forward each frame"
    assert max(steps) - min(steps) < 1.0, "glide should be even, not jumpy"


def test_tiled_chase_has_no_dark_seams():
    # Eight adjacent same-colour heads → partition of unity fills the strip.
    m = LEDMapper()
    zero = [[0.0] * 8 for _ in range(4)]
    ms = [(BLUE, float(c), 1.0) for c in range(8)]
    px = m.render([0, 0, 0, 0], effects={}, zone_cell_levels=zero,
                  motion_sources=ms)
    lum = _lum(px)
    assert min(lum) > 0.7 * max(lum), \
        f"dark seam between tiled heads: min={min(lum)} max={max(lum)}"


def test_scanner_composites_over_static_wash():
    # RED scanner over a full YELLOW wash: at the core it reads red, off the
    # scanner it reads the yellow wash (alpha-composite, not additive wipe-out).
    m = LEDMapper()
    levels = [[0.0] * 8 for _ in range(4)]
    for c in range(8):
        levels[YELLOW][c] = 1.0
    px = m.render([0, 0, 0, 0xFF], effects={}, zone_cell_levels=levels,
                  motion_sources=[(RED, 2.0, 1.0)])
    n = MAPPED_REGION
    core = (2 * CELL_SIZE) + CELL_SIZE // 2      # pixel under the scanner core
    # Core pixel is red-dominant (red channel well above green).
    assert px[core * 3] > px[core * 3 + 1] + 60
    # A pixel far from the scanner keeps the yellow wash (green channel lit).
    far = (6 * CELL_SIZE) + CELL_SIZE // 2
    assert px[far * 3 + 1] > 100


if __name__ == "__main__":
    import traceback
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
