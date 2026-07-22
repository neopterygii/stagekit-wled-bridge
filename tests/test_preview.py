"""Tests for the live strip / per-layer dashboard preview (VISION Phase 7).

The status dashboard shows the composed frame and each compositor layer's
effective contribution, downsampled to PREVIEW_CELLS. These tests pin the
downsample maths, the per-layer capture (shape, wash-always-on, accent layers
lighting up only when active, blackout-safe), the idle cost gate (no capture
unless preview=True), and the tracker snapshot carrying beat telemetry + the
preview blob.

Run: python -m pytest tests/test_preview.py -v
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import CueEngine  # noqa: E402
from effects.mapper import LEDMapper, MAPPED_REGION, PREVIEW_CELLS  # noqa: E402
from protocol.yarg_packet import CueByte, BeatByte  # noqa: E402
from status_server import StatusTracker  # noqa: E402


LAYER_NAMES = ["wash", "motion", "sparkle", "flash", "bonus", "note", "vocal"]


def _render(mapper, engine, preview=True, frames=1, bpm=120.0):
    engine.bpm = bpm
    now = time.monotonic()
    out = None
    for k in range(frames):
        engine.tick(now + k * 0.05)
        fx = engine.get_effects()
        fx["fps"] = 40
        out = mapper.render(engine.zones, effects=fx,
                            zone_cell_levels=engine.zone_cell_levels,
                            motion_sources=engine.motion_sources,
                            preview=preview)
    return out


# ── downsample ──────────────────────────────────────────────────

def test_downsample_uniform_preserves_colour():
    n = MAPPED_REGION
    src = bytearray([10, 20, 30] * n)
    out = LEDMapper.downsample_rgb(src, n, PREVIEW_CELLS)
    assert len(out) == PREVIEW_CELLS * 3
    # A uniform source averages to itself in every cell.
    assert set(out[0::3]) == {10}
    assert set(out[1::3]) == {20}
    assert set(out[2::3]) == {30}


def test_downsample_averages_within_a_cell():
    # Two source pixels per cell: one 0, one 100 → average 50.
    cells = 4
    n = 8
    src = bytearray(n * 3)
    for i in range(n):
        if i % 2 == 1:
            src[i * 3] = 100
    out = [0] * (cells * 3)
    LEDMapper._downsample(src, n, cells, out)
    assert out[0::3] == [50, 50, 50, 50]


def test_downsample_handles_more_cells_than_pixels():
    # cells > n_src: each cell still maps to at least one pixel, no div-by-zero.
    src = bytearray([7, 7, 7] * 4)
    out = [0] * (10 * 3)
    LEDMapper._downsample(src, 4, 10, out)
    assert all(v == 7 for v in out)


# ── per-layer capture ───────────────────────────────────────────

def test_layer_preview_shape_and_order():
    m = LEDMapper()
    _render(m, CueEngine())
    lp = m.layer_preview()
    assert lp["cells"] == PREVIEW_CELLS
    assert [L["name"] for L in lp["layers"]] == LAYER_NAMES
    for L in lp["layers"]:
        assert isinstance(L["active"], bool)
        # Active layers carry cells*3 ints; idle layers carry an empty list.
        assert len(L["rgb"]) == (PREVIEW_CELLS * 3 if L["active"] else 0)


def test_wash_always_active_and_lit_for_a_wash_cue():
    m = LEDMapper()
    e = CueEngine()
    e.on_cue(CueByte.VERSE)
    _render(m, e)
    wash = next(L for L in m.layer_preview()["layers"] if L["name"] == "wash")
    assert wash["active"] is True
    assert any(v > 0 for v in wash["rgb"])


def test_motion_layer_lights_up_for_a_scanner_cue():
    m = LEDMapper()
    e = CueEngine()
    e.on_cue(CueByte.SWEEP)
    for k in range(4):
        e.on_beat(BeatByte.MEASURE if k == 0 else BeatByte.STRONG)
    _render(m, e, frames=4, bpm=140.0)
    active = {L["name"] for L in m.layer_preview()["layers"] if L["active"]}
    assert "motion" in active


def test_no_capture_without_preview_flag():
    # With preview=False the capture must not run — layer_preview keeps its
    # default (all-idle) state, proving an unwatched dashboard costs nothing.
    m = LEDMapper()
    e = CueEngine()
    e.on_cue(CueByte.VERSE)
    _render(m, e, preview=False)
    assert all(not L["active"] for L in m.layer_preview()["layers"])


def test_preview_values_bounded_and_blackout_safe():
    m = LEDMapper()
    e = CueEngine()
    e.on_cue(CueByte.NO_CUE)  # blackout
    _render(m, e)
    for L in m.layer_preview()["layers"]:
        for v in L["rgb"]:
            assert 0 <= v <= 255
    # A blackout wash carries no light.
    wash = next(L for L in m.layer_preview()["layers"] if L["name"] == "wash")
    assert not any(wash["rgb"])


# ── tracker snapshot ────────────────────────────────────────────

def test_snapshot_carries_beat_telemetry():
    t = StatusTracker()
    t.on_render([0, 0, 0, 0], 0, 128.0, beat_phase=0.42, bar_beat=2)
    snap = t.snapshot()
    assert snap["beat_phase"] == 0.42
    assert snap["bar_beat"] == 2


def test_has_subscribers_gate():
    t = StatusTracker()
    assert t.has_subscribers is False
    q = t.subscribe()
    assert t.has_subscribers is True
    t.unsubscribe(q)
    assert t.has_subscribers is False


def test_snapshot_includes_preview_from_render_thread():
    t = StatusTracker()

    class FakeRT:
        def render_stats(self):
            return {"fps": 40}

        def preview_snapshot(self):
            return {"cells": PREVIEW_CELLS, "strip": [1, 2, 3], "layers": []}

    t.render_thread = FakeRT()
    snap = t.snapshot()
    assert snap["preview"]["cells"] == PREVIEW_CELLS
    assert snap["preview"]["strip"] == [1, 2, 3]


def test_snapshot_omits_preview_when_none():
    t = StatusTracker()

    class FakeRT:
        def render_stats(self):
            return {"fps": 40}

        def preview_snapshot(self):
            return None

    t.render_thread = FakeRT()
    assert "preview" not in t.snapshot()
