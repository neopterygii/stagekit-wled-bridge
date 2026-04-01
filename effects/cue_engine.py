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
        """Launch the appropriate pattern primitives for a cue."""
        # Reset all zones
        for i in range(4):
            self.zones[i] = NONE

        if cue == CueByte.NO_CUE or cue == CueByte.BLACKOUT_FAST or \
           cue == CueByte.BLACKOUT_SLOW or cue == CueByte.BLACKOUT_SPOTLIGHT:
            return  # All off

        elif cue == CueByte.WARM_AUTOMATIC:
            # Two-bit wide chase for more strip coverage
            self._start_beat_pattern(RED, [
                ZERO | ONE | FOUR | FIVE,
                ONE | TWO | FIVE | SIX,
                TWO | THREE | SIX | SEVEN,
                THREE | FOUR | SEVEN | ZERO,
            ], cycles_per_beat=0.25)
            self._start_beat_pattern(YELLOW, [
                TWO | THREE, ONE | TWO, ZERO | ONE, SEVEN | ZERO,
                SIX | SEVEN, FIVE | SIX, FOUR | FIVE, THREE | FOUR,
            ], cycles_per_beat=0.125)

        elif cue == CueByte.COOL_AUTOMATIC:
            self._start_beat_pattern(BLUE, [
                ZERO | ONE | FOUR | FIVE,
                ONE | TWO | FIVE | SIX,
                TWO | THREE | SIX | SEVEN,
                THREE | FOUR | SEVEN | ZERO,
            ], cycles_per_beat=0.25)
            self._start_beat_pattern(GREEN, [
                TWO | THREE, ONE | TWO, ZERO | ONE, SEVEN | ZERO,
                SIX | SEVEN, FIVE | SIX, FOUR | FIVE, THREE | FOUR,
            ], cycles_per_beat=0.125)

        elif cue == CueByte.BIG_ROCK_ENDING:
            # 4 colors in rotating 2-bit segments — strip always fully lit
            self._start_multi_zone_chase([RED, GREEN, BLUE, YELLOW],
                                         bits_per_zone=2, cycles_per_beat=0.125)

        elif cue == CueByte.FRENZY:
            # 4 colors in rotating 2-bit segments, faster
            self._start_multi_zone_chase([RED, BLUE, GREEN, YELLOW],
                                         bits_per_zone=2, cycles_per_beat=0.25)

        elif cue == CueByte.SEARCHLIGHTS:
            # Two-bit wide searchlights
            self._start_beat_pattern(RED, [
                ZERO | ONE, ONE | TWO, TWO | THREE, THREE | FOUR,
                FOUR | FIVE, FIVE | SIX, SIX | SEVEN, SEVEN | ZERO,
            ], cycles_per_beat=0.125)
            self._start_beat_pattern(BLUE, [
                FOUR | FIVE, FIVE | SIX, SIX | SEVEN, SEVEN | ZERO,
                ZERO | ONE, ONE | TWO, TWO | THREE, THREE | FOUR,
            ], cycles_per_beat=0.125)

        elif cue == CueByte.SWEEP:
            # Three-bit wide sweep
            self._start_beat_pattern(BLUE, [
                ZERO | ONE | TWO, ONE | TWO | THREE, TWO | THREE | FOUR,
                THREE | FOUR | FIVE, FOUR | FIVE | SIX, FIVE | SIX | SEVEN,
                SIX | SEVEN | ZERO, SEVEN | ZERO | ONE,
                SEVEN | ZERO | ONE, SIX | SEVEN | ZERO, FIVE | SIX | SEVEN,
                FOUR | FIVE | SIX, THREE | FOUR | FIVE, TWO | THREE | FOUR,
                ONE | TWO | THREE, ZERO | ONE | TWO,
            ], cycles_per_beat=0.0625)
            self._start_beat_pattern(GREEN, [
                FOUR | FIVE | SIX, FIVE | SIX | SEVEN, SIX | SEVEN | ZERO,
                SEVEN | ZERO | ONE, ZERO | ONE | TWO, ONE | TWO | THREE,
                TWO | THREE | FOUR, THREE | FOUR | FIVE,
                THREE | FOUR | FIVE, TWO | THREE | FOUR, ONE | TWO | THREE,
                ZERO | ONE | TWO, SEVEN | ZERO | ONE, SIX | SEVEN | ZERO,
                FIVE | SIX | SEVEN, FOUR | FIVE | SIX,
            ], cycles_per_beat=0.0625)

        elif cue == CueByte.HARMONY:
            # Two-bit wide chase
            self._start_beat_pattern(YELLOW, [
                THREE | FOUR, TWO | THREE, ONE | TWO, ZERO | ONE,
                SEVEN | ZERO, SIX | SEVEN, FIVE | SIX, FOUR | FIVE,
            ], cycles_per_beat=0.125)
            self._start_beat_pattern(RED, [
                FOUR | FIVE, THREE | FOUR, TWO | THREE, ONE | TWO,
                ZERO | ONE, SEVEN | ZERO, SIX | SEVEN, FIVE | SIX,
            ], cycles_per_beat=0.125)

        elif cue == CueByte.FLARE_SLOW:
            # Outside-in collapse: edges → middle → gone
            self._start_listen_pattern(RED, [
                ALL,
                ONE | TWO | THREE | FOUR | FIVE | SIX,
                TWO | THREE | FOUR | FIVE,
                THREE | FOUR,
                NONE,
            ], listen="beat_major")
            self._set_zone(BLUE, ALL)

        elif cue == CueByte.FLARE_FAST:
            # Outside-in collapse
            self._start_listen_pattern(YELLOW, [
                ALL,
                ONE | TWO | THREE | FOUR | FIVE | SIX,
                TWO | THREE | FOUR | FIVE,
                THREE | FOUR,
                NONE,
            ], listen="beat_major")
            self._set_zone(GREEN, ALL)

        elif cue == CueByte.SILHOUETTES or cue == CueByte.SILHOUETTES_SPOTLIGHT:
            self._set_zone(BLUE, ALL)

        elif cue == CueByte.DEFAULT or cue == CueByte.VERSE:
            # Two-bit wide chase
            self._start_beat_pattern(BLUE, [
                ZERO | ONE | FOUR | FIVE,
                ONE | TWO | FIVE | SIX,
                TWO | THREE | SIX | SEVEN,
                THREE | FOUR | SEVEN | ZERO,
            ], cycles_per_beat=0.25)

        elif cue == CueByte.CHORUS:
            # Two-bit wide red chase over solid yellow
            self._start_beat_pattern(RED, [
                ZERO | ONE | FOUR | FIVE,
                ONE | TWO | FIVE | SIX,
                TWO | THREE | SIX | SEVEN,
                THREE | FOUR | SEVEN | ZERO,
            ], cycles_per_beat=0.25)
            self._set_zone(YELLOW, ALL)

        elif cue == CueByte.WARM_MANUAL or cue == CueByte.COOL_MANUAL:
            # Listen for next keyframe
            if cue == CueByte.WARM_MANUAL:
                self._start_listen_pattern(RED, [ALL, NONE], listen="keyframe")
            else:
                self._start_listen_pattern(BLUE, [ALL, NONE], listen="keyframe")

        elif cue == CueByte.STOMP:
            # Red and yellow in alternating 4-bit segments, swap on beat
            self._start_multi_zone_chase([RED, YELLOW],
                                         bits_per_zone=4, cycles_per_beat=0.25,
                                         listen="beat_major")

        elif cue == CueByte.DISCHORD:
            # Red and blue in alternating 4-bit segments, rotating
            self._start_multi_zone_chase([RED, BLUE],
                                         bits_per_zone=4, cycles_per_beat=0.25)

        elif cue == CueByte.INTRO:
            self._set_zone(BLUE, ALL)
            self._set_zone(GREEN, ALL)

        elif cue == CueByte.MENU:
            # Three-bit wide bounce
            self._start_beat_pattern(BLUE, [
                ZERO | ONE | TWO, ONE | TWO | THREE, TWO | THREE | FOUR,
                THREE | FOUR | FIVE, FOUR | FIVE | SIX, FIVE | SIX | SEVEN,
                SIX | SEVEN | ZERO, SEVEN | ZERO | ONE,
                SEVEN | ZERO | ONE, SIX | SEVEN | ZERO, FIVE | SIX | SEVEN,
                FOUR | FIVE | SIX, THREE | FOUR | FIVE, TWO | THREE | FOUR,
                ONE | TWO | THREE, ZERO | ONE | TWO,
            ], cycles_per_beat=0.0625)

        elif cue == CueByte.SCORE:
            for i in range(4):
                self.zones[i] = ALL

    def _start_beat_pattern(self, zone: int, pattern: list[int], cycles_per_beat: float):
        """Launch a beat-synced pattern loop on a zone."""
        task = asyncio.ensure_future(
            self._run_beat_pattern(zone, pattern, cycles_per_beat)
        )
        self._active_tasks.append(task)

    async def _run_beat_pattern(self, zone: int, pattern: list[int], cycles_per_beat: float):
        """Beat-synced pattern loop."""
        idx = 0
        try:
            while True:
                self.zones[zone] = pattern[idx]
                idx = (idx + 1) % len(pattern)
                steps_per_beat = len(pattern) * cycles_per_beat
                bpm = self.bpm if self.bpm > 0 else 120.0
                seconds_per_beat = 60.0 / bpm
                await asyncio.sleep(seconds_per_beat / steps_per_beat)
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
            listen: If "beat_major", advance on beats instead of timed.
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
