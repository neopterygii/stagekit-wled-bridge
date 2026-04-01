"""Stage Kit cue engine.

Translates YARG lighting cues + beat events into per-zone bitmask state,
mirroring YALCY's StageKitTalker behavior. Each cue launches one or more
pattern primitives (beat-synced loops, listen-triggered patterns, etc.)
that set zone bitmask values over time.
"""

import asyncio
import time

from protocol.yarg_packet import CueByte, BeatByte, KeyframeByte, StrobeSpeed

# Bitmask constants matching YALCY
NONE = 0b00000000
ZERO = 0b00000001
ONE = 0b00000010
TWO = 0b00000100
THREE = 0b00001000
FOUR = 0b00010000
FIVE = 0b00100000
SIX = 0b01000000
SEVEN = 0b10000000
ALL = 0b11111111

# Zone indices
RED = 0
GREEN = 1
BLUE = 2
YELLOW = 3

# Strobe rates in Hz
STROBE_RATES = {
    StrobeSpeed.OFF: 0,
    StrobeSpeed.SLOW: 2,
    StrobeSpeed.MEDIUM: 4,
    StrobeSpeed.FAST: 8,
    StrobeSpeed.FASTEST: 16,
}


class CueEngine:
    """Manages active lighting cue and produces zone bitmask state."""

    def __init__(self):
        # Current zone bitmasks [red, green, blue, yellow]
        self.zones = [NONE, NONE, NONE, NONE]

        # Strobe state
        self.strobe_rate = 0  # Hz, 0 = off
        self._strobe_on = True  # strobe phase toggle

        # BPM from YARG
        self.bpm = 120.0

        # Active primitives (asyncio tasks)
        self._active_tasks: list[asyncio.Task] = []

        # Beat/keyframe event for listen patterns
        self._beat_event = asyncio.Event()
        self._keyframe_event = asyncio.Event()
        self._last_beat_type = BeatByte.OFF
        self._last_keyframe_type = KeyframeByte.OFF
        self._last_drum_notes = 0

        # Current cue ID
        self._current_cue = CueByte.NO_CUE

    def get_strobe_visible(self) -> bool:
        """Returns whether pixels should be visible (considering strobe)."""
        if self.strobe_rate == 0:
            return True
        return self._strobe_on

    async def run_strobe(self):
        """Background task that toggles strobe phase."""
        while True:
            if self.strobe_rate > 0:
                period = 1.0 / self.strobe_rate
                self._strobe_on = not self._strobe_on
                await asyncio.sleep(period / 2)
            else:
                self._strobe_on = True
                await asyncio.sleep(0.05)

    def on_beat(self, beat_type: int):
        """Called when a beat event arrives from YARG."""
        self._last_beat_type = beat_type
        if beat_type != BeatByte.OFF:
            self._beat_event.set()

    def on_keyframe(self, keyframe_type: int):
        """Called when a keyframe event arrives from YARG."""
        self._last_keyframe_type = keyframe_type
        if keyframe_type != KeyframeByte.OFF:
            self._keyframe_event.set()

    def on_drum(self, drum_notes: int):
        self._last_drum_notes = drum_notes

    def on_strobe(self, strobe_byte: int):
        """Called when strobe state changes."""
        self.strobe_rate = STROBE_RATES.get(strobe_byte, 0)

    def on_cue(self, cue_byte: int):
        """Called when the lighting cue changes."""
        if cue_byte == self._current_cue:
            return
        self._current_cue = cue_byte
        self._kill_primitives()
        self._launch_cue(cue_byte)

    def _kill_primitives(self):
        """Cancel all active pattern tasks."""
        for task in self._active_tasks:
            task.cancel()
        self._active_tasks.clear()

    def _set_zone(self, zone: int, mask: int):
        """Set a zone's bitmask."""
        self.zones[zone] = mask

    def _launch_cue(self, cue: int):
        """Launch the appropriate pattern primitives for a cue.

        Patterns match YALCY's StageKitTalker definitions (large venue variant).
        BRE and Frenzy use smooth rotating chase instead of YALCY's ALL/NONE
        flash to avoid seizure-inducing strobing on a full LED strip.
        Stomp uses keyframe-triggered chase instead of ALL/NONE toggle for
        the same reason.
        """
        # Reset all zones
        for i in range(4):
            self.zones[i] = NONE

        if cue == CueByte.NO_CUE or cue == CueByte.BLACKOUT_FAST or \
           cue == CueByte.BLACKOUT_SLOW or cue == CueByte.BLACKOUT_SPOTLIGHT:
            return  # All off

        elif cue == CueByte.DEFAULT:
            # YALCY large: Blue/Red ALL toggle on KeyframeNext
            # We use a 2-color chase instead of ALL/NONE toggle
            self._start_listen_pattern(BLUE, [ALL, NONE], listen="keyframe")
            self._start_listen_pattern(RED, [NONE, ALL], listen="keyframe")

        elif cue == CueByte.VERSE:
            # Not in YALCY — blue chase as ambient verse lighting
            self._start_beat_pattern(BLUE, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN,
            ], cycles_per_beat=0.25)

        elif cue == CueByte.CHORUS:
            # Not in YALCY — energetic red+yellow
            self._start_beat_pattern(RED, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN,
            ], cycles_per_beat=0.25)
            self._set_zone(YELLOW, ALL)

        elif cue == CueByte.WARM_MANUAL:
            # YALCY: same patterns as LoopWarm (Warm Automatic)
            self._start_beat_pattern(RED, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN,
            ], cycles_per_beat=0.25)
            self._start_beat_pattern(YELLOW, [
                TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR, THREE,
            ], cycles_per_beat=0.125)

        elif cue == CueByte.COOL_MANUAL:
            # YALCY: same patterns as LoopCool (Cool Automatic)
            self._start_beat_pattern(BLUE, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN,
            ], cycles_per_beat=0.25)
            self._start_beat_pattern(GREEN, [
                TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR, THREE,
            ], cycles_per_beat=0.125)

        elif cue == CueByte.WARM_AUTOMATIC:
            # YALCY: Red opposing-pair chase + Yellow CCW single
            self._start_beat_pattern(RED, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN,
            ], cycles_per_beat=0.25)
            self._start_beat_pattern(YELLOW, [
                TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR, THREE,
            ], cycles_per_beat=0.125)

        elif cue == CueByte.COOL_AUTOMATIC:
            # YALCY: Blue opposing-pair chase + Green CCW single
            self._start_beat_pattern(BLUE, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN,
            ], cycles_per_beat=0.25)
            self._start_beat_pattern(GREEN, [
                TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR, THREE,
            ], cycles_per_beat=0.125)

        elif cue == CueByte.BIG_ROCK_ENDING:
            # YALCY: 4 zones flash ALL/NONE in sequence (seizure risk)
            # We use 4-color rotating chase — strip always fully lit
            self._start_multi_zone_chase([RED, GREEN, BLUE, YELLOW],
                                         bits_per_zone=2, cycles_per_beat=0.5)

        elif cue == CueByte.FRENZY:
            # YALCY large: Red/Blue/Yellow flash ALL/NONE (seizure risk)
            # We use 3-color rotating chase — strip always fully lit
            self._start_multi_zone_chase([RED, BLUE, YELLOW],
                                         bits_per_zone=2, cycles_per_beat=0.5)

        elif cue == CueByte.SEARCHLIGHTS:
            # YALCY large: Yellow CW + Blue CCW single-bit, 0.5 cpb
            self._start_beat_pattern(YELLOW, [
                TWO, THREE, FOUR, FIVE, SIX, SEVEN, ZERO, ONE,
            ], cycles_per_beat=0.5)
            self._start_beat_pattern(BLUE, [
                ZERO, SEVEN, SIX, FIVE, FOUR, THREE, TWO, ONE,
            ], cycles_per_beat=0.5)

        elif cue == CueByte.SWEEP:
            # YALCY large: Red opposing-pair sweep
            self._start_beat_pattern(RED, [
                SIX | TWO, FIVE | ONE, FOUR | ZERO, THREE | SEVEN,
            ], cycles_per_beat=0.25)

        elif cue == CueByte.HARMONY:
            # YALCY large: Yellow + Red counter-rotating single-bit
            self._start_beat_pattern(YELLOW, [
                THREE, TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR,
            ], cycles_per_beat=0.125)
            self._start_beat_pattern(RED, [
                FOUR, THREE, TWO, ONE, ZERO, SEVEN, SIX, FIVE,
            ], cycles_per_beat=0.125)

        elif cue == CueByte.FLARE_SLOW:
            # YALCY: ALL on every zone (static bright)
            for i in range(4):
                self.zones[i] = ALL

        elif cue == CueByte.FLARE_FAST:
            # YALCY: Blue ALL static. Green ALL if prev was cool cue.
            # We always set Blue ALL (no previous-cue tracking yet).
            self._set_zone(BLUE, ALL)

        elif cue == CueByte.SILHOUETTES:
            # YALCY: Green ALL (others off)
            self._set_zone(GREEN, ALL)

        elif cue == CueByte.SILHOUETTES_SPOTLIGHT:
            # YALCY: complex context-dependent. Simplified to green ambient.
            self._set_zone(GREEN, ALL)

        elif cue == CueByte.STOMP:
            # YALCY: R+G+Y ALL toggle on KeyframeNext (seizure risk)
            # We use multi-zone chase triggered by keyframe instead
            self._start_multi_zone_chase([RED, GREEN, YELLOW],
                                         bits_per_zone=2, cycles_per_beat=0.25,
                                         listen="keyframe")

        elif cue == CueByte.DISCHORD:
            # YALCY large: Yellow CW beat-triggered + Green CCW spinning +
            # Blue pattern switching + Red drum flash.
            # Simplified: Yellow + Green counter-rotating chases
            self._start_beat_pattern(YELLOW, [
                ZERO, ONE, TWO, THREE, FOUR, FIVE, SIX, SEVEN,
            ], cycles_per_beat=0.125, listen="beat_any")
            self._start_beat_pattern(GREEN, [
                ZERO, SEVEN, SIX, FIVE, FOUR, THREE, TWO, ONE,
            ], cycles_per_beat=0.5)
            self._set_zone(BLUE, TWO | SIX)

        elif cue == CueByte.INTRO:
            # YALCY: Green ALL only
            self._set_zone(GREEN, ALL)

        elif cue == CueByte.MENU:
            # YALCY: Blue single-bit chase, TimedPattern 2.0s (not BPM)
            self._start_timed_pattern(BLUE, [
                ZERO, ONE, TWO, THREE, FOUR, FIVE, SIX, SEVEN,
            ], seconds=2.0)

        elif cue == CueByte.SCORE:
            # YALCY large: Red opposing-pair + Yellow opposing-pair, timed
            self._start_timed_pattern(RED, [
                SIX | TWO, ONE | FIVE, ZERO | FOUR, SEVEN | THREE,
            ], seconds=1.0)
            self._start_timed_pattern(YELLOW, [
                SIX | TWO, SEVEN | THREE, ZERO | FOUR, ONE | FIVE,
            ], seconds=2.0)

    def _start_beat_pattern(self, zone: int, pattern: list[int],
                            cycles_per_beat: float, listen: str | None = None):
        """Launch a beat-synced pattern loop on a zone.

        Args:
            listen: If "beat_any", advance on any beat (Measure or Strong)
                    instead of timed intervals.
        """
        task = asyncio.ensure_future(
            self._run_beat_pattern(zone, pattern, cycles_per_beat, listen)
        )
        self._active_tasks.append(task)

    async def _run_beat_pattern(self, zone: int, pattern: list[int],
                                cycles_per_beat: float, listen: str | None = None):
        """Beat-synced pattern loop."""
        idx = 0
        try:
            while True:
                self.zones[zone] = pattern[idx]
                idx = (idx + 1) % len(pattern)

                if listen == "beat_any":
                    # Advance on Measure or Strong beats
                    while True:
                        await self._beat_event.wait()
                        self._beat_event.clear()
                        if self._last_beat_type in (BeatByte.MEASURE, BeatByte.STRONG):
                            break
                else:
                    steps_per_beat = len(pattern) * cycles_per_beat
                    bpm = self.bpm if self.bpm > 0 else 120.0
                    seconds_per_beat = 60.0 / bpm
                    await asyncio.sleep(seconds_per_beat / steps_per_beat)
        except asyncio.CancelledError:
            pass

    def _start_timed_pattern(self, zone: int, pattern: list[int], seconds: float):
        """Launch a time-based (not BPM) pattern loop on a zone."""
        task = asyncio.ensure_future(
            self._run_timed_pattern(zone, pattern, seconds)
        )
        self._active_tasks.append(task)

    async def _run_timed_pattern(self, zone: int, pattern: list[int], seconds: float):
        """Time-based pattern loop (fixed period, independent of BPM)."""
        idx = 0
        interval = seconds / len(pattern)
        try:
            while True:
                self.zones[zone] = pattern[idx]
                idx = (idx + 1) % len(pattern)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    def _start_multi_zone_chase(self, zone_order: list[int], bits_per_zone: int,
                                 cycles_per_beat: float, listen: str | None = None):
        """Launch a rotating multi-zone chase where all zones fill the strip.

        Divides the 8 bits into segments assigned to each zone, then rotates
        the assignment so colors chase around the strip. Strip is always fully
        lit — no flashing.

        Args:
            zone_order: List of zone indices to use (e.g. [RED, BLUE] for 2-color).
            bits_per_zone: How many consecutive bits per zone (8 / len(zone_order)).
            cycles_per_beat: How many full rotations per beat.
            listen: If "beat_major", advance on major beats. If "keyframe",
                    advance on KeyframeNext events.
        """
        # Build rotation frames: 8 steps, shifting by 1 bit each step
        num_zones = len(zone_order)
        bit_values = [1 << i for i in range(8)]  # ZERO through SEVEN
        frames = []
        for shift in range(8):
            masks = {z: 0 for z in zone_order}
            for bit_pos in range(8):
                zone_idx = ((bit_pos + shift) // bits_per_zone) % num_zones
                masks[zone_order[zone_idx]] |= bit_values[bit_pos]
            frames.append(masks)

        task = asyncio.ensure_future(
            self._run_multi_zone_chase(zone_order, frames, cycles_per_beat, listen)
        )
        self._active_tasks.append(task)

    async def _run_multi_zone_chase(self, zone_order: list[int], frames: list[dict],
                                     cycles_per_beat: float, listen: str | None):
        """Run the multi-zone rotating chase."""
        idx = 0
        # Clear unused zones
        used = set(zone_order)
        for z in range(4):
            if z not in used:
                self.zones[z] = NONE

        try:
            while True:
                frame = frames[idx]
                for zone, mask in frame.items():
                    self.zones[zone] = mask
                idx = (idx + 1) % len(frames)

                if listen == "beat_major":
                    await self._beat_event.wait()
                    self._beat_event.clear()
                    if self._last_beat_type not in (BeatByte.MEASURE, BeatByte.STRONG):
                        continue
                elif listen == "keyframe":
                    await self._keyframe_event.wait()
                    self._keyframe_event.clear()
                    if self._last_keyframe_type != KeyframeByte.NEXT:
                        continue
                else:
                    steps_per_beat = len(frames) * cycles_per_beat
                    bpm = self.bpm if self.bpm > 0 else 120.0
                    await asyncio.sleep(60.0 / bpm / steps_per_beat)
        except asyncio.CancelledError:
            pass

    def _start_listen_pattern(self, zone: int, pattern: list[int], listen: str):
        """Launch an event-triggered pattern on a zone."""
        task = asyncio.ensure_future(
            self._run_listen_pattern(zone, pattern, listen)
        )
        self._active_tasks.append(task)

    async def _run_listen_pattern(self, zone: int, pattern: list[int], listen: str):
        """Event-triggered pattern."""
        idx = 0
        try:
            while True:
                if listen == "beat_major":
                    await self._beat_event.wait()
                    self._beat_event.clear()
                    if self._last_beat_type not in (BeatByte.MEASURE, BeatByte.STRONG):
                        continue
                elif listen == "keyframe":
                    await self._keyframe_event.wait()
                    self._keyframe_event.clear()
                    if self._last_keyframe_type != KeyframeByte.NEXT:
                        continue
                else:
                    await asyncio.sleep(0.05)
                    continue

                self.zones[zone] = pattern[idx]
                idx = (idx + 1) % len(pattern)
        except asyncio.CancelledError:
            pass
