# Stage Kit → WLED Bridge

A Docker container that receives **YARG / RB3E** Stage Kit lighting data over UDP and outputs it as DDP pixel data to a **WLED** controller driving an APA102/SK9822 LED strip.

Translates all Stage Kit cues (Warm Auto, Cool Auto, Frenzy, Sweep, Big Rock Ending, etc.) into beat-synced color patterns across 120 LEDs with strobe overlay support. Includes a live web dashboard with built-in test pattern controls.

## Features

- **Full Stage Kit cue engine** — 17+ cues with beat-synced patterns, event-triggered flares, and static presets
- **DDP output** — sends raw RGB pixels directly to WLED (no segment config needed)
- **Interleaved zone layout** — 4 color zones (Red, Green, Blue, Yellow) spread across the strip for chase/sweep effects
- **Strobe overlay** — global brightness modulation at 2/4/8/16 Hz
- **Live web dashboard** — real-time zone visualization, event log, and SSE streaming
- **Built-in test controls** — trigger any cue from the web UI with adjustable BPM, no need to run YARG
- **Pure Python stdlib** — zero external dependencies, runs on Python 3.12+
- **Multi-arch Docker image** — builds for both `amd64` and `arm64`

## Quick Start

### Docker Compose (recommended)

```yaml
services:
  stagekit-bridge:
    image: ghcr.io/neopterygii/stagekit-wled-bridge:latest
    container_name: stagekit-wled-bridge
    restart: unless-stopped
    network_mode: host
    environment:
      WLED_HOST: "192.168.0.53"      # Your WLED controller IP
      LED_COUNT: "120"                # Number of LEDs on your strip
      GLOBAL_BRIGHTNESS: "255"        # 0-255
```

```bash
docker compose up -d
```

The status page will be available at `http://<host>:8080/`.

### Run Locally

```bash
git clone https://github.com/neopterygii/stagekit-wled-bridge.git
cd stagekit-wled-bridge
WLED_HOST=192.168.0.53 python main.py
```

## Configuration

All settings are controlled via environment variables:

| Variable | Default | Description |
|---|---|---|
| `YARG_LISTEN_HOST` | `0.0.0.0` | Bind address for YARG UDP packets |
| `YARG_LISTEN_PORT` | `36107` | UDP port for YARG lighting data |
| `WLED_HOST` | `192.168.1.100` | IP address of your WLED controller |
| `WLED_DDP_PORT` | `4048` | DDP port on WLED (default is fine) |
| `LED_COUNT` | `120` | Total number of LEDs on the strip |
| `TARGET_FPS` | `40` | Render/DDP frame rate |
| `GLOBAL_BRIGHTNESS` | `255` | Master brightness (0–255) |
| `STATUS_HOST` | `0.0.0.0` | Bind address for the web status page |
| `STATUS_PORT` | `8080` | HTTP port for the status page |

## Architecture

```
YARG                            This Container                         WLED
┌──────────┐   UDP :36107   ┌──────────────────────┐   DDP :4048   ┌──────────┐
│  Data     │──────────────►│  YARG Packet Parser   │              │  ESP32 + │
│  Stream   │               │         │              │              │  SK9822  │
│           │               │  ┌──────▼──────────┐  │              │  120 LEDs│
└──────────┘               │  │  Cue Engine      │  │──────────►  │          │
                            │  │  (beat-synced    │  │              └──────────┘
                            │  │   zone bitmasks) │  │
                            │  └──────┬──────────┘  │
                            │  ┌──────▼──────────┐  │
                            │  │  LED Mapper      │  │
                            │  │  (zones→120 RGB) │  │
                            │  └──────┬──────────┘  │
                            │  ┌──────▼──────────┐  │
                            │  │  DDP Sender      │  │
                            │  └─────────────────┘  │
                            │                        │
                            │  Status Page :8080     │
                            └──────────────────────┘
```

### LED Layout

The 120-LED strip uses an **interleaved** zone layout for smooth spatial effects:

| Position | Zone |
|---|---|
| 0, 12, 24, 36, 48, 60, 72, 84 | Red (zone 1) |
| 3, 15, 27, 39, 51, 63, 75, 87 | Green (zone 2) |
| 6, 18, 30, 42, 54, 66, 78, 90 | Blue (zone 3) |
| 9, 21, 33, 45, 57, 69, 81, 93 | Yellow (zone 4) |
| All other positions 0–95 | Fill (nearest zone color) |
| 96–119 | Mirror of positions 0–23 |

## Status Page

The built-in web dashboard at port 8080 shows:

