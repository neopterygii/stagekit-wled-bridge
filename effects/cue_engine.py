"""Stage Kit cue engine with per-pixel effects.

Translates YARG lighting cues + beat events into per-zone bitmask state
and an effects configuration dict that the mapper uses for per-pixel
post-processing (decay trails, breathing, sparkle, etc.).

Based on YALCY's StageKitTalker behavior, enhanced with LedFx/WLED-inspired
effects for a modern LED strip look.
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
    """Manages active lighting cue and produces zone bitmask state + effects."""

    def __init__(self):
        # Current zone bitmasks [red, green, blue, yellow]
        self.zones = [NONE, NONE, NONE, NONE]

        # Effects config dict consumed by the mapper each frame
        self.effects: dict = {}

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

        # Frame-level flags (set per-beat, consumed by mapper, cleared by engine)
        self._beat_flash = False
        self._glitch_trigger = False
        self._initial_flash_frames = 0

    def get_strobe_visible(self) -> bool:
        """Returns whether pixels should be visible (considering strobe)."""
        if self.strobe_rate == 0:
            return True
        return self._strobe_on

    def get_effects(self) -> dict:
        """Return current effects dict with transient flags, then clear them."""
        fx = dict(self.effects)
        fx["bpm"] = self.bpm
        fx["beat_flash"] = self._beat_flash
        fx["glitch_trigger"] = self._glitch_trigger
        fx["initial_flash"] = self._initial_flash_frames

        # Clear transient flags after consumption
        self._beat_flash = False
        self._glitch_trigger = False
        if self._initial_flash_frames > 0:
            self._initial_flash_frames -= 1

        return fx

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
            # Set transient flags for sparkle/glitch effects
            if beat_type in (BeatByte.MEASURE, BeatByte.STRONG):
                self._beat_flash = True
                self._glitch_trigger = True

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

    def _set_effects(self, **kwargs):
        """Set the effects dict for the current cue."""
        self.effects = dict(kwargs)

    def _launch_cue(self, cue: int):
        """Launch the appropriate pattern primitives for a cue.

        Each cue sets both zone bitmask patterns and an effects configuration
        that tells the mapper how to render the pixels. Effects include:
          - trails: decay trail length in frames
          - breathing: sine breathing rate (fraction of BPM)
          - sparkle: random white pixel density 0.0-1.0
          - sparkle_continuous: sparkle every frame vs beat-triggered
          - additive: use additive color blending for overlapping zones
          - glitch: probability of per-cell color inversion on beat
          - initial_flash: frames of white flash on cue activation
        """
        # Reset all zones and effects
        for i in range(4):
            self.zones[i] = NONE
        self.effects = {}
        self._initial_flash_frames = 0

        if cue == CueByte.NO_CUE or cue == CueByte.BLACKOUT_FAST or \
           cue == CueByte.BLACKOUT_SLOW or cue == CueByte.BLACKOUT_SPOTLIGHT:
            return  # All off

        elif cue == CueByte.DEFAULT:
            # Blue/Red alternating toggle on KeyframeNext
            self._set_effects(trails=3)
            self._set_zone(BLUE, ALL)
            self._start_listen_pattern(BLUE, [NONE, ALL], listen="keyframe")
            self._start_listen_pattern(RED, [ALL, NONE], listen="keyframe")

        elif cue == CueByte.VERSE:
            # Ambient breathing wash — "settle in" moment
            # Full blue wash with slow sine breathing like WLED Breathe
            self._set_effects(breathing=0.25, trails=10)
            self._set_zone(BLUE, ALL)

        elif cue == CueByte.CHORUS:
            # Peak energy — BPM-synced chase with beat sparkles
            # Red chase + yellow solid base + sparkle on downbeats
            self._set_effects(trails=5, sparkle=0.10)
            self._start_beat_pattern(RED, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN,
            ], cycles_per_beat=0.25)
            self._set_zone(YELLOW, ALL)

        elif cue == CueByte.WARM_MANUAL:
            # Warm mood — smooth scanner trails
            self._set_effects(trails=8)
            self._start_beat_pattern(RED, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN,
            ], cycles_per_beat=0.25)
            self._start_beat_pattern(YELLOW, [
                TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR, THREE,
            ], cycles_per_beat=0.125)

        elif cue == CueByte.COOL_MANUAL:
            # Cool mood — smooth scanner trails
            self._set_effects(trails=8)
            self._start_beat_pattern(BLUE, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN,
            ], cycles_per_beat=0.25)
            self._start_beat_pattern(GREEN, [
                TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR, THREE,
            ], cycles_per_beat=0.125)

        elif cue == CueByte.WARM_AUTOMATIC:
            # Red opposing-pair chase + Yellow CCW accent with scanner trails
            self._set_effects(trails=8)
            self._start_beat_pattern(RED, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN,
            ], cycles_per_beat=0.25)
            self._start_beat_pattern(YELLOW, [
                TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR, THREE,
            ], cycles_per_beat=0.125)

        elif cue == CueByte.COOL_AUTOMATIC:
            # Blue opposing-pair chase + Green CCW accent with scanner trails
            self._set_effects(trails=8)
            self._start_beat_pattern(BLUE, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN,
            ], cycles_per_beat=0.25)
            self._start_beat_pattern(GREEN, [
                TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR, THREE,
            ], cycles_per_beat=0.125)

        elif cue == CueByte.BIG_ROCK_ENDING:
            # Chaotic climax — fast rotating chase + beat sparkles
            # 4-color rotating chase with 10% sparkle on downbeats
            self._set_effects(trails=4, sparkle=0.10)
            self._start_multi_zone_chase([RED, GREEN, BLUE, YELLOW],
                                         bits_per_zone=2, cycles_per_beat=0.5)

        elif cue == CueByte.FRENZY:
            # Barely controlled chaos — fast chase + dense sparkles +
            # random direction reversals
            self._set_effects(trails=3, sparkle=0.20)
            self._start_multi_zone_chase([RED, BLUE, YELLOW],
                                         bits_per_zone=2, cycles_per_beat=1.0,
                                         reverse_on_beat=True)

        elif cue == CueByte.SEARCHLIGHTS:
            # Searchlight beams — single-bit chase with comet trails
            # Yellow CW + Blue CCW, long trailing decay
            self._set_effects(trails=12, additive=True)
            self._start_beat_pattern(YELLOW, [
                TWO, THREE, FOUR, FIVE, SIX, SEVEN, ZERO, ONE,
            ], cycles_per_beat=0.5)
            self._start_beat_pattern(BLUE, [
                ZERO, SEVEN, SIX, FIVE, FOUR, THREE, TWO, ONE,
            ], cycles_per_beat=0.5)

        elif cue == CueByte.SWEEP:
            # Smooth red sweep with trailing decay
            self._set_effects(trails=10)
            self._start_beat_pattern(RED, [
                SIX | TWO, FIVE | ONE, FOUR | ZERO, THREE | SEVEN,
            ], cycles_per_beat=0.25)

        elif cue == CueByte.HARMONY:
            # Blending counter-rotation — additive overlap = color mixing
            # Yellow + Red counter-rotating with additive blend and trails
            self._set_effects(trails=10, additive=True)
            self._start_beat_pattern(YELLOW, [
                THREE, TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR,
            ], cycles_per_beat=0.125)
            self._start_beat_pattern(RED, [
                FOUR, THREE, TWO, ONE, ZERO, SEVEN, SIX, FIVE,
            ], cycles_per_beat=0.125)

        elif cue == CueByte.FLARE_SLOW:
            # Explosion settling into gentle pulse — initial white flash
            # then all 4 zones breathing slowly (additive blend → white)
            self._set_effects(additive=True, breathing=0.10)
            self._initial_flash_frames = 4
            for i in range(4):
                self.zones[i] = ALL

        elif cue == CueByte.FLARE_FAST:
            # Quick blue flare with faster breathing
            self._set_effects(additive=True, breathing=0.5)
            self._initial_flash_frames = 3
            self._set_zone(BLUE, ALL)

        elif cue == CueByte.SILHOUETTES:
            # Slow ambient green breathing
            self._set_effects(breathing=0.05)
            self._set_zone(GREEN, ALL)

        elif cue == CueByte.SILHOUETTES_SPOTLIGHT:
            # Slightly faster green breathing for spotlight variant
            self._set_effects(breathing=0.08)
            self._set_zone(GREEN, ALL)

        elif cue == CueByte.STOMP:
            # Percussive hits — keyframe-triggered chase + beat flash overlay
            self._set_effects(trails=4, sparkle=0.15)
            self._start_multi_zone_chase([RED, GREEN, YELLOW],
                                         bits_per_zone=2, cycles_per_beat=0.25,
                                         listen="keyframe")

        elif cue == CueByte.DISCHORD:
            # Tension/chaos — counter-rotating chases + random glitches
            self._set_effects(trails=6, glitch=0.25)
            self._start_beat_pattern(YELLOW, [
                ZERO, ONE, TWO, THREE, FOUR, FIVE, SIX, SEVEN,
            ], cycles_per_beat=0.125, listen="beat_any")
            self._start_beat_pattern(GREEN, [
                ZERO, SEVEN, SIX, FIVE, FOUR, THREE, TWO, ONE,
            ], cycles_per_beat=0.5)
            self._set_zone(BLUE, TWO | SIX)

        elif cue == CueByte.INTRO:
            # Green ambient breathing (same as Silhouettes but distinct cue)
            self._set_effects(breathing=0.05)
            self._set_zone(GREEN, ALL)

        elif cue == CueByte.MENU:
            # Polished idle — blue scanner with long comet trail
            self._set_effects(trails=14)
            self._start_timed_pattern(BLUE, [
                ZERO, ONE, TWO, THREE, FOUR, FIVE, SIX, SEVEN,
            ], seconds=2.0)

        elif cue == CueByte.SCORE:
            # Victory celebration — timed chase + continuous confetti sparkle
            self._set_effects(trails=6, sparkle=0.05, sparkle_continuous=True)
            self._start_timed_pattern(RED, [
                SIX | TWO, ONE | FIVE, ZERO | FOUR, SEVEN | THREE,
            ], seconds=1.0)
            self._start_timed_pattern(YELLOW, [
                SIX | TWO, SEVEN | THREE, ZERO | FOUR, ONE | FIVE,
            ], seconds=2.0)

    def _start_beat_pattern(self, zone: int, pattern: list[int],
                            cycles_per_beat: float, listen: str | None = None):
        """Launch a beat-synced pattern loop on a zone."""
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
                                 cycles_per_beat: float, listen: str | None = None,
                                 reverse_on_beat: bool = False):
        """Launch a rotating multi-zone chase where all zones fill the strip.

        Args:
            zone_order: Zone indices to use.
            bits_per_zone: Consecutive bits per zone.
            cycles_per_beat: Full rotations per beat.
            listen: Event trigger mode.
            reverse_on_beat: If True, randomly reverse direction on every 4th beat.
        """
        num_zones = len(zone_order)
        bit_values = [1 << i for i in range(8)]
        frames = []
        for shift in range(8):
            masks = {z: 0 for z in zone_order}
            for bit_pos in range(8):
                zone_idx = ((bit_pos + shift) // bits_per_zone) % num_zones
                masks[zone_order[zone_idx]] |= bit_values[bit_pos]
            frames.append(masks)

        task = asyncio.ensure_future(
            self._run_multi_zone_chase(zone_order, frames, cycles_per_beat,
                                       listen, reverse_on_beat)
        )
        self._active_tasks.append(task)

    async def _run_multi_zone_chase(self, zone_order: list[int], frames: list[dict],
                                     cycles_per_beat: float, listen: str | None,
                                     reverse_on_beat: bool):
        """Run the multi-zone rotating chase."""
        idx = 0
        direction = 1
        beat_count = 0

        used = set(zone_order)
        for z in range(4):
            if z not in used:
                self.zones[z] = NONE

        try:
            while True:
                frame = frames[idx]
                for zone, mask in frame.items():
                    self.zones[zone] = mask
                idx = (idx + direction) % len(frames)

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

                # Random direction reversal for Frenzy-style chaos
                if reverse_on_beat:
                    beat_count += 1
                    if beat_count % 4 == 0:
                        direction = -direction
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
