"""Persistent bridge settings (brightness, color palette).

Settings are stored in a JSON file and survive restarts. The status page
can modify them at runtime via the /api/settings endpoint.
"""

import json
import logging
import os
import threading

log = logging.getLogger(__name__)

SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "/data/settings.json")

# ── Color palettes ─────────────────────────────────────────────────
# Each palette maps the 4 zone names to (R, G, B) tuples.
# "red" zone doesn't have to be red — it's just the Stage Kit zone ID.

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

DEFAULT_SETTINGS = {
    "brightness": 255,
    "palette": "default",
    "fps": 40,
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
    # Each toggle defaults on unless its registry row opts out with default:False.
    "effects": {tid: meta.get("default", True) for tid, meta in EFFECT_TOGGLES.items()},
}


class BridgeSettings:
    """Thread-safe persistent settings manager."""

    def __init__(self, path: str = SETTINGS_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULT_SETTINGS)
        # Deep-copy the nested effects dict so instances (and the module-level
        # DEFAULT_SETTINGS) don't share one mutable object.
        self._data["effects"] = dict(DEFAULT_SETTINGS["effects"])
        self._writable = self._probe_writable()
        if not self._writable:
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
                "fps_options": list(VALID_FPS),
                "direction": self._data["direction"],
                "blur_amount": self._data["blur_amount"],
                "venue_intensity": self._data["venue_intensity"],
                "section_intensity": self._data["section_intensity"],
                "effects": dict(self._data["effects"]),
                "effect_toggles": {
                    tid: {"label": m["label"], "description": m["description"]}
                    for tid, m in EFFECT_TOGGLES.items()
                },
            }
