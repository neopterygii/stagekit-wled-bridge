# Backlog

Per the mission-control protocol, this is the full backlog; the dashboard in
`~/mission-control/README.md` carries only the headline plus a link here.

This file holds open work and a short list of what closed most recently.
Finished write-ups live in [`BACKLOG_ARCHIVE.md`](BACKLOG_ARCHIVE.md); when an
item closes, move its write-up there and leave one line under *Recently
closed*.

Item status: `OPEN` · `IN PROGRESS` · `BLOCKED` · `DONE` · `DEFERRED`.

Item kind, which follows the status on each heading:

| Kind | Means |
|---|---|
| `bugfix` | The bridge does the wrong thing in production today. |
| `feature` | A capability that does not exist yet. |
| `perf` | Same behaviour, less latency or less work per frame. |
| `refactor` | Same behaviour, better structure or testability. |
| `evidence` | A measurement or investigation. May end in no code at all. |
| `ops` | Firmware, backup, recovery — the rig rather than the repo. |
| `docs` | The documentation says something the code or the rig no longer does. |
| `hardening` | Nothing is wrong today, but one fault or one hostile input would make it so. |
| `review` | A question for the operator about how something should look or behave. |

Kind is about the work, not its importance: an `evidence` item can outrank a
`bugfix`, and twice already has.

## Next up (refreshed 2026-11-16)

**Deployment is not tracked here.** The operator updates the container template
and judges changes on the rig on their own time; this backlog covers the work in
the repo. Query the running container when an item needs live evidence
(`AGENTS.md` has the triage commands) — do not add "deploy" or "flip the
template" as a backlog step.

In order:

