"""Persistent bridge settings (brightness, color palette).

Settings are stored in a JSON file and survive restarts. The status page
can modify them at runtime via the /api/settings endpoint.
"""

import json
import logging
import os
import threading

from config import GLOBAL_BRIGHTNESS, TARGET_FPS
from protocol.yarg_packet import CueByte

log = logging.getLogger(__name__)

SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "/data/settings.json")

# ── Cue cross-fade ─────────────────────────────────────────────────
# How long the render thread blends a new cue against the last frame it sent.
# The base duration is the operator's `cue_fade_ms` setting; these cues override
# it, because YARG already tells us which transitions are meant to be abrupt —
# BLACKOUT_FAST and BLACKOUT_SLOW are separate cue bytes, and fading both over
# the same 250 ms (as the bridge did until 2026-07-29) throws that away.
#
# The fade is not decoration: it removes a one-frame black blink between cues.
# But it was measured as the single largest latency term in the whole pipeline
# — ~120 ms of the 145-164 ms cue-change step, against ~23 ms for the entire
# network and device path. See BACKLOG.md.
#
# Every cue not listed here uses `cue_fade_ms`. That deliberately includes
# STROBE_OFF (a *return* to a lit look, where softness is still wanted),
# BIG_ROCK_ENDING, MENU and SCORE.
#
# Setting this to {} and cue_fade_ms to 250 restores the pre-2026-07-29
# behaviour exactly; tests/test_replay.py pins that combination.
CUE_FADE_MS_OVERRIDES = {
    CueByte.BLACKOUT_FAST: 0,     # these cue bytes say "fast"
    CueByte.FLARE_FAST: 0,
    CueByte.FRENZY: 0,
    CueByte.STROBE_FASTEST: 0,
    CueByte.STROBE_FAST: 0,
    CueByte.STROBE_MEDIUM: 0,
    CueByte.STROBE_SLOW: 0,
    CueByte.BLACKOUT_SLOW: 250,   # and these say "slow"
    CueByte.FLARE_SLOW: 250,
}

# Clamp for the operator-set base duration. 1000 ms is far past anything
# usable; it exists so a bad API call can't park the strip in a fade forever.
CUE_FADE_MS_MAX = 1000

# ── Color palettes ─────────────────────────────────────────────────
# Each palette maps the 4 zone names to (R, G, B) tuples.
# "red" zone doesn't have to be red — it's just the Stage Kit zone ID.
#
# `colors` is what the Stage Kit model can carry: four zones, four colours. Most
# of these were derived by *truncating* a richer LedFx or WLED gradient down to
# four, and that truncation is what made the newer continuous layers (vocal
# ribbon, the biases, cue gradients) look foreign — they need a whole colour
# family, not four swatches.
#
# So a palette may also carry `ramp`: the full upstream gradient, as
# `(position, (r, g, b))` stops over [0, 1], recovered by
# `tools/extract_palette_ramps.py` from the vendored LedFx/WLED checkouts and
# pasted here as literals (the deployed image ships only this directory, so
# runtime never reads those clones). `effects/palette_map.py` turns it into the
# ring every non-zone layer draws from.
#
# Six palettes have no `ramp` and fall back to a ring built from their four
# colours: `default` and `neon` are ours with no upstream at all, while `party`,
# `forest`, `sakura` and `frost` failed the generator's audit — their four
# colours are not actually the upstream family the description claims (Party has
# no green in it; LedFx Frost runs on into purple and pink, and ours is ice).
# Run `tools/extract_palette_ramps.py --check` to see that audit.

