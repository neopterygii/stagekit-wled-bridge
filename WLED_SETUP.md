# WLED Controller Settings for Stage Kit Bridge

Written against **WLED 16.0.1 "Niji"** (stock `ESP32` release build), the
firmware the reference controller runs. Values below were read off that
controller on 2026-11-16; the menu names are 16.x's. Older firmware (0.14 and
0.15) has the same settings under the same pages, minus *Respect LED Maps*.

The bridge needs two things from WLED: **DDP realtime** on UDP 4048 for pixels,
and the **JSON API** on HTTP port 80 for power, brightness and the idle look.

## The one setting that can break everything: realtime timeout

**Config → Sync Interfaces → Realtime → Timeout: `2500` ms.**

Never `65000`. The field accepts it as an ordinary number, but in the firmware
65000 means *never time out*: after the first DDP frame the controller stays in
realtime mode forever. The strip freezes on the last frame when the bridge
stops, the WLED app cannot change anything, and OTA updates are refused until
the controller is power-cycled. This is unchanged in 16.0.1. It is stored as
`if.live.timeout: 25` (tenths of a second) in `cfg.json`.

2500 ms is also why the idle look appears a couple of seconds after a song
ends: WLED holds the last frame until the timeout lapses.

## LED hardware

**Config → LED & Hardware → Hardware setup / LED outputs:**

| Setting | Reference value | Notes |
|---|---|---|
| LED type | **APA102** | SK9822 strips are APA102-compatible (bus type 51) |
| Length | **120** | Must equal the bridge's `LED_COUNT` |
| Data GPIO | **18** | Board-specific |
| Clk GPIO | **5** | Board-specific |
| Clock speed | **5 MHz** | |
| Color Order | **GRB** | Whichever shows red as red — see below |
| Reversed | Off | Use the bridge's direction setting instead |
| Skip first LEDs | 0 | |
| Maximum PSU Current | **3000 mA**, 55 mA/LED | Size it to your supply |
| Target refresh rate | **42 FPS** | Applies to WLED's own effects only — it does not cap DDP |

Leave the strip as **one segment covering every LED**, with no LED map. The
bridge re-asserts that segment (full length, grouping 1, spacing 0, not
mirrored or reversed) each time it takes the strip live.

### Verify color order

1. With the bridge stopped, set a solid **red** in the WLED UI.
2. Red on the strip: the order is right. Blue or green: try the other orders
   until red, green and blue each show correctly.

## Realtime (DDP)

**Config → Sync Interfaces → Realtime:**

| Setting | Required | Why |
|---|---|---|
| **Receive UDP realtime** | ✅ Checked | Enables DDP on port 4048. There is no separate DDP switch |
| Use main segment only | ❌ Unchecked | DDP addresses the whole strip |
| Respect LED Maps | Either | New in 0.15+. Checked on the reference controller; inert without an LED map |
| **Timeout** | **2500 ms** | See above. Never 65000 |
| **Force max brightness** | ❌ Unchecked | Brightness is the bridge's |
| **Disable realtime gamma correction** | ✅ Checked | The bridge's colours are tuned with WLED's gamma out of the path |
| Realtime LED offset | **0** | |

The *Network DMX input* block on the same page (E1.31 / Art-Net type, port,
universe, *Skip out-of-sequence packets*) does not affect DDP. Leave it alone.

Everything else on the page — WLED broadcast, MQTT, Hue, Alexa — is unused.
The bridge switches *Receive* sync off in the live state itself, so another
WLED node on the LAN cannot repaint the strip mid-song.

## What the bridge changes on the controller

Over the JSON API, without asking:

- **Power** on when YARG connects, off after the idle grace period.
- **Live baseline** on each transition into live output: master brightness,
  transition 0, nightlight off, live override cleared, sync receive off, and
  segment 0 reset to a full-length black solid.
- **Idle look**: the effect, palette and brightness chosen on the dashboard.

Anything you set in the WLED app survives between songs but not past the next
time the bridge takes the strip.

## Pointing the bridge at the controller

Set `WLED_HOST` to the controller's address — in the Unraid template, or in
`docker-compose.yml`:

```yaml
environment:
  WLED_HOST: "192.168.0.53"
```

Give the controller a DHCP reservation; the bridge has no discovery.

## Verifying it works

1. Start the bridge and open its dashboard (`http://<bridge-host>:8080/` by
   default; the port is `STATUS_PORT`).
2. The dashboard should show the controller's WiFi signal, which proves the
   JSON API is reachable.
3. Use **Test Controls** on the dashboard, or `python test_sender.py --pattern
   cycle_cues` from another machine, and watch the strip.
4. While frames arrive, `http://<controller-ip>/json/info` reports
   `"live": true`, `"lm": "DDP"` and the bridge's address in `"lip"`. All
   three clear 2.5 s after the frames stop.

To compare what the bridge is sending with what the controller is displaying,
run `python tools/frame_match.py --wled <controller-ip> --bridge
<bridge-host>:<port>`. On 16.x `/json/live` answers 501; the live view is a
WebSocket (`/ws`, send `{"lv":true}`, wrapped by `tools/wled_peek.py`), and it
reads all zero while WLED is switched off even if DDP is lighting the strip.

## Backup and recovery

Take a backup before changing anything on the controller, and before any
firmware update.

**Back up** (read-only, safe at any time):

```bash
d=~/wled-backups/<controller-ip>/$(date +%F); mkdir -p "$d"; cd "$d"
for p in cfg.json presets.json json/cfg json/info json/state; do
  curl -s -o "$(echo "$p" | tr / -)" "http://<controller-ip>/$p"
done
```

Keep backups **out of this repository**: they carry the network name, MAC and
addresses.

**What the backup does not contain.** WLED keeps passwords in a separate
`wsec.json` that it never serves over the network (404 on 16.0.1). `cfg.json`
records only their lengths (`pskl`). The WiFi and OTA passwords have to be
known from elsewhere; getting `wsec.json` itself needs USB/serial access.

**Restore settings:** Config → Security & Updates → Backup & Restore → restore
`cfg.json`, re-enter the WiFi and OTA passwords, reboot. A restore without the
WiFi password brings the controller up on its own access point (`WLED_xxxxxx`,
address `4.3.2.1`), where the password can be entered. Then confirm the
realtime timeout is 2500 ms.

**Firmware:**

- Record `ver`, `release` and `bootloaderSHA256` from `/json/info` first. The
  rollback image is the release asset matching `release` — for the reference
  controller, `WLED_16.0.1_ESP32.bin` from the
  [v16.0.1 release](https://github.com/wled/WLED/releases/tag/v16.0.1).
  Download it before updating, not after something goes wrong.
- OTA needs the controller *out* of realtime mode. Stop the bridge (or wait
  for it to power the strip down) before uploading.
- If the controller will not boot or will not join WiFi after an update, the
  recovery path is USB/serial: reflash the rollback image with
  [install.wled.me](https://install.wled.me/) or `esptool`, then restore
  `cfg.json` as above. **This path has not been exercised on the reference
  controller**; treat the first use as a test, with the strip disconnected.
- After any update, re-run *Verifying it works* and check the color order, the
  realtime timeout, and that a strobe cue still looks like a strobe.
