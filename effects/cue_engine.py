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

# Animation durations in seconds (wall-clock, independent of render FPS)
REVEAL_DURATION = 1.5        # Intro center-out reveal
BONUS_BURST_DURATION = 0.25  # bonus_effect white celebration flash


class _TimePattern:
    """Time-driven zone pattern ticked deterministically by the render thread.

    Instead of asyncio.sleep() between steps, the current step is computed
    from wall-clock time — immune to event-loop congestion.
    """
    __slots__ = ('steps', 'step_dicts', 'step', 'next_time', 'bpm_sync',
                 'param', 'direction', 'reverse_on_beat', 'reverse_counter')

    def __init__(self, steps, *, bpm_sync, param, now, init_bpm=120.0,
                 direction=1, reverse_on_beat=False):
        self.steps = steps           # list of list[(zone, mask)]
        # Per-step {zone: mask} lookups, precomputed so tick() doesn't
        # rebuild a dict per pattern per frame during interpolation.
        self.step_dicts = [dict(s) for s in steps]
        self.step = 0                # current step index
        self.bpm_sync = bpm_sync     # True → param is cycles_per_beat
        self.param = param           # cycles_per_beat (bpm) or total_seconds (timed)
        self.direction = direction
        self.reverse_on_beat = reverse_on_beat
        self.reverse_counter = 0     # counts steps for reversal timing
        # Schedule first transition one interval in the future so step 0
        # is visible for the correct duration on the very first tick.
        self.next_time = now + self.step_interval(init_bpm)

    def step_interval(self, bpm: float) -> float:
        """Seconds per step at the given BPM."""
        n = len(self.steps)
        if self.bpm_sync:
            effective_bpm = bpm if bpm > 0 else 120.0
            return 60.0 / effective_bpm / (n * self.param)
        return self.param / n


