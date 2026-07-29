"""Deterministic in-process replay of a YARG capture.

Feeds recorded datagrams through the *real* pipeline — `YARGProtocol` →
`CueEngine` → `LEDMapper` → `DDPSender` — with a synthetic clock, so the same
capture produces byte-identical frames on every run and on every machine. No
sockets, no sleeps, no WLED; a capture replays far faster than real time.

    result = replay_capture("tests/fixtures/captures/warm_beats.jsonl", fps=40)
    assert result.sampled_digest() == "…"

The determinism rests on three injection points, all of which default to
`time.monotonic()` in production:

- `CueEngine(clock=…)` — every cue/beat/pause/note deadline
- `LEDMapper(clock=…)` — the breathing phase
- `RenderThread.render_frame(now, fps)` — tick, strobe phase, cross-fade

Packets are delivered at their own recorded timestamps rather than being
rounded to the next frame, so a cue landing between two frames still stamps its
cross-fade where it really happened.
"""

import hashlib
from dataclasses import dataclass, field

from config import LED_COUNT
from effects.cue_engine import CueEngine
from effects.mapper import LEDMapper
from main import RenderThread, YARGProtocol
from protocol.yarg_packet import CameraCutSubject, SongSectionByte, VenueSizeByte
from replay.capture import CaptureHeader, read_capture
from settings import BridgeSettings
from status_server import StatusTracker

# A path that cannot exist or be written, so a replay never reads the operator's
# saved settings and never writes to them. BridgeSettings falls back to the
# in-code defaults (see its _probe_writable) — the same trick
# tests/test_runtime_boundaries.py uses.
_NO_SETTINGS_FILE = "/proc/replay-has-no-settings/settings.json"

REPLAY_ADDR = ("replay", 0)

# The synthetic clock's starting value. Deliberately not 0.0: the engine uses
# 0.0 as its "hasn't happened yet" sentinel for `_beat_at`, `_cue_change_at` and
# `_reveal_started_at`, so a replay starting at 0.0 collides with those and (for
# one example) skips the opening cue cross-fade. Production can't hit that —
# time.monotonic() is system uptime and is never 0 — so replaying at 0.0 would
# render light the real bridge never renders.
#
# The exact value is otherwise arbitrary but must stay fixed: floating-point
# resolution falls off with magnitude, so a different epoch shifts some channels
# by ±1 (see tests/test_replay.py::test_epoch_only_shifts_channels_by_rounding).
# Golden digests are taken at this epoch.
DEFAULT_EPOCH = 1000.0


class FrameSink:
    """Stands in for `DDPSender`: keeps frames instead of putting them on a wire.

    Implements the two methods `RenderThread` calls, so the real render path
    runs unmodified right up to the socket.
    """

    def __init__(self, keep: bool = True):
        self.frames: list[bytes] = []
        self.keep = keep
        self.frames_sent = 0

    def send_pixels(self, pixel_data) -> None:
        self.frames_sent += 1
        if self.keep:
            # render_frame's return value aliases reused buffers — copy.
            self.frames.append(bytes(pixel_data))

    def stats(self) -> dict:
        return {"send_us_avg": 0.0, "send_us_max": 0.0,
                "send_errors": 0, "frames_sent": self.frames_sent}

    def close(self) -> None:
        pass


class FailingSink(FrameSink):
    """A sink that raises OSError for a window of frames.

    Models WLED dropping off the network. `DDPSender` swallows `OSError` and
    counts it, so this exercises the loss-and-recovery path without unplugging
    anything.
    """

    def __init__(self, fail_from: int = 0, fail_until: int = 0):
        super().__init__()
        self.fail_from = fail_from
        self.fail_until = fail_until
        self.send_errors = 0

    def send_pixels(self, pixel_data) -> None:
        n = self.frames_sent
        if self.fail_from <= n < self.fail_until:
            self.frames_sent += 1
            self.send_errors += 1
            raise OSError("simulated WLED loss")
        super().send_pixels(pixel_data)

    def stats(self) -> dict:
        s = super().stats()
        s["send_errors"] = self.send_errors
        return s


class PowerStub:
    """Minimal `WLEDPowerManager` stand-in.

    Replay has no controller to power on, but the render path gates DDP on
    `is_on`, so it defaults to True — otherwise a replay would render frames and
    send none.
    """

    def __init__(self, on: bool = True):
        self.on = on
        self.activity_count = 0

    @property
    def is_on(self) -> bool:
        return self.on

    def on_activity(self) -> None:
        self.activity_count += 1


@dataclass
class FrameState:
    """Per-frame trace of what the bridge thought was happening."""
    index: int
    t: float
    cue: str
    scene: int
    paused: bool
    venue_size: str
    song_section: str
    camera_subject: str
    auto_gen: bool
    bpm: float
    strobe_hz: float
    sp_active: bool
    lit_pixels: int


