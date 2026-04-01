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
            self._start_beat_pattern(RED, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN
            ], cycles_per_beat=0.25)
            self._start_beat_pattern(YELLOW, [
                TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR, THREE
            ], cycles_per_beat=0.125)

        elif cue == CueByte.COOL_AUTOMATIC:
            self._start_beat_pattern(BLUE, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN
            ], cycles_per_beat=0.25)
            self._start_beat_pattern(GREEN, [
                TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR, THREE
            ], cycles_per_beat=0.125)

        elif cue == CueByte.BIG_ROCK_ENDING:
            for i in range(4):
                self.zones[i] = ALL
            self._start_beat_pattern(RED, [ALL, NONE, NONE, NONE], cycles_per_beat=0.5)
            self._start_beat_pattern(YELLOW, [NONE, NONE, ALL, NONE], cycles_per_beat=0.5)
            self._start_beat_pattern(GREEN, [NONE, ALL, NONE, NONE], cycles_per_beat=0.5)
            self._start_beat_pattern(BLUE, [NONE, NONE, NONE, ALL], cycles_per_beat=0.5)

        elif cue == CueByte.FRENZY:
            self._start_beat_pattern(RED, [ALL, NONE], cycles_per_beat=0.5)
            self._start_beat_pattern(BLUE, [NONE, ALL], cycles_per_beat=0.5)
            self._start_beat_pattern(GREEN, [ALL, NONE, NONE, ALL], cycles_per_beat=0.25)
            self._start_beat_pattern(YELLOW, [NONE, ALL, ALL, NONE], cycles_per_beat=0.25)

        elif cue == CueByte.SEARCHLIGHTS:
            self._start_beat_pattern(RED, [
                ZERO, ONE, TWO, THREE, FOUR, FIVE, SIX, SEVEN
            ], cycles_per_beat=0.125)
            self._start_beat_pattern(BLUE, [
                FOUR, FIVE, SIX, SEVEN, ZERO, ONE, TWO, THREE
            ], cycles_per_beat=0.125)

        elif cue == CueByte.SWEEP:
            self._start_beat_pattern(BLUE, [
                ZERO, ONE, TWO, THREE, FOUR, FIVE, SIX, SEVEN,
                SEVEN, SIX, FIVE, FOUR, THREE, TWO, ONE, ZERO
            ], cycles_per_beat=0.0625)
            self._start_beat_pattern(GREEN, [
                FOUR, FIVE, SIX, SEVEN, ZERO, ONE, TWO, THREE,
                THREE, TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR
            ], cycles_per_beat=0.0625)

        elif cue == CueByte.HARMONY:
            self._start_beat_pattern(YELLOW, [
                THREE, TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR
            ], cycles_per_beat=0.125)
            self._start_beat_pattern(RED, [
                FOUR, THREE, TWO, ONE, ZERO, SEVEN, SIX, FIVE
            ], cycles_per_beat=0.125)

        elif cue == CueByte.FLARE_SLOW:
            self._start_listen_pattern(RED, [
                ZERO | ONE | TWO | THREE | FOUR | FIVE | SIX | SEVEN,
                ZERO | ONE | SIX | SEVEN,
                ZERO | SEVEN,
                NONE,
            ], listen="beat_major")
            self._set_zone(BLUE, ALL)

        elif cue == CueByte.FLARE_FAST:
            self._start_listen_pattern(YELLOW, [
                ALL, ZERO | ONE | SIX | SEVEN, ZERO | SEVEN, NONE
            ], listen="beat_major")
            self._set_zone(GREEN, ALL)

        elif cue == CueByte.SILHOUETTES or cue == CueByte.SILHOUETTES_SPOTLIGHT:
            self._set_zone(BLUE, ALL)

        elif cue == CueByte.DEFAULT or cue == CueByte.VERSE:
            self._start_beat_pattern(BLUE, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN
            ], cycles_per_beat=0.25)

        elif cue == CueByte.CHORUS:
            self._start_beat_pattern(RED, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN
            ], cycles_per_beat=0.25)
            self._set_zone(YELLOW, ALL)

        elif cue == CueByte.WARM_MANUAL or cue == CueByte.COOL_MANUAL:
            # Listen for next keyframe
            if cue == CueByte.WARM_MANUAL:
                self._start_listen_pattern(RED, [ALL, NONE], listen="keyframe")
            else:
                self._start_listen_pattern(BLUE, [ALL, NONE], listen="keyframe")

        elif cue == CueByte.STOMP:
            self._start_listen_pattern(RED, [ALL, NONE], listen="beat_major")
            self._start_listen_pattern(YELLOW, [NONE, ALL], listen="beat_major")

        elif cue == CueByte.DISCHORD:
            self._start_beat_pattern(RED, [ALL, NONE, NONE, NONE], cycles_per_beat=0.25)
            self._start_beat_pattern(BLUE, [NONE, NONE, ALL, NONE], cycles_per_beat=0.25)

        elif cue == CueByte.INTRO:
            self._set_zone(BLUE, ALL)
            self._set_zone(GREEN, ALL)

        elif cue == CueByte.MENU:
            self._start_beat_pattern(BLUE, [
                ZERO, ONE, TWO, THREE, FOUR, FIVE, SIX, SEVEN,
                SEVEN, SIX, FIVE, FOUR, THREE, TWO, ONE, ZERO
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
