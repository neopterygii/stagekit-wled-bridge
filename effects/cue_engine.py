"""Stage Kit cue engine with per-pixel effects.

Translates YARG lighting cues + beat events into per-zone bitmask state
and an effects configuration dict that the mapper uses for per-pixel
post-processing (decay trails, breathing, sparkle, etc.).

Based on YALCY's StageKitTalker behavior, enhanced with LedFx/WLED-inspired
effects for a modern LED strip look.
"""

import asyncio
import time

from protocol.yarg_packet import (
    CueByte, BeatByte, KeyframeByte, StrobeSpeed, CameraCutPriority,
    VenueSizeByte, SongSectionByte)
from effects.gradient import GRADIENTS

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

# Strobe rates in Hz — fallback when BPM is unknown (0).
STROBE_RATES = {
    StrobeSpeed.OFF: 0,
    StrobeSpeed.SLOW: 2,
    StrobeSpeed.MEDIUM: 4,
    StrobeSpeed.FAST: 8,
    StrobeSpeed.FASTEST: 16,
}

# Tempo-locked strobe (YALCY StrobeDmxFromBpm: hz = bpm * speed / 60, where
# speed is flashes per beat). Each speed byte maps to a note division — the
# rate is derived live from the current BPM so a tempo change retunes the
# strobe. The divisions are chosen so that at the 120 BPM default they
# reproduce the fixed fallback rates exactly (2/4/8/16 Hz).
STROBE_DIVISIONS = {
    StrobeSpeed.SLOW: 1,     # quarter note
    StrobeSpeed.MEDIUM: 2,   # eighth note
    StrobeSpeed.FAST: 4,     # sixteenth note
    StrobeSpeed.FASTEST: 8,  # thirty-second note
}

# Animation durations in seconds (wall-clock, independent of render FPS)
REVEAL_DURATION = 1.5        # Intro center-out reveal
BONUS_BURST_DURATION = 0.25  # bonus_effect white celebration flash

# ── Camera-cut lighting (VISION Phase 5) ─────────────────────────
CAMERA_CUT_DURATION = 0.28   # seconds — one-shot accent decay on a *directed* cut
CAMERA_EASE = 0.30           # seconds — subject bias fades in over this after a cut

# ── Blur post-process (VISION Phase 6) ───────────────────────────
# The engine emits the blur *strength* (0..1 wet mix; the mapper owns the kernel
# width). A light base is always on so discrete cue events read as smooth stage
# light; venue fog/haze (parsed at offset 36) lifts it toward a soft blur-glow
# floor while the haze is up.
BLUR_BASE = 0.35             # wet mix applied every frame (0 = crisp, 1 = full blur)
BLUR_FOG_BOOST = 0.30        # extra wet mix while fog_state is on

# Note-hold (VISION Phase 4). A YARG note bitmask edge (a fret/pad hit, offsets
# 14-17) seeds a per-instrument accent that holds for at least a 1/32 note so a
# hit living in a single ~88 Hz packet still reads on the strip, then decays.
# 1/32 note = 1/8 of a beat; the floor keeps it visible at very fast tempo.
NOTE_HOLD_BEATS = 0.125      # 1/32 note = 1/8 beat
NOTE_HOLD_FLOOR = 0.045      # seconds — minimum decay-tail length regardless of BPM

# Anti-strobe cap. An isolated hit gets a crisp short accent; but a fast run
# (hits closer together than this) would restart that flash on every hit and
# read as a strobe. When a rising edge lands within NOTE_REFRESH_MIN of the
# previous one, we instead *sustain* the lane as a steady glow — hold it lit
# past the next expected hit and freeze the level — so the accent can't flash
# faster than ~1/NOTE_REFRESH_MIN (≈7 Hz), well under the photosensitive band.
NOTE_REFRESH_MIN = 0.14      # seconds — ~7 Hz max re-flash cadence

# Beat-lock phase-locked loop (drives BPM-synced chase motion onto the beat).
# The pattern free-runs on tempo, then each frame is pulled a fraction
# min(1, dt/PLL_TAU) of the way toward the beat-locked target — a smooth
# exponential correction (no snap). Small TAU = tight lock; the correction is
# clamped so motion never runs backwards (it hesitates instead). Locking is
# only applied while beats are fresh; after BEAT_LOCK_TIMEOUT with no beat the
# pattern free-runs on tempo alone (the fallback).
PLL_TAU = 0.10               # seconds — lock time-constant
BEAT_LOCK_TIMEOUT = 2.0      # seconds without a beat → free-run fallback
PLL_DT_MAX = 0.2             # clamp per-frame dt so a stall can't fling motion

# ── Venue-size density branching (VISION signal inventory) ──────
# YALCY branches per cue on the venue-size byte (offset 8): Large venues get
# denser multi-pattern variants, anything else sparser ones. We do the same
# generically instead of hand-authoring 33 per-cue variants: a mask transform
# applied to chase patterns at cue launch, plus a sparkle-density scale read
# per frame. NoVenue/unknown keeps the authored look exactly (scale 1.0, no
# transform) — unlike YALCY, which treats NoVenue as small.
SPARKLE_SCALE_SMALL = 0.5    # small venue → sparser sparkle field
SPARKLE_SCALE_LARGE = 1.5    # large venue → denser sparkle field

