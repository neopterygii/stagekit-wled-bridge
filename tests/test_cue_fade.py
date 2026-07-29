"""Tests for the per-cue cue cross-fade.

The render thread blends each new cue against the last frame it sent, which
removes the one-frame black blink between cues but costs latency: measured on
the rig, a cue change took 145-164 ms through the bridge against ~23 ms for the
whole network and device path, and the 250 ms fade was ~120 ms of that. It is
the largest latency term in the pipeline and no hardware change touches it.

So the duration is now an operator setting (`cue_fade_ms`, default 120) with a
per-cue override table, because YARG already distinguishes BLACKOUT_FAST from
BLACKOUT_SLOW and the bridge was fading both identically.

These pin the parts that are easy to break silently: which duration a given cue
gets, that a zero-duration cue produces no blended frame at all, that the
duration is latched at cue-change time rather than re-read every frame, and
that the two strobe interactions still hold.

`test_rapid_cue_changes_still_converge` is the measurement that answered
BACKLOG.md's open question about fades compounding.

Run: python -m pytest tests/test_cue_fade.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import replay_fixtures as fx  # noqa: E402
from protocol.yarg_packet import CueByte, StrobeSpeed  # noqa: E402
from replay.player import ReplayBridge, replay_packets  # noqa: E402
from settings import CUE_FADE_MS_OVERRIDES, BridgeSettings  # noqa: E402
from test_sender import build_packet  # noqa: E402

# The same unwritable path replay uses, so these never touch a real settings
# file and never inherit the operator's saved values.
_NO_FILE = "/proc/cue-fade-tests-have-no-settings/settings.json"


def _settings(**kw) -> BridgeSettings:
    return BridgeSettings(path=_NO_FILE, warn_unwritable=False, **kw)


def _frames(name, **kw):
    """Replay a fixture and return the sent frames."""
    return replay_packets(fx.build(name), bridge=ReplayBridge(fps=40, **kw)).frames


def _mean_diff(a: bytes, b: bytes) -> float:
    """Mean absolute per-channel difference between two frames."""
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


# ── which duration a cue gets ───────────────────────────────────

def test_unlisted_cues_use_the_operator_base():
    s = _settings()
    s.cue_fade_ms = 120
    assert s.fade_seconds_for_cue(CueByte.WARM_AUTOMATIC) == 0.120
    s.cue_fade_ms = 40
    assert s.fade_seconds_for_cue(CueByte.WARM_AUTOMATIC) == 0.040


def test_fast_cues_snap_regardless_of_the_base():
    """The cue byte says "fast"; a fade would contradict the game."""
    s = _settings()
    for base in (0, 120, 250, 1000):
        s.cue_fade_ms = base
        for cue in (CueByte.BLACKOUT_FAST, CueByte.FLARE_FAST, CueByte.FRENZY,
                    CueByte.STROBE_FASTEST, CueByte.STROBE_FAST,
                    CueByte.STROBE_MEDIUM, CueByte.STROBE_SLOW):
            assert s.fade_seconds_for_cue(cue) == 0.0


def test_slow_cues_keep_a_long_fade_even_when_the_base_is_short():
    s = _settings()
    s.cue_fade_ms = 0
    assert s.fade_seconds_for_cue(CueByte.BLACKOUT_SLOW) == 0.250
    assert s.fade_seconds_for_cue(CueByte.FLARE_SLOW) == 0.250


def test_strobe_off_and_the_big_moments_use_the_base():
    """Deliberately not overridden: STROBE_OFF is a *return* to a lit look."""
    s = _settings()
    s.cue_fade_ms = 120
    for cue in (CueByte.STROBE_OFF, CueByte.BIG_ROCK_ENDING,
                CueByte.MENU, CueByte.SCORE, CueByte.NO_CUE):
        assert s.fade_seconds_for_cue(cue) == 0.120


def test_an_unknown_cue_byte_falls_back_to_the_base():
    """A future YARG cue must not crash the render thread."""
    s = _settings()
    s.cue_fade_ms = 120
    assert s.fade_seconds_for_cue(200) == 0.120
    assert s.fade_seconds_for_cue(None) == 0.120


def test_overrides_can_be_pinned_off_for_the_rollback_path():
    s = _settings(cue_fade_overrides={})
    s.cue_fade_ms = 250
    for cue in (CueByte.BLACKOUT_FAST, CueByte.BLACKOUT_SLOW,
                CueByte.WARM_AUTOMATIC):
        assert s.fade_seconds_for_cue(cue) == 0.250


def test_the_base_is_clamped():
    s = _settings()
    s.cue_fade_ms = -50
    assert s.cue_fade_ms == 0
    s.cue_fade_ms = 99999
    assert s.cue_fade_ms == 1000


def test_module_table_is_not_shared_between_instances():
    """A pinned instance must not mutate the production table."""
    before = dict(CUE_FADE_MS_OVERRIDES)
    s = _settings(cue_fade_overrides={})
    s._cue_fade_overrides[CueByte.CHORUS] = 7
    assert CUE_FADE_MS_OVERRIDES == before
    assert _settings().fade_seconds_for_cue(CueByte.CHORUS) != 0.007


# ── render-thread behaviour ─────────────────────────────────────

def _step(bridge, cue, seconds, strobe=StrobeSpeed.OFF):
    """Feed one cue for `seconds` and return the frames rendered."""
    out = []
    interval = 1.0 / bridge.fps
    end = bridge.now + seconds
    bridge.feed(build_packet(cue=cue, bpm=120.0, strobe=strobe))
    while bridge.now < end:
        out.append(bytes(bridge.render_frame()))
        bridge.advance_to(bridge.now + interval)
    return out


def test_a_faded_cue_produces_intermediate_frames():
    """The whole point of the fade: no hard step between cues."""
    b = ReplayBridge(fps=60, cue_fade_ms=120)
    _step(b, CueByte.WARM_AUTOMATIC, 0.5)
    frames = _step(b, CueByte.COOL_AUTOMATIC, 0.5)
    settled = frames[-1]
    # Early frames sit between the old look and the new one, so they differ
    # from where the cue ends up.
    assert _mean_diff(frames[0], settled) > 1.0
    assert _mean_diff(frames[-1], settled) == 0.0


def test_a_zero_duration_cue_is_not_blended_at_all():
    """A snapping cue must skip the blend, not run a zero-length one."""
    b = ReplayBridge(fps=60, cue_fade_ms=120)
    _step(b, CueByte.WARM_AUTOMATIC, 0.5)
    frames = _step(b, CueByte.BLACKOUT_FAST, 0.3)
    # BLACKOUT_FAST is dark; with no fade the very first frame is already there.
    assert max(frames[0]) <= max(frames[-1])
    assert _mean_diff(frames[0], frames[-1]) < 1.0


def test_zero_duration_does_not_divide_by_zero():
    """The guard is `_fade_until = 0.0`, not a division. Cheap to lose."""
    b = ReplayBridge(fps=60, cue_fade_ms=0)
    _step(b, CueByte.WARM_AUTOMATIC, 0.2)
    _step(b, CueByte.COOL_AUTOMATIC, 0.2)   # base 0 → no fade
    _step(b, CueByte.BLACKOUT_FAST, 0.2)    # override 0 → no fade
    assert b.render.render_frame(b.now, b.fps) is not None


def test_the_duration_is_latched_at_cue_change():
    """Dragging the dashboard slider mid-fade must not rescale a running fade.

    Both runs change cue identically; the second moves the setting a frame
    later. If the render thread re-read the setting each frame, the fade in
    progress would jump and the frames would diverge.
    """
    def run(change_setting):
        b = ReplayBridge(fps=60, cue_fade_ms=120)
        _step(b, CueByte.WARM_AUTOMATIC, 0.3)
        from test_sender import build_packet
        b.feed(build_packet(cue=CueByte.COOL_AUTOMATIC, bpm=120.0))
        out = []
        for i in range(8):
            out.append(bytes(b.render_frame()))
            if change_setting and i == 1:
                b.settings.cue_fade_ms = 20
            b.advance_to(b.now + 1.0 / 60)
        return out

    assert run(False) == run(True)


def test_strobe_blackout_does_not_become_the_fade_source():
    """A cue change mid-strobe fades from the last *visible* frame.

    Strobe rides on its own byte, not the cue byte, and blanks the frame to the
    pre-allocated `_black`. `_last_sent` must skip those, or a cue change
    landing on a blank frame would fade up from darkness instead of from the
    stage look that was actually on the strip.
    """
    b = ReplayBridge(fps=60, cue_fade_ms=120)
    frames = _step(b, CueByte.WARM_AUTOMATIC, 0.5, strobe=StrobeSpeed.FAST)
    # The strobe really is blanking frames, or this proves nothing.
    assert any(max(f) == 0 for f in frames)
    assert max(b.render._last_sent) > 0


# ── the compounding question (BACKLOG.md) ───────────────────────

def test_rapid_cue_changes_still_converge():
    """Do fades compound into a permanent smear when cues change fast?

    Each new fade starts from the last sent frame, which during a fade is
    itself a blend — so in principle the strip might never reach any cue's real
    colour. Replaying the same stream with the fade disabled gives the
    un-blended target sequence to compare against.

    Answer: it converges. The fixture changes cue every 80 ms against a 120 ms
    fade, so frames stay measurably behind the target throughout the rapid
    section — but the moment the changes stop, the strip lands exactly on the
    target rather than settling somewhere between the cues it passed through.
    A smear would leave a residual difference here forever.
    """
    faded = _frames("rapid_cue_changes", cue_fade_ms=120)
    snapped = _frames("rapid_cue_changes", cue_fade_ms=0, cue_fade_overrides={})
    assert len(faded) == len(snapped)

    # The trailing hold is 1.5 s; the last frames are well past any fade.
    assert _mean_diff(faded[-1], snapped[-1]) == 0.0

    # And during the rapid section the fade is doing something — otherwise this
    # fixture would prove nothing about compounding at all.
    rapid = [_mean_diff(f, s) for f, s in zip(faded[:150], snapped[:150])]
    assert max(rapid) > 1.0


def test_rapid_changes_reach_each_cue_when_the_fade_is_short_enough():
    """With the fade shorter than the dwell, cues land fully.

    The fixture dwells 80 ms per cue. At a 40 ms fade each one finishes inside
    its own dwell, so the faded run matches the snapped run on the frames just
    before each change — 43 of the 90 sampled, measured. This is the property
    that fails at the default; see the test below.
    """
    faded = _frames("rapid_cue_changes", cue_fade_ms=40)
    snapped = _frames("rapid_cue_changes", cue_fade_ms=0, cue_fade_overrides={})
    # Sample the back half of the rapid section, where any accumulated smear
    # would have built up.
    matched = sum(1 for f, s in zip(faded[60:150], snapped[60:150])
                  if _mean_diff(f, s) == 0.0)
    assert matched > 0, "no frame ever reached its cue's real colour"


def test_fades_do_compound_when_the_dwell_is_shorter_than_the_fade():
    """Pins the measured limitation, deliberately — delete it when it's fixed.

    At the 120 ms default against this fixture's 80 ms dwell, the strip never
    once shows any cue's actual colour during the rapid section: every frame is
    a blend of cues it is passing through. Measured mean per-channel deviation
    from the un-faded target is 55.7 at 120 ms and 66.4 at the old 250 ms, so
    shortening the fade reduces the smear substantially without removing it.

    **What this does not establish is whether real songs change cue this fast.**
    The fixture is synthetic and was built to force the condition. Answering the
    exposure needs a capture of a genuinely dense song, which is why the
    restart behaviour was left alone rather than changed on this evidence.

    Like tests/test_replay.py::test_event_driven_cues_do_not_advance_under_replay,
    this asserts the limitation so that fixing it fails here and prompts an
    update rather than passing silently.
    """
    snapped = _frames("rapid_cue_changes", cue_fade_ms=0, cue_fade_overrides={})
    for base in (120, 250):
        faded = _frames("rapid_cue_changes", cue_fade_ms=base)
        landed = sum(1 for f, s in zip(faded[60:150], snapped[60:150])
                     if _mean_diff(f, s) == 0.0)
        assert landed == 0, (
            f"cue_fade_ms={base} now lands on target during rapid changes — "
            "the compounding limitation is fixed; update this test and BACKLOG.md")