PALETTES = {
    "default": {
        "label": "Default (RGBY)",
        "colors": {
            "red": (255, 0, 0),
            "green": (0, 255, 0),
            "blue": (0, 0, 255),
            "yellow": (255, 255, 0),
        },
    },
    "party": {
        "label": "Party",
        "description": "FastLED/WLED Party — vibrant club colors",
        "colors": {
            "red": (255, 0, 100),
            "green": (0, 230, 118),
            "blue": (100, 0, 255),
            "yellow": (255, 160, 0),
        },
    },
    "dancefloor": {
        "label": "Dancefloor",
        "description": "LedFx Dancefloor — hot pinks and deep blues",
        "colors": {
            "red": (255, 0, 100),
            "green": (200, 0, 255),
            "blue": (0, 50, 255),
            "yellow": (255, 50, 200),
        },
        "ramp": [
            (0.0, (255, 0, 0)),
            (0.5, (255, 0, 178)),
            (1.0, (0, 0, 255)),
        ],
    },
    "plasma": {
        "label": "Plasma",
        "description": "LedFx Plasma — electric purples and oranges",
        "colors": {
            "red": (180, 0, 255),
            "green": (0, 80, 255),
            "blue": (255, 0, 120),
            "yellow": (255, 120, 0),
        },
        "ramp": [
            (0.0, (0, 0, 255)),
            (0.25, (128, 0, 128)),
            (0.5, (255, 0, 0)),
            (0.75, (255, 40, 0)),
            (1.0, (255, 200, 0)),
        ],
    },
    "lava": {
        "label": "Lava",
        "description": "WLED Lava — deep reds and molten oranges",
        "colors": {
            "red": (200, 0, 0),
            "green": (255, 80, 0),
            "blue": (140, 0, 10),
            "yellow": (255, 180, 0),
        },
        "ramp": [
            (0.0, (77, 0, 0)),
            (0.2525, (177, 0, 0)),
            (0.3131, (196, 38, 9)),
            (0.3687, (215, 76, 19)),
            (0.5051, (235, 115, 29)),
            (0.6465, (255, 153, 41)),
            (0.7172, (255, 178, 41)),
            (0.7879, (255, 204, 41)),
            (0.8687, (255, 230, 41)),
            (0.9495, (255, 255, 41)),
            (1.0, (255, 255, 143)),
        ],
    },
    "ocean": {
        "label": "Ocean",
        "description": "WLED/LedFx Ocean — deep water blues and teals",
        "colors": {
            "red": (0, 150, 200),
            "green": (0, 80, 180),
            "blue": (0, 10, 100),
            "yellow": (80, 220, 240),
        },
        "ramp": [
            (0.0, (25, 25, 112)),
            (0.0667, (0, 0, 139)),
            (0.1333, (25, 25, 112)),
            (0.2, (0, 0, 128)),
            (0.3333, (0, 0, 205)),
            (0.4, (46, 139, 87)),
            (0.4667, (0, 128, 128)),
            (0.5333, (95, 158, 160)),
            (0.6, (0, 0, 255)),
            (0.6667, (0, 139, 139)),
            (0.7333, (100, 149, 237)),
            (0.8, (127, 255, 212)),
            (0.8667, (46, 139, 87)),
            (0.9333, (0, 255, 255)),
            (1.0, (135, 206, 250)),
        ],
    },
    "forest": {
        "label": "Forest",
        "description": "WLED Forest — greens, lime, and amber",
        "colors": {
            "red": (180, 100, 0),
            "green": (0, 180, 30),
            "blue": (50, 120, 20),
            "yellow": (120, 200, 0),
        },
    },
    "sunset": {
        "label": "Sunset",
        "description": "WLED Sunset Real — warm horizon glow",
        "colors": {
            "red": (255, 40, 0),
            "green": (255, 140, 20),
            "blue": (160, 0, 100),
            "yellow": (255, 200, 40),
        },
        "ramp": [
            (0.0, (181, 0, 0)),
            (0.163, (218, 85, 0)),
            (0.3778, (255, 170, 0)),
            (0.6296, (211, 85, 77)),
            (1.0, (167, 0, 169)),
        ],
    },
    "borealis": {
        "label": "Borealis",
        "description": "LedFx Borealis — northern lights",
        "colors": {
            "red": (0, 255, 120),
            "green": (0, 180, 200),
            "blue": (80, 0, 200),
            "yellow": (0, 220, 160),
        },
        "ramp": [
            (0.0, (128, 0, 128)),
            (0.5001, (0, 199, 140)),
            (1.0, (0, 255, 0)),
        ],
    },
    "frost": {
        "label": "Frost",
        "description": "LedFx Frost/Winter — ice and cold whites",
        "colors": {
            "red": (180, 200, 255),
            "green": (80, 140, 255),
            "blue": (20, 40, 180),
            "yellow": (200, 220, 255),
        },
    },
    "sakura": {
        "label": "Sakura",
        "description": "WLED Sakura — cherry blossom pinks",
        "colors": {
            "red": (255, 80, 140),
            "green": (255, 140, 180),
            "blue": (200, 80, 200),
            "yellow": (255, 180, 210),
        },
    },
    "neon": {
        "label": "Neon",
        "description": "Bright saturated neon — maximum contrast",
        "colors": {
            "red": (255, 0, 60),
            "green": (0, 255, 140),
            "blue": (0, 160, 255),
            "yellow": (255, 240, 0),
        },
    },
}