- **Connection status** — whether YARG packets are being received
- **Current cue** — active Stage Kit lighting cue name
- **BPM / Strobe / Packets/sec / DDP frames** — real-time metrics
- **Zone bitmask visualization** — 4×8 LED grid showing which zone LEDs are on
- **Event log** — scrolling log of cue changes, beats, and strobe events

### Test Controls

The dashboard includes a **Test Patterns** panel for triggering cues directly from the browser:

- Click any cue button to activate it with simulated beats
- Adjust **BPM** with the slider (60–240)
- Toggle **strobe** at different speeds
- Click **Stop Test** to return to blackout

This is especially useful for verifying your WLED/LED setup without running YARG.

## YARG Setup

In YARG, enable the UDP data stream:

1. Open **Settings → All Settings → Experimental**
2. Enable **"UDP Data Stream"**

That's it — YARG will broadcast lighting data on UDP port 36107 to all devices on the local network.

> **Note:** The "Enable Stage Kit" and "Enable DMX" settings are for **USB Stage Kit hardware** and **sACN/DMX lighting** respectively. They are not needed for this bridge.

### Troubleshooting

If the status page shows "Disconnected" with 0 packets/sec:

| Check | Details |
|---|---|
| **YARG setting** | "UDP Data Stream" must be enabled in Settings → Experimental |
| **Firewall** | UDP port 36107 must not be blocked on the host running the container |
| **Network mode** | The container must use `network_mode: host` to receive UDP broadcasts |
| **Same subnet** | YARG PC and the container host must be on the same network/VLAN |
| **Play a song** | Full lighting data (BPM, beats, cues) requires an active song — though YARG does send basic data from the menu screen |
| **Test the strip first** | Use the built-in Test Patterns on the status page to verify WLED/LED connectivity independently of YARG |

## WLED Setup

See [WLED_SETUP.md](WLED_SETUP.md) for detailed WLED configuration instructions.

**Key requirements:**
- LED type: SK9822 (APA102-compatible) with Data + Clock pins
- LED count: match your `LED_COUNT` setting
- "Receive UDP realtime" must be checked (enables DDP automatically in WLED 0.14.x)

## Supported Cues

| Cue | Description |
|---|---|
| Warm Automatic | Red + Yellow chase patterns |
| Cool Automatic | Blue + Green chase patterns |
| Big Rock Ending | All zones, rotating full-on flashes |
| Frenzy | Fast alternating all zones |
| Searchlights | Single-LED sweep on Red + Blue |
| Sweep | Bidirectional sweep on Blue + Green |
| Harmony | Yellow + Red rotating single LEDs |
| Flare Slow/Fast | Beat-triggered red/yellow burst, blue/green static |
| Silhouettes | Static blue |
| Default / Verse | Blue rotating pairs |
| Chorus | Red rotating pairs + full yellow |
| Stomp | Beat-triggered alternating red/yellow |
| Dischord | Alternating red/blue quarter-beat |
| Intro | Static blue + green |
| Menu | Blue bidirectional sweep |
| Score | All zones full on |
| Blackout | All off |

## Development

### Test Packet Sender

A standalone test sender is included for development without YARG:

```bash
# Cycle through all cues
python test_sender.py --pattern cycle_cues

# Specific pattern at custom BPM
python test_sender.py --pattern warm_loop --bpm 140

# Available patterns: all_on, warm_loop, cool_loop, sweep,
#                     big_rock_ending, strobe_fast, cycle_cues
```

### Build Docker Image Locally

```bash
docker build -t stagekit-wled-bridge .
docker run --network host -e WLED_HOST=192.168.0.53 stagekit-wled-bridge
```

### Project Structure

```
├── main.py                  # Entry point — asyncio event loop
├── config.py                # Environment variable configuration
├── status_server.py         # Web dashboard + SSE + test controls
├── test_sender.py           # Standalone fake YARG packet generator
├── protocol/
│   ├── yarg_packet.py       # YARG UDP packet parser & enums
│   └── ddp_sender.py        # DDP protocol sender
├── effects/
│   ├── cue_engine.py        # Stage Kit cue state machine
│   └── mapper.py            # Zone bitmasks → RGB pixel data
├── Dockerfile
├── docker-compose.yml
└── .github/workflows/
    └── docker.yml           # CI: build & push to ghcr.io
```

## Hardware

Built for and tested with:
- **LEDs:** BTF-LIGHTING SK9822 (APA102-compatible), 60 LEDs/m, 2× 1m strips daisy-chained (120 total)
- **Housing:** 1m aluminum channels with frosted diffusers
- **Controller:** ESP32 running WLED 0.14.4 "Hoshi"
- **Protocol:** DDP over WiFi (UDP port 4048)

## License

MIT
