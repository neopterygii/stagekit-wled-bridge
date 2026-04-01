# WLED Controller Settings for Stage Kit Bridge

Your controller: **WLED 0.14.4 "Hoshi"** at `192.168.0.53`

## Current LED Hardware (already configured)

**Config → LED Preferences → Hardware setup** — these are already correct:

| Setting | Current Value |
|---------|--------------|
| Length | **120** |
| Data GPIO | **18** |
| Clk GPIO | **5** |
| Color Order | (verify — see below) |
| Reversed | Off |

**Config → LED Preferences → Advanced:**

| Setting | Current Value | Notes |
|---------|--------------|-------|
| Target refresh rate | **42 FPS** | Good, matches our 40 FPS output |

### Verify Color Order

SK9822 strips can be BGR or RGB. To test:
1. Open WLED web UI, set a solid **red** color
2. If the strip shows **red** → color order is correct
3. If it shows **blue** → change Color Order to **BGR**

## Sync Settings to Change

**Config → Sync Interfaces → Realtime section:**

| Setting | Required Value | Why |
|---------|---------------|-----|
| **Receive UDP realtime** | ✅ **Checked** | This enables DDP reception on port 4048 |
| Use main segment only | ❌ Unchecked | Let DDP address all LEDs |
| Type | **E1.31 (sACN)** | Leave as-is, doesn't affect DDP |
| **Timeout** | **2500** ms | Already set. How long before WLED reverts to normal mode after DDP stops |
| **Force max brightness** | ❌ **Unchecked** | Let the bridge control brightness |
| Disable realtime gamma correction | ❌ Unchecked | Keep gamma correction on |
| Realtime LED offset | **0** | No offset needed |
| Skip out-of-sequence packets | ✅ Checked | Helps with packet ordering |

**Everything else on that page can stay as-is.** The WLED broadcast, MQTT, Hue, and Alexa sections are not used by this bridge.

### Key Point: DDP is automatic

In WLED 0.14.4, there's no separate "Enable DDP" checkbox. DDP reception is **automatically enabled** when "Receive UDP realtime" is checked. WLED listens on **port 4048** for DDP packets alongside E1.31/Art-Net on their respective ports.

## docker-compose.yml

Update `WLED_HOST` to match your controller:

```yaml
environment:
  WLED_HOST: "192.168.0.53"
```

## Verifying It Works

1. Start the bridge container
2. Run `python test_sender.py --pattern all_on` (or use the test sender inside the container)
3. The strip should light up with all four zone colors
4. The WLED web UI will show a small **realtime mode** indicator when DDP data is being received
5. Open `http://<bridge-host>:8080/` for the live status dashboard