VALID_FPS = (10, 15, 20, 25, 30, 40, 50, 60)

# ── Runtime-toggleable render effects ───────────────────────────────
# The framework backing the dashboard's per-effect on/off switches. Each entry
# is one toggle: `label`/`description` drive the UI (the dashboard renders the
# switches from this registry, so it needs no per-effect code), and `key`/`off`
# tell the render loop how to suppress it — when the toggle is off,
# apply_effect_toggles() forces effects[`key`] to `off` before the mapper sees
# it. A `key` of None marks a toggle consumed directly by the engine instead.
# The dashboard renders both kinds from this registry.
EFFECT_TOGGLES = {
    "note_accents": {
        "label": "Note Accents",
        "description": "Per-instrument whitening flash on note/pad hits.",
        "key": "note_accents", "off": None,
    },
    "vocal_ribbon": {
        "label": "Vocal Ribbon",
        "description": "Colour-by-pitch blobs tracking the vocal + harmony lines.",
        "key": "vocal_notes", "off": None,
    },
    "performer_bias": {
        "label": "Performer Highlight",
        "description": "Bias the wash toward the spotlighted performer's colour.",
        "key": "performers", "off": 0,
    },
    "post_processing": {
        "label": "Post-Processing Grade",
        "description": "Venue film colour grades (sepia, B&W, channel tints…).",
        "key": "post_processing", "off": 0,
    },
    "camera_cut": {
        "label": "Camera Cuts",
        "description": "Bias the wash toward the on-camera player + a directed-cut accent.",
        "key": "camera", "off": None,
    },
    "venue_patterns": {
        "label": "Venue Chase Density",
        "description": "Use sparse/dense chase masks for small/large venues; takes effect on the next cue.",
        # Consumed by the cue engine at pattern launch, not by the mapper.
        "key": None, "off": None,
    },
    # Post-process chain (Phase 6): blur → mirror → brightness → background.
    # Both gate a signal the engine emits every frame; when the toggle is off,
    # apply_effect_toggles() forces the key to `off`. Blur defaults on (a light
    # always-on smoothing look); mirror is an opt-in symmetric look, so it uses
    # `default: False` — the framework's suppress-only gate then reads as "off
    # until enabled" (the engine emits mirror=True as the capability, the toggle
    # decides whether it lands).
    "blur": {
        "label": "Blur",
        "description": "Soft Gaussian blur so cue events read as smooth stage light (stronger under fog).",
        "key": "blur", "off": 0.0,
    },
    "mirror": {
        "label": "Mirror",
        "description": "Fold the strip into a left–right symmetric look (max of each half).",
        "key": "mirror", "off": False, "default": False,
    },
}

# ── Idle look ──────────────────────────────────────────────────────
# What an owned strip shows once YARG stops feeding the bridge. The bridge
# stops sending DDP and hands WLED a look to run standalone, so "no DDP" is a
# defined state on the strip instead of whatever it was last left on.
#
# Three modes, all of them defined:
#   "preset"  WLED renders the effect/palette/colour below on its own;
#   "black"   same push with the look forced to an unlit solid — the strip
#             stays powered and owned, it just shows nothing;
#   "off"     skip the idle stage and power down at the grace period instead
#             of waiting out the full idle timeout.
#
# `grace_seconds` is how long a gap in the YARG stream is tolerated before the
# held cue gives way to the idle look. It exists because YARG sends at ~88 Hz
# and a couple of dropped datagrams must not drop the stage look mid-song; it
# is not the idle *timeout*, which powers the strip off much later.
IDLE_MODES = ("preset", "black", "off")
IDLE_GRACE_MAX = 300

DEFAULT_IDLE = {
    "mode": "preset",
    "grace_seconds": 15,
    "effect": 2,            # Breathe — slow, unobtrusive, present on every build
    "palette": 0,           # Default (uses the segment colour below)
    "color": [255, 160, 0],
    "brightness": 40,       # dim: this runs unattended between songs
    "speed": 40,            # slow
    "intensity": 128,
}