class CueEngine:
    """Manages active lighting cue and produces zone bitmask state + effects."""

    def __init__(self):
        # Current zone bitmasks [red, green, blue, yellow]
        self.zones = [NONE, NONE, NONE, NONE]

        # Effects config dict consumed by the mapper each frame
        self.effects: dict = {}

        # Strobe state
        self.strobe_rate = 0  # Hz, 0 = off

        # BPM from YARG
        self.bpm = 120.0

        # Active primitives (asyncio tasks for event-driven patterns only)
        self._active_tasks: list[asyncio.Task] = []

        # Time-driven patterns (ticked from render thread)
        self._time_patterns: list[_TimePattern] = []

        # Beat/keyframe event for listen patterns
        self._beat_event = asyncio.Event()
        self._keyframe_event = asyncio.Event()
        self._last_beat_type = BeatByte.OFF
        self._last_keyframe_type = KeyframeByte.OFF
        self._last_drum_notes = 0

        # Current cue ID
        self._current_cue = CueByte.NO_CUE
        # Wall-clock time of the most recent cue change — read by the
        # render thread to drive the cross-fade between cues.
        self._cue_change_at: float = 0.0

        # Frame-level flags (set per-beat, consumed by mapper, cleared by engine)
        self._beat_flash = False
        self._downbeat_flash = False    # MEASURE only — stronger accent
        self._glitch_trigger = False
        self._initial_flash_frames = 0
        # One-shot reveal (Intro): monotonic start time, 0.0 = inactive.
        self._reveal_started_at = 0.0
        # YARG bonus_effect celebration burst: monotonic deadline, 0.0 = inactive.
        self._bonus_until = 0.0

        # Star power (v4 datagram). Sustained state — unlike the one-shot
        # bonus burst — updated every packet and surfaced to the mapper's
        # "tasteful surge" overlay. sp_active drives the surge, sp_charge the
        # pre-activation glow.
        self._sp_active = False
        self._sp_amount = 0.0        # max among active players, 0..1
        self._sp_charge = 0.0        # max among all players, 0..1
        self._sp_active_count = 0

        # Pause state (frozen patterns + global dim). While paused, animation
        # clocks read _paused_at instead of now; on unpause, active deadlines
        # are shifted forward by the pause duration so nothing is consumed.
        self.paused = False
        self._paused_at = 0.0

        # Per-zone, per-cell brightness 0.0..1.0. Computed by tick() with
        # sub-cell interpolation between consecutive pattern steps so
        # scanner/comet movement isn't quantised to 8 hops across the strip.
        self.zone_cell_levels: list[list[float]] = [[0.0] * 8 for _ in range(4)]

    def get_strobe_visible(self) -> bool:
        """Returns whether pixels should be visible (considering strobe).

        Computed from wall-clock time so the render thread gets accurate
        strobe phase without depending on an asyncio coroutine.
        """
        if self.strobe_rate == 0:
            return True
        period = 1.0 / self.strobe_rate
        return (time.monotonic() % period) < (period / 2)

    def get_effects(self) -> dict:
        """Return current effects dict with transient flags, then clear them."""
        # While paused, animation clocks freeze at the pause instant.
        now = self._paused_at if self.paused else time.monotonic()
        fx = dict(self.effects)
        fx["bpm"] = self.bpm
        fx["beat_flash"] = self._beat_flash
        fx["downbeat_flash"] = self._downbeat_flash
        fx["glitch_trigger"] = self._glitch_trigger
        fx["initial_flash"] = self._initial_flash_frames
        fx["reveal_progress"] = self._reveal_progress(now)
        fx["bonus_t"] = self._bonus_remaining(now)
        fx["paused"] = self.paused
        fx["cue_change_at"] = self._cue_change_at
        fx["sp_active"] = self._sp_active
        fx["sp_amount"] = self._sp_amount
        fx["sp_charge"] = self._sp_charge
        fx["sp_active_count"] = self._sp_active_count

        # Clear transient flags after consumption
        self._beat_flash = False
        self._downbeat_flash = False
        self._glitch_trigger = False
        if self._initial_flash_frames > 0:
            self._initial_flash_frames -= 1

        return fx

    def _reveal_progress(self, now: float) -> float:
        """Intro reveal progress 0.0..1.0; 1.0 = finished or inactive."""
        if self._reveal_started_at <= 0.0:
            return 1.0
        progress = (now - self._reveal_started_at) / REVEAL_DURATION
        return min(1.0, max(0.0, progress))

    def _bonus_remaining(self, now: float) -> float:
        """Bonus burst intensity 1.0 → 0.0; 0.0 = expired or inactive."""
        if self._bonus_until <= 0.0:
            return 0.0
        remaining = self._bonus_until - now
        if remaining <= 0.0:
            return 0.0
        return min(1.0, remaining / BONUS_BURST_DURATION)

    def tick(self, now: float):
        """Advance all time-based patterns to the current timestamp.

        Called from the render thread each frame.  Zone bitmasks AND the
        per-cell brightness levels (zone_cell_levels) are set
        deterministically from wall-clock time — immune to asyncio lag.

        Sub-cell motion: each pattern interpolates between its current and
        next step based on the fractional progress through the step
        interval. So instead of cells popping on/off in 8 discrete jumps,
        a moving bit fades out of one cell while fading into the next —
        the LedFx-style smooth scanner look.

        If the deadline is far in the past (host suspend, container pause,
        long GC), snap forward instead of catching up step-by-step — that
        would block the render thread and emit a flurry of stale frames.

        Pause handling: when self.paused is True, freeze pattern motion
        (don't advance steps or interpolate). The bitmask→levels reset
        still runs so cue changes and web test patterns made while paused
        reach the strip. The mapper applies the brightness dim separately.
        """
        bpm = self.bpm
        levels = self.zone_cell_levels

        # Reset levels from the current bitmask state. Patterns will
        # overwrite the cells they own with interpolated values; zones set
        # by static cue setup (no pattern) stay at their bitmask values.
        for zone_idx in range(4):
            mask = self.zones[zone_idx]
            row = levels[zone_idx]
            for cell in range(8):
                row[cell] = 1.0 if (mask >> cell) & 1 else 0.0

        if self.paused:
            return

        patterns = self._time_patterns  # local ref — safe across threads
        for p in patterns:
            interval = p.step_interval(bpm)
            # Snap forward if we've fallen more than 1s (or 100 steps) behind.
            max_catchup = max(100, int(1.0 / interval)) if interval > 0 else 100
            if interval > 0 and (now - p.next_time) > max(1.0, interval * max_catchup):
                p.next_time = now + interval
            while now >= p.next_time:
                p.step = (p.step + p.direction) % len(p.steps)
                p.next_time += interval
                if p.reverse_on_beat:
                    p.reverse_counter += 1
                    if p.reverse_counter % 4 == 0:
                        p.direction = -p.direction

            # Fractional progress (0.0 at step start, 1.0 at next-step boundary).
            if interval > 0:
                progress = 1.0 - (p.next_time - now) / interval
                if progress < 0.0:
                    progress = 0.0
                elif progress > 1.0:
                    progress = 1.0
            else:
                progress = 0.0

            cur_step = p.steps[p.step]
            nxt_idx = (p.step + p.direction) % len(p.steps)
            nxt_zones = p.step_dicts[nxt_idx]

            inv_p = 1.0 - progress
            for zone, cur_mask in cur_step:
                nxt_mask = nxt_zones.get(zone, 0)
                self.zones[zone] = cur_mask
                row = levels[zone]
                for cell in range(8):
                    cur_on = (cur_mask >> cell) & 1
                    nxt_on = (nxt_mask >> cell) & 1
                    # Linear blend between current step's bit and next step's bit
                    row[cell] = cur_on * inv_p + nxt_on * progress

    def on_beat(self, beat_type: int):
        """Called when a beat event arrives from YARG.

        MEASURE = downbeat (start of a measure) — most prominent.
        STRONG  = strong beat within the measure.
        WEAK    = sub-beat — fires _beat_event so listen-patterns nudge,
                  but no sparkle/glitch overlay (would feel cluttered).
        """
        self._last_beat_type = beat_type
        if beat_type == BeatByte.OFF:
            return
        self._beat_event.set()
        if beat_type == BeatByte.MEASURE:
            self._beat_flash = True
            self._downbeat_flash = True
            self._glitch_trigger = True
        elif beat_type == BeatByte.STRONG:
            self._beat_flash = True
            self._glitch_trigger = True
        # WEAK: event already set; no flash/glitch on the off-beats.

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

    def on_bonus(self):
        """Called when YARG flags a bonus_effect — celebration burst."""
        # Mapper renders a white-tinted flash that decays over the duration.
        self._bonus_until = time.monotonic() + BONUS_BURST_DURATION

    def on_star_power(self, active: bool, amount: float, charge: float,
                      active_count: int):
        """Update per-frame star-power state (v4 datagram).

        Sustained, not a one-shot: the mapper reads these each frame to render
        the "charging" glow (charge, while no player is active) and the surge
        (active + amount). Purely stored here — no pattern/clock changes — so
        it composites over whatever cue is running.
        """
        self._sp_active = active
        self._sp_amount = amount
        self._sp_charge = charge
        self._sp_active_count = active_count

    def on_paused(self, paused: bool):
        """Freeze pattern motion; mapper handles the global dim."""
        if paused == self.paused:
            return
        if paused:
            self._paused_at = time.monotonic()
        else:
            # Shift active animation deadlines forward by the pause duration
            # so reveal/bonus resume where they froze instead of being spent.
            delta = time.monotonic() - self._paused_at
            if self._reveal_started_at > 0.0:
                self._reveal_started_at += delta
            if self._bonus_until > 0.0:
                self._bonus_until += delta
        self.paused = paused

    def on_cue(self, cue_byte: int):
        """Called when the lighting cue changes."""
        if cue_byte == self._current_cue:
            return
        self._current_cue = cue_byte
        self._cue_change_at = time.monotonic()
        self._kill_primitives()
        self._launch_cue(cue_byte)

    def _kill_primitives(self):
        """Cancel all active pattern tasks and time-driven patterns."""
        for task in self._active_tasks:
            task.cancel()
        self._active_tasks.clear()
        self._time_patterns = []  # atomic reference swap — safe for render thread

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
        self._reveal_started_at = 0.0  # cancel an in-flight Intro reveal

        if cue == CueByte.NO_CUE or cue == CueByte.BLACKOUT_FAST or \
           cue == CueByte.BLACKOUT_SLOW:
            return  # All off

        elif cue == CueByte.BLACKOUT_SPOTLIGHT:
            # Single warm-white spotlight in the strip center, everything
            # else dark. The mapper paints the spotlight when it sees
            # spotlight_only=(r,g,b) with a spotlight_region < 1.0.
            self._set_effects(
                spotlight_only=(255, 200, 140),
                spotlight_region=0.18,
            )
            return

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
            # Warm mood — manual cue: pattern only steps on KEYFRAME events
            # from the chart (instead of running procedurally on BPM). Gives
            # the chart-author control over rhythm of the scanner.
            self._set_effects(trails=8)
            self._start_beat_pattern(RED, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN,
            ], cycles_per_beat=0.25, listen="keyframe")
            self._start_beat_pattern(YELLOW, [
                TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR, THREE,
            ], cycles_per_beat=0.125, listen="keyframe")

        elif cue == CueByte.COOL_MANUAL:
            # Cool mood — manual cue: keyframe-stepped scanner.
            self._set_effects(trails=8)
            self._start_beat_pattern(BLUE, [
                ZERO | FOUR, ONE | FIVE, TWO | SIX, THREE | SEVEN,
            ], cycles_per_beat=0.25, listen="keyframe")
            self._start_beat_pattern(GREEN, [
                TWO, ONE, ZERO, SEVEN, SIX, FIVE, FOUR, THREE,
            ], cycles_per_beat=0.125, listen="keyframe")

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
            # Slow ambient green breathing across the full strip
            self._set_effects(breathing=0.05)
            self._set_zone(GREEN, ALL)

        elif cue == CueByte.SILHOUETTES_SPOTLIGHT:
            # Same breathing green, but constrained to a spotlight region
            # in the middle of the strip — the rest stays dark, evoking
            # a single performer lit on a darkened stage.
            self._set_effects(breathing=0.08, spotlight_region=0.40)
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
            # Reveal animation: pixels light up sequentially from the
            # center outward over REVEAL_DURATION seconds, then settle
            # into the green breathing wash. The mapper masks pixels
            # beyond the radius given by reveal_progress.
            self._set_effects(breathing=0.05)
            self._reveal_started_at = time.monotonic()
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
        """Launch a beat-synced pattern loop on a zone.

        When *listen* is None the pattern is time-driven and ticked from the
        render thread (immune to event-loop congestion).  Event-driven
        patterns still use asyncio tasks.
        """
        if listen is not None:
            task = asyncio.ensure_future(
                self._run_beat_pattern(zone, pattern, cycles_per_beat, listen)
            )
            self._active_tasks.append(task)
            return

        steps = [[(zone, mask)] for mask in pattern]
        now = time.monotonic()
        self._time_patterns.append(_TimePattern(
            steps, bpm_sync=True, param=cycles_per_beat,
            now=now, init_bpm=self.bpm,
        ))

    async def _run_beat_pattern(self, zone: int, pattern: list[int],
                                cycles_per_beat: float, listen: str | None = None):
        """Beat-synced pattern loop (event-driven only — kept for listen modes)."""
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
                elif listen == "keyframe":
                    # Step on YARG NEXT keyframes — used by the *_MANUAL
                    # cues so the chart drives the rhythm — but fall back
                    # to BPM pacing when keyframes stop arriving so the
                    # pattern never freezes on charts without them.
                    steps_per_beat = len(pattern) * cycles_per_beat
                    bpm = self.bpm if self.bpm > 0 else 120.0
                    timeout = 2.0 * 60.0 / bpm / steps_per_beat
                    while True:
                        try:
                            await asyncio.wait_for(
                                self._keyframe_event.wait(), timeout)
                        except asyncio.TimeoutError:
                            if self.paused:
                                continue  # frozen — keep waiting
                            break  # no keyframes — step on BPM cadence
                        self._keyframe_event.clear()
                        if self._last_keyframe_type == KeyframeByte.NEXT:
                            break
                else:
                    steps_per_beat = len(pattern) * cycles_per_beat
                    bpm = self.bpm if self.bpm > 0 else 120.0
                    seconds_per_beat = 60.0 / bpm
                    await asyncio.sleep(seconds_per_beat / steps_per_beat)
        except asyncio.CancelledError:
            pass

    def _start_timed_pattern(self, zone: int, pattern: list[int], seconds: float):
        """Launch a fixed-period pattern (not BPM-synced), ticked from render thread."""
        steps = [[(zone, mask)] for mask in pattern]
        now = time.monotonic()
        self._time_patterns.append(_TimePattern(
            steps, bpm_sync=False, param=seconds, now=now,
        ))

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

        if listen is not None:
            task = asyncio.ensure_future(
                self._run_multi_zone_chase(zone_order, frames, cycles_per_beat,
                                           listen, reverse_on_beat)
            )
            self._active_tasks.append(task)
            return

        # Time-based — store for render-thread ticking
        steps = [list(f.items()) for f in frames]
        used = set(zone_order)
        for z in range(4):
            if z not in used:
                self.zones[z] = NONE
        now = time.monotonic()
        self._time_patterns.append(_TimePattern(
            steps, bpm_sync=True, param=cycles_per_beat,
            now=now, init_bpm=self.bpm,
            reverse_on_beat=reverse_on_beat,
        ))

    async def _run_multi_zone_chase(self, zone_order: list[int], frames: list[dict],
                                     cycles_per_beat: float, listen: str | None,
                                     reverse_on_beat: bool):
        """Run the multi-zone rotating chase (event-driven only)."""
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