# ── Song-section palette/energy bias (VISION signal inventory) ───
# YARG's section byte (offset 13) carries the last Verse/Chorus lighting
# event (LightingType values — see SongSectionByte). Each section gets a slow,
# subtle bias that modulates the current look without repainting it: a hue
# lean (the mapper blends lit pixels a little toward it) and an energy scale
# applied to the breathing swing and the beat-pump depth — a verse settles,
# a chorus lifts. The knobs are resolved at signal-change time (see
# on_song_section); the bias eases in over SECTION_EASE so a section change
# drifts rather than snaps. Sections not in the table (None/unknown) are
# identity — the authored look stays bit-exact.
SECTION_EASE = 1.0             # seconds — bias fade-in after a section change
SECTION_BIAS = {
    SongSectionByte.VERSE:  ((70, 110, 255), 0.85),   # cooler, settled
    SongSectionByte.CHORUS: ((255, 170, 70), 1.20),   # warmer, lifted
}

# StageKit addressing: every zone bitmask is 8 bits → 8 cells on the ring.
_CELLS = 8


def _thin_opposites(mask: int) -> int:
    """Sparser variant: collapse each opposite pair {i, i+4} to one bit.

    The authored two-bit chase steps are all opposite pairs (ZERO|FOUR,
    ONE|FIVE, …); thinning keeps the lower member of each pair, turning them
    into single-head chases — the same rotation, half the lit cells. Single-bit
    steps pass through unchanged, and the transform is idempotent.
    """
    low = mask & 0x0F
    high = (mask >> 4) & 0x0F
    return low | ((high & ~low) << 4)


def _fill_opposites(mask: int) -> int:
    """Denser variant: light the opposite cell of every lit cell.

    Single-head chases (SEARCHLIGHTS, HARMONY, MENU) become opposing-pair
    chases. Steps that already light opposite pairs (most authored chases)
    and ALL pass through unchanged — the transform is idempotent.
    """
    return mask | ((mask << 4) & 0xF0) | (mask >> 4)


def _venue_safe(mask: int) -> bool:
    """Whether the density transforms are well-defined on this step mask.

    Both transforms only carry their intended "half/double the lit cells"
    meaning when a step is built from opposite pairs {i, i+4} and/or single
    heads — i.e. the low and high nibbles are equal (pure opposite pairs) or
    one nibble is empty (heads confined to a single nibble). A step that mixes
    a nibble-spanning pair (e.g. THREE|FOUR, as `_start_multi_zone_chase`
    produces) would be silently mis-thinned/over-filled, so such masks are
    passed through untransformed instead. `test_venue_size` asserts every cue
    routed through the transform path is already safe, so this guard turns a
    future non-conforming pattern into a caught invariant rather than a subtle
    wrong look on stage.
    """
    low = mask & 0x0F
    high = (mask >> 4) & 0x0F
    return low == high or low == 0 or high == 0


