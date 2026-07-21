"""Tests for the Gradient palette primitive.

Run: python -m pytest tests/test_gradient.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.gradient import Gradient, GRADIENTS, RESOLUTION, _smootherstep  # noqa: E402


def test_smootherstep_endpoints():
    assert _smootherstep(0.0) == 0.0
    assert _smootherstep(1.0) == 1.0
    assert 0.0 < _smootherstep(0.5) < 1.0


def test_endpoints_match_stops():
    g = Gradient([(255, 0, 0), (0, 0, 255)])
    assert g.color_at(0.0) == (255, 0, 0)
    assert g.color_at(0.999) == (0, 0, 255)


def test_midpoint_is_a_blend():
    g = Gradient([(255, 0, 0), (0, 0, 255)])
    r, gr, b = g.color_at(0.5)
    assert 0 < r < 255 and 0 < b < 255 and gr == 0


def test_wraps_into_unit_interval():
    g = Gradient([(255, 0, 0), (0, 255, 0)])
    assert g.color_at(1.0) == g.color_at(0.0)
    assert g.color_at(2.5) == g.color_at(0.5)
    assert g.color_at(-0.25) == g.color_at(0.75)


def test_positioned_stops_are_honoured():
    # Green sits at 0.25; before it the ramp is mostly red, after mostly blue.
    g = Gradient([(0.0, (255, 0, 0)), (0.25, (0, 255, 0)), (1.0, (0, 0, 255))])
    assert g.color_at(0.25)[1] > 200          # green dominates at its stop
    assert g.color_at(0.0) == (255, 0, 0)
    assert g.color_at(0.999) == (0, 0, 255)


def test_single_stop_is_solid():
    g = Gradient([(10, 20, 30)])
    assert g.color_at(0.0) == (10, 20, 30)
    assert g.color_at(0.7) == (10, 20, 30)


def test_lut_size():
    g = Gradient([(0, 0, 0), (255, 255, 255)])
    assert len(g._lut) == RESOLUTION


def test_named_cyclic_gradients_are_seamless():
    # The scroll-safe ramps must return to their start colour at the wrap.
    for name in ("rainbow", "warm", "cool"):
        g = GRADIENTS[name]
        assert g.color_at(0.0) == g.color_at(0.999), f"{name} has a wrap seam"


def test_registry_has_expected_names():
    for name in ("rainbow", "warm", "cool", "fire", "ocean", "sunset"):
        assert isinstance(GRADIENTS[name], Gradient)


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
