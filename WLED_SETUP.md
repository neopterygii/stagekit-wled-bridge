# WLED Controller Settings for Stage Kit Bridge

## LED Hardware Configuration

In WLED web UI → **Config** → **LED Preferences**:

| Setting | Value | Notes |
|---------|-------|-------|
| LED Type | SK9822 (or APA102) | Your BTF-LIGHTING SK9822 strips |
| Color Order | BGR | SK9822 typically uses BGR; verify with a single-color test |
| Length | **120** | 2 × 60 LED strips daisy-chained |
| GPIO Data | Your data pin | ESP32 default is typically GPIO16 |
| GPIO Clock | Your clock pin | SK9822/APA102 needs a clock line; ESP32 default GPIO4 |

## Sync/Realtime Configuration

In WLED web UI → **Config** → **Sync Interfaces**:

| Setting | Value | Notes |
|---------|-------|-------|
| **Receive DDP** | ✅ Enabled | This is how the bridge sends pixel data |
| DDP port | **4048** | Default, must match `WLED_DDP_PORT` env var |
| Realtime receive timeout | **2500** ms | How long WLED waits before reverting to normal mode |
| Force max brightness | ❌ Off | Let the bridge control brightness |
| Realtime override | **Always** | DDP takes priority over local effects |

### Other Sync Settings (optional)

| Setting | Recommended |
|---------|------------|
| Receive UDP Realtime | Can leave enabled, won't conflict |
| Send WLED notifications | ❌ Off (not needed) |
| Receive WLED notifications | ❌ Off (avoid other WLED instances interfering) |
| E1.31 / Art-Net | Off unless you need them for other purposes |

## Network

- The WLED controller must be reachable at the IP configured in `WLED_HOST`
- Use a **static IP** or DHCP reservation for the controller
- The bridge sends UDP to port 4048 — no firewall rules needed on the ESP side
- Using `network_mode: host` in Docker ensures the container can send to the WLED IP directly

## Verifying DDP reception

1. Start the bridge: `docker compose up`
2. In a separate terminal, run: `python test_sender.py --pattern all_on`
3. The strip should light up with all colors
4. In WLED web UI, a small icon should appear indicating realtime mode is active

## Recommended WLED Version

This bridge is tested against **WLED 0.14.4 "Hoshi"**. DDP support has been
stable since 0.13.x. No special build or usermod is required.
