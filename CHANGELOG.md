# Changelog

What changed in each release of the Stage Kit → WLED bridge, written for the
person running it. The reasoning behind each change is in `BACKLOG_ARCHIVE.md`;
how a release is cut is in the README under *Releasing*.

The dashboard's version line names the image a container is running
(`v1.2.0 · <commit> · built <date>`). Images built from `main` between releases
carry the version of the *next* release, so the commit is what identifies one.

## 1.2.0 — draft

Not tagged yet. Everything below is on `main` and in the `:latest` image.

This release is hardening: nothing here changes how a song looks. It covers
the changes merged on 2026-11-16, on top of the image that reported
`v1.1.0 · b733c27`.

### Changed behaviour you may notice

- **Test patterns stop on their own.** A pattern started from the dashboard
  ends after 10 minutes, or the moment YARG starts sending. While YARG is
  connected the test controls are refused with HTTP 409 and the dashboard says
  why. Previously a forgotten pattern ran until someone stopped it and could be
  started over a live song.
- **Cross-site requests to the control endpoints are refused.** A `POST` whose
  `Origin` header names a different host than the bridge gets HTTP 403. The
  dashboard itself, `curl`, scripts and the party watcher are unaffected: a
  request with no `Origin` header is still accepted. A reverse proxy that
  rewrites `Host` but not `Origin` will now see 403 on the controls. There is
  still no authentication.
- **A crashed render thread is replaced in-process.** The bridge restarts it
  with a back-off (1 s up to 30 s). After five replacements in a row have
  failed it exits non-zero, so a container restart policy can take over. A
  container with restart policy `no` will stay stopped at that point, where
  before it stayed running with a dark strip.

### Fixed

- **A stuck one-shot input can no longer pin an overlay on.** Beat, keyframe
  and bonus-effect inputs that stay set for eight consecutive packets are
  treated as stuck and ignored until they clear. This bounds the damage
  when a new YARG datagram layout puts a different field where a one-shot used
  to be.

### Internal

- **The datagram layout is one table** (`protocol/schema.py`) covering versions
  1, 3, 4 and 5, and the parser is driven from it. A datagram with a newer
  version byte is parsed with the newest known layout and logged once.
- **Daily upstream drift check.** A scheduled workflow compares that table with
  YARG's `DataStreamController.cs` on the `dev` branch and fails when they
  differ, so a layout change is noticed before it reaches the rig.
- **Release check.** CI refuses to build a `v*` tag that disagrees with
  `__version__` or with this file.
- **Dashboard script check.** The page's JavaScript is syntax-checked and run
  against real status snapshots in Node as part of the suite, so a script
  error fails the build instead of shipping a blank dashboard.

### Documentation

- `WLED_SETUP.md` rewritten against WLED 16.0.1, with a backup command, what a
  restore needs, and the firmware rollback path.
- `BACKLOG.md` now holds only open work; finished write-ups moved to
  `BACKLOG_ARCHIVE.md`. `VISION.md` and the README describe datagram v5.

### Upgrading from an image reporting `v1.1.0`

- No settings migrate and no environment variables changed.
- The WLED controller needs no changes.
- Check the container's restart policy. `unless-stopped` or `on-failure` lets
  the new non-zero exit bring the bridge back; `no` does not.
- After the update the dashboard's version line should read `v1.2.0` with a
  commit other than `b733c27`.

## 1.1.0 — never tagged

`__version__` was set to 1.1.0 on 2026-08-26, when the bridge first reported
its build, and no `v1.1.0` tag was made. Every image built from `main` between
then and the 1.2.0 bump reports `v1.1.0` with its own commit. The last of them
before the changes above is `b733c27`, built 2026-11-03. What those images
gained over 1.0.0:

- **YARG datagram v5.** YARG inserted a fog timer in the middle of the packet,
  which shifted every later field. The parser reads v5 by its version byte.
- **Replay and capture.** Datagrams can be recorded from the dashboard and
  replayed deterministically (`replay/`). Every cue pattern is stepped from
  counters rather than timers so replay can judge it.
- **Strip ownership.** The bridge drives the strip through three defined modes,
  re-asserts a known WLED baseline each time it goes live, and shows an
  operator-chosen idle look between songs. WLED's power state is adopted in
  both directions. A cue held by a sender that has gone away is released.
- **Cue motion.** Multi-spot spotlight cues that pulse to the beat, beat-locked
  cues that launch in phase, a beat-grid fallback for keyframe cues, and a
  shorter cue cross-fade, adjustable as `cue_fade_ms` (default 120 ms).
- **Dashboard.** Build identity beside the title, a preview of what the strip
  is showing, the Stage Kit pod visualizer, and a Health card.
- **Anomaly detection.** The bridge watches its own output for known failure
  shapes, lists them at `/api/anomalies`, and keeps replayable evidence for
  each. A party watcher (`tools/party_monitor.py`) reads the same feed.
- **Rig tooling.** Lag and frame-match tools that read the strip through WLED
  16's WebSocket live view, since 16.x no longer serves `/json/live`.

## 1.0.0 — 2026-07-27

Roadmap phases 0 to 8: all 33 lighting cues, the layer compositor, beat clock,
star power, camera-cut lighting, the dashboard preview, tempo-locked strobe and
song-section bias, with the image build gated on the test suite. Tagged
`v1.0.0`.
