"""Synthesised YARG datagram streams for replay tests.

Built on `test_sender.build_packet`, the same builder the dev sender uses, so a
fixture and a hand-driven test session can't drift apart in what a "YARG packet"
means.

These are *protocol* fixtures. A capture records what came over the wire, which
means it carries `auto_gen` and the resulting cue stream but says nothing about
where the venue data came from — MIDI `VENUE` text, legacy Rock Band note
numbers, or a Milo. Venue-source labels come from classifying the *song* with
`venue_scan/`, not from the capture. `venue_source` below records that claim
where a stream is modelled on a known song shape; it is metadata, not evidence.

Real captures recorded off the live bridge are the eventual source of truth.
These exist so CI has deterministic input with no hardware and no song files.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protocol.yarg_packet import (  # noqa: E402
    BeatByte, CueByte, KeyframeByte, SceneIndexByte, StrobeSpeed,
)
from test_sender import build_packet  # noqa: E402

# YARG sends roughly 90 datagrams/second.
PACKET_INTERVAL = 1.0 / 90.0

# Paused byte values, per DataStreamController: 0 = at menu, 1 = playing,
# 2 = paused. main.YARGProtocol treats only 2 as paused.
AT_MENU = 0
PLAYING = 1
PAUSED = 2


def _patch(pkt: bytes, **fields) -> bytes:
    """Set datagram fields `build_packet` doesn't expose.

    build_packet covers the fields the dev sender needs; a lifecycle fixture also
    has to move scene, paused, venue size, section, fog and auto-gen. Offsets are
    `protocol/yarg_packet.parse_packet`'s, which is the contract under test.
    """
    buf = bytearray(pkt)
    offsets = {
        "scene": 6, "paused": 7, "venue_size": 8, "song_section": 13,
        "post_processing": 35, "fog": 36, "bonus": 40, "auto_gen": 41,
        "spotlight": 42, "singalong": 43,
        "camera_constraint": 44, "camera_priority": 45, "camera_subject": 46,
    }
    for name, value in fields.items():
        if name not in offsets:
            raise KeyError(f"unknown datagram field {name!r}")
        buf[offsets[name]] = int(value) & 0xFF
    return bytes(buf)


class StreamBuilder:
    """Accumulates timestamped datagrams on a virtual clock.

    Time only moves when you ask it to, so a fixture's timeline is exact rather
    than dependent on how fast the test machine is.
    """

    def __init__(self, bpm: float = 120.0, start: float = 0.0,
                 interval: float = PACKET_INTERVAL):
        self.bpm = bpm
        self.t = float(start)
        self.interval = interval
        self.packets: list[tuple[float, bytes]] = []
        self._beat_index = 0
        self._defaults: dict = {"scene": SceneIndexByte.GAMEPLAY,
                                "paused": PLAYING, "venue_size": 1}

    # ── low level ───────────────────────────────────────────────

    def set_defaults(self, **fields) -> "StreamBuilder":
        """Fields applied to every later packet until changed."""
        self._defaults.update(fields)
        return self

    def emit(self, count: int = 1, *, cue: int = CueByte.DEFAULT,
             strobe: int = StrobeSpeed.OFF, beat: int = BeatByte.OFF,
             keyframe: int = 0, drum_notes: int = 0, camera_subject: int = 0,
             camera_priority: int = 0, star_power=None,
             raw: bytes | None = None, **fields) -> "StreamBuilder":
        """Append *count* packets, advancing the clock by one interval each."""
        merged = {**self._defaults, **fields}
        for _ in range(count):
            pkt = raw if raw is not None else build_packet(
                cue=cue, strobe=strobe, beat=beat, bpm=self.bpm,
                keyframe=keyframe, drum_notes=drum_notes,
                camera_subject=camera_subject, camera_priority=camera_priority,
                star_power=star_power)
            if merged and raw is None:
                pkt = _patch(pkt, **merged)
            self.packets.append((round(self.t, 6), pkt))
            self.t += self.interval
        return self

    def emit_raw(self, data: bytes, count: int = 1) -> "StreamBuilder":
        """Append bytes verbatim — for malformed and truncated cases."""
        for _ in range(count):
            self.packets.append((round(self.t, 6), data))
            self.t += self.interval
        return self

    def idle(self, seconds: float) -> "StreamBuilder":
        """Advance the clock without sending anything (a transmission gap)."""
        self.t += seconds
        return self

    # ── musical helpers ─────────────────────────────────────────

    def play(self, seconds: float, *, cue: int = CueByte.DEFAULT,
             strobe: int = StrobeSpeed.OFF, beats: bool = True,
             keyframe_every: int = 0, **fields) -> "StreamBuilder":
        """Emit ~90 packets/second for *seconds*, with beat pulses on the grid.

        Beats land on the sub-beat grid (4 per beat) exactly as YARG's stream
        does, so the bridge's beat clock and PLL see a realistic cadence.
        """
        sub_beat = 60.0 / self.bpm / 4.0
        n = max(1, int(round(seconds / self.interval)))
        next_beat_at = self.t
        for _ in range(n):
            beat = BeatByte.OFF
            keyframe = 0
            if beats and self.t >= next_beat_at - 1e-9:
                beat = (BeatByte.MEASURE if self._beat_index % 4 == 0
                        else BeatByte.STRONG)
                if keyframe_every and self._beat_index % keyframe_every == 0:
                    keyframe = KeyframeByte.NEXT
                self._beat_index += 1
                next_beat_at += sub_beat
            self.emit(1, cue=cue, strobe=strobe, beat=beat, keyframe=keyframe,
                      **fields)
        return self

    def build(self) -> list[tuple[float, bytes]]:
        return list(self.packets)


# ── the fixture streams ─────────────────────────────────────────

def warm_beats(seconds: float = 4.0, bpm: float = 120.0):
    """Steady warm-automatic wash with beats. The baseline good case."""
    return StreamBuilder(bpm=bpm).play(seconds, cue=CueByte.WARM_AUTOMATIC).build()


def song_lifecycle():
    """Menu → gameplay → pause → resume → score → menu.

    The transitions the bridge has to survive between songs, which is where
    scene and pause handling actually gets exercised.
    """
    b = StreamBuilder(bpm=128.0)
    b.set_defaults(scene=SceneIndexByte.MENU, paused=AT_MENU, venue_size=0)
    b.play(1.0, cue=CueByte.MENU, beats=False)

    b.set_defaults(scene=SceneIndexByte.GAMEPLAY, paused=PLAYING, venue_size=1)
    b.play(1.5, cue=CueByte.INTRO)
    b.play(2.0, cue=CueByte.VERSE, song_section=5)
    b.play(2.0, cue=CueByte.CHORUS, song_section=2)

    # Paused mid-chorus: YARG keeps sending, with paused=2 and no beats.
    b.set_defaults(paused=PAUSED)
    b.play(1.5, cue=CueByte.CHORUS, beats=False, song_section=2)

    b.set_defaults(paused=PLAYING)
    b.play(2.0, cue=CueByte.CHORUS, song_section=2)
    b.play(1.0, cue=CueByte.BIG_ROCK_ENDING)

    b.set_defaults(scene=SceneIndexByte.SCORE, paused=AT_MENU, venue_size=0)
    b.play(1.0, cue=CueByte.SCORE, beats=False)

    b.set_defaults(scene=SceneIndexByte.MENU)
    b.play(1.0, cue=CueByte.MENU, beats=False)
    return b.build()


def authored_venue():
    """A hand-lit chart: keyframed manual cues, spotlights, fog, post grades.

    Models the MIDI `VENUE` shape venue_scan finds most often — `next` keyframes
    dominating by an order of magnitude, with manual cues and spotlights.
    `auto_gen` is 0 because a human lit this one.
    """
    b = StreamBuilder(bpm=140.0)
    b.set_defaults(auto_gen=0, fog=1)
    b.play(2.0, cue=CueByte.WARM_MANUAL, keyframe_every=1)
    b.play(2.0, cue=CueByte.COOL_MANUAL, keyframe_every=1, spotlight=1)
    b.play(1.5, cue=CueByte.DISCHORD, post_processing=7)   # black and white
    b.play(1.5, cue=CueByte.STOMP, keyframe_every=2, singalong=8)
    b.play(1.0, cue=CueByte.FLARE_FAST, post_processing=13)
    return b.build()


def auto_generated_venue():
    """The majority case: YARG synthesised the lighting (`auto_gen` set).

    Per the venue_scan inventory this covers 18,626 songs including every
    `.chart` file, so it is the shape most of the library actually produces.
    Synthesised fog is on, because fog generation runs even on authored venues.
    """
    b = StreamBuilder(bpm=100.0)
    b.set_defaults(auto_gen=1, fog=1)
    b.play(2.5, cue=CueByte.VERSE, song_section=5)
    b.play(2.5, cue=CueByte.CHORUS, song_section=2)
    b.play(2.0, cue=CueByte.VERSE, song_section=5)
    return b.build()


def strobe_and_blackout():
    """Tempo-locked strobe, then a hard blackout, then recovery.

    The strobe path is the one that would silently go non-deterministic if the
    render seam stopped feeding `get_strobe_visible` its clock.
    """
    b = StreamBuilder(bpm=150.0)
    b.play(2.0, cue=CueByte.FRENZY, strobe=StrobeSpeed.FAST)
    b.play(1.0, cue=CueByte.BLACKOUT_FAST)
    b.play(2.0, cue=CueByte.SEARCHLIGHTS, strobe=StrobeSpeed.MEDIUM)
    return b.build()


def dropped_beats():
    """Beat pulses that stop for two bars, then come back.

    The beat clock is meant to coast through the gap (free-running) rather than
    freeze the chase — the behaviour tests/test_beat_lock.py pins at unit level,
    here driven through the whole path.
    """
    b = StreamBuilder(bpm=120.0)
    b.play(2.0, cue=CueByte.CHORUS, beats=True)
    b.play(4.0, cue=CueByte.CHORUS, beats=False)   # pulses stop, cue holds
    b.play(2.0, cue=CueByte.CHORUS, beats=True)
    return b.build()


def repeated_beats():
    """Every packet claims a beat — a duplicated/stuck pulse stream.

    Guards against a stuck beat byte turning into a 90 Hz strobe.
    """
    b = StreamBuilder(bpm=120.0)
    for _ in range(180):  # ~2s at 90/s
        b.emit(1, cue=CueByte.CHORUS, beat=BeatByte.STRONG)
    return b.build()


def malformed_stream():
    """Valid packets interleaved with junk the parser must reject or survive.

    - too short to hold the 44-byte minimum
    - right length, wrong magic
    - truncated mid-star-power, with a player count larger than the bytes present
    - a future/unknown datagram version, which must still render
    - a longer-than-known packet, since YARG's datagram is append-only
    """
    good = build_packet(cue=CueByte.WARM_AUTOMATIC, bpm=120.0,
                        beat=BeatByte.STRONG)

    short = good[:20]
    bad_magic = b"NOPE" + good[4:]

    # count says 8 players, only 1 pair of bytes follows
    lying_count = bytearray(build_packet(cue=CueByte.CHORUS, bpm=120.0,
                                         star_power=[(200, True)]))
    lying_count[47] = 8
    lying_count[48] = 0
    truncated_sp = bytes(lying_count[:51])

    future_version = bytearray(good)
    future_version[4] = 99

    longer = bytes(good) + b"\x00" * 32   # appended unknown fields

    b = StreamBuilder(bpm=120.0)
    b.play(1.0, cue=CueByte.WARM_AUTOMATIC)
    b.emit_raw(short)
    b.emit_raw(bad_magic)
    b.emit_raw(truncated_sp)
    b.emit_raw(bytes(future_version), count=10)
    b.emit_raw(longer, count=10)
    b.play(1.0, cue=CueByte.WARM_AUTOMATIC)
    return b.build()


def spotlight_cues():
    """Both spotlight cues, long enough for the beat chase to wrap.

    The multi-spot spotlights are the one cue family whose look is pure
    geometry, so a moved digest here means the spots or the chase moved. At
    120 BPM each 3-second leg is 6 beats — two full trips around the three
    spots, which catches a chase that advances at the wrong rate as well as one
    that has stopped.

    The leading beat-less second matters: beat_clock is 0.0 until the first
    beat, and the cue has to be lit through that, not dark.
    """
    b = StreamBuilder(bpm=120.0)
    b.play(1.0, cue=CueByte.BLACKOUT_SPOTLIGHT, beats=False)
    b.play(3.0, cue=CueByte.BLACKOUT_SPOTLIGHT)
    b.play(3.0, cue=CueByte.SILHOUETTES_SPOTLIGHT)
    return b.build()


def star_power_run():
    """A charge → overdrive → release cycle with per-player star power (v4)."""
    b = StreamBuilder(bpm=132.0)
    for amt in range(0, 256, 16):
        b.emit(3, cue=CueByte.WARM_AUTOMATIC, beat=BeatByte.STRONG,
               star_power=[(amt, False), (amt // 2, False)])
    for amt in range(255, -1, -12):
        b.emit(3, cue=CueByte.WARM_AUTOMATIC, beat=BeatByte.STRONG,
               star_power=[(amt, True), (0, False)])
    return b.build()


# name → (builder, venue_source claim, one-line description)
FIXTURES = {
    "warm_beats": (warm_beats, "n/a", "steady warm-auto wash with beats"),
    "song_lifecycle": (song_lifecycle, "n/a",
                       "menu → gameplay → pause → resume → score → menu"),
    "authored_venue": (authored_venue, "midi-venue",
                       "keyframed manual cues, spotlights, fog, post grades"),
    "auto_generated_venue": (auto_generated_venue, "auto-generated",
                             "YARG-synthesised lighting, the majority case"),
    "strobe_and_blackout": (strobe_and_blackout, "n/a",
                            "tempo-locked strobe, blackout, recovery"),
    "dropped_beats": (dropped_beats, "n/a", "beat pulses stop, then return"),
    "repeated_beats": (repeated_beats, "n/a", "every packet claims a beat"),
    "malformed_stream": (malformed_stream, "n/a",
                         "junk and future-version packets among good ones"),
    "star_power_run": (star_power_run, "n/a", "charge → overdrive → release"),
    "spotlight_cues": (spotlight_cues, "n/a",
                       "multi-spot spotlights, beat chase wrapping"),
}


def build(name: str):
    """Packets for a named fixture."""
    if name not in FIXTURES:
        raise KeyError(f"unknown fixture {name!r}; have {sorted(FIXTURES)}")
    return FIXTURES[name][0]()


def write_all(directory) -> list:
    """Write every fixture as a capture file. Used by tests and by hand."""
    from replay.capture import write_capture

    out = []
    directory = Path(directory)
    for name, (fn, source, desc) in FIXTURES.items():
        path = directory / f"{name}.jsonl"
        write_capture(path, fn(), note=desc,
                      extra={"fixture": name, "venue_source": source,
                             "synthetic": True})
        out.append(path)
    return out


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Write replay fixture captures")
    ap.add_argument("directory", nargs="?",
                    default=str(Path(__file__).parent / "fixtures" / "captures"))
    args = ap.parse_args()
    for p in write_all(args.directory):
        print(f"{p}  ({p.stat().st_size} bytes)")
