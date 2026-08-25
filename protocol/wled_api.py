"""WLED JSON API helper: power, device-state ownership, and capability lists.

The bridge treats every strip it drives as **owned**. While it is running, the
device's state is the bridge's to define — not whatever the WLED app, a saved
preset, a sync packet from another node, or a reboot last left behind. The
device this was written against boots with `def: {"ps":0,"on":true,"bri":128}`
and `if.live.maxbri:false`, which between them mean an unowned strip comes up
lit at half brightness and then silently halves every realtime pixel the bridge
sends. Ownership is what makes the look reproducible.

Two assertions carry that, and both live here:

  - `assert_live_baseline()` — pushed *before* DDP frames start flowing, so
    realtime pixels land on a device with known master brightness, segment
    geometry, transition and sync behaviour;
  - `apply_idle_look()` — pushed when DDP stops, so "the bridge is not sending"
    is a *defined* state on the strip rather than an undefined one.

Pure stdlib — no requests library needed. Every call is blocking HTTP with a
short timeout and is expected to be run via `asyncio.to_thread`.
"""

import json
import logging
import urllib.request
import urllib.error

log = logging.getLogger(__name__)

# WLED's own realtime timeout (cfg `if.live.timeout`, units of 100 ms) decides
# how long the strip holds the last DDP frame after we stop sending. Until it
# lapses, realtime pixels override whatever state we push — so an idle look
# only becomes visible this long after the final frame. Informational: we read
# the device's value rather than setting it, because forcing it to "never"
# (65000) latches the strip and blocks OTA until a power cycle.
DEFAULT_REALTIME_TIMEOUT_MS = 2500

# Master brightness asserted while the bridge owns the strip. The bridge already
# bakes its own `brightness` setting into every pixel, so anything below 255
# here is a second, invisible dimmer multiplying the first.
OWNED_MASTER_BRIGHTNESS = 255