1. **W4 — read-only state, optionally over MQTT** (`feature`).
2. **W5 — WLED controller backup and recovery** (`ops`). The controller is
   already on 16.0.1, so there is no upgrade to qualify; what is left is
   operator work at the device. [Write-up below](#open-ops--w5-be-able-to-put-the-wled-controller-back).
3. **Review: should the smooth cues show the beat?** (`review`) — new
   2026-11-02. [Write-up below](#open-review--the-smooth-cues-are-tempo-locked-but-nothing-on-the-strip-marks-the-beat).

W4 and W5 are workstreams 4 and 5 of the reliability push described in
`VISION.md` under "Current focus". Workstreams 1 to 3 (the `v1.0.0` release,
the replay harness and the alignment trim) are done.

**Deferred:** a multi-WLED logical canvas with performer-region routing
(`feature`). Blocked on having enough equivalent hardware to test geometry and
partial failure.

The `evidence` items further down are open questions, not queued work. Each
says what would reopen it.

### Recently closed

- ~~W3 — lighting alignment trim~~ — built 2026-11-16;
  [write-up](BACKLOG_ARCHIVE.md#done-2026-11-16-feature--w3-lighting-alignment-trim-output-delay-and-calibration-flash).
  An Output Delay slider (0–500 ms, default 0) holds frames before they are
  sent, and a Calibration Flash toggle shows a white flash on each beat to set
  it by. It can only make the light later. Not judged on the rig yet.
- ~~The dashboard's JavaScript had no automated check~~ — built 2026-11-16;
  [write-up](BACKLOG_ARCHIVE.md#done-2026-11-16-hardening--the-dashboards-javascript-had-no-automated-check).
  The page script is syntax-checked and run in Node against real snapshots on
  every test run. Not a browser: layout and CSS are still checked by eye.
- ~~The version said 1.1.0 for three months and nothing checked it~~ — built 2026-11-16;
  [write-up](BACKLOG_ARCHIVE.md#done-2026-11-16-hardening--the-version-said-110-for-three-months-and-nothing-checked-it).
  `__version__` is 1.2.0, `CHANGELOG.md` has the draft notes, and CI refuses a
  `v*` tag that disagrees with either. **The tag itself is not made**: dating
  the changelog entry and pushing `v1.2.0` is the operator's call.
- ~~The docs described a project three months older than the code~~ —
  refreshed 2026-11-16;
  [write-up](BACKLOG_ARCHIVE.md#done-2026-11-16-docs--the-docs-described-a-project-three-months-older-than-the-code).
  This file was 2,226 lines, most of them finished work; the finished
  write-ups moved to `BACKLOG_ARCHIVE.md`.
- ~~`WLED_SETUP.md` described firmware the controller no longer runs~~ —
  rewritten 2026-11-16 for 16.0.1 with a fresh backup; the recovery steps that
  need hands on the device stay open as
  [W5](#open-ops--w5-be-able-to-put-the-wled-controller-back).
- ~~Cross-site POSTs to the control endpoints were accepted~~ — built
  2026-11-16;
  [write-up](BACKLOG_ARCHIVE.md#done-2026-11-16-hardening--any-web-page-on-the-lan-could-drive-the-bridges-controls).
- ~~Test patterns ran forever and could be started over a live song~~ — built
  2026-11-16;
  [write-up](BACKLOG_ARCHIVE.md#done-2026-11-16-bugfix--a-dashboard-test-pattern-ran-until-someone-stopped-it-and-could-run-over-a-live-song).
  A pattern now ends after 10 minutes or when YARG starts sending, and the
  test controls answer 409 while YARG is connected. No golden digest moved.
- ~~A crashed render thread stayed dead~~ — built 2026-11-16;
  [write-up](BACKLOG_ARCHIVE.md#done-2026-11-16-bugfix--a-crashed-render-thread-left-the-strip-dark-until-someone-restarted-the-container).
  The bridge now replaces the thread itself and exits non-zero if it cannot.
  The exit only helps a container that has a restart policy. No golden digest
  moved.
- ~~Version-proof the datagram parser~~ — built 2026-11-16;
  [write-up](BACKLOG_ARCHIVE.md#done-2026-11-16-refactor--the-datagram-parser-hardcodes-absolute-offsets-so-any-yarg-field-insertion-silently-renders-wrong-lighting).
  The layout is one table (`protocol/schema.py`) and a daily workflow compares
  it with YARG `dev`. No golden digest moved.
- ~~A stuck one-shot input could pin an overlay on~~ — built 2026-11-16;
  [write-up](BACKLOG_ARCHIVE.md#done-2026-11-16-bugfix--one-shot-inputs-were-not-guarded-against-a-stuck-value).
  Option 3 of the parser item above. Bounds the next layout shift; does not
  prevent it. Three synthetic golden digests moved.
- ~~The dashboard showed bitmasks, not the pattern~~ — requested and built
  2026-11-02; [write-up](BACKLOG_ARCHIVE.md#done-2026-11-02-feature--the-dashboard-showed-zone-bitmasks-not-the-pattern).
  A Stage Kit pod visualizer that replays every mask change instead of sampling
  at the 10 Hz page rate.

Everything closed before November 2026, and every `DONE` write-up, is in
[`BACKLOG_ARCHIVE.md`](BACKLOG_ARCHIVE.md).

---

## OPEN `ops` — W5: be able to put the WLED controller back

**Rewritten 2026-11-16.** W5 was "qualify a 0.14.4 firmware upgrade". The
operator upgraded the controller to stock WLED 16.0.1 outside the plan (found
2026-11-02) and it has run the rig since, so that question is closed by the
fact. The risk the item was guarding against is unchanged: there is one
controller, and nobody has shown it can be recovered.

### What exists

- A GET-only backup of the 16.0.1 state, taken 2026-11-16, at
  `~/wled-backups/192.168.0.53/2026-11-16/` (outside every repository), with a
  `NOTES.txt` that diffs it against the 2026-07-28 backup of 0.14.4.
- The settings the bridge depends on survived the upgrade: `if.live.timeout`
  **25** (2.5 s — the latch fix held), realtime gamma correction disabled,
  force-max-brightness off, 120 px APA102 on GPIO 18/5 at 5 MHz, 3 A limiter.
- `WLED_SETUP.md` rewritten for 16.0.1 with the backup command, what a restore
  needs, and the firmware rollback path.
- The identity of the running build: `release: ESP32`, `repo: wled/WLED`,
  `brand: WLED` — the upstream build, not the Athom vendor image. The rollback
  image is therefore `WLED_16.0.1_ESP32.bin` from the upstream v16.0.1 release.

### What is still open

All three need the operator at the controller; none is bridge code.

1. **Passwords.** `wsec.json` is not served over HTTP (404), so the backup has
   only password lengths: WiFi PSK 8 characters, OTA password 7, no AP
   password. Confirm both are recorded somewhere a restore can reach. Without
   the PSK a restored controller comes up on its own open access point.
2. **Rollback image on disk.** Download `WLED_16.0.1_ESP32.bin` and keep it
   beside the backup with its SHA-256. Not done here.
3. **One exercised USB/serial reflash.** Until this has been done once the
   recovery path is a belief. Do it with the strip disconnected, restore
   `cfg.json`, re-enter the passwords, then check the realtime timeout reads
   2500 ms before the bridge sends a frame.

### Unexplained, worth a look during step 3

`/json/info` on 16.0.1 reports `flash: 8` (MB, from `ESP.getFlashChipSize()`).
The Athom build reported `getflash: 4194304` and the July baseline recorded
4 MB. The filesystem is still 983 KB, which is the 4 MB partition layout, so
the running image is the 4 MB one either way. Read the chip with
`esptool flash_id` before choosing an image for a full erase-and-flash.

### What this is not

Not a reason to change firmware again. Not blocked by the realtime latch any
more: the timeout is 25, the controller leaves realtime mode 2.5 s after the
bridge stops, and OTA works whenever the bridge is not sending. The
`tools/wled_lag.py` and `tools/frame_match.py` breakage from the upgrade
(`/json/live` now 501) was already fixed on 2026-11-02 via the WebSocket peek.

---

## OPEN `review` — The smooth cues are tempo-locked, but nothing on the strip marks the beat

**Requested:** 2026-11-02 by the operator — "review if our smooth effects like
the auto ones and sweep should have a beat/bpm indicator."

The cues in question glide rather than step: `WARM_AUTOMATIC` and
`COOL_AUTOMATIC` (an opposing-pair chase at `cycles_per_beat=0.25` over 4
steps, plus a counter-rotating accent at `0.125` over 8) and `SWEEP` (`0.25`
over 4, trails 10). All three are already beat-locked: each advances exactly
one step per beat and launches in phase with the beat clock. But the motion is
continuous, so the tempo shows only as speed — nothing on the strip lands *on*
the beat. Cues like `BIG_ROCK_ENDING` already pump brightness on the beat
through the beat oscillator, so the machinery exists.

Decide, with the operator watching the rig:

- **Whether** these cues should mark the beat at all. Their job in YARG's
  vocabulary is ambient motion for calmer sections; an accent could make them
  busier than the chart intends. Check what YALCY and a real Stage Kit show for
  them (Stage Kit steps the masks, so its beat is visible as the step) and what
  photonics-dmx does.
- **Where**: on the strip (a gentle on-beat brightness swell, a downbeat-only
  accent, or easing the glide so it dwells on each step), or only as an
  indicator on the dashboard's Stage Kit card.
- **Scope**: whether it extends to the other glide cues (`HARMONY`,
  `SEARCHLIGHTS`, the scanners) for consistency.

Confirm what the operator means by "beat/bpm indicator" before building — see
the memory note on confirming rig reports. Any strip change moves the golden
replay digests; which fixtures move is the evidence.

---

## OPEN `evidence` — VenueSize is hardcoded to Small, so Phase 8 density branching is dead code

**Found:** 2026-07-27, while tracing venue sources for the library inventory.

YARG never varies the venue size it sends. In
`YARG/Assets/Script/Integration/Data Stream/DataStreamGameplayMonitor.cs:85`:

```csharp
// This should be read from the venue itself eventually, but for now, we'll just randomize it.
DataStreamController.MLCVenueSize = (DataStreamController.VenueType)Random.Range(1, 2);
```

Unity's `int` overload of `Random.Range` is max-exclusive, so this always
returns `1` — `VenueType.Small`. Never `Large`, never `None`. The comment says
"randomize", but it cannot.

**Consequence:** the Phase 8 venue-size density branching
(`tests/test_venue_size.py`) can only ever take the Small path. The strip is
permanently thinned to single-head chase steps with halved sparkle, and the
Large fill-out path never runs on real input. `VISION.md` describes the feature
as branching on small/large, which does not match what arrives on the wire.

**Corroborated live, 2026-07-27.** The running Tower container's
`/api/status` reported `"venue_size": "Small"` (`venue_size_id: 1`) while
attached to a live YARG session mid-song. That is consistent with the hardcode
but is still a single observation of one song, so it cannot by itself prove
`Large` is unreachable. A capture across several songs — which the replay
harness (item 2) will produce — is what settles it. Do not change behaviour on
the strength of the source read alone; that is the whole point of `AGENTS.md`'s
"check the running system first" rule.

Options once confirmed:

1. Treat Small as the only real input and simplify/retire the Large path,
   keeping the density transform as an operator-selectable taste control rather
   than a venue-driven one.
2. Keep the code and drive it from an operator setting until YARG populates the
   field for real.
3. Upstream a fix to YARG so venue size is read from the venue.

Do not delete the Large path before the capture confirms it — YARG nightly may
fix this at any time, and the transform itself is tested and correct.

---

## OPEN `evidence` — Venue fixture coverage now has a measured answer

**Found:** 2026-07-27, from the `venue_scan` library inventory
(`~/yarg-lighting/evidence/venue-inventory-2026-07-27.md`).

The replay/capture harness (VISION "Next push" item 2) wants fixtures covering
MIDI venue, Milo venue, and auto-generation. Two of those are now settled:

- **MIDI venue fixtures are abundant** — 16,961 songs, and the inventory ranks
  them by richness.
- **A Milo-only fixture does not exist in this library.** Of 460 songs shipping
  a Milo, 459 of those Milos are stubs with no `song.anim` member. The single
  Milo with real animation data (Hazbin Hotel Cast — "Happy Day In Hell") is
  shadowed by its own non-empty MIDI venue, so YARG's `IsEmpty` gate blocks the
  load. Exercise the Milo path directly from that file rather than waiting for a
  song that triggers it naturally.
- **Auto-generation fixtures are the majority case** — 18,626 songs, including
  every one of the 13,169 `.chart` songs.

**Also worth encoding in the harness:** 96.1% of the library receives
synthesised fog, because fog generation runs even on fully authored venues. Fog
is not an edge case; it is the default.

---

## OPEN `evidence` — Render priority is now measured, not assumed

**Found:** 2026-07-27, same inventory.

Cue frequency across every authored venue in the library:

- `next` (keyframe advance) — 1,400,989 events across 7,966 songs, roughly 10×
  the next-most-common cue. Legacy note numbers can express *only* keyframes
  (48/49/50), never a lighting cue, which is why this dominates.
- Then `flare_fast`, `blackout_fast`, `dischord`, `warm_manual`, `stomp`,
  `chorus`, `cool_manual`.
- Spotlights are heavy: ~86k guitar, ~79k drums, ~78k vocals.
- `bonus_fx` (13,871) outnumbers `fog_on` (9,721).

Worth checking the cue engine's handling of `next` specifically — at that volume
its behaviour dominates the look far more than any individual cue.

---

## OPEN `evidence` — WLED drifts out of sync after hours of DDP; only a physical power-cycle clears it

**Observed:** 2026-07-27 by the operator. Left paused on a song for **4+ hours**;
the strip ended up visibly out of sync with the game. Turning WLED off from the
container did **not** clear it — only pulling the controller's power did.

That last detail is the important one: **the bad state lives on the ESP32, not in
the bridge.** A container-side power-off is a JSON API call; it doesn't reset the
device. So this is very unlikely to be a cue-engine or render-thread bug, and
correspondingly unlikely to be fixed by anything in this repo.

### Root cause found for the stuck half — 2026-07-28

**The controller is latched in realtime mode permanently.** Full write-up with
the source chain and the live readings:
`~/yarg-lighting/evidence/wled-realtime-latch-2026-07-28.md`.

`if.live.timeout` on `192.168.0.53` is **650**, which `cfg.cpp` scales to
`realtimeTimeoutMs = 65000` — and 65000 is precisely the sentinel value
`realtimeLock()` maps to `realtimeTimeout = UINT32_MAX`, i.e. *never time out*.
The WLED settings page presents the field as a plain `max="65000"` number input
with no hint that the top of its own range means "never", so this is a very easy
setting to land on by accident while trying to stop the strip dropping out of
realtime mid-song.

Because `wled.cpp:96` gates the main loop on not being in realtime, a latched
device permanently skips `strip.service()` — so no state change reaches the
pixels (that's why `{"on": false}` did nothing), the strip holds its last DDP
frame indefinitely, and **`ArduinoOTA.handle()` never runs, so OTA is blocked**.
That last one was not previously known and directly affected W5 as it was then planned (a firmware upgrade): the
controller cannot be OTA-upgraded while latched, and it latches on the first DDP
frame after every boot.

Confirmed live on 2026-07-28: twelve hours after the bridge's last
`WLED: powered OFF (idle)`, with `ddp_frames_sent` static across repeated
samples, the device still reported `"live": true, "lm": "DDP"` while
`"on": false`.

**Fix (device config, not code): `DONE` 2026-07-28.** Timeout set to `2500`
(`if.live.timeout: 25`) and the controller rebooted — a config change alone does
not clear an already-latched `realtimeTimeout`, because `realtimeLock()` only
recomputes it when it is not already `UINT32_MAX`. Reversible by setting `65000`
back.

Verified by driving all-black DDP for 3 s and watching the device: `live` went
`false → true (lm DDP, 58 fps) → false` ~3 s after the stream stopped, with
strip FPS returning to 38. `strip.service()` runs again, so state changes reach
the pixels and OTA is reachable between streams.

A pre-change backup is at `~/wled-backups/192.168.0.53/2026-07-28/`. Note it is
**not a complete restore image**: WLED never serves `wsec.json` over HTTP, so
the WiFi/OTA passwords are absent (only their lengths are recorded). See
[W5](#open-ops--w5-be-able-to-put-the-wled-controller-back).

**Still open:** progressive drift *during* an active stream. The evidence rules
out the "growing UDP backlog" hypothesis — DDP lands on an AsyncUDP callback
that overwrites the realtime pixel buffer rather than queueing — but heap
fragmentation and WiFi power-save are untested. That needs the soak run, which
is only meaningful once the latch is fixed, because until then the device never
returns to a known state between tests.

**Instrumentation is built.** Two tools, because the proxies and the actual
symptom are different measurements:

`tools/wled_soak.py` samples `/json/info` (heap, uptime, RSSI, live/lm, device
FPS, HTTP reply time) and the bridge's `/api/status` (render gaps, DDP send
timing, stalls, counters) onto one timeline. Its `analyse` subcommand reports
trends, flags counter resets that void deltas, and detects the realtime latch.

`tools/wled_lag.py` measures the symptom itself — how far the strip is behind
the bridge. It works because the two ends are directly comparable on this rig:
the bridge publishes what it just rendered at `/api/status` `preview.strip`
(60 cells, a 2:1 downsample of the strip), WLED publishes what it is displaying
at `/json/live` (120 hex triples out of the live buffer DDP writes into), and
with `bri: 128` and `if.live["no-gc"]: true` the device is just a linear scale
of the bridge. Each side is reduced to one scalar per sample (total frame
brightness) and the lag is the shift that maximises cross-correlation;
normalising each series cancels the brightness scale and the downsample.

**Read only the deltas, not the absolute number.** The two endpoints are polled
over HTTP with very different round trips (Tower is sub-millisecond, the ESP32
is ~30 ms), and the probe attributes each reading to the midpoint of its own
request, so there is an uncorrected systematic offset — the first baseline came
back *negative* (`-60 ms`, correlation 0.66), which is physically impossible and
is exactly that offset showing. Constant latency vs. progressive drift is
answered by whether the number *moves* across a long soak, which is unaffected.

```bash
.venv/bin/python -m tools.wled_soak log --out soak.jsonl --interval 30 --duration 6h
.venv/bin/python -m tools.wled_soak analyse soak.jsonl
.venv/bin/python -m tools.wled_lag probe --seconds 45 --out lag-t0.json
```

What we know about the conditions:

- Pausing does **not** stop the packet stream. YARG keeps sending ~90
  datagrams/s with `paused=2`, so `IDLE_TIMEOUT` never fires and the bridge keeps
  rendering and sending DDP the whole time.
- At 60 FPS that is roughly **860,000 DDP frames** and ~310 MB of UDP over four
  hours, sustained, to a WiFi-attached ESP32.
- Live baseline was WLED 0.14.4 when this was written (16.0.1 since
  2026-11-02), APA102/type 51 on data GPIO 18 / clock GPIO 5 at
  5 MHz, 120 pixels (`evidence/wled-live-baseline-2026-07-27.md`).

Candidate causes, in rough order of suspicion — **none confirmed** when first
written; see the root-cause section above for what has since been settled:

1. ESP32-side accumulation over a long realtime session (heap fragmentation, a
   growing UDP backlog, or a WiFi power-save state) adding latency that never
   drains. *(The UDP-backlog part of this is now ruled out.)*
2. APA102 clock/data timing degrading at 5 MHz once the device has been warm and
   busy for hours.
3. Something in WLED's realtime/DDP timeout handling that behaves differently
   after a very long uninterrupted stream.

**"Desync" needs pinning down before anything else.** It could be constant added
latency (lights consistently late), progressive drift (getting later), or dropped
frames making motion stutter. Those have different causes. Cheapest evidence:

- Reproduce with the replay CLI rather than a 4-hour game session — replay a
  capture on a loop for hours and watch, which is exactly what the harness is
  for.
- Read `/json/info` (heap, uptime, WiFi RSSI) periodically across the run and see
  what moves. The bridge already fetches WiFi info; extend that to log heap.
- Compare bridge-side timing over the same window (`render.gap_ms_max`,
  `ddp.send_us_max`, `stalls`) to rule the host out — if bridge telemetry stays
  flat while the strip drifts, the device is proven guilty.
- Consider whether pausing should stop DDP output entirely after some period.
  A paused game does not need 60 FPS of unchanged frames, and not sending is the
  most reliable way to avoid whatever the long-stream state is.

Related: when this was written it was an argument *for* a firmware upgrade,
with the warning that an upgrade could mask the cause rather than fix it. The
controller has since moved to 16.0.1 without that evidence being captured
first, so a recurrence on 16.0.1 is the only remaining way to learn more.

---

## OPEN `evidence` — Classify every rig finding by whether the wired QuinLED will fix it

**Raised:** 2026-07-28 by the operator, who expects to move to a QuinLED quad
with ethernet once it ships.

Findings about the lighting rig fall into four classes, and conflating them
leads to buying hardware that fixes nothing:

1. **Transport** (WiFi RSSI, association, retries, airtime contention) — a
   wired board fixes these outright.
2. **This specific board** (Athom, APA102 at 5 MHz on GPIO 18/5, its power
   design) — new hardware very likely fixes these, but check the replacement
   actually supports **clocked** LEDs (data *and* clock) on the channel you
   intend; digital LED boards differ on this and it is not safe to assume.
3. **ESP32/WLED firmware architecture** (a single-threaded sketch where
   `strip.service()` and the async web server contend) — a wired board does
   **not** fix these. Same firmware, same loop.
4. **WLED configuration** (e.g. the realtime-timeout latch above) — these
   **follow you to the new board**. The realtime timeout must be set on the new
   controller before streaming to it, or it will latch identically.

The classification table lives in
`~/yarg-lighting/evidence/wled-realtime-latch-2026-07-28.md`. Keep it current as
findings land; it is the input to the buying decision, not a retrospective.

**The measurement that decides class 1 vs class 3** is in `tools/wled_soak.py`:
sample TCP connect time and HTTP reply time against the device together. TCP
handshakes complete in lwIP/AsyncTCP, below the Arduino `loop()`; an HTTP
response needs `loop()` to run. Both rising together means the network path is
at fault (ethernet helps); HTTP rising while TCP stays flat means the main loop
is blocking (ethernet does not help). `analyse` prints the verdict.

Note RSSI alone cannot settle it — it reports link strength, not congestion or
airtime, so a flat RSSI correlation does not clear WiFi.

### Finding 2026-11-16: every `wled_slow_http` warning was raised with the strip off

Tower's anomaly logs for 3–15 November (one bridge run, v1.1.0 `b733c27`, 23
songs) hold seven `wled_slow_http` warnings. Times are local (UTC−5). "State"
is read from the 20 s of status snapshots saved with each warning.

| Date | Time | `/json/info` reply | Episode | Song playing | State at the time | RSSI |
|---|---|---|---|---|---|---|
| 11-04 | 14:11:41 | 2516 ms | 30 s | no | strip off, YARG silent 20.9 h | −47 dBm |
| 11-06 | 10:48:45 | 1076 ms | 30 s | no | strip off, YARG silent 65.5 h | −46 dBm |
| 11-06 | 20:02:15 | 1113 ms | 30 s | no | strip off, YARG silent 74.7 h | −50 dBm |
| 11-11 | 01:49:45 | 2307 ms | 30 s | no | strip off, YARG silent 176.5 h | −47 dBm |
| 11-14 | 08:52:33 | 544 ms | 30 s | no | strip off, YARG silent 63.1 h | −46 dBm |
| 11-15 | 20:48:13 | 1051 ms | 30 s | no | strip off, YARG silent 32.3 h | −47 dBm |
| 11-15 | 23:15:09 | 1855 ms | 30 s | no | strip off, YARG silent 34.7 h | −47 dBm |

**Conclusion: all seven at idle, none during a song.** In every case the strip
was powered off, YARG was not sending, and the bridge had sent no DDP frame for
at least 20 hours. The logs hold no `wled_weak_wifi` and no `wled_heap_low`
event at all, so there is no nearest one to cite.

What that settles:

- **DDP load is not the cause.** The slow replies happen with no stream
  running, so there is nothing to gain from suppressing the warning or raising
  `HTTP_SLOW_MS` while sending. No detector change is proposed.
- **Each episode is one slow poll.** The bridge polls every 30 s and every
  episode cleared after 30 s, so each is a single slow reply followed by a
  normal one. Seven in roughly 35,000 polls is about 1 in 5,000.

What it does not settle:

- **Songs are not shown to be clean.** The 23 songs cover about 80 minutes,
  roughly 160 polls. At 1 in 5,000 the expected count during songs is 0.03, so
  seeing none says nothing either way.
- **Class 1 against class 3.** The warning text reads "TCP connect None ms":
  the bridge times the HTTP reply but not the TCP connect, so the measurement
  that separates transport from main loop is missing from all seven. Signal
  strength was good throughout (−45 to −50 dBm), which by the note above does
  not clear WiFi.

One unverified reading: three of the seven replies are 1.05–1.11 s, which is
what a single lost SYN costs on Linux (1 s retransmit plus a normal reply).
That would point at transport, class 1. The other four do not fit that
pattern, and nothing here tests it.

To classify them, leave `tools/wled_soak.py log` sampling the powered-off strip
for a day or more, then run `analyse`; it records both timings and at this rate
should catch a few.

---

## OPEN `evidence` — Desync: not reproduced since the realtime-timeout fix; testing is parked, not finished

**Status 2026-07-28 (end of day):** two instrumented runs since the latch fix,
neither reproduced the failure. Parked at the operator's request — the tooling
stays ready and is called back up the next time the symptom appears.

| Run | Condition | Result |
|---|---|---|
| 1 | 3.5 h continuous DDP, synthetic stimulus | step latency 145 ms → 164 ms. Flat. No reproduction. |
| 2 | Real song, then **2h51m paused** with the stream still running (585,366 DDP frames) | lag −60/−40 ms before → **−50/−50 ms after**. Operator independently: "looks pretty good still". No reproduction. |

Run 2 is the meaningful one: it is the operator's exact trigger, and their visual
verdict and the instrument agreed **without either seeing the other first**.

**Not settled.** The original failure took **4+ hours**; run 2's pause was
2h51m. A longer pause is the remaining test and costs nothing but time.

**Next time it appears — do not power cycle first.** The bad state is the
evidence. `tools/RUNBOOK.md` has the capture sequence, the healthy reference
values to compare against, and which probe to trust under which conditions.

### Secondary finding: device RSSI degrades across a session

Observed in both runs: −34 dBm fresh, drifting ~**−1.38 dB/hour**, restored by a
power cycle. It is the only metric seen so far that accumulates over a session
and clears only on power cycle — the same shape as the reported symptom.

**But at the magnitude observed it is benign.** Across run 2's −34 → −39 dBm
drift, heap, device FPS, TCP connect and HTTP reply were all flat, and neither
the lag measurement nor the operator noticed anything. Worth continuing to watch;
not worth acting on. It sits in the class a wired board would eliminate, but it
was well below the 250 ms cue crossfade in priority, which cost ~120 ms on
every cue change until it was
[shortened on 2026-07-29](BACKLOG_ARCHIVE.md#done-2026-07-29-perf--cue-changes-are-crossfaded-over-250-ms-and-that-is-the-visible-lag).
