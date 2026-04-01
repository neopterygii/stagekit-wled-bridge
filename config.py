"""Configuration via environment variables."""

import os

# YARG UDP intake
YARG_LISTEN_HOST = os.environ.get("YARG_LISTEN_HOST", "0.0.0.0")
YARG_LISTEN_PORT = int(os.environ.get("YARG_LISTEN_PORT", "36107"))

# WLED target
WLED_HOST = os.environ.get("WLED_HOST", "192.168.1.100")
WLED_DDP_PORT = int(os.environ.get("WLED_DDP_PORT", "4048"))

# LED strip
LED_COUNT = int(os.environ.get("LED_COUNT", "120"))

# Rendering
TARGET_FPS = int(os.environ.get("TARGET_FPS", "40"))

# Brightness 0-255
GLOBAL_BRIGHTNESS = int(os.environ.get("GLOBAL_BRIGHTNESS", "255"))

# Status page
STATUS_HOST = os.environ.get("STATUS_HOST", "0.0.0.0")
STATUS_PORT = int(os.environ.get("STATUS_PORT", "8080"))
