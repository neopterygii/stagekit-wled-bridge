"""Full-path replay tests: datagram → parser → cue engine → mapper → DDP.

These are the only tests that exercise the whole pipeline end to end. Everything
else in the suite tests a layer; this asserts the layers still add up, driven by
recorded-shape datagram streams instead of direct method calls.

The validation matrix (VISION "Current focus", item 2):

- deterministic frames, pinned by golden sampled digests
- connection and song lifecycle transitions
- pause/resume and scene changes
- malformed, truncated and future-length packets
- dropped and repeated beat pulses, and beat-clock coasting
- WLED/DDP loss and recovery
- venue sources: MIDI-authored and YARG auto-generated

Run: python -m pytest tests/test_replay.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import replay_fixtures as fx  # noqa: E402
from config import LED_COUNT  # noqa: E402
from protocol.ddp_sender import DDP_FLAGS_PUSH, DDP_HEADER_LEN, DDPSender  # noqa: E402
from protocol.yarg_packet import CueByte, SceneIndexByte  # noqa: E402
from replay.capture import read_capture, write_capture  # noqa: E402
from replay.ddp_receiver import DDPReceiver  # noqa: E402
from replay.player import (  # noqa: E402
    FailingSink, FrameSink, ReplayBridge, replay_capture, replay_packets,
)

# Golden digests are frames of a specific strip width. A different LED_COUNT is a
# valid deployment, just not one these values describe.
pytestmark = pytest.mark.skipif(
    LED_COUNT != 120, reason=f"golden digests assume LED_COUNT=120, got {LED_COUNT}")


def _replay(name, fps=40, **kw):
    return replay_packets(fx.build(name), fps=fps, **kw)


def _dark_after_the_opening_fade(result) -> int:
    """Dark frames excluding frame 0.

    Frame 0 is legitimately black: a cue change starts a cross-fade from the
    last sent frame, and at the start of a replay that is an empty buffer. So
    "nothing went dark" means nothing after the fade-in, not nothing at all.
    """
    return sum(1 for f in result.frames[1:] if not any(f))


# ── determinism ─────────────────────────────────────────────────

def test_the_same_stream_renders_identical_frames_twice():
    """The point of the whole harness: replay is reproducible."""
    a = _replay("warm_beats")
    b = _replay("warm_beats")
    assert a.frames == b.frames
    assert a.sampled_digest() == b.sampled_digest()


def test_every_fixture_is_deterministic():
    for name in fx.FIXTURES:
        a, b = _replay(name), _replay(name)
        assert a.sampled_digest() == b.sampled_digest(), f"{name} is not deterministic"


def test_replay_does_not_read_the_wall_clock():
    """A slow machine must not render different light than a fast one.

    Two bridges at the same synthetic epoch agree exactly no matter how long the
    replay itself takes, which is only true if nothing samples the real clock.
    """
    packets = fx.build("strobe_and_blackout")
    a = replay_packets(packets, bridge=ReplayBridge(fps=40))
    import time as _t
    _t.sleep(0.05)   # real time moves; rendered light must not
    b = replay_packets(packets, bridge=ReplayBridge(fps=40))
    assert a.frames == b.frames


def test_epoch_only_shifts_channels_by_rounding():
    """Changing the epoch must not change the *show*, only the last bit.

    Cue state, motion and beat phase are all relative, so a different starting
    clock leaves them identical; what does move is floating-point resolution,
    which falls off with magnitude and lands as ±1 on some channels. Asserting
    the bound rather than exact equality documents the real property — and would
    catch a genuine absolute-time dependence creeping in, which would blow well
    past 1.
    """
    packets = fx.build("warm_beats")
    near = replay_packets(packets, bridge=ReplayBridge(fps=40, start_time=1000.0))
    far = replay_packets(packets, bridge=ReplayBridge(fps=40, start_time=9_000_000.0))

    assert near.frame_count == far.frame_count
    worst = 0
    for f1, f2 in zip(near.frames, far.frames):
        for c1, c2 in zip(f1, f2):
            worst = max(worst, abs(c1 - c2))
    assert worst <= 1, f"epoch changed a channel by {worst}, not just rounding"

    # The cue/beat state machine is bit-identical regardless of epoch.
    assert [s.cue for s in near.trace] == [s.cue for s in far.trace]
    assert [s.bpm for s in near.trace] == [s.bpm for s in far.trace]


def test_zero_epoch_is_rejected_as_unrepresentative():
    """0.0 collides with the engine's "not yet happened" sentinels.

    `_beat_at`/`_cue_change_at` use 0.0 to mean "never", so a replay starting
    there silently loses the opening cross-fade — light the live bridge, whose
    clock is system uptime, never produces. The default epoch avoids it; this
    test pins *why* so nobody helpfully changes it back to zero.
    """
    packets = fx.build("warm_beats")
    at_zero = replay_packets(packets, bridge=ReplayBridge(fps=40, start_time=0.0))
    normal = replay_packets(packets, bridge=ReplayBridge(fps=40))
    # The opening frames differ structurally, not by rounding.
    assert at_zero.frames[0] != normal.frames[0]
    assert not any(normal.frames[0]), "first frame should fade in from black"
    assert any(at_zero.frames[0]), "at epoch 0 the fade-in is skipped"


def test_strobe_frames_are_reproducible():
    """Strobe reads the injected clock; if it ever samples the wall clock
    instead, these digests start flapping."""
    a = _replay("strobe_and_blackout")
    b = _replay("strobe_and_blackout")
    assert a.frames == b.frames
    # A fast strobe must actually blank some frames, or we're asserting nothing.
    assert a.dark_frames() > 0


# ── golden digests ──────────────────────────────────────────────
#
# Regenerate deliberately, never reflexively: a changed digest means rendered
# light changed. Confirm the change is wanted (ideally on the real strip), then
# update. `python -m pytest tests/test_replay.py -k golden -v` prints the new
# values on failure.

# Seven of these moved when the cue cross-fade went from a flat 250 ms to a
# 120 ms base with per-cue overrides (2026-07-29). The three that did not —
# authored_venue, dropped_beats, warm_beats — are the fixtures that never change
# cue mid-stream, which is the expected signal. PRE_FADE_DIGESTS below is the
# table as it stood before that change, and it is what the rollback reproduces.
#
# Three had previously moved when palette strictness landed and shipped on by
# default (auto_generated_venue, song_lifecycle, star_power_run — the fixtures
# that actually drive a remapped layer).
GOLDEN_DIGESTS = {
    "authored_venue": "a2f88178909b177c6d3d05c4b14c4933d7f8636e3b0ae8befeeac5f9a480c154",
    "auto_generated_venue": "538576dbc5c32bba140a440867f299c2150ae2fa3a6dacd8e8725fef1b264687",
    "dropped_beats": "f69ebadf8228983448e94159dfd472114ad92e88d5e47b674fe5d29ebbd1f252",
    "malformed_stream": "f34b2836915ed0b2ead764626aa861d44d3860269fea93a55301e59756ca3c3e",
    "rapid_cue_changes": "d7fd746fce81cffbc0287d7f07e45046cc454c3aeab6191aa3832a8eee63d300",
    "repeated_beats": "fe551d135dccdf5a8450f85923020b6a33f7b25f19fe0872b44d30387482b86f",
    "song_lifecycle": "2bdebd56a2d10f43bb4287f9762acd99a0f9770079cdaf89e033f2a7577da3c8",
    "spotlight_cues": "a19d66440e7e1e8f96f5f0abfb2ef77fc51c7e779e6d2661adea35cdd9cf0fb4",
    "star_power_run": "ce056eedb3c03ea341af7e792cd43986889e0fcec42b38d37cc7da9b75251eb5",
    "strobe_and_blackout": "7ba49eaad60c6c9c72340969693d8a36f3a1954c88d90d19ee97091f53963ee3",
    "warm_beats": "730b8cfe9840c28aefbbdad1bbda4b3f8776ad20909a303f7b7b403facafe478",
}


@pytest.mark.parametrize("name", sorted(GOLDEN_DIGESTS))
def test_golden_frame_digest(name):
    result = _replay(name)
    assert result.sampled_digest() == GOLDEN_DIGESTS[name], (
        f"rendered light changed for {name!r}.\n"
        f"  new digest: {result.sampled_digest()}\n"
        f"  frames: {result.frame_count}, dark: {result.dark_frames()}\n"
        "If the change is intended, update GOLDEN_DIGESTS."
    )


# Digests as they stood before the cue cross-fade change of 2026-07-29 — which
# is to say, the previous contents of GOLDEN_DIGESTS above, unedited. The
# documented rollback is `cue_fade_ms = 250` plus an empty override table, and
# this asserts that combination still renders the old light exactly rather than
# approximately.
PRE_FADE_DIGESTS = {
    "authored_venue": "a2f88178909b177c6d3d05c4b14c4933d7f8636e3b0ae8befeeac5f9a480c154",
    "auto_generated_venue": "b7ad4ec42b45a9453a606ba0e9c49561e339575d997e28e4448b5eeb71b0c522",
    "dropped_beats": "f69ebadf8228983448e94159dfd472114ad92e88d5e47b674fe5d29ebbd1f252",
    "malformed_stream": "811d2c4e8c5d2b3ebb004c4dd1b6c3f8a7a3bcebba261f4de9f73fc8d9cbd526",
    # No history — this fixture arrived with the fade change. It is what the
    # rollback renders today, kept for coverage, not as a pre-change capture.
    "rapid_cue_changes": "7ff4e04a57aaedf416c99f2c15ec4554087e5b80b8d102a4450b981d23e06e9c",
    "repeated_beats": "52f90374499073a6da0eab0587b05058c775366efe1a4ad10adc15db7b002c1b",
    "song_lifecycle": "0aa0b0ba616d0c2d9f6cb40c99bf5ff2e9bad6b537860bd0b210e76e2bca75fe",
    "spotlight_cues": "49e171cfc867beba338f3b474da4113a647a477f3273a9842f9bf07433baaaa9",
    "star_power_run": "1410de93e6d6851cb352aa4203c9e6a662e3dc72c38084d29972de777ee4da59",
    "strobe_and_blackout": "7694b79c2f55378fd9158445f9b14c20c8cae091b513c94221dee46bc3c4dd13",
    "warm_beats": "730b8cfe9840c28aefbbdad1bbda4b3f8776ad20909a303f7b7b403facafe478",
}

# The pre-fade look is the whole strip, not one knob, so pin every knob at its
# historical value.
_PRE_FADE = dict(cue_fade_ms=250, cue_fade_overrides={})


@pytest.mark.parametrize("name", sorted(PRE_FADE_DIGESTS))
def test_pre_fade_settings_reproduce_the_old_look(name):
    result = _replay(name, bridge=ReplayBridge(fps=40, **_PRE_FADE))
    assert result.sampled_digest() == PRE_FADE_DIGESTS[name], (
        f"cue_fade_ms=250 with no per-cue overrides no longer reproduces the "
        f"pre-2026-07-29 look for {name!r} — the rollback path has drifted.")


# Digests as they stood before palette strictness. Strictness 0.0 is the
# operator's documented way back to the pre-palette look, so "bit-exact" is a
# claim the suite should be able to keep making rather than one taken on trust
# the day it was written.
#
# These are replayed with the *fade* knobs rolled back too (`_PRE_FADE`): the
# pre-palette look is a historical claim, and reproducing it means putting every
# knob back where it was, not just the palette one.
#
# spotlight_cues and rapid_cue_changes are the exceptions: both were added after
# strictness shipped, so their values here are not pre-change captures — they
# are what strictness 0.0 renders today. They still earn their place, because
# the rollback path has to keep lighting these cues, but do not read them as
# history.
PRE_STRICTNESS_DIGESTS = {
    "authored_venue": "a2f88178909b177c6d3d05c4b14c4933d7f8636e3b0ae8befeeac5f9a480c154",
    "auto_generated_venue": "39ddaabb8e6cf7fcc93b69dbcfd44fa44850b1982e376d1f2b47ad82650d7aa6",
    "dropped_beats": "f69ebadf8228983448e94159dfd472114ad92e88d5e47b674fe5d29ebbd1f252",
    "malformed_stream": "811d2c4e8c5d2b3ebb004c4dd1b6c3f8a7a3bcebba261f4de9f73fc8d9cbd526",
    "rapid_cue_changes": "6d758dd4c98cca4d2eaf5454eb3f5f536562dd4e36c75064b3ab150f196a49ae",
    "repeated_beats": "52f90374499073a6da0eab0587b05058c775366efe1a4ad10adc15db7b002c1b",
    "song_lifecycle": "333d3654a248709bc8386d8b99aff493d93107123c8a433d3e242a90903e0365",
    "spotlight_cues": "c7df29b2439f4e8f19016d5753275d96b506720aa5ad3872b7f810caf810493a",
    "star_power_run": "c64cf03b7273438f7f95512261355f6ce250edcebb0712646258782d3bc90424",
    "strobe_and_blackout": "7694b79c2f55378fd9158445f9b14c20c8cae091b513c94221dee46bc3c4dd13",
    "warm_beats": "730b8cfe9840c28aefbbdad1bbda4b3f8776ad20909a303f7b7b403facafe478",
}


@pytest.mark.parametrize("name", sorted(PRE_STRICTNESS_DIGESTS))
def test_zero_strictness_reproduces_the_pre_palette_look(name):
    result = _replay(name, bridge=ReplayBridge(fps=40, palette_strictness=0.0,
                                               **_PRE_FADE))
    assert result.sampled_digest() == PRE_STRICTNESS_DIGESTS[name], (
        f"palette_strictness=0.0 no longer reproduces the pre-palette look for "
        f"{name!r} — the rollback path has drifted.")


# ── lifecycle ───────────────────────────────────────────────────

def test_song_lifecycle_scene_transitions_reach_the_tracker():
    result = _replay("song_lifecycle")
    scenes = []
    for st in result.trace:
        if not scenes or scenes[-1] != st.scene:
            scenes.append(st.scene)
    assert scenes == [
        SceneIndexByte.MENU, SceneIndexByte.GAMEPLAY,
        SceneIndexByte.SCORE, SceneIndexByte.MENU,
    ]


def test_song_lifecycle_cue_order():
    result = _replay("song_lifecycle")
    assert result.cue_sequence() == [
        "MENU", "INTRO", "VERSE", "CHORUS", "BIG_ROCK_ENDING", "SCORE", "MENU",
    ]


def test_pause_and_resume_round_trip():
    result = _replay("song_lifecycle")
    paused = [st.index for st in result.trace if st.paused]
    assert paused, "fixture never reported paused"
    # Paused is a contiguous stretch in the middle, not the start or the end.
    assert paused == list(range(paused[0], paused[-1] + 1))
    assert paused[0] > 0
    assert paused[-1] < len(result.trace) - 1


def test_pause_dims_the_strip_then_resume_restores_it():
    """Pausing should visibly pull the level down, not freeze at full brightness."""
    result = _replay("song_lifecycle")
    trace = result.trace

    def mean_level(frames):
        vals = [sum(f) / len(f) for f in frames if f]
        return sum(vals) / len(vals) if vals else 0.0

    paused_idx = [st.index for st in trace if st.paused]
    lo, hi = paused_idx[0], paused_idx[-1]
    # Sample away from the edges so cross-fades aren't what we're measuring.
    during = mean_level(result.frames[lo + 8:hi - 8])
    after = mean_level(result.frames[hi + 12:hi + 40])
    assert during < after, f"paused level {during:.1f} not below resumed {after:.1f}"


def test_song_section_and_venue_size_are_surfaced():
    result = _replay("song_lifecycle")
    sections = {st.song_section for st in result.trace}
    assert {"Verse", "Chorus"} <= sections
    assert "Small" in {st.venue_size for st in result.trace}


def test_connection_state_tracks_the_stream():
    """Every datagram in the capture is counted, and the bridge reads connected
    while they're flowing.

    The *disconnect* half can't be replayed: `StatusTracker.connected` compares
    against `time.monotonic()` directly, not an injected clock, so the timeout
    only elapses in real time. Waiting it out would make this test sleep for
    CONNECTED_TIMEOUT. Left to the unit level rather than faked here.
    """
    packets = fx.build("warm_beats")
    bridge = ReplayBridge(fps=40)
    replay_packets(packets, bridge=bridge)
    assert bridge.tracker.packets_received == len(packets)
    assert bridge.tracker.connected


# ── venue sources ───────────────────────────────────────────────

def test_authored_venue_is_not_reported_as_auto_generated():
    result = _replay("authored_venue")
    assert not any(st.auto_gen for st in result.trace)
    assert result.cue_sequence() == [
        "WARM_MANUAL", "COOL_MANUAL", "DISCHORD", "STOMP", "FLARE_FAST",
    ]


def test_auto_generated_venue_sets_the_flag():
    result = _replay("auto_generated_venue")
    assert all(st.auto_gen for st in result.trace[1:])


def test_authored_and_auto_streams_render_differently():
    """Two different charts should not produce the same light."""
    a = _replay("authored_venue")
    b = _replay("auto_generated_venue")
    assert a.sampled_digest() != b.sampled_digest()


def test_event_driven_cues_do_not_advance_under_replay():
    """KNOWN LIMITATION, pinned deliberately — see BACKLOG.md.

    Time-driven patterns were migrated to the deterministic `engine.tick()`
    path, but the *event*-driven ones (`listen="keyframe"` / `"beat_any"`) still
    run as asyncio coroutines: DEFAULT, WARM_MANUAL, COOL_MANUAL, STOMP and
    DISCHORD. Replay has no event loop, so those coroutines never step and their
    zones stay dark.

    That matters more than the cue count suggests: per the venue_scan inventory,
    `next` keyframes are the most common cue in the library by roughly 10×, and
    warm_manual/dischord/stomp are all in the top eight. So the harness cannot
    yet judge the look of the library's most common cues.

    This test exists so the limitation is visible rather than lurking. When
    those patterns move onto tick(), it will fail — that is the signal to delete
    it and assert the motion instead.
    """
    result = _replay("authored_venue")
    # The keyframe-stepped stretch renders nothing.
    assert result.dark_frames() > 0
    early = result.frames[20:60]
    assert len({bytes(f) for f in early}) == 1, (
        "event-driven cues now advance under replay — the asyncio patterns were "
        "migrated to tick(). Delete this test and assert the motion instead."
    )


def test_time_driven_cues_do_advance_under_replay():
    """The counterpart: cues already on the tick() path animate fine.

    Keeps the limitation above scoped to event-driven patterns rather than
    reading as "replay can't animate anything".
    """
    result = _replay("auto_generated_venue")   # VERSE/CHORUS are time-driven
    frames = result.frames[20:80]
    assert len({bytes(f) for f in frames}) > 1
    assert _dark_after_the_opening_fade(result) == 0


# ── malformed input ─────────────────────────────────────────────

def test_malformed_packets_are_dropped_without_killing_the_stream():
    result = _replay("malformed_stream")
    # Junk was fed but not parsed: short + wrong-magic are rejected outright.
    assert result.packets_fed > result.packets_parsed
    assert result.packets_fed - result.packets_parsed == 2
    assert result.frame_count > 0


def test_a_future_datagram_version_still_renders():
    """YARG's datagram is append-only, so an unknown version parses at the
    known offsets and warns rather than going dark."""
    result = _replay("malformed_stream")
    assert result.cue_sequence()[-1] == "WARM_AUTOMATIC"
    assert _dark_after_the_opening_fade(result) == 0


def test_a_longer_than_known_packet_is_accepted():
    good = fx.build("warm_beats")[0][1]
    longer = good + b"\x00" * 64
    result = replay_packets([(0.0, longer), (0.1, longer)], fps=40)
    assert result.packets_parsed == 2


def test_a_lying_star_power_count_does_not_read_past_the_buffer():
    """The parser clamps to the pairs actually present; a bad count must not
    crash the UDP handler mid-song."""
    result = _replay("malformed_stream")
    assert result.frame_count > 0  # got here without an exception


@pytest.mark.parametrize("junk", [b"", b"\x00", b"YARG", b"\xff" * 43])
def test_undersized_datagrams_are_ignored(junk):
    result = replay_packets([(0.0, junk)], fps=40, tail=0.1)
    assert result.packets_parsed == 0
    assert result.frame_count > 0


# ── beat handling ───────────────────────────────────────────────

def test_beat_clock_coasts_through_dropped_pulses():
    """When pulses stop, motion must keep running rather than freezing —
    the free-running beat clock, seen through the whole path."""
    result = _replay("dropped_beats")
    # The gap is the middle third of the fixture (2s beats, 4s silent, 2s beats).
    start = int(2.5 * result.fps)
    end = int(5.5 * result.fps)
    during_gap = result.frames[start:end]
    assert len({bytes(f) for f in during_gap}) > 1, "motion froze without pulses"


def test_repeated_beat_pulses_do_not_render_a_strobe():
    """Every packet claiming a beat is a stuck byte, not a 90 Hz flash order."""
    result = _replay("repeated_beats")
    levels = [sum(f) / len(f) for f in result.frames if f]
    # Consecutive frames should be close together; a per-packet flash would show
    # up as a large mean swing frame to frame.
    swings = [abs(b - a) for a, b in zip(levels, levels[1:])]
    assert max(swings) < 40, f"suspicious frame-to-frame swing: {max(swings):.1f}"


def test_star_power_charge_then_overdrive_changes_the_look():
    result = _replay("star_power_run")
    charging = result.frames[10:20]
    active = [st for st in result.trace if st.sp_active]
    assert active, "fixture never engaged overdrive"
    overdrive = result.frames[active[2].index:active[2].index + 10]
    assert {bytes(f) for f in charging} != {bytes(f) for f in overdrive}


# ── DDP loss and recovery ───────────────────────────────────────

def test_frames_reach_the_sink_when_wled_is_on():
    sink = FrameSink()
    result = replay_packets(fx.build("warm_beats"), fps=40, sink=sink)
    assert sink.frames_sent == result.frame_count
    assert len(sink.frames) == result.frame_count


def test_nothing_is_sent_while_wled_is_off():
    """The render thread gates DDP on power state; a dark strip must not be fed."""
    bridge = ReplayBridge(fps=40, wled_on=False)
    result = replay_packets(fx.build("warm_beats"), bridge=bridge)
    assert result.frame_count > 0
    assert bridge.sink.frames_sent == 0


def test_ddp_loss_then_recovery():
    """WLED drops off mid-song: frames are lost, rendering never stops, and
    delivery resumes on its own without restarting the bridge."""
    sender = DDPSender("127.0.0.1", 4048)

    class _FlakySocket:
        """Fails sends 20..59, like a controller that vanished for a second."""

        def __init__(self):
            self.attempts = 0

        def sendto(self, packet, addr):
            self.attempts += 1
            if 20 <= self.attempts < 60:
                raise OSError("network unreachable")

        def close(self):
            pass

    sender._sock = _FlakySocket()
    result = replay_packets(fx.build("warm_beats"), fps=40, sink=sender)

    stats = sender.stats()
    assert stats["send_errors"] == 40          # the outage was counted
    assert stats["frames_sent"] == result.frame_count
    assert result.frame_count > 60             # kept rendering right through it
    # Recovery: the last frame went out, so sending resumed after the outage.
    assert sender._sock.attempts == result.frame_count


def test_a_raising_sink_is_the_senders_job_to_absorb():
    """Pins where the error handling lives.

    `DDPSender` wraps `sendto` in try/except OSError; a bare sink does not. If
    this ever stops raising, error swallowing has moved somewhere unexpected.
    """
    sink = FailingSink(fail_from=5, fail_until=10)
    with pytest.raises(OSError):
        replay_packets(fx.build("warm_beats"), fps=40, sink=sink)
    assert sink.send_errors == 1   # raised on the first failing frame


# ── headless DDP receiver ───────────────────────────────────────

def test_ddp_receiver_reassembles_a_frame():
    with DDPReceiver() as rx:
        sender = DDPSender("127.0.0.1", rx.port)
        pixels = bytes(range(256)) * 2 + bytes(96)   # 608 bytes
        sender.send_pixels(pixels)
        frame = rx.next_frame(timeout=2.0)
        sender.close()

    assert frame is not None
    assert frame.pixels == pixels
    assert frame.chunks == 1


def test_ddp_sequence_numbers_cycle_1_to_15():
    """Per the DDP spec, 0 means "no sequence"; the counter must skip it."""
    with DDPReceiver() as rx:
        sender = DDPSender("127.0.0.1", rx.port)
        for _ in range(20):
            sender.send_pixels(bytes(30))
        assert rx.wait_for_frames(20, timeout=3.0)
        seqs = [f.seq for f in rx.frames]
        sender.close()

    assert seqs[:16] == list(range(1, 16)) + [1]
    assert 0 not in seqs


def test_a_large_frame_splits_and_only_the_last_chunk_pushes():
    """>480 LEDs must span packets, sharing one sequence, PUSH on the last."""
    with DDPReceiver() as rx:
        sender = DDPSender("127.0.0.1", rx.port)
        pixels = bytes(600 * 3)   # 1800 channels > 1440 per packet
        sender.send_pixels(pixels)
        frame = rx.next_frame(timeout=2.0)
        sender.close()

    assert frame is not None
    assert frame.chunks == 2
    assert frame.offsets == [0, 1440]
    assert frame.pixels == pixels


def test_receiver_counts_malformed_packets():
    import socket

    with DDPReceiver() as rx:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(b"\x01\x02\x03", (rx.host, rx.port))          # too short
        sock.sendto(b"\x00" * (DDP_HEADER_LEN + 3), (rx.host, rx.port))  # no version bit
        sender = DDPSender("127.0.0.1", rx.port)
        sender.send_pixels(bytes(30))
        assert rx.wait_for_frames(1, timeout=2.0)
        sock.close()
        sender.close()

    assert rx.bad_packets == 2
    assert rx.frame_count == 1


def test_replay_through_the_real_sender_and_receiver():
    """The one test that runs the entire path over a real socket.

    Recorded datagram → parser → cue engine → mapper → DDP → reassembled frame,
    with nothing stubbed but the WLED hardware itself.
    """
    with DDPReceiver() as rx:
        sender = DDPSender("127.0.0.1", rx.port)
        result = replay_packets(fx.build("warm_beats"), fps=40, sink=sender)
        assert rx.wait_for_frames(result.frame_count, timeout=5.0)
        frames = rx.frames
        sender.close()

    assert len(frames) >= result.frame_count
    assert all(len(f.pixels) == LED_COUNT * 3 for f in frames)
    # The bytes that crossed the socket are the bytes the mapper rendered.
    assert frames[-1].pixels == result.frames[len(frames) - 1]


# ── capture files end to end ────────────────────────────────────

def test_capture_file_replays_the_same_as_its_packets(tmp_path):
    """Round-tripping through the file format must not change the light."""
    packets = fx.build("authored_venue")
    path = write_capture(tmp_path / "c.jsonl", packets, note="test")

    from_memory = replay_packets(packets, fps=40)
    from_disk = replay_capture(path, fps=40)
    assert from_disk.frames == from_memory.frames
    assert from_disk.header is not None
    assert from_disk.header.note == "test"


def test_committed_golden_capture_replays():
    golden = Path(__file__).parent / "fixtures" / "format_v1_sample.jsonl"
    result = replay_capture(golden, fps=40, tail=0.2)
    assert result.frame_count > 0
    assert result.cue_sequence()[-1] == "WARM_AUTOMATIC"


def test_tail_keeps_rendering_after_the_last_packet():
    short = _replay("warm_beats", tail=0.0)
    long = _replay("warm_beats", tail=2.0)
    assert long.frame_count > short.frame_count
    assert long.frame_count - short.frame_count == pytest.approx(80, abs=2)


def test_fps_changes_the_frame_count_not_the_duration():
    at40 = _replay("warm_beats", fps=40)
    at60 = _replay("warm_beats", fps=60)
    assert at60.frame_count > at40.frame_count
    assert at60.duration == pytest.approx(at40.duration, abs=0.1)


def test_frame_at_indexes_by_seconds():
    result = _replay("warm_beats")
    assert result.frame_at(0.0) == result.frames[0]
    assert result.frame_at(1.0) == result.frames[40]
    # Past the end clamps rather than raising.
    assert result.frame_at(9999.0) == result.frames[-1]


def test_summary_is_json_safe():
    import json

    json.dumps(_replay("warm_beats").summary())


def test_sampled_digest_notices_a_single_changed_pixel():
    """A digest that ignored small changes would give false confidence."""
    result = _replay("warm_beats")
    original = result.sampled_digest()
    idx = result.sample_indices()[3]
    tampered = bytearray(result.frames[idx])
    tampered[0] ^= 0x01
    result.frames[idx] = bytes(tampered)
    assert result.sampled_digest() != original