class WLEDApi:
    """Controls WLED power and device state via its JSON API."""

    def __init__(self, host: str):
        self._base_url = f"http://{host}/json"
        self.reachable: bool = False
        self.wifi_info: dict = {}
        # Effect and palette names, fetched once and cached. The dashboard's
        # idle-look editor offers whatever this firmware actually has rather
        # than a table baked in here that drifts with every WLED release.
        self.effects: list[str] = []
        self.palettes: list[str] = []
        self.realtime_timeout_ms: int = DEFAULT_REALTIME_TIMEOUT_MS

    # ── plumbing ────────────────────────────────────────────────────────

    def _get(self, path: str):
        """GET and decode one JSON endpoint. Returns None on any failure."""
        try:
            with urllib.request.urlopen(f"{self._base_url}{path}", timeout=3) as resp:
                data = json.loads(resp.read())
                self.reachable = True
                return data
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            self.reachable = False
            return None

    def _post_state(self, payload: dict) -> bool:
        """POST one state document. Returns True on success."""
        req = urllib.request.Request(
            f"{self._base_url}/state",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                self.reachable = resp.status == 200
                return self.reachable
        except (urllib.error.URLError, OSError):
            self.reachable = False
            return False

    # ── power ───────────────────────────────────────────────────────────

    def set_power(self, on: bool, transition_ms: int | None = None) -> bool:
        """Turn WLED on or off. Returns True on success.

        `transition_ms` sets WLED's per-request transition (`tt`, in 100 ms
        units) so a power change can fade rather than snap, without disturbing
        the transition the baseline asserts for realtime frames.
        """
        payload: dict = {"on": on}
        if transition_ms is not None:
            payload["tt"] = max(0, round(transition_ms / 100))
        return self._post_state(payload)

    def is_on(self) -> bool | None:
        """Check if WLED is currently on. Returns None on failure."""
        data = self._get("/state")
        if data is None:
            return None
        return data.get("on", False)

    def get_state(self) -> dict | None:
        """Full `/json/state` document, or None on failure."""
        return self._get("/state")

    # ── ownership ───────────────────────────────────────────────────────

    def assert_live_baseline(self, led_count: int) -> bool:
        """Put the device into the known state realtime frames expect.

        Asserted on every transition into live output — bridge start, power-on
        for a song, and adoption of a strip someone powered up outside the
        bridge — rather than continuously, so the WLED app stays usable between
        transitions.

        Each field earns its place:
          `bri`            the bridge's own brightness is the only dimmer;
          `transition: 0`  state changes snap, so cue timing is the bridge's;
          `nl.on: false`   a running nightlight would ramp the strip down under
                           us and then switch it off mid-song;
          `lor: 0`         clear any live-override lock, which would otherwise
                           make WLED ignore our DDP frames entirely;
          `udpn.recv`      the device shipped with sync-receive on, so another
                           WLED node on the LAN could repaint this strip;
          segment 0        spans the whole strip, unfrozen, ungrouped, at full
                           segment brightness and with no mirroring or reversal
                           of its own — the bridge does its own mapping;
          `fx: 0`, black   what the strip falls back to when realtime lapses.
                           Without this it reverts to whatever effect and colour
                           it was last left on (amber solid, on this rig).
        """
        return self._post_state({
            "on": True,
            "bri": OWNED_MASTER_BRIGHTNESS,
            "transition": 0,
            "lor": 0,
            "nl": {"on": False},
            "udpn": {"send": False, "recv": False},
            "seg": [{
                "id": 0, "start": 0, "stop": led_count,
                "on": True, "frz": False, "bri": 255,
                "grp": 1, "spc": 0, "of": 0,
                "fx": 0, "sx": 128, "ix": 128, "pal": 0,
                "col": [[0, 0, 0], [0, 0, 0], [0, 0, 0]],
                "rev": False, "mi": False, "sel": True,
            }],
        })

    def apply_idle_look(self, spec: dict, led_count: int,
                        transition_ms: int = 700) -> bool:
        """Hand the strip a defined look to run on its own while DDP is stopped.

        WLED renders this itself, so the bridge can stop sending frames without
        leaving the strip's appearance undefined. Note the look does not become
        visible until the device's realtime timeout lapses (~2.5 s by default)
        and it stops holding the final DDP frame — the state lands immediately,
        the pixels follow.

        `spec` is the operator's idle-look settings block; `mode` "black" is the
        same push with the effect forced to a black solid, which is a defined
        look too, just an unlit one.
        """
        black = spec.get("mode") == "black"
        colour = list(spec.get("color", [0, 0, 0]))
        return self._post_state({
            "on": True,
            "bri": 0 if black else int(spec.get("brightness", 40)),
            "tt": max(0, round(transition_ms / 100)),
            "lor": 0,
            "nl": {"on": False},
            "seg": [{
                "id": 0, "start": 0, "stop": led_count,
                "on": True, "frz": False, "bri": 255,
                "fx": 0 if black else int(spec.get("effect", 0)),
                "sx": int(spec.get("speed", 128)),
                "ix": int(spec.get("intensity", 128)),
                "pal": 0 if black else int(spec.get("palette", 0)),
                "col": [[0, 0, 0] if black else colour, [0, 0, 0], [0, 0, 0]],
            }],
        })

    # ── capabilities ────────────────────────────────────────────────────

    def fetch_capabilities(self) -> bool:
        """Cache the firmware's effect and palette names and its realtime
        timeout. Called once at startup; the lists do not change at runtime.
        """
        effects = self._get("/effects")
        palettes = self._get("/palettes")
        if isinstance(effects, list):
            self.effects = [str(e) for e in effects]
        if isinstance(palettes, list):
            self.palettes = [str(p) for p in palettes]
        cfg = self._get("/cfg")
        if isinstance(cfg, dict):
            live = cfg.get("if", {}).get("live", {})
            timeout = live.get("timeout")
            if isinstance(timeout, int) and timeout > 0:
                self.realtime_timeout_ms = timeout * 100
        return bool(self.effects and self.palettes)

    def fetch_wifi_info(self) -> dict:
        """Fetch WiFi diagnostics from /json/info. Returns {} on failure."""
        data = self._get("/info")
        if data is None:
            self.wifi_info = {}
            return {}
        wifi = data.get("wifi", {})
        self.wifi_info = {
            "signal": wifi.get("signal", 0),
            "rssi": wifi.get("rssi", 0),
            "bssid": wifi.get("bssid", ""),
            "channel": wifi.get("channel", 0),
        }
        return self.wifi_info
