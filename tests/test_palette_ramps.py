"""Structural tests for the palette ramps extracted from LedFx/WLED.

`tools/extract_palette_ramps.py` recovers each palette's full upstream gradient
and its output is pasted into `settings.py` as literals. These tests guard the
shape of that data, not the extraction: a hand-edit or a future palette must not
be able to introduce a ramp the ring builder can't use.

Run: python -m pytest tests/test_palette_ramps.py -v
"""

import colorsys
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from settings import PALETTES  # noqa: E402

# Palettes that deliberately carry no ramp. `default` and `neon` are ours with
# no upstream; the other four failed the generator's audit — their four colours
# are not the family the upstream gradient describes. All six fall back to a
# ring built from their own four colours.
RING_FALLBACK = {"default", "neon", "party", "forest", "sakura", "frost"}


def _hsv(rgb):
    return colorsys.rgb_to_hsv(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)


def test_every_palette_is_either_ramped_or_a_known_fallback():
    """A new palette can't silently skip the family it hands the layers."""
    for key, palette in PALETTES.items():
        has_ramp = bool(palette.get("ramp"))
        if key in RING_FALLBACK:
            assert not has_ramp, f"{key} is listed as a ring fallback but has a ramp"
        else:
            assert has_ramp, f"{key} has no ramp and is not a known fallback"


def test_fallback_list_names_real_palettes():
    assert RING_FALLBACK <= set(PALETTES), "fallback list names a missing palette"


@pytest.mark.parametrize("key", sorted(set(PALETTES) - RING_FALLBACK))
def test_ramp_stops_are_ordered_and_normalised(key):
    ramp = PALETTES[key]["ramp"]
    positions = [p for p, _ in ramp]
    assert positions == sorted(positions), f"{key}: stops out of order"
    assert positions[0] == pytest.approx(0.0), f"{key}: does not start at 0"
    assert positions[-1] == pytest.approx(1.0), f"{key}: does not end at 1"
    for pos, rgb in ramp:
        assert 0.0 <= pos <= 1.0
        assert len(rgb) == 3
        assert all(isinstance(c, int) and 0 <= c <= 255 for c in rgb)


@pytest.mark.parametrize("key", sorted(set(PALETTES) - RING_FALLBACK))
def test_ramp_is_richer_than_the_four_colours_it_supplements(key):
    """Otherwise it is not worth having — the ring fallback would say more."""
    assert len(PALETTES[key]["ramp"]) >= 3


@pytest.mark.parametrize("key", sorted(set(PALETTES) - RING_FALLBACK))
def test_ramp_carries_no_achromatic_stops(key):
    """The generator's black/white filter actually ran.

    Upstream ramps are authored as spatial gradients and routinely open on black
    or close on white (lava_gp does both). Those stops carry no hue, so a layer
    remapped onto one would go dark or grey instead of taking the palette's
    colour — the exact failure this whole change exists to prevent.
    """
    for pos, rgb in PALETTES[key]["ramp"]:
        _, s, v = _hsv(rgb)
        assert v >= 0.15, f"{key}: near-black stop {rgb} at {pos}"
        assert s >= 0.25, f"{key}: near-grey stop {rgb} at {pos}"