@dataclass
class ReplayResult:
    """Everything a replay produced."""
    frames: list[bytes]
    fps: int
    packets_fed: int
    packets_parsed: int
    duration: float
    header: CaptureHeader | None = None
    trace: list[FrameState] = field(default_factory=list)
    sink: FrameSink | None = None

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    def frame_at(self, seconds: float) -> bytes:
        """The frame covering *seconds* into the replay."""
        if not self.frames:
            raise IndexError("replay produced no frames")
        idx = min(int(seconds * self.fps), len(self.frames) - 1)
        return self.frames[idx]

    def sample_indices(self, count: int = 16) -> list[int]:
        """Evenly spaced frame indices — the sampling a golden digest pins."""
        n = len(self.frames)
        if n == 0:
            return []
        if n <= count:
            return list(range(n))
        step = (n - 1) / (count - 1) if count > 1 else 0
        return [round(i * step) for i in range(count)]

    def sampled_digest(self, count: int = 16) -> str:
        """SHA-256 over evenly spaced frames.

        A whole-frame digest over a whole song would change on any pixel
        anywhere and tell you nothing about where; sampling keeps the golden
        value small and stable while still catching a real render change.
        """
        h = hashlib.sha256()
        h.update(f"{self.fps}:{len(self.frames)}:".encode())
        for i in self.sample_indices(count):
            h.update(i.to_bytes(4, "big"))
            h.update(self.frames[i])
        return h.hexdigest()

    def frame_digest(self, index: int) -> str:
        return hashlib.sha256(self.frames[index]).hexdigest()

    def cue_sequence(self) -> list[str]:
        """Cues in the order they became active — one entry per change."""
        out: list[str] = []
        for st in self.trace:
            if not out or out[-1] != st.cue:
                out.append(st.cue)
        return out

    def dark_frames(self) -> int:
        return sum(1 for f in self.frames if not any(f))

    def summary(self) -> dict:
        return {
            "frames": len(self.frames),
            "fps": self.fps,
            "duration": round(self.duration, 3),
            "packets_fed": self.packets_fed,
            "packets_parsed": self.packets_parsed,
            "dark_frames": self.dark_frames(),
            "cues": self.cue_sequence(),
            "digest": self.sampled_digest(),
        }


class ReplayBridge:
    """The bridge's packet and render path, wired for offline replay.

    Uses the configured `LED_COUNT` so replayed frames are the same width as
    the real strip's.
    """

    def __init__(self, fps: int = 40, sink: FrameSink | None = None,
                 start_time: float = DEFAULT_EPOCH, wled_on: bool = True,
                 brightness: int = 255, palette: str = "default",
                 palette_strictness: float = 1.0, seed: int = 0,
                 cue_fade_ms: int = 120, cue_fade_overrides: dict | None = None):
        self._now = float(start_time)
        clock = self._clock

        self.engine = CueEngine(clock=clock)
        # Fixed seed: sparkle/glitch/shimmer scatter is random by design, so a
        # replay has to pin the RNG or no two runs would agree.
        self.mapper = LEDMapper(LED_COUNT, clock=clock, seed=seed)
        self.sink = sink if sink is not None else FrameSink()
        self.tracker = StatusTracker()
        self.power = PowerStub(on=wled_on)

        # Pin the operator-tunable knobs so a replay depends only on the
        # capture, not on the environment or a saved settings file.
        self.settings = BridgeSettings(path=_NO_SETTINGS_FILE,
                                       warn_unwritable=False,
                                       cue_fade_overrides=cue_fade_overrides)
        self.settings.brightness = brightness
        self.settings.palette_name = palette
        # Pinned like the palette itself: the reactivity layers are constrained
        # to the palette's family, so a capture recorded under one strictness
        # would not reproduce under another.
        self.settings.palette_strictness = palette_strictness
        # Same reasoning for the cue cross-fade: it blends every cue change over
        # a wall-clock window, so a capture replayed under a different fade
        # renders different light. `cue_fade_overrides=None` means the
        # production per-cue table; `{}` plus cue_fade_ms=250 is the documented
        # rollback to the pre-2026-07-29 look.
        self.settings.cue_fade_ms = cue_fade_ms
        self.settings.fps = fps
        self.fps = fps

        # Real render path; constructed but never started as a thread — replay
        # drives render_frame() itself.
        self.render = RenderThread(self.engine, self.mapper, self.sink,
                                   self.tracker, self.settings, self.power)

        self.protocol = YARGProtocol(self.engine, self.tracker, self.power)
        self.packets_fed = 0

    def _clock(self) -> float:
        return self._now

    @property
    def now(self) -> float:
        return self._now

    def advance_to(self, t: float) -> None:
        """Move the synthetic clock forward. Never backwards."""
        if t > self._now:
            self._now = t

    def feed(self, data: bytes) -> None:
        """Deliver one datagram through the real UDP entry point."""
        self.packets_fed += 1
        self.protocol.datagram_received(data, REPLAY_ADDR)

    def render_frame(self) -> bytes:
        return self.render.render_frame(self._now, self.fps)

    def state(self, index: int, lit: int) -> FrameState:
        t = self.tracker
        return FrameState(
            index=index, t=round(self._now, 6),
            cue=t.current_cue_name, scene=t.scene, paused=t.paused,
            venue_size=VenueSizeByte.name(t.venue_size),
            song_section=SongSectionByte.name(t.song_section),
            camera_subject=CameraCutSubject.name(t.camera_subject),
            auto_gen=t.auto_gen, bpm=round(self.engine.bpm, 3),
            strobe_hz=round(self.engine.strobe_hz(), 3),
            sp_active=t.sp_active, lit_pixels=lit,
        )


