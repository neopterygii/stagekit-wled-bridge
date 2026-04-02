"""Persistent bridge settings (brightness, color palette).

Settings are stored in a JSON file and survive restarts. The status page
can modify them at runtime via the /api/settings endpoint.
"""

import json
import os
import threading

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

DEFAULT_SETTINGS = {
    "brightness": 255,
    "palette": "default",
}


class BridgeSettings:
    """Thread-safe persistent settings manager."""

    def __init__(self, path: str = SETTINGS_FILE):
        self._path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULT_SETTINGS)
        self._load()

    def _load(self):
        try:
            with open(self._path, "r") as f:
                stored = json.load(f)
            # Validate and merge
            if isinstance(stored.get("brightness"), int):
                self._data["brightness"] = max(0, min(255, stored["brightness"]))
            if stored.get("palette") in PALETTES:
                self._data["palette"] = stored["palette"]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except OSError as e:
            print(f"Settings: failed to save: {e}")

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
    def zone_colors(self) -> dict[str, tuple[int, int, int]]:
        """Current palette's zone color mapping."""
        with self._lock:
            return dict(PALETTES[self._data["palette"]]["colors"])

    def snapshot(self) -> dict:
        """Return current settings for the status page."""
        with self._lock:
            palette_key = self._data["palette"]
            return {
                "brightness": self._data["brightness"],
                "palette": palette_key,
                "palettes": {k: v["label"] for k, v in PALETTES.items()},
                "colors": PALETTES[palette_key]["colors"],
            }
