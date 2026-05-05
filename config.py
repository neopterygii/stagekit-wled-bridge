"""Configuration via environment variables."""

import os
import sys


def _env_int(name: str, default: int, *, min_val: int | None = None,
             max_val: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"config: {name}={raw!r} is not an integer, using default {default}",
              file=sys.stderr)
        return default
    if min_val is not None and value < min_val:
        print(f"config: {name}={value} below min {min_val}, clamping",
              file=sys.stderr)
        value = min_val
    if max_val is not None and value > max_val:
        print(f"config: {name}={value} above max {max_val}, clamping",
              file=sys.stderr)
        value = max_val
    return value


# YARG UDP intake
YARG_LISTEN_HOST = os.environ.get("YARG_LISTEN_HOST", "0.0.0.0")
YARG_LISTEN_PORT = _env_int("YARG_LISTEN_PORT", 36107, min_val=1, max_val=65535)

# WLED target
WLED_HOST = os.environ.get("WLED_HOST", "192.168.1.100")
WLED_DDP_PORT = _env_int("WLED_DDP_PORT", 4048, min_val=1, max_val=65535)

# LED strip
LED_COUNT = _env_int("LED_COUNT", 120, min_val=1, max_val=10000)

# Rendering
TARGET_FPS = _env_int("TARGET_FPS", 40, min_val=1, max_val=240)

# Brightness 0-255
GLOBAL_BRIGHTNESS = _env_int("GLOBAL_BRIGHTNESS", 255, min_val=0, max_val=255)

# Status page
STATUS_HOST = os.environ.get("STATUS_HOST", "0.0.0.0")
STATUS_PORT = _env_int("STATUS_PORT", 8080, min_val=1, max_val=65535)

# Idle timeout — seconds without YARG packets before turning WLED off (0 = disabled)
IDLE_TIMEOUT = _env_int("IDLE_TIMEOUT", 1800, min_val=0)

# Logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