DEFAULT_SETTINGS = {
    "brightness": GLOBAL_BRIGHTNESS,
    "palette": "default",
    "fps": TARGET_FPS,
    "direction": "normal",
    # Post-process blur strength 0.0-1.0 (Phase 6). Operator taste knob for the
    # always-on Gaussian smoothing; fog lifts it further at render time. Default
    # mirrors the engine's BLUR_BASE. The Blur toggle still gates it on/off.
    "blur_amount": 0.35,
    # Venue-size sparkle intensity 0.0-1.0 (Phase 8). Chase-mask density is a
    # discrete choice exposed separately as the venue_patterns effect toggle.
    "venue_intensity": 1.0,
    # Song-section bias intensity 0.0-1.0 (Phase 8). Scales the verse/chorus
    # hue-lean + energy bias; 0 = off (bit-exact), 1 = full.
    "section_intensity": 1.0,
    # How tightly the reactivity layers follow the selected palette, 0.0-1.0.
    # The vocal ribbon, performer/camera/section biases, cue gradients and
    # star-power tint each paint from a fixed colour table of their own, which
    # looks right under `default` (RGBY) and foreign under every other palette.
    # 1.0 constrains them all to the palette's family; 0.0 restores the fixed
    # tables exactly. Defaults on — following the palette is the point.
    "palette_strictness": 1.0,
    # Base cue cross-fade in milliseconds, 0-CUE_FADE_MS_MAX. The cues in
    # CUE_FADE_MS_OVERRIDES ignore this and use their own duration. 0 makes
    # every non-overridden cue snap; 250 was the old hardcoded value.
    "cue_fade_ms": 120,
    # Each toggle defaults on unless its registry row opts out with default:False.
    "effects": {tid: meta.get("default", True) for tid, meta in EFFECT_TOGGLES.items()},
    # What the strip shows when the bridge owns it but is not sending frames.
    "idle": dict(DEFAULT_IDLE),
}


def _clean_idle(raw: dict, base: dict) -> dict:
    """Validate an idle-look document field by field, against `base`.

    Every field is bounds-checked independently and a bad one falls back to the
    current value rather than rejecting the whole document: this block is
    edited by the dashboard a field at a time, and one out-of-range effect id
    should not silently revert the operator's colour too.

    Effect and palette ids are only range-checked, not checked against the
    device's actual lists — the firmware can be reflashed under a stored
    settings file, and WLED itself clamps an id it does not have.
    """
    out = dict(base)
    if raw.get("mode") in IDLE_MODES:
        out["mode"] = raw["mode"]
    if isinstance(raw.get("grace_seconds"), (int, float)):
        out["grace_seconds"] = max(0, min(IDLE_GRACE_MAX, int(raw["grace_seconds"])))
    for key, hi in (("effect", 255), ("palette", 255),
                    ("brightness", 255), ("speed", 255), ("intensity", 255)):
        if isinstance(raw.get(key), (int, float)):
            out[key] = max(0, min(hi, int(raw[key])))
    colour = raw.get("color")
    if (isinstance(colour, (list, tuple)) and len(colour) == 3
            and all(isinstance(c, (int, float)) for c in colour)):
        out["color"] = [max(0, min(255, int(c))) for c in colour]
    return out


