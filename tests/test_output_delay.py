"""The output delay ("alignment trim") and the calibration flash.

`RenderThread.render_frame` is driven directly with an injected clock and a
sender that records what it is given. The mapper is a stand-in that returns a
frame carrying its own frame number, so each frame sent can be traced to the
moment it was rendered.
"""

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LED_COUNT  # noqa: E402
from effects.cue_engine import CueEngine  # noqa: E402
from main import (  # noqa: E402
    CALIBRATION_FLASH_S, CALIBRATION_MAX_S, DELAY_RING, DeviceMode, RenderThread,
)
from protocol.yarg_packet import BeatByte  # noqa: E402
from replay.player import ReplayBridge  # noqa: E402
from settings import OUTPUT_DELAY_MS_MAX, VALID_FPS, BridgeSettings  # noqa: E402
from status_server import StatusServer, StatusTracker  # noqa: E402

_NO_SETTINGS_FILE = "/proc/output-delay-tests-have-no-settings/settings.json"
FRAME_BYTES = LED_COUNT * 3
BLACK = bytes(FRAME_BYTES)
T0 = 1000.0


def tag(n: int) -> bytes:
    """The frame the stand-in mapper returns for its n-th render (never black)."""
    return bytes([n % 250 + 1, n // 250 % 250 + 1, 7]) * LED_COUNT


def untag(frame: bytes) -> int | None:
    """Which render a sent frame came from; None for black."""
    if frame == BLACK:
        return None
    n = (frame[0] - 1) + (frame[1] - 1) * 250
    assert frame == tag(n)
    return n


class TaggedMapper:
    def __init__(self):
        self.renders = 0
        self.last = None

    def render(self, zones, **kwargs):
        self.last = tag(self.renders)
        self.renders += 1
        return self.last

    def layer_preview(self):
        return {"layers": []}


class Sink:
    def __init__(self):
        self.frames = []

    def send_pixels(self, data):
        self.frames.append(bytes(data))

    def stats(self):
        return {}


class Power:
    def __init__(self):
        self.output_enabled = True

    @property
    def mode(self):
        return DeviceMode.LIVE if self.output_enabled else DeviceMode.IDLE


class Rig:
    """A render thread that is never started; `frame()` renders one frame."""

    def __init__(self, fps=60, delay_ms=0):
        self.fps = fps
        self.n = 0
        self.settings = BridgeSettings(path=_NO_SETTINGS_FILE, warn_unwritable=False)
        self.settings.output_delay_ms = delay_ms
        self.engine = CueEngine(clock=lambda: self.now)
        self.mapper = TaggedMapper()
        self.sink = Sink()
        self.power = Power()
        self.tracker = StatusTracker()
        self.thread = RenderThread(self.engine, self.mapper, self.sink,
                                   self.tracker, self.settings, self.power)

    @property
    def now(self) -> float:
        return T0 + self.n / self.fps

    def frame(self) -> bytes:
        out = bytes(self.thread.render_frame(self.now, self.fps))
        self.n += 1
        return out

    def run(self, frames: int) -> list:
        return [self.frame() for _ in range(frames)]


# ── the delay ───────────────────────────────────────────────────────────


def test_zero_delay_sends_each_frame_as_rendered_and_never_touches_the_ring():
    rig = Rig(delay_ms=0)
    for n in range(200):
        returned = rig.thread.render_frame(rig.now, rig.fps)
        rig.n += 1
        assert returned is rig.mapper.last          # the mapper's own object: no copy made
        assert rig.sink.frames[-1] == tag(n)
        assert rig.thread._delay_count == 0
    assert all(buf == BLACK for buf in rig.thread._delay_bufs)


@pytest.mark.parametrize("fps, delay_ms, frames_late", [
    (60, 100, 6),      # the case in the brief: 100 ms is exactly six frames
    (40, 100, 4),
    (40, 110, 5),      # not a whole number of frames: the next frame after it is due
    (30, 250, 8),
    (60, 500, 30),
    (10, 500, 5),
    (60, 5, 1),
])
def test_frame_n_is_sent_a_fixed_number_of_frames_after_it_was_rendered(fps, delay_ms, frames_late):
    rig = Rig(fps=fps, delay_ms=delay_ms)
    sent = rig.run(frames_late + 300)
    assert sent == rig.sink.frames                  # the return value is what went out
    assert sent[:frames_late] == [BLACK] * frames_late
    for k in range(frames_late, len(sent)):
        assert untag(sent[k]) == k - frames_late


@pytest.mark.parametrize("fps", VALID_FPS)
def test_the_ring_holds_the_longest_delay_at_every_frame_rate(fps):
    rig = Rig(fps=fps, delay_ms=OUTPUT_DELAY_MS_MAX)
    sent = rig.run(4 * fps)
    late = [k - untag(f) for k, f in enumerate(sent) if f != BLACK]
    assert late and len(set(late)) == 1
    assert OUTPUT_DELAY_MS_MAX / 1000 <= late[0] / fps < OUTPUT_DELAY_MS_MAX / 1000 + 1 / fps + 1e-3
    assert rig.thread._delay_count < DELAY_RING


def test_ring_is_emptied_when_output_goes_idle_and_comes_back():
    rig = Rig(fps=60, delay_ms=100)
    rig.run(60)
    assert rig.thread._delay_count > 0
    sent_before = len(rig.sink.frames)
    renders_before = rig.mapper.renders

    rig.power.output_enabled = False
    rig.run(1)
    assert rig.thread._delay_count == 0 and not rig.thread._delay_started
    rig.run(120)
    assert len(rig.sink.frames) == sent_before      # nothing goes out while idle
    assert rig.mapper.renders == renders_before

    rig.power.output_enabled = True
    back = rig.run(30)
    assert back[:6] == [BLACK] * 6                  # the delay starts again from nothing
    assert [untag(f) for f in back[6:]] == list(range(renders_before, renders_before + 24))


def test_returning_to_zero_delay_drops_what_was_held():
    rig = Rig(fps=60, delay_ms=200)
    rig.run(60)
    rig.settings.output_delay_ms = 0
    assert untag(rig.frame()) == 60                 # the frame just rendered, at once
    assert rig.thread._delay_count == 0
    rig.settings.output_delay_ms = 200
    again = rig.run(13)
    assert again[:12] == [BLACK] * 12 and untag(again[12]) == 61


def test_raising_the_delay_repeats_the_frame_on_show_instead_of_going_black():
    rig = Rig(fps=60, delay_ms=100)
    rig.run(60)
    rig.settings.output_delay_ms = 300
    sent = [untag(f) for f in rig.run(30)]
    assert None not in sent
    assert sent[:12] == [53] * 12                   # held until frame 54 is 300 ms old
    assert sent[12:] == list(range(54, 72))


def test_changing_the_delay_mid_stream_never_sends_a_stale_frame():
    """However the slider is moved, a frame goes out at most the largest delay
    plus one frame period after it was rendered, and never out of order."""
    rng = random.Random(13)
    for fps in (25, 40, 60):
        rig = Rig(fps=fps, delay_ms=0)
        last = -1
        for k in range(6000):
            if k % 37 == 0:
                rig.settings.output_delay_ms = rng.choice(
                    [0, 5, 50, 120, 250, 495, 500, rng.randrange(0, 501)])
            n = untag(rig.frame())
            if n is None:
                continue
            assert 0 <= k - n
            assert (k - n) / fps <= OUTPUT_DELAY_MS_MAX / 1000 + 1 / fps + 1e-3
            assert n >= last
            last = n


def test_a_frame_rate_above_the_rings_capacity_is_late_by_less_not_black():
    rig = Rig(fps=200, delay_ms=500)                # 100 frames in the delay, 40 slots
    sent = rig.run(400)
    assert sent[:DELAY_RING] == [BLACK] * DELAY_RING
    late = {k - untag(f) for k, f in enumerate(sent[DELAY_RING:], DELAY_RING)}
    assert late == {DELAY_RING - 1}                 # 195 ms, the most the ring can hold


def test_level_ring_and_preview_describe_the_frame_sent():
    rig = Rig(fps=40, delay_ms=250)                 # ten frames
    rig.tracker._sse_queues.append(object())        # someone is watching: build previews
    rig.thread._PREVIEW_INTERVAL = 0.0              # and capture one on every frame
    rig.run(8)
    count, levels = rig.thread.frame_levels_since(0)
    assert count == 8 and levels == [0.0] * 8       # black went out, not the frames rendered
    assert not any(rig.thread.preview_snapshot()["strip"])
    rig.run(12)
    _, levels = rig.thread.frame_levels_since(count)
    assert levels[-1] > 0
    strip = rig.thread.preview_snapshot()["strip"]
    assert list(strip[:3]) == list(tag(9)[:3])      # frame 19 shows what was rendered at frame 9


# ── calibration mode ────────────────────────────────────────────────────


def _calibrating(fps=40, delay_ms=0, brightness=128) -> Rig:
    rig = Rig(fps=fps, delay_ms=delay_ms)
    rig.settings.brightness = brightness
    assert rig.settings.set_effect("calibration", True)
    return rig


def test_calibration_is_off_by_default_and_changes_nothing_then():
    rig = Rig()
    assert rig.settings.effect_enabled("calibration") is False
    rig.engine.on_beat(BeatByte.MEASURE, rig.now)
    assert untag(rig.frame()) == 0


@pytest.mark.parametrize("beat", [BeatByte.MEASURE, BeatByte.STRONG])
def test_calibration_sends_white_on_a_beat_and_black_between(beat):
    rig = _calibrating(fps=40, brightness=128)
    assert rig.run(10) == [BLACK] * 10              # no beat yet
    white = bytes([128]) * FRAME_BYTES
    for _ in range(4):                              # a beat every 20 frames (120 bpm)
        rig.engine.on_beat(beat, rig.now)
        sent = rig.run(20)
        lit = int(CALIBRATION_FLASH_S * 40) + 1     # frames at 0, 25 and 50 ms
        assert sent[:lit] == [white] * lit
        assert sent[lit:] == [BLACK] * (20 - lit)
    assert rig.mapper.renders == 90                 # the stage look is still rendered underneath


def test_calibration_ignores_the_weak_filler_beat():
    rig = _calibrating()
    rig.engine.on_beat(BeatByte.WEAK, rig.now)
    assert rig.run(10) == [BLACK] * 10


def test_calibration_flash_follows_the_brightness_setting():
    rig = _calibrating(brightness=40)
    rig.engine.on_beat(BeatByte.MEASURE, rig.now)
    assert rig.frame() == bytes([40]) * FRAME_BYTES
    rig.settings.brightness = 200
    assert rig.frame() == bytes([200]) * FRAME_BYTES


def test_calibration_flash_is_delayed_like_any_other_frame():
    rig = _calibrating(fps=40, delay_ms=100, brightness=255)
    rig.run(40)
    rig.engine.on_beat(BeatByte.MEASURE, rig.now)
    sent = rig.run(12)
    white = bytes([255]) * FRAME_BYTES
    assert sent == [BLACK] * 4 + [white] * 3 + [BLACK] * 5


def test_calibration_switches_itself_off_after_ten_minutes():
    rig = _calibrating(fps=10)
    rig.run(int(CALIBRATION_MAX_S * 10))
    assert rig.settings.effect_enabled("calibration") and rig.thread.calibrating
    rig.run(2)
    assert not rig.settings.effect_enabled("calibration") and not rig.thread.calibrating
    assert untag(rig.sink.frames[-1]) is not None   # the stage look is back


def test_calibration_times_out_while_the_strip_is_idle_too():
    rig = _calibrating(fps=10)
    rig.power.output_enabled = False
    rig.run(int(CALIBRATION_MAX_S * 10) + 2)
    assert not rig.settings.effect_enabled("calibration")


def test_turning_calibration_on_again_starts_a_new_ten_minutes():
    rig = _calibrating(fps=10)
    rig.run(3000)
    rig.settings.set_effect("calibration", False)
    rig.run(1)
    rig.settings.set_effect("calibration", True)
    rig.run(int(CALIBRATION_MAX_S * 10))
    assert rig.settings.effect_enabled("calibration")


# ── the setting ─────────────────────────────────────────────────────────


def test_setting_defaults_to_zero_and_is_clamped():
    s = BridgeSettings(path=_NO_SETTINGS_FILE, warn_unwritable=False)
    assert s.output_delay_ms == 0
    for given, stored in ((120, 120), (-5, 0), (9999, OUTPUT_DELAY_MS_MAX), (250.9, 250)):
        s.output_delay_ms = given
        assert s.output_delay_ms == stored
    snap = s.snapshot()
    assert snap["output_delay_ms"] == 250 and snap["output_delay_ms_max"] == OUTPUT_DELAY_MS_MAX


def test_setting_persists_and_a_bad_stored_value_is_clamped_or_ignored(tmp_path):
    path = str(tmp_path / "settings.json")
    BridgeSettings(path=path).output_delay_ms = 85
    assert BridgeSettings(path=path).output_delay_ms == 85
    for stored, loaded in ((7000, OUTPUT_DELAY_MS_MAX), (-3, 0), ("80", 0), (None, 0)):
        Path(path).write_text(json.dumps({"output_delay_ms": stored}))
        assert BridgeSettings(path=path).output_delay_ms == loaded


def test_calibration_does_not_survive_a_restart(tmp_path):
    path = str(tmp_path / "settings.json")
    s = BridgeSettings(path=path)
    s.set_effect("calibration", True)
    s.set_effect("mirror", True)
    again = BridgeSettings(path=path)
    assert again.effect_enabled("mirror") is True
    assert again.effect_enabled("calibration") is False


def test_settings_api_accepts_and_rejects_output_delay():
    s = BridgeSettings(path=_NO_SETTINGS_FILE, warn_unwritable=False)
    server = StatusServer(StatusTracker(), settings=s)
    status, _ = server._handle_settings_action({"output_delay_ms": 140})
    assert status == 200 and s.output_delay_ms == 140
    status, _ = server._handle_settings_action({"output_delay_ms": 100000})
    assert status == 200 and s.output_delay_ms == OUTPUT_DELAY_MS_MAX
    status, message = server._handle_settings_action({"output_delay_ms": "soon"})
    assert status == 400 and "output_delay_ms" in message
    assert s.output_delay_ms == OUTPUT_DELAY_MS_MAX
    status, _ = server._handle_settings_action({"effects": {"calibration": True}})
    assert status == 200 and s.effect_enabled("calibration")


def test_replay_pins_no_delay_and_no_calibration():
    bridge = ReplayBridge()
    assert bridge.settings.output_delay_ms == 0
    assert bridge.settings.effect_enabled("calibration") is False