def _ring_delta(a: int, b: int) -> int:
    """Signed shortest distance a→b on the 8-cell ring, in [-4, 3].

    Used to glide a scanner head between its cell in one step and the next
    along the *shorter* way round (so a 7→0 hop moves +1, not -7).
    """
    return ((b - a + _CELLS // 2) % _CELLS) - _CELLS // 2


class _TimePattern:
    """Time-driven zone pattern ticked deterministically by the render thread.

    Instead of asyncio.sleep() between steps, the current step is computed
    from wall-clock time — immune to event-loop congestion.
    """
    __slots__ = ('steps', 'step_dicts', 'zone_cells', 'owned_zones', 'step',
                 'next_time', 'bpm_sync', 'param', 'direction',
                 'reverse_on_beat', 'reverse_counter', 'beat_lock', 'pos',
                 'last_tick')

    def __init__(self, steps, *, bpm_sync, param, now, init_bpm=120.0,
                 direction=1, reverse_on_beat=False):
        self.steps = steps           # list of list[(zone, mask)]
        # Per-step {zone: mask} lookups, precomputed so tick() doesn't
        # rebuild a dict per pattern per frame during interpolation.
        self.step_dicts = [dict(s) for s in steps]
        # Per-step {zone: [lit cell indices]} — the "heads" the motion
        # renderer glides between (see motion_heads()). Precomputed so the
        # hot path only interpolates positions, never re-scans bitmasks.
        self.zone_cells = []
        for d in self.step_dicts:
            zc = {}
            for zone, mask in d.items():
                cells = [c for c in range(_CELLS) if (mask >> c) & 1]
                if cells:
                    zc[zone] = cells
            self.zone_cells.append(zc)
        # Zones this pattern ever drives — tick() zeros their cell levels so
        # the base cell mapping skips them (motion owns those pixels).
        self.owned_zones = set().union(*[zc.keys() for zc in self.zone_cells]) \
            if self.zone_cells else set()
        self.step = 0                # current step index
        self.bpm_sync = bpm_sync     # True → param is cycles_per_beat
        self.param = param           # cycles_per_beat (bpm) or total_seconds (timed)
        self.direction = direction
        self.reverse_on_beat = reverse_on_beat
        self.reverse_counter = 0     # counts steps for reversal timing
        # Schedule first transition one interval in the future so step 0
        # is visible for the correct duration on the very first tick.
        self.next_time = now + self.step_interval(init_bpm)
        # Beat-lock: BPM-synced forward chases are phase-locked to the beat
        # oscillator (see tick()). Randomly-reversing chaos patterns (Frenzy)
        # and non-tempo timed patterns keep the free-run scheduler above.
        self.beat_lock = bpm_sync and not reverse_on_beat
        self.pos = 0.0               # continuous step position for the PLL
        self.last_tick = now         # wall-clock of the last PLL advance

    def steps_per_second(self, bpm: float) -> float:
        """Free-run motion rate for a beat-locked pattern (steps/sec)."""
        effective_bpm = bpm if bpm > 0 else 120.0
        return len(self.steps) * self.param * effective_bpm / 60.0

    def step_interval(self, bpm: float) -> float:
        """Seconds per step at the given BPM."""
        n = len(self.steps)
        if self.bpm_sync:
            effective_bpm = bpm if bpm > 0 else 120.0
            return 60.0 / effective_bpm / (n * self.param)
        return self.param / n

    def motion_heads(self, cur_idx: int, nxt_idx: int, progress: float):
        """Continuous per-zone scanner heads gliding from step→step.

        Each lit StageKit cell is a "head". Between the current and next
        step, a zone's heads are matched by the cyclic rotation that
        minimises total travel (these patterns are pure rotations), then
        each head's cell position is interpolated along the shorter way
        round the ring. This is what lets a scanner glide pixel-by-pixel
        at a *continuous* position instead of snapping between 8 cells.

        Returns a list of (zone, cell_pos, level): cell_pos is a float in
        [0, 8); level is 1.0 for a matched head, or a crossfade weight when
        a zone's head count changes between steps (appear/disappear), so a
        head fades in/out in place rather than teleporting.
        """
        cur = self.zone_cells[cur_idx]
        nxt = self.zone_cells[nxt_idx]
        heads = []
        for zone in cur.keys() | nxt.keys():
            c = cur.get(zone)
            m = nxt.get(zone)
            if c and m and len(c) == len(m):
                # Align next-step heads to current by the rotation with the
                # least squared travel (favours a uniform ±1 rotation over a
                # ragged pairing at the wrap seam).
                best_cost = None
                best = m
                for shift in range(len(m)):
                    cand = m[shift:] + m[:shift]
                    cost = 0
                    for a, b in zip(c, cand):
                        d = _ring_delta(a, b)
                        cost += d * d
                    if best_cost is None or cost < best_cost:
                        best_cost = cost
                        best = cand
                for a, b in zip(c, best):
                    d = _ring_delta(a, b)
                    heads.append((zone, (a + progress * d) % _CELLS, 1.0))
            else:
                # Head count changed — crossfade the old set out and the new
                # set in, each held at its own cell (no spurious glide).
                if c:
                    w = 1.0 - progress
                    for cell in c:
                        heads.append((zone, float(cell), w))
                if m:
                    for cell in m:
                        heads.append((zone, float(cell), progress))
        return heads


class CueEngine:
    """Manages active lighting cue and produces zone bitmask state + effects."""

    def __init__(self):
        # Current zone bitmasks [red, green, blue, yellow]
        self.zones = [NONE, NONE, NONE, NONE]

        # Effects config dict consumed by the mapper each frame
        self.effects: dict = {}

        # Strobe state — the raw speed byte; the effective rate is derived
        # live from BPM by strobe_hz() (tempo-locked, fixed-Hz fallback).
        self.strobe_byte = StrobeSpeed.OFF

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

        # Note-hold accents (Phase 4). Rising-edge per-instrument hits, order
        # [guitar, bass, drums, keys]. _note_prev holds the last bitmask for
        # edge detection; _note_until/_note_dur/_note_level drive the decaying
        # accent the mapper paints in each instrument's slice of the strip.
        self._note_prev = [0, 0, 0, 0]
        self._note_until = [0.0, 0.0, 0.0, 0.0]
        self._note_dur = [0.0, 0.0, 0.0, 0.0]
        self._note_level = [0.0, 0.0, 0.0, 0.0]
        # Time of the last rising edge per instrument, for the anti-strobe cap
        # (see NOTE_REFRESH_MIN). Seeded far in the past so the first hit always
        # reads as isolated (a fresh flash), even at t≈0 in tests.
        self._note_last_hit = [-1.0e9, -1.0e9, -1.0e9, -1.0e9]

        # Vocal/harmony pitch (Phase 4). MIDI pitch per voice (lead + 3
        # harmonies), 0.0 = no note sounding. Passed straight to the mapper,
        # which paints a colour-by-pitch "ribbon" blob per active voice.
        self._vocal_notes = [0.0, 0.0, 0.0, 0.0]

        # Performer highlight (Phase 4). Spotlight + singalong are performer
        # bitmasks (offsets 42-43); their union biases the wash toward the
        # highlighted performers' colours in the mapper.
        self._spotlight = 0
        self._singalong = 0

        # Post-processing grade (Phase 4). The venue film grade (offset 35);
        # the mapper applies the colour-tint ones as a global palette modifier.
        self._post_processing = 0

        # Fog/haze (offset 36, Phase 6). While the venue haze is up we lift the
        # post-process blur toward a soft blur-glow floor.
        self._fog = False
        # Operator blur strength (dashboard slider, synced from settings each
        # frame). The always-on base the fog boost stacks on; defaults to
        # BLUR_BASE so a bare engine (tests) still emits the documented value.
        self._blur_base = BLUR_BASE

        # Venue size (offset 8). Branches pattern density per cue: small
        # venues thin chase steps to single heads, large venues fill single
        # heads out to opposing pairs, and both rescale sparkle density.
        # _mask_transform is applied to pattern steps at cue launch (never
        # per frame); NoVenue/unknown leaves both knobs at identity, which
        # preserves the authored look exactly. _venue_sparkle_base holds the
        # authored deviation; _venue_intensity (operator knob, synced from
        # settings each frame like blur) scales how far both knobs lean —
        # 1.0 = full, 0.0 = off (bit-exact, transform gated out).
        self._venue_size = VenueSizeByte.NO_VENUE
        self._mask_transform = None          # None | _thin_opposites | _fill_opposites
        self._venue_sparkle_base = 1.0
        self._venue_intensity = 1.0

        # Song section (offset 13). Sustained state — the mapper leans the
        # current look toward the section's hue and scales its energy
        # (breathing swing + beat-pump depth). Both knobs are resolved at
        # signal-change time (on_song_section); _section_changed_at eases the
        # bias in over SECTION_EASE so a section change drifts, never snaps.
        # _section_hue None = identity (None/unknown section → bit-exact).
        # _section_intensity (operator knob, synced from settings each frame)
        # scales the whole bias — 1.0 = full, 0.0 = off (bit-exact).
        self._song_section = SongSectionByte.NONE
        self._section_hue = None             # (r,g,b) or None
        self._section_energy = 1.0
        self._section_changed_at = 0.0
        self._section_intensity = 1.0

        # Camera cut (v3 datagram, Phase 5). The subject is sustained state —
        # it biases the wash toward the on-camera player's strip region + hue.
        # _camera_changed_at eases that bias in after each cut so the band
        # doesn't snap as the director cuts; _camera_cut_at arms a brief
        # one-shot accent, but only on a *directed* cut. -1 = no subject yet.
        self._camera_subject = -1
        self._camera_changed_at = 0.0
        self._camera_cut_at = 0.0

        # Beat oscillator: a continuous musical phase synthesized from BPM +
        # beat edges, so motion/colour can be driven off a smooth phase that is
        # quantised to but continuous *within* the beat (LedFx's beat/bar
        # oscillator idea). YARG sends no absolute time or beat counter, so we
        # derive it: _beat_at is the monotonic time of the last real beat edge
        # (MEASURE or STRONG); _bar_beat is the beat index within the current
        # bar (reset to 0 on the downbeat). WEAK beats never drive it — they're
        # ambiguous with YARG's post-send "3" sentinel (see VISION.md).
        self._beat_at = 0.0
        self._bar_beat = 0
        # Monotonic beat count (never resets) — with beat_phase it forms a
        # continuous, tempo-locked clock for scrolling motion/colour that must
        # not restart each bar (e.g. a rolling gradient).
        self._beat_count = 0

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

        # Motion sources for the sub-pixel scanner renderer: a flat list of
        # (zone, cell_pos, level) heads rebuilt by tick() from the continuous
        # pattern position. The mapper paints each as a soft profile at a
        # float pixel position — gliding, not cell-snapping. Zones driven by a
        # time pattern have their zone_cell_levels zeroed (motion owns them);
        # static cues keep the cell model. Rebuilt (atomic rebind) each tick.
        self.motion_sources: list[tuple[int, float, float]] = []

    def strobe_hz(self) -> float:
        """Effective strobe rate in Hz (tempo-locked per YALCY StrobeDmxFromBpm).

        The speed byte maps to a note division (flashes per beat) and the rate
        is computed from the current BPM — a tempo change retunes the strobe.
        When BPM is unknown (0), the fixed STROBE_RATES values are the
        fallback. O(1), no allocation — safe on the render hot path.
        """
        division = STROBE_DIVISIONS.get(self.strobe_byte, 0)
        if division and self.bpm > 0:
            return self.bpm * division / 60.0
        return STROBE_RATES.get(self.strobe_byte, 0)

    def get_strobe_visible(self, now: float | None = None) -> bool:
        """Returns whether pixels should be visible (considering strobe).

        Computed from wall-clock time so the render thread gets accurate
        strobe phase without depending on an asyncio coroutine. *now* is
        injectable for tests; production passes None.
        """
        rate = self.strobe_hz()
        if rate == 0:
            return True
        if now is None:
            now = time.monotonic()
        period = 1.0 / rate
        return (now % period) < (period / 2)

    def get_effects(self) -> dict:
        """Return current effects dict with transient flags, then clear them."""
        # While paused, animation clocks freeze at the pause instant.
        now = self._paused_at if self.paused else time.monotonic()
        fx = dict(self.effects)
        fx["bpm"] = self.bpm
        # Venue-size density knob: rescale the cue's sparkle field. The
        # authored deviation (_venue_sparkle_base) is leaned toward 1.0 by the
        # operator intensity, so a full/half/off dial lands live. Skipped at
        # the identity scale so the NoVenue/unknown (and intensity 0) path
        # stays bit-exact.
        sparkle_scale = 1.0 + (self._venue_sparkle_base - 1.0) * self._venue_intensity
        if sparkle_scale != 1.0:
            sparkle = fx.get("sparkle")
            if sparkle:
                fx["sparkle"] = min(1.0, sparkle * sparkle_scale)
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
        fx["beat_phase"] = self.beat_phase(now)
        fx["bar_phase"] = self.bar_phase(now)
        fx["bar_beat"] = self._bar_beat
        fx["beat_clock"] = self.beat_clock(now)
        fx["note_accents"] = self._note_accents(now)
        fx["vocal_notes"] = self._vocal_notes
        fx["performers"] = self._spotlight | self._singalong
        fx["post_processing"] = self._post_processing
        fx["camera"] = (self._camera_subject,
                        self._camera_gain(now), self._camera_accent(now))
        # Song-section bias (signal inventory): (hue, eased gain, energy) or
        # None for the identity section (None/unknown → bit-exact look). The
        # operator intensity scales the eased gain, which the mapper uses for
        # both the hue-lean strength and the energy deviation — so intensity 0
        # emits gain 0 and the mapper reads it as identity (bit-exact).
        fx["section"] = ((self._section_hue,
                          self._section_gain(now) * self._section_intensity,
                          self._section_energy)
                         if self._section_hue is not None else None)
        # Post-process chain (Phase 6). blur is a strength the mapper wet-mixes;
        # mirror is the capability signal — the dashboard toggle gates it (off by
        # default), so we emit True and let apply_effect_toggles() decide.
        fx["blur"] = self._blur_base + (BLUR_FOG_BOOST if self._fog else 0.0)
        fx["mirror"] = True

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

    def beat_phase(self, now: float) -> float:
        """Continuous 0.0→1.0 phase across one beat.

        0.0 the instant a beat lands, ramping to 1.0 over the beat interval
        (60/BPM). Saturates at 1.0 and holds if the next beat is late or
        dropped, so a missed UDP packet reads as "no motion" rather than a
        jump. Returns 0.0 until the first beat is seen.
        """
        if self._beat_at <= 0.0:
            return 0.0
        interval = 60.0 / self.bpm if self.bpm > 0 else 0.5
        p = (now - self._beat_at) / interval
        if p <= 0.0:
            return 0.0
        return p if p < 1.0 else 1.0

    def bar_phase(self, now: float) -> float:
        """Continuous phase across the bar: beat index + intra-beat phase.

        Runs 0.0 upward, resetting to 0.0 on each downbeat (MEASURE). The
        integer part is the current beat within the bar; the fraction is the
        smooth beat_phase. Beats-per-bar is inferred from the downbeat spacing
        (no time-signature is transmitted), so odd metres self-correct at the
        next downbeat.
        """
        return self._bar_beat + self.beat_phase(now)

    def beat_clock(self, now: float) -> float:
        """Monotonic, free-running, tempo-locked beat count (beats + phase).

        The clock for motion/colour that must run continuously across bars (a
        chase, a rolling gradient). Unlike beat_phase it does NOT saturate: on a
        dropped beat it keeps advancing at tempo (a missing beat is almost
        always a lost UDP packet, not a musical stop), so motion coasts instead
        of freezing. Real beats re-align it by the *rounded* elapsed beats (see
        on_beat), so bridging a dropped beat stays continuous with no lurch, and
        the per-pattern PLL absorbs any residual smoothly. Only used while beats
        are fresh; the render loop's BEAT_LOCK_TIMEOUT catches a genuine stop.
        """
        if self._beat_at <= 0.0:
            return 0.0
        interval = 60.0 / self.bpm if self.bpm > 0 else 0.5
        p = (now - self._beat_at) / interval
        if p < 0.0:
            p = 0.0
        return self._beat_count + p

    def tick(self, now: float):
        """Advance all time-based patterns to the current timestamp.

        Called from the render thread each frame.  Static zone brightness
        (zone_cell_levels) and continuous scanner heads (motion_sources) are
        both set deterministically from wall-clock time — immune to asyncio lag.

        Two render models, split by cue type:
          - Static cues (washes, spotlights) keep the 8-cell model: their
            bitmask is expanded into zone_cell_levels and the mapper fills
            cell blocks.
          - Motion cues (scanners/chases/comets) are time patterns. Each
            holds a continuous float position; tick() turns that into gliding
            "heads" (motion_sources) which the mapper paints as soft profiles
            at a sub-pixel position — no cell-block crossfade, so a moving
            light keeps a constant peak and width instead of throbbing.

        Zones a pattern owns have their cell levels zeroed here so the base
        cell mapping skips them; the profile renderer paints them instead.

        If the deadline is far in the past (host suspend, container pause,
        long GC), snap forward instead of catching up step-by-step — that
        would block the render thread and emit a flurry of stale frames.

        Pause handling: when self.paused is True, freeze pattern motion
        (don't advance steps). motion_sources keeps its last positions so the
        frozen scanner holds still; owned zones stay zeroed so it never
        double-paints as blocks. The mapper applies the brightness dim.
        """
        bpm = self.bpm
        levels = self.zone_cell_levels
        patterns = self._time_patterns  # local ref — safe across threads

        # Reset levels from the current bitmask state. Static (patternless)
        # zones keep their bitmask values; motion-owned zones are zeroed next.
        for zone_idx in range(4):
            mask = self.zones[zone_idx]
            row = levels[zone_idx]
            for cell in range(8):
                row[cell] = 1.0 if (mask >> cell) & 1 else 0.0

        # Motion-owned zones render as gliding profiles, not cell blocks.
        # Zero their levels every tick (incl. while paused) so the base
        # mapping skips them and a frozen scanner never double-paints.
        for p in patterns:
            for zone in p.owned_zones:
                row = levels[zone]
                for cell in range(8):
                    row[cell] = 0.0

        if self.paused:
            return  # motion_sources frozen at last positions

        # Beat-lock context: patterns lock to the beat only while beats are
        # fresh; otherwise they free-run on tempo (the fallback).
        beats_live = (self._beat_at > 0.0 and
                      (now - self._beat_at) < BEAT_LOCK_TIMEOUT)
        beat_clock = self.beat_clock(now) if beats_live else 0.0

        motion: list[tuple[int, float, float]] = []
        for p in patterns:
            n = len(p.steps)
            if p.beat_lock:
                # ── Phase-locked motion ──────────────────────────
                # Free-run on tempo, then pull a fraction min(1, dt/PLL_TAU) of
                # the way toward the beat-locked target — a smooth exponential
                # correction, never a snap. The advance is clamped ≥ 0 so a
                # dropped beat makes the chase hesitate, not jump backwards.
                dt = now - p.last_tick
                p.last_tick = now
                if dt < 0.0:
                    dt = 0.0
                elif dt > PLL_DT_MAX:
                    dt = PLL_DT_MAX
                advance = dt * p.steps_per_second(bpm)
                if beats_live:
                    # target position (in steps) implied by the beat clock
                    target = beat_clock * n * p.param
                    err = target - (p.pos + advance)
                    err = (err + n * 0.5) % n - n * 0.5   # shortest wrapped path
                    advance += err * (dt / PLL_TAU if dt < PLL_TAU else 1.0)
                    if advance < 0.0:
                        advance = 0.0
                p.pos = (p.pos + advance) % n
                p.step = int(p.pos)
                if p.step >= n:
                    p.step = n - 1
                progress = p.pos - p.step
                nxt_idx = (p.step + 1) % n
            else:
                # ── Free-run scheduler (timed + chaos patterns) ──
                interval = p.step_interval(bpm)
                # Snap forward if we've fallen more than 1s (or 100 steps) behind.
                max_catchup = max(100, int(1.0 / interval)) if interval > 0 else 100
                if interval > 0 and (now - p.next_time) > max(1.0, interval * max_catchup):
                    p.next_time = now + interval
                while now >= p.next_time:
                    p.step = (p.step + p.direction) % n
                    p.next_time += interval
                    if p.reverse_on_beat:
                        p.reverse_counter += 1
                        if p.reverse_counter % 4 == 0:
                            p.direction = -p.direction

                # Fractional progress (0.0 at step start, 1.0 at next boundary).
                if interval > 0:
                    progress = 1.0 - (p.next_time - now) / interval
                    if progress < 0.0:
                        progress = 0.0
                    elif progress > 1.0:
                        progress = 1.0
                else:
                    progress = 0.0
                nxt_idx = (p.step + p.direction) % n

            # Keep self.zones reflecting the logical step for the status page;
            # the pixels come from the gliding heads below, not these cells.
            for zone, cur_mask in p.steps[p.step]:
                self.zones[zone] = cur_mask
            motion.extend(p.motion_heads(p.step, nxt_idx, progress))

        self.motion_sources = motion  # atomic rebind for the render read

    def on_beat(self, beat_type: int, now: float | None = None):
        """Called when a beat event arrives from YARG.

        MEASURE = downbeat (start of a measure) — most prominent.
        STRONG  = strong beat within the measure.
        WEAK    = sub-beat — fires _beat_event so listen-patterns nudge,
                  but no sparkle/glitch overlay (would feel cluttered).

        MEASURE and STRONG also drive the beat oscillator (reset phase, advance
        the bar). WEAK does not — it can't be told apart from YARG's post-send
        "3" sentinel. *now* is injectable for tests; production passes None.
        """
        self._last_beat_type = beat_type
        if beat_type == BeatByte.OFF:
            return
        self._beat_event.set()
        if now is None:
            now = time.monotonic()
        if beat_type == BeatByte.MEASURE:
            self._beat_flash = True
            self._downbeat_flash = True
            self._glitch_trigger = True
            # Downbeat: restart the bar and re-align the beat clock.
            self._bar_beat = 0
            self._advance_clock(now)
        elif beat_type == BeatByte.STRONG:
            self._beat_flash = True
            self._glitch_trigger = True
            # Advance within the bar, debounced so a duplicated packet (or a
            # WEAK immediately promoted to STRONG) can't double-count. Only a
            # counted beat moves _beat_at, so phase keeps ramping from the true
            # edge when a duplicate is ignored.
            interval = 60.0 / self.bpm if self.bpm > 0 else 0.5
            if self._beat_at <= 0.0 or (now - self._beat_at) > 0.3 * interval:
                self._bar_beat += 1
                self._advance_clock(now)
        # WEAK: event already set; no flash/glitch and no oscillator update.

    def _advance_clock(self, now: float):
        """Re-align the free-running beat clock on a real beat.

        Advance _beat_count by the *rounded* number of beats since the last
        real beat (≥ 1). Normally that's 1; when a beat's UDP packet was lost
        it's ~2, which bridges the gap so beat_clock (which coasted at tempo
        meanwhile) stays continuous instead of lurching back a beat. Then move
        the phase origin to this beat.
        """
        if self._beat_at > 0.0:
            interval = 60.0 / self.bpm if self.bpm > 0 else 0.5
            elapsed = (now - self._beat_at) / interval
            self._beat_count += max(1, round(elapsed))
        else:
            self._beat_count += 1
        self._beat_at = now

    def on_keyframe(self, keyframe_type: int):
        """Called when a keyframe event arrives from YARG."""
        self._last_keyframe_type = keyframe_type
        if keyframe_type != KeyframeByte.OFF:
            self._keyframe_event.set()

    def on_notes(self, guitar: int, bass: int, drums: int, keys: int,
                 now: float | None = None):
        """Rising-edge note-hold accents (Phase 4).

        Each instrument's note bitmask is the set of currently-lit frets/pads.
        A bit going 0→1 is a fresh hit. Two regimes keep it musical *and* safe:

        - **Isolated hit** (≥ NOTE_REFRESH_MIN since the last one): a fresh,
          crisp accent that decays over a 1/32 note (NOTE_HOLD_BEATS, floored at
          NOTE_HOLD_FLOOR) — so a hit living in one ~88 Hz packet still reads.
          Simultaneous new bits (a chord) seed a slightly stronger accent.
        - **Rapid passage** (a hit within NOTE_REFRESH_MIN of the previous):
          instead of restarting the flash on every hit — which would strobe —
          sustain the lane as a steady glow, holding it lit past the next hit
          and freezing the level. The accent then can't flash faster than
          ~1/NOTE_REFRESH_MIN. See NOTE_REFRESH_MIN.

        Held bits (no rising edge) don't re-trigger. *now* is injectable for
        tests; production passes None.
        """
        if now is None:
            now = time.monotonic()
        interval = 60.0 / self.bpm if self.bpm > 0 else 0.5
        dur = interval * NOTE_HOLD_BEATS
        if dur < NOTE_HOLD_FLOOR:
            dur = NOTE_HOLD_FLOOR
        # Sustain window bridges the gaps between rapid hits so the glow never
        # dips dark; always ≥ the decay tail so a slow-tempo tail isn't clipped.
        sustain = dur if dur > NOTE_REFRESH_MIN else NOTE_REFRESH_MIN
        masks = (guitar, bass, drums, keys)
        for i in range(4):
            m = masks[i]
            new_bits = m & ~self._note_prev[i]
            self._note_prev[i] = m
            if not new_bits:
                continue
            gap = now - self._note_last_hit[i]
            self._note_last_hit[i] = now
            if gap >= NOTE_REFRESH_MIN:
                # Isolated hit (or the first of a run): a fresh, crisp accent.
                self._note_until[i] = now + dur
                self._note_dur[i] = dur
                self._note_level[i] = min(1.0, 0.55 + 0.15 * new_bits.bit_count())
            else:
                # Rapid passage: sustain the lane without restarting the flash,
                # so it glows steadily rather than strobing at the hit rate.
                self._note_until[i] = now + sustain

    def on_vocals(self, vocal: float, harmony0: float, harmony1: float,
                  harmony2: float):
        """Store the current vocal + harmony MIDI pitches (Phase 4).

        0.0 means no note is sounding for that voice. Sustained state read by
        the mapper each frame; atomic rebind (a fresh list) so the render thread
        never sees a half-updated set.
        """
        self._vocal_notes = [vocal, harmony0, harmony1, harmony2]

    def on_performers(self, spotlight: int, singalong: int):
        """Store the spotlight + singalong performer bitmasks (Phase 4).

        Sustained state; their union is surfaced to the mapper each frame to
        bias the wash toward the highlighted performers' colours.
        """
        self._spotlight = spotlight
        self._singalong = singalong

    def on_post_processing(self, post_processing: int):
        """Store the venue post-processing grade byte (Phase 4)."""
        self._post_processing = post_processing

    def on_fog(self, foggy: bool):
        """Store venue fog/haze state (Phase 6) — lifts the post-process blur."""
        self._fog = bool(foggy)

    def on_venue_size(self, venue_size: int):
        """Store the venue-size byte (offset 8) and precompute the density knobs.

        Both knobs are resolved here, at signal-change time: the hot path only
        reads them (a sparkle multiply in get_effects, a transform applied to
        pattern steps at cue launch). NoVenue/unknown resets to identity, so
        the authored look is bit-exact when the chart doesn't say. YARG only
        changes the byte on chart load, so there is no mid-cue re-launch — a
        new size takes effect at the next cue, as in YALCY.
        """
        self._venue_size = venue_size
        if venue_size == VenueSizeByte.SMALL:
            self._mask_transform = _thin_opposites
            self._venue_sparkle_base = SPARKLE_SCALE_SMALL
        elif venue_size == VenueSizeByte.LARGE:
            self._mask_transform = _fill_opposites
            self._venue_sparkle_base = SPARKLE_SCALE_LARGE
        else:
            self._mask_transform = None
            self._venue_sparkle_base = 1.0

    def on_song_section(self, section: int, now: float | None = None):
        """Store the song-section byte (offset 13) and resolve the bias knobs.

        Both knobs (hue lean + energy scale) are resolved here, at
        signal-change time — the hot path only reads them (a gain ramp plus
        one tuple in get_effects; the mapper does the per-pixel lean). The
        change time arms a slow ease-in over SECTION_EASE so a verse→chorus
        transition drifts rather than snaps. None/unknown resets to identity,
        so the authored look is bit-exact when no section is playing. *now* is
        injectable for tests; production passes None.
        """
        if now is None:
            now = time.monotonic()
        self._song_section = section
        bias = SECTION_BIAS.get(section)
        if bias is None:
            self._section_hue = None
            self._section_energy = 1.0
        else:
            self._section_hue, self._section_energy = bias
        self._section_changed_at = now

    def _section_gain(self, now: float) -> float:
        """Section-bias ease-in 0.0 → 1.0 over SECTION_EASE after a change."""
        if self._section_changed_at <= 0.0:
            return 0.0
        t = (now - self._section_changed_at) / SECTION_EASE
        if t < 0.0:
            return 0.0
        return t if t < 1.0 else 1.0

    def set_blur_base(self, amount: float):
        """Set the operator blur strength (0..1); fog stacks on top of it."""
        self._blur_base = max(0.0, min(1.0, float(amount)))

    def set_venue_intensity(self, amount: float):
        """Set the venue-density operator knob (0..1).

        Scales how far the sparkle field leans from the authored default and
        gates the pattern transform (0 = off → bit-exact authored look, 1 =
        the full YALCY-style density branch). Synced from settings each frame.
        """
        self._venue_intensity = max(0.0, min(1.0, float(amount)))

    def set_section_intensity(self, amount: float):
        """Set the song-section bias operator knob (0..1).

        Scales the whole section bias (hue lean + energy deviation); 0 = off
        (bit-exact), 1 = full. Synced from settings each frame like blur.
        """
        self._section_intensity = max(0.0, min(1.0, float(amount)))

    def on_camera_cut(self, subject: int, priority: int,
                      now: float | None = None):
        """Camera-cut lighting (Phase 5).

        Called on each *change* of camera subject. The subject is stored as
        sustained state — the mapper biases the wash toward that player's strip
        region + hue — and the change time arms a short bias ease-in so the band
        fades rather than snaps. A *directed* cut (priority Directed) also arms a
        brief one-shot accent; the constant auto-cuts (priority Normal) update
        the subject silently. *now* is injectable for tests; production passes
        None.
        """
        if now is None:
            now = time.monotonic()
        self._camera_subject = subject
        self._camera_changed_at = now
        if priority == CameraCutPriority.DIRECTED:
            self._camera_cut_at = now

    def _camera_accent(self, now: float) -> float:
        """Directed-cut accent 1.0 → 0.0; 0.0 = expired or never fired."""
        if self._camera_cut_at <= 0.0:
            return 0.0
        rem = self._camera_cut_at + CAMERA_CUT_DURATION - now
        if rem <= 0.0:
            return 0.0
        return rem / CAMERA_CUT_DURATION

    def _camera_gain(self, now: float) -> float:
        """Subject-bias ease-in 0.0 → 1.0 over CAMERA_EASE after a cut."""
        if self._camera_changed_at <= 0.0:
            return 0.0
        t = (now - self._camera_changed_at) / CAMERA_EASE
        return t if t < 1.0 else 1.0

    def _note_accents(self, now: float) -> list[float]:
        """Current decayed note-hold level per instrument (0.0 = none)."""
        out = [0.0, 0.0, 0.0, 0.0]
        for i in range(4):
            dur = self._note_dur[i]
            if dur <= 0.0:
                continue
            rem = self._note_until[i] - now
            if rem <= 0.0:
                continue
            frac = rem / dur
            out[i] = self._note_level[i] * (frac if frac < 1.0 else 1.0)
        return out

    def on_strobe(self, strobe_byte: int):
        """Called when strobe state changes.

        Stores the raw speed byte only — the effective rate is derived live
        from BPM by strobe_hz(), so a tempo change retunes the strobe without
        needing the byte re-sent.
        """
        self.strobe_byte = strobe_byte

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
            if self._beat_at > 0.0:
                self._beat_at += delta
            if self._camera_cut_at > 0.0:
                self._camera_cut_at += delta
            if self._camera_changed_at > 0.0:
                self._camera_changed_at += delta
            if self._section_changed_at > 0.0:
                self._section_changed_at += delta
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
            # Ambient breathing wash — "settle in" moment. A full wash that
            # breathes, recoloured by a slowly-rolling "cool" gradient locked to
            # the beat (1 gradient cycle per 8 beats) so the colour drifts with
            # the song instead of sitting on a flat blue. The BLUE zone just
            # lights the strip; the gradient supplies the hue.
            self._set_effects(breathing=0.25, trails=10,
                              gradient=GRADIENTS["cool"], gradient_roll=0.125)
            self._set_zone(BLUE, ALL)

        elif cue == CueByte.CHORUS:
            # Peak energy — BPM-synced chase with beat sparkles
            # Red chase + yellow solid base + sparkle on downbeats + a gentle
            # on-beat brightness pump from the beat oscillator.
            self._set_effects(trails=5, sparkle=0.10, beat_pulse=0.18)
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
            # Chaotic climax — fast rotating chase + beat sparkles + a stronger
            # on-beat brightness pump (beat oscillator) to hit the climax.
            self._set_effects(trails=4, sparkle=0.10, beat_pulse=0.22)
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

    def _venue_transform_pattern(self, pattern: list[int]) -> list[int]:
        """Apply the venue density transform to a pattern's step masks at launch.

        No-op (returns the pattern unchanged) when there's no active transform
        (NoVenue/unknown) or the operator has dialled venue intensity to 0 —
        both keep the authored look bit-exact. Only `_venue_safe` masks are
        transformed; anything else passes through untouched so a nibble-spanning
        step can never be silently mis-transformed on stage.
        """
        if self._mask_transform is None or self._venue_intensity <= 0.0:
            return pattern
        xform = self._mask_transform
        return [xform(m) if _venue_safe(m) else m for m in pattern]

    def _start_beat_pattern(self, zone: int, pattern: list[int],
                            cycles_per_beat: float, listen: str | None = None):
        """Launch a beat-synced pattern loop on a zone.

        When *listen* is None the pattern is time-driven and ticked from the
        render thread (immune to event-loop congestion).  Event-driven
        patterns still use asyncio tasks.

        The venue-size density transform (if any) is applied to the steps
        here, at launch — never per frame.
        """
        pattern = self._venue_transform_pattern(pattern)
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
        pattern = self._venue_transform_pattern(pattern)
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
