"""Tests for the runtime effect-toggle framework (VISION dashboard phase).

Each Phase 4 reactivity layer (note accents, vocal ribbon, performer bias,
post-processing grade) can be switched off at runtime from the dashboard. The
framework is registry-driven (settings.EFFECT_TOGGLES): the render loop calls
apply_effect_toggles() to suppress a disabled layer's signal before the mapper
sees it, so a disabled layer is truly inert. These tests pin the registry
contract, the suppression, persistence, and the dashboard snapshot shape.

Run: python -m pytest tests/test_effect_toggles.py -v
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from settings import BridgeSettings, EFFECT_TOGGLES  # noqa: E402
from effects.mapper import LEDMapper, MAPPED_REGION  # noqa: E402


def _settings():
    # Isolated, writable settings file per instance.
    d = tempfile.mkdtemp()
    return BridgeSettings(path=str(Path(d) / "settings.json"))


# ── Registry / defaults ──────────────────────────────────────────

def test_toggles_default_to_their_registry_default():
    # Each toggle defaults on unless its row opts out with default:False
    # (mirror — an opt-in symmetric look the suppress-only gate reads as "off").
    s = _settings()
    assert s.effects == {tid: meta.get("default", True)
                         for tid, meta in EFFECT_TOGGLES.items()}
    assert s.effect_enabled("blur") is True
    assert s.effect_enabled("mirror") is False


def test_registry_covers_the_reactive_layers():
    assert set(EFFECT_TOGGLES) == {
        "note_accents", "vocal_ribbon", "performer_bias", "post_processing",
        "camera_cut", "venue_patterns", "blur", "mirror"}
    # Every entry carries the UI + gating metadata the framework relies on.
    for meta in EFFECT_TOGGLES.values():
        assert {"label", "description", "key", "off"} <= set(meta)


# ── Suppression (the core of the framework) ──────────────────────

def test_enabled_effects_pass_through_untouched():
    s = _settings()
    # Enable every toggle (mirror defaults off) so only pass-through is tested.
    for tid in EFFECT_TOGGLES:
        s.set_effect(tid, True)
    fx = {"note_accents": [0.5, 0.0, 0.0, 0.0], "vocal_notes": [60.0, 0, 0, 0],
          "performers": 8, "post_processing": 5, "bpm": 120.0}
    before = dict(fx)
    assert s.apply_effect_toggles(fx) == before


def test_disabled_toggle_suppresses_its_key():
    s = _settings()
    s.set_effect("note_accents", False)
    s.set_effect("performer_bias", False)
    fx = {"note_accents": [1.0, 1.0, 1.0, 1.0], "vocal_notes": [60.0, 0, 0, 0],
          "performers": 8, "post_processing": 5}
    s.apply_effect_toggles(fx)
    assert fx["note_accents"] is None          # note_accents off → None
    assert fx["performers"] == 0               # performer_bias off → 0
    # Untouched toggles keep their live signal.
    assert fx["vocal_notes"] == [60.0, 0, 0, 0]
    assert fx["post_processing"] == 5


def test_each_toggle_maps_to_the_mapper_off_value():
    # off values must equal what the mapper treats as "inactive".
    s = _settings()
    for tid, meta in EFFECT_TOGGLES.items():
        if meta["key"] is None:
            continue  # engine-stage toggle, not a mapper signal
        s2 = _settings()
        s2.set_effect(tid, False)
        fx = {meta["key"]: "LIVE"}
        s2.apply_effect_toggles(fx)
        assert fx[meta["key"]] == meta["off"]


def test_unknown_effect_id_rejected():
    s = _settings()
    assert s.set_effect("does_not_exist", False) is False
    # A stray key in the effects dict is ignored by apply (no crash).
    assert s.effects == {tid: meta.get("default", True)
                         for tid, meta in EFFECT_TOGGLES.items()}


# ── Persistence + snapshot ───────────────────────────────────────

def test_toggle_state_persists_across_reload():
    d = tempfile.mkdtemp()
    path = str(Path(d) / "settings.json")
    s1 = BridgeSettings(path=path)
    s1.set_effect("vocal_ribbon", False)
    s2 = BridgeSettings(path=path)
    assert s2.effect_enabled("vocal_ribbon") is False
    assert s2.effect_enabled("note_accents") is True


def test_snapshot_exposes_states_and_registry():
    s = _settings()
    s.set_effect("post_processing", False)
    snap = s.snapshot()
    assert snap["effects"]["post_processing"] is False
    assert snap["effects"]["note_accents"] is True
    # Registry (label/description) for the dashboard to render switches from.
    for tid, meta in snap["effect_toggles"].items():
        assert tid in EFFECT_TOGGLES
        assert set(meta) == {"label", "description"}


# ── End-to-end: a disabled layer is inert at the mapper ──────────

def _lum(px):
    n = len(px) // 3
    return [max(px[i * 3], px[i * 3 + 1], px[i * 3 + 2]) for i in range(n)]


def test_disabled_note_accents_render_identical_to_no_notes():
    s = _settings()
    s.set_effect("note_accents", False)
    m = LEDMapper(MAPPED_REGION)
    colors = {"red": (255, 0, 0), "green": (0, 255, 0),
              "blue": (0, 0, 255), "yellow": (255, 255, 0)}
    lit = [0xFF, 0, 0, 0]

    fx_hit = {"note_accents": [1.0, 1.0, 1.0, 1.0]}
    s.apply_effect_toggles(fx_hit)
    with_toggle_off = m.render(lit, zone_colors=colors, effects=fx_hit, brightness=1.0)

    baseline = m.render(lit, zone_colors=colors, effects={}, brightness=1.0)
    assert with_toggle_off == baseline