def replay_packets(packets, fps: int = 40, tail: float = 0.0,
                   sink: FrameSink | None = None, trace: bool = True,
                   header: CaptureHeader | None = None,
                   bridge: ReplayBridge | None = None) -> ReplayResult:
    """Replay `(t, data)` pairs (or `CapturePacket`s) and return the frames.

    *tail* keeps rendering for extra seconds after the last packet, so decays,
    cross-fades and the drop to "disconnected" can be observed.
    """
    items = []
    for p in packets:
        if hasattr(p, "data"):
            items.append((float(p.t), p.data))
        else:
            items.append((float(p[0]), p[1]))
    items.sort(key=lambda it: it[0])

    if bridge is None:
        bridge = ReplayBridge(fps=fps, sink=sink)
    elif sink is not None:
        raise ValueError("pass sink to ReplayBridge, not replay_packets, "
                         "when supplying a bridge")

    interval = 1.0 / fps
    last_t = items[-1][0] if items else 0.0
    end_t = last_t + max(tail, 0.0)

    # Capture timestamps are relative to the start of the recording, so shift
    # them onto the bridge's clock. Without this a bridge started at a non-zero
    # epoch would never advance — every offset would be in its past, and
    # advance_to() refuses to move backwards.
    origin = bridge.now

    frames: list[bytes] = []
    states: list[FrameState] = []
    i = 0
    frame_t = 0.0
    frame_index = 0

    while frame_t <= end_t + 1e-9 or i < len(items):
        # Deliver everything due by this frame, each at its own timestamp so
        # cue/beat deadlines land where the capture says they did.
        while i < len(items) and items[i][0] <= frame_t + 1e-9:
            t, data = items[i]
            bridge.advance_to(origin + t)
            bridge.feed(data)
            i += 1

        bridge.advance_to(origin + frame_t)
        pixels = bytes(bridge.render_frame())
        frames.append(pixels)
        if trace:
            lit = sum(1 for k in range(0, len(pixels), 3)
                      if pixels[k] or pixels[k + 1] or pixels[k + 2])
            states.append(bridge.state(frame_index, lit))

        frame_index += 1
        frame_t = frame_index * interval
        if i >= len(items) and frame_t > end_t + 1e-9:
            break

    return ReplayResult(
        frames=frames, fps=fps, packets_fed=bridge.packets_fed,
        packets_parsed=bridge.tracker.packets_received,
        duration=frame_index * interval, header=header, trace=states,
        sink=bridge.sink,
    )


def replay_capture(path, fps: int = 40, tail: float = 0.0,
                   sink: FrameSink | None = None, trace: bool = True,
                   bridge: ReplayBridge | None = None) -> ReplayResult:
    """Read a capture file and replay it. See `replay_packets`."""
    header, packets = read_capture(path)
    return replay_packets(packets, fps=fps, tail=tail, sink=sink, trace=trace,
                          header=header, bridge=bridge)


def main() -> None:
    """Replay a capture and print a summary — no hardware involved."""
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="Replay a YARG capture through the bridge pipeline")
    ap.add_argument("capture")
    ap.add_argument("--fps", type=int, default=40)
    ap.add_argument("--tail", type=float, default=0.0,
                    help="extra seconds to render after the last packet")
    ap.add_argument("--digest-samples", type=int, default=16)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    result = replay_capture(args.capture, fps=args.fps, tail=args.tail)

    if args.json:
        print(json.dumps(result.summary(), indent=2))
        return

    hdr = result.header
    print(f"capture:  {args.capture}")
    if hdr:
        print(f"recorded: {hdr.started}  {hdr.note}")
    print(f"packets:  {result.packets_fed} fed, {result.packets_parsed} parsed")
    print(f"frames:   {result.frame_count} at {result.fps} FPS "
          f"({result.duration:.2f}s), {result.dark_frames()} dark")
    print(f"cues:     {' → '.join(result.cue_sequence()) or '(none)'}")
    print(f"digest:   {result.sampled_digest(args.digest_samples)}")


if __name__ == "__main__":
    main()
