# Backlog

Per the mission-control protocol, this is the full backlog; the dashboard in
`~/mission-control/README.md` carries only the headline plus a link here.

Item status: `OPEN` · `IN PROGRESS` · `BLOCKED` · `DONE` · `DEFERRED`.

---

## OPEN — VenueSize is hardcoded to Small, so Phase 8 density branching is dead code

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

**This is a source read, not a live observation.** Confirm against a real
capture before changing behaviour — the whole point of `AGENTS.md`'s
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

## OPEN — Venue fixture coverage now has a measured answer

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

## OPEN — Render priority is now measured, not assumed

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

## Existing headline items

See `VISION.md` "Next push — reliability, replay, alignment, and state" for the
full description of each:

1. `OPEN` — Release the live baseline: merge `fix/bridge-review-findings` to
   `main`, publish/redeploy, reconcile docs with the deployed feature set.
2. `OPEN` — Timestamped YARG datagram capture/replay harness with a headless
   DDP receiver in CI.
3. `OPEN` — Persisted operator lighting-alignment trim plus a calibration
   pattern. Default 0 ms.
4. `OPEN` — Expanded read-only game/bridge state, optionally over MQTT.
5. `OPEN` — WLED 0.14.4 firmware upgrade qualification with backups, an exact
   rollback image, and proven recovery.
6. `DEFERRED` — Multi-WLED logical canvas and performer-region routing; blocked
   on having enough equivalent hardware to test geometry and partial failure.
