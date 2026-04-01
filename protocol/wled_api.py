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
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def is_on(self) -> bool | None:
        """Check if WLED is currently on. Returns None on failure."""
        try:
            with urllib.request.urlopen(f"{self._base_url}/state", timeout=3) as resp:
                data = json.loads(resp.read())
                return data.get("on", False)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return None
