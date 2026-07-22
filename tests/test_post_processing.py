"""Tests for post-processing colour grades (VISION Phase 4).

YARG's venue post-processing byte (offset 35) is a film grade. The bridge
applies only the *colour* grades as a global palette modifier on lit pixels;
camera-only grades pass through untouched. These tests pin the enum offset, the
pass-through set, and the behaviour of representative grades (B&W, sepia,
invert, channel tints) — plus the blackout-safe / bounded invariants.

Run: python -m pytest tests/test_post_processing.py -v
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from effects.cue_engine import CueEngine  # noqa: E402
from effects.mapper import LEDMapper, MAPPED_REGION, POST_GRADES  # noqa: E402
from protocol.yarg_packet import (  # noqa: E402
    PostProcessing, parse_packet, PACKET_HEADER,
)

_COLORS = {"red": (255, 0, 0), "green": (0, 255, 0),
           "blue": (0, 0, 255), "yellow": (255, 255, 0)}


def _render(post, zones=(0xFF, 0xFF, 0xFF, 0xFF), colors=None):
    m = LEDMapper(MAPPED_REGION)
    return m.render(list(zones), zone_colors=colors or _COLORS,
                    effects={"post_processing": post}, brightness=1.0)


def _px0(px):
    return px[0], px[1], px[2]


# ── Parsing / enum offset ────────────────────────────────────────

def test_post_processing_parsed_at_offset_35():
    buf = bytearray(44)
    struct.pack_into("<I", buf, 0, PACKET_HEADER)
    buf[4] = 4                              # datagram version
    buf[35] = PostProcessing.SEPIA_TONE
    pkt = parse_packet(bytes(buf))
    assert pkt is not None
    assert pkt.post_processing == PostProcessing.SEPIA_TONE


def test_engine_passes_post_processing_through():
    eng = CueEngine()
    eng.on_post_processing(PostProcessing.DESATURATED_BLUE)
    assert eng.get_effects()["post_processing"] == PostProcessing.DESATURATED_BLUE


# ── Pass-through (non-colour) grades ─────────────────────────────

def test_default_is_noop():
    # Use a mid grey wash so a desaturate/tint would be detectable.
    grey = {"red": (200, 120, 60), "green": (200, 120, 60),
            "blue": (200, 120, 60), "yellow": (200, 120, 60)}
    assert _render(PostProcessing.DEFAULT, colors=grey) == \
        _render(999, colors=grey)  # 999 not in table → also no-op


def test_camera_only_grades_pass_through():
    grey = {"red": (200, 120, 60), "green": (200, 120, 60),
            "blue": (200, 120, 60), "yellow": (200, 120, 60)}
    base = _render(PostProcessing.DEFAULT, colors=grey)
    for grade in (PostProcessing.BLOOM, PostProcessing.BRIGHT,
                  PostProcessing.POSTERIZE, PostProcessing.MIRROR,
                  PostProcessing.GRAINY_FILM, PostProcessing.SCANLINES,
                  PostProcessing.TRAILS, PostProcessing.TRAILS_LONG):
        assert grade not in POST_GRADES
        assert _render(grade, colors=grey) == base


# ── Colour grades ────────────────────────────────────────────────

def test_black_and_white_desaturates():
    # Pure red wash → grey (all channels equal to its luma).
    px = _render(PostProcessing.BLACK_AND_WHITE, colors={
        "red": (255, 0, 0), "green": (255, 0, 0),
        "blue": (255, 0, 0), "yellow": (255, 0, 0)})
    r, g, b = _px0(px)
    assert r == g == b
    assert 0 < r < 255            # luma of pure red, not black/white


def test_photo_negative_inverts():
    # Pure red (255,0,0) → cyan (0,255,255).
    px = _render(PostProcessing.PHOTO_NEGATIVE, colors={
        "red": (255, 0, 0), "green": (255, 0, 0),
        "blue": (255, 0, 0), "yellow": (255, 0, 0)})
    assert _px0(px) == (0, 255, 255)


def test_sepia_is_warm_monochrome():
    # A blue wash graded to sepia: red channel should exceed blue (warm cast)
    # and the hue collapses to a single tone scaled per channel.
    px = _render(PostProcessing.SEPIA_TONE, colors={
        "red": (0, 0, 255), "green": (0, 0, 255),
        "blue": (0, 0, 255), "yellow": (0, 0, 255)})
    r, g, b = _px0(px)
    assert r > b                  # warm > cool


def test_contrast_blue_favours_blue():
    # Neutral grey wash: Contrast_Blue lifts blue above red/green.
    px = _render(PostProcessing.CONTRAST_BLUE, colors={
        "red": (150, 150, 150), "green": (150, 150, 150),
        "blue": (150, 150, 150), "yellow": (150, 150, 150)})
    r, g, b = _px0(px)
    assert b > r and b > g


def test_grade_never_lights_blackout():
    # Dark strip + an inverting grade must stay dark (lit-pixels-only).
    px = _render(PostProcessing.PHOTO_NEGATIVE, zones=(0, 0, 0, 0))
    assert max(px) == 0


def test_grade_stays_bounded():
    for grade in POST_GRADES:
        px = _render(grade)   # full white wash
        assert max(px) <= 255
