"""WLED JSON API helper for power management.

Uses the WLED HTTP JSON API to turn the strip on/off.
Pure stdlib — no requests library needed.
"""

import json
import urllib.request
import urllib.error


class WLEDApi:
    """Controls WLED power state via its JSON API."""

    def __init__(self, host: str):
        self._base_url = f"http://{host}/json"
        self.reachable: bool = False
        self.wifi_info: dict = {}

    def set_power(self, on: bool) -> bool:
        """Turn WLED on or off. Returns True on success."""
        payload = json.dumps({"on": on}).encode()
        req = urllib.request.Request(
            f"{self._base_url}/state",
            data=payload,
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

    def is_on(self) -> bool | None:
        """Check if WLED is currently on. Returns None on failure."""
        try:
            with urllib.request.urlopen(f"{self._base_url}/state", timeout=3) as resp:
                data = json.loads(resp.read())
                self.reachable = True
                return data.get("on", False)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            self.reachable = False
            return None

    def fetch_wifi_info(self) -> dict:
        """Fetch WiFi diagnostics from /json/info. Returns {} on failure."""
        try:
            with urllib.request.urlopen(f"{self._base_url}/info", timeout=3) as resp:
                data = json.loads(resp.read())
                wifi = data.get("wifi", {})
                self.wifi_info = {
                    "signal": wifi.get("signal", 0),
                    "rssi": wifi.get("rssi", 0),
                    "bssid": wifi.get("bssid", ""),
                    "channel": wifi.get("channel", 0),
                }
                return self.wifi_info
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            self.wifi_info = {}
            return {}