class BridgeSettings:
    """Thread-safe persistent settings manager."""

    def __init__(self, path: str = SETTINGS_FILE, warn_unwritable: bool = True,
                 cue_fade_overrides: dict | None = None):
        self._path = path
        self._lock = threading.Lock()
        # Per-cue fade durations. An instance copy rather than the module table
        # so replay can pin it — passing {} plus cue_fade_ms=250 is the
        # documented way back to the pre-2026-07-29 look, and
        # tests/test_replay.py keeps that claim under test.
        self._cue_fade_overrides = dict(
            CUE_FADE_MS_OVERRIDES if cue_fade_overrides is None
            else cue_fade_overrides)
        self._data = dict(DEFAULT_SETTINGS)
        # Deep-copy the nested effects dict so instances (and the module-level
        # DEFAULT_SETTINGS) don't share one mutable object.
        self._data["effects"] = dict(DEFAULT_SETTINGS["effects"])
        self._data["idle"] = dict(DEFAULT_SETTINGS["idle"])
        self._writable = self._probe_writable()
        # Replay and tests deliberately point at an unwritable path to get
        # in-code defaults; there the warning is noise telling them to mount a
        # volume they don't want (see replay/player.py).
        if not self._writable and warn_unwritable:
            log.warning(
                "Settings: %s is not writable — runtime changes will not persist. "
                "Mount a volume at %s to enable persistence.",
                self._path, os.path.dirname(self._path) or ".",
            )
        self._load()

    def _probe_writable(self) -> bool:
        directory = os.path.dirname(self._path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            return False
        return os.access(directory, os.W_OK)

    def _load(self):
        try:
            with open(self._path, "r") as f:
                stored = json.load(f)
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Settings: failed to load %s: %s", self._path, e)
            return
        if isinstance(stored.get("brightness"), int):
            self._data["brightness"] = max(0, min(255, stored["brightness"]))
        if stored.get("palette") in PALETTES:
            self._data["palette"] = stored["palette"]
        if isinstance(stored.get("fps"), int) and stored["fps"] in VALID_FPS:
            self._data["fps"] = stored["fps"]
        if stored.get("direction") in ("normal", "reverse"):
            self._data["direction"] = stored["direction"]
        if isinstance(stored.get("blur_amount"), (int, float)):
            self._data["blur_amount"] = max(0.0, min(1.0, float(stored["blur_amount"])))
        if isinstance(stored.get("venue_intensity"), (int, float)):
            self._data["venue_intensity"] = max(0.0, min(1.0, float(stored["venue_intensity"])))
        if isinstance(stored.get("section_intensity"), (int, float)):
            self._data["section_intensity"] = max(0.0, min(1.0, float(stored["section_intensity"])))
        if isinstance(stored.get("palette_strictness"), (int, float)):
            self._data["palette_strictness"] = max(0.0, min(1.0, float(stored["palette_strictness"])))
        if isinstance(stored.get("cue_fade_ms"), (int, float)):
            self._data["cue_fade_ms"] = max(0, min(CUE_FADE_MS_MAX, int(stored["cue_fade_ms"])))
        stored_idle = stored.get("idle")
        if isinstance(stored_idle, dict):
            self._data["idle"] = _clean_idle(stored_idle, self._data["idle"])
        stored_effects = stored.get("effects")
        if isinstance(stored_effects, dict):
            for tid in EFFECT_TOGGLES:
                if isinstance(stored_effects.get(tid), bool):
                    self._data["effects"][tid] = stored_effects[tid]

    def _save(self):
        if not self._writable:
            return
        # Atomic write: tmp + rename so a crash mid-write can't corrupt the file.
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._path)
        except OSError as e:
            log.error("Settings: failed to save: %s", e)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    @property
    def brightness(self) -> int:
        with self._lock:
            return self._data["brightness"]

    @brightness.setter
    def brightness(self, value: int):
        value = max(0, min(255, int(value)))
        with self._lock:
            self._data["brightness"] = value
            self._save()

    @property
    def palette_name(self) -> str:
        with self._lock:
            return self._data["palette"]

    @palette_name.setter
    def palette_name(self, name: str):
        if name not in PALETTES:
            return
        with self._lock:
            self._data["palette"] = name
            self._save()

    @property
    def fps(self) -> int:
        with self._lock:
            return self._data["fps"]

    @fps.setter
    def fps(self, value: int):
        value = int(value)
        if value not in VALID_FPS:
            return
        with self._lock:
            self._data["fps"] = value
            self._save()

    @property
    def blur_amount(self) -> float:
        with self._lock:
            return self._data["blur_amount"]

    @blur_amount.setter
    def blur_amount(self, value: float):
        value = max(0.0, min(1.0, float(value)))
        with self._lock:
            self._data["blur_amount"] = value
            self._save()

    @property
    def venue_intensity(self) -> float:
        with self._lock:
            return self._data["venue_intensity"]

    @venue_intensity.setter
    def venue_intensity(self, value: float):
        value = max(0.0, min(1.0, float(value)))
        with self._lock:
            self._data["venue_intensity"] = value
            self._save()

    @property
    def section_intensity(self) -> float:
        with self._lock:
            return self._data["section_intensity"]

    @section_intensity.setter
    def section_intensity(self, value: float):
        value = max(0.0, min(1.0, float(value)))
        with self._lock:
            self._data["section_intensity"] = value
            self._save()

    @property
    def palette_strictness(self) -> float:
        with self._lock:
            return self._data["palette_strictness"]

    @palette_strictness.setter
    def palette_strictness(self, value: float):
        value = max(0.0, min(1.0, float(value)))
        with self._lock:
            self._data["palette_strictness"] = value
            self._save()

    @property
    def cue_fade_ms(self) -> int:
        with self._lock:
            return self._data["cue_fade_ms"]

    @cue_fade_ms.setter
    def cue_fade_ms(self, value: int):
        value = max(0, min(CUE_FADE_MS_MAX, int(value)))
        with self._lock:
            self._data["cue_fade_ms"] = value
            self._save()

    def fade_seconds_for_cue(self, cue: int | None) -> float:
        """Cross-fade duration in seconds for an incoming cue byte.

        The render thread latches this once per cue change rather than reading
        it every frame — see the note at `RenderThread.render_frame`.
        """
        ms = self._cue_fade_overrides.get(cue)
        if ms is None:
            with self._lock:
                ms = self._data["cue_fade_ms"]
        return ms / 1000.0

    @property
    def direction(self) -> str:
        with self._lock:
            return self._data["direction"]

    @direction.setter
    def direction(self, value: str):
        if value not in ("normal", "reverse"):
            return
        with self._lock:
            self._data["direction"] = value
            self._save()

    @property
    def zone_colors(self) -> dict[str, tuple[int, int, int]]:
        """Current palette's zone color mapping."""
        with self._lock:
            return dict(PALETTES[self._data["palette"]]["colors"])

    @property
    def palette_ramp(self):
        """Current palette's full upstream gradient, or None.

        Six palettes have none — see the note above `PALETTES`. Callers pass
        this to `palette_map.build_ring`, which falls back to the four zone
        colours when it is absent.
        """
        with self._lock:
            ramp = PALETTES[self._data["palette"]].get("ramp")
        return list(ramp) if ramp else None

    # ── Effect toggles ─────────────────────────────────────────────
    @property
    def effects(self) -> dict[str, bool]:
        """Current on/off state of every effect toggle (a copy)."""
        with self._lock:
            return dict(self._data["effects"])

    def effect_enabled(self, effect_id: str) -> bool:
        """Whether a single effect toggle is on (unknown id → True)."""
        with self._lock:
            return self._data["effects"].get(effect_id, True)

    def set_effect(self, effect_id: str, enabled: bool) -> bool:
        """Enable/disable one effect toggle. Returns False for an unknown id."""
        if effect_id not in EFFECT_TOGGLES:
            return False
        with self._lock:
            self._data["effects"][effect_id] = bool(enabled)
            self._save()
        return True

    def apply_effect_toggles(self, effects: dict) -> dict:
        """Suppress the render signal of any disabled effect toggle.

        Called each frame by the render thread on the fresh effects dict from
        the engine, *before* it reaches the mapper: for every toggle that's off,
        force its `key` to the `off` value the mapper reads as "inactive".
        Enabled effects pass through untouched. Mutates and returns `effects`.
        """
        with self._lock:
            states = dict(self._data["effects"])
        for tid, meta in EFFECT_TOGGLES.items():
            if meta["key"] is not None and not states.get(tid, True):
                effects[meta["key"]] = meta["off"]
        return effects

    @property
    def idle(self) -> dict:
        """The idle-look spec, as a copy the caller may hold across frames."""
        with self._lock:
            return dict(self._data["idle"])

    @property
    def idle_grace_seconds(self) -> int:
        with self._lock:
            return int(self._data["idle"]["grace_seconds"])

    def update_idle(self, raw: dict) -> dict:
        """Merge a partial idle-look document in. Returns the result."""
        with self._lock:
            self._data["idle"] = _clean_idle(raw, self._data["idle"])
            self._save()
            return dict(self._data["idle"])

    def snapshot(self) -> dict:
        """Return current settings for the status page."""
        with self._lock:
            palette_key = self._data["palette"]
            return {
                "brightness": self._data["brightness"],
                "palette": palette_key,
                "palettes": {k: v["label"] for k, v in PALETTES.items()},
                "colors": PALETTES[palette_key]["colors"],
                "fps": self._data["fps"],
                "fps_options": sorted(set(VALID_FPS) | {self._data["fps"]}),
                "direction": self._data["direction"],
                "blur_amount": self._data["blur_amount"],
                "venue_intensity": self._data["venue_intensity"],
                "section_intensity": self._data["section_intensity"],
                "palette_strictness": self._data["palette_strictness"],
                "cue_fade_ms": self._data["cue_fade_ms"],
                "cue_fade_ms_max": CUE_FADE_MS_MAX,
                "effects": dict(self._data["effects"]),
                "idle": dict(self._data["idle"]),
                "idle_modes": list(IDLE_MODES),
                "effect_toggles": {
                    tid: {"label": m["label"], "description": m["description"]}
                    for tid, m in EFFECT_TOGGLES.items()
                },
            }
