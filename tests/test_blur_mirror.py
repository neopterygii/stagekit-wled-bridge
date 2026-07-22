"""Tests for the blur/mirror post-process chain (VISION Phase 6).

The final composed frame runs through an LedFx-style filter chain just before
brightness: a light Gaussian **blur** (fog-lifted) smooths discrete cue events
into stage-quality light, then an optional **mirror(max)** folds the strip into
a left-right symmetric look. Both are gated by the runtime effect-toggle
framework — blur defaults on, mirror is an opt-in look (default off).

These tests pin: the blur kernel's spreading/blackout-safe/bounded invariants,
the mirror(max) symmetry + fold semantics, the engine's fog-driven strength,
and the toggle gating that makes each layer inert when off.

Run: python -m pytest tests/test_blur_mirror.py -v
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from settings import BridgeSettings  # noqa: E402
from effects.cue_engine import CueEngine, BLUR_BASE, BLUR_FOG_BOOST  # noqa: E402
from effects.mapper import LEDMapper, MAPPED_REGION  # noqa: E402

COLORS = {"red": (255, 0, 0), "green": (0, 255, 0),
          "blue": (0, 0, 255), "yellow": (255, 255, 0)}

# A scene lit on the left only (cells 0-1 red), so mirror has something to fold.
LEFT_LIT = [0b00000011, 0, 0, 0]


def _settings():
    d = tempfile.mkdtemp()
    return BridgeSettings(path=str(Path(d) / "settings.json"))


def _render(m, effects):
    return m.render(LEFT_LIT, zone_colors=COLORS, effects=effects, brightness=1.0)


def _lit_count(px):
    return sum(1 for i in range(MAPPED_REGION)
               if px[i * 3] or px[i * 3 + 1] or px[i * 3 + 2])


# ── Blur kernel invariants ───────────────────────────────────────

def test_blur_zero_is_identity():
    m = LEDMapper(MAPPED_REGION)
    buf = bytearray(MAPPED_REGION * 3)
    buf[10 * 3] = 200
    before = bytes(buf)
    m._blur(buf, 0.0)
    assert bytes(buf) == before


def test_blur_spreads_light_to_neighbours():
    m = LEDMapper(MAPPED_REGION)
    buf = bytearray(MAPPED_REGION * 3)
    c = 20
    buf[c * 3] = 240  # a single bright red pixel, dark neighbours
    m._blur(buf, 1.0)
    assert buf[(c - 1) * 3] > 0     # light bled left
    assert buf[(c + 1) * 3] > 0     # light bled right
    assert buf[c * 3] < 240         # centre spread out


def test_blur_keeps_blackout_black():
    # Blurring an all-black frame must not lift it — a blackout stays a blackout.
    m = LEDMapper(MAPPED_REGION)
    buf = bytearray(MAPPED_REGION * 3)
    m._blur(buf, 1.0)
    assert not any(buf)


def test_blur_is_bounded_and_preserves_uniform_field():
    m = LEDMapper(MAPPED_REGION)
    buf = bytearray([255] * (MAPPED_REGION * 3))
    m._blur(buf, 1.0)
    assert all(v == 255 for v in buf)   # a flat field is a blur fixed point


def test_blur_wet_mix_scales_the_effect():
    # A partial wet mix lands between crisp and fully-blurred.
    def peak_after(wet):
        m = LEDMapper(MAPPED_REGION)
        buf = bytearray(MAPPED_REGION * 3)
        buf[20 * 3] = 240
        m._blur(buf, wet)
        return buf[20 * 3]
    assert peak_after(1.0) < peak_after(0.4) < 240


# ── Mirror(max) semantics ────────────────────────────────────────

def test_mirror_off_is_identity():
    m = LEDMapper(MAPPED_REGION)
    assert _render(m, {"mirror": False}) == _render(m, {})


def test_mirror_makes_output_symmetric():
    m = LEDMapper(MAPPED_REGION)
    out = _render(m, {"mirror": True})
    for i in range(MAPPED_REGION // 2):
        j = MAPPED_REGION - 1 - i
        assert out[i * 3:i * 3 + 3] == out[j * 3:j * 3 + 3]


def test_mirror_max_folds_light_onto_the_dark_half():
    m = LEDMapper(MAPPED_REGION)
    crisp = _render(m, {})               # left lit, right dark
    mirrored = _render(m, {"mirror": True})
    # The dark half now carries the mirrored light, so more pixels are lit,
    # and max keeps the brighter of each pair — no byte is ever dimmed.
    assert _lit_count(mirrored) > _lit_count(crisp)
    assert all(mirrored[k] >= crisp[k] for k in range(MAPPED_REGION * 3))


# ── Engine emission + fog ────────────────────────────────────────

def test_engine_emits_default_blur_and_mirror_capability():
    fx = CueEngine().get_effects()
    assert fx["blur"] == BLUR_BASE
    assert fx["mirror"] is True


def test_fog_lifts_blur_toward_the_glow_floor():
    e = CueEngine()
    e.on_fog(True)
    assert e.get_effects()["blur"] == BLUR_BASE + BLUR_FOG_BOOST
    e.on_fog(False)
    assert e.get_effects()["blur"] == BLUR_BASE


# ── Toggle gating ────────────────────────────────────────────────

def test_blur_toggle_off_zeros_the_strength():
    s = _settings()
    s.set_effect("blur", False)
    fx = {"blur": 0.6, "mirror": True}
    s.apply_effect_toggles(fx)
    assert fx["blur"] == 0.0


def test_mirror_defaults_off_and_suppresses_the_capability():
    s = _settings()  # mirror defaults off
    assert s.effect_enabled("mirror") is False
    fx = {"blur": BLUR_BASE, "mirror": True}
    s.apply_effect_toggles(fx)
    assert fx["mirror"] is False


def test_disabled_blur_renders_crisp():
    s = _settings()
    s.set_effect("blur", False)
    m = LEDMapper(MAPPED_REGION)
    fx = {"blur": 0.6}
    s.apply_effect_toggles(fx)
    off = _render(m, fx)
    crisp = _render(m, {})
    assert off == crisp


# ── Operator blur-amount setting (dashboard slider) ──────────────

def test_blur_amount_defaults_to_base_and_clamps():
    s = _settings()
    assert s.blur_amount == BLUR_BASE
    s.blur_amount = 2.0
    assert s.blur_amount == 1.0
    s.blur_amount = -0.5
    assert s.blur_amount == 0.0


def test_blur_amount_persists_across_reload():
    d = tempfile.mkdtemp()
    path = str(Path(d) / "settings.json")
    s1 = BridgeSettings(path=path)
    s1.blur_amount = 0.7
    assert BridgeSettings(path=path).blur_amount == 0.7


def test_blur_amount_in_snapshot():
    s = _settings()
    s.blur_amount = 0.5
    assert s.snapshot()["blur_amount"] == 0.5


def test_set_blur_base_drives_the_emitted_strength():
    e = CueEngine()
    e.set_blur_base(0.8)
    assert e.get_effects()["blur"] == 0.8
    # Fog still stacks on top of the operator base.
    e.on_fog(True)
    assert e.get_effects()["blur"] == 0.8 + BLUR_FOG_BOOST
    # And it clamps.
    e.set_blur_base(5.0)
    e.on_fog(False)
    assert e.get_effects()["blur"] == 1.0
