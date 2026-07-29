"""Measure end-to-end lag between what the bridge renders and what the strip shows.

The soak sampler (`tools.wled_soak`) tracks proxies — heap, device FPS, send
timing. This measures the thing the operator actually reported: how far behind
the game the lights are. It is the probe that tells constant latency apart from
progressive drift, which the desync backlog item says must happen before any
lighting code is touched.

How it works, and why it can work at all:

- The bridge publishes what it just rendered at `/api/status` under
  `preview.strip` — 60 cells x RGB, a 2:1 downsample of the 120-LED strip.
- WLED publishes what it is currently displaying at `/json/live` — 120 hex RGB
  triples, read straight out of the live buffer that DDP writes into.
- On this rig the two are related by a plain linear scale: `bri` is 128 and
  realtime gamma correction is off (`if.live["no-gc"]: true`), so a device LED
  is its bridge cell times ~128/255.

Rather than match frames pixel-for-pixel, both sides are reduced to one scalar
per sample — total frame brightness — giving two time series of the same
underlying animation observed at two points in the pipeline. Normalising each
series removes the brightness scale and the downsample entirely, and the lag is
then whichever shift maximises their cross-correlation.

    python -m tools.wled_lag probe --seconds 45
    python -m tools.wled_lag probe --seconds 45 --out lag-t0.json

Run it repeatedly across a long soak and compare. A lag that is the same at
+6 h as at +0 h is constant latency; one that grows is drift.

Keep probes short and occasional: this polls the ESP32's HTTP server hard, and
the point is to observe the stream, not to perturb it.
"""

import argparse
import json
import socket
import statistics
import sys
import time
import urllib.error
import urllib.request

DEFAULT_WLED = "192.168.0.53"
DEFAULT_BRIDGE = "192.168.0.230:36180"

# Below this correlation the lag estimate is not trustworthy — usually because
# the pattern was too static during the probe to carry any timing information.
MIN_CORRELATION = 0.5


class LagError(Exception):
    """A lag probe could not produce a usable estimate."""


def _get(url: str, timeout: float = 4.0):
    """GET and decode JSON, timestamping the midpoint of the round trip.

    The midpoint is the least-wrong single instant to attribute a reading to:
    the sample was taken somewhere inside the request, and without knowing the
    split between outbound and return legs the centre is the best estimate.
    """
    start = time.monotonic()
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = resp.read()
    mid = (start + time.monotonic()) / 2
    return json.loads(body), mid


def bridge_brightness(addr: str) -> tuple[float, float]:
    """Total brightness of the frame the bridge last rendered, and its time."""
    data, t = _get(f"http://{addr}/api/status")
    strip = data.get("preview", {}).get("strip") or []
    if not strip:
        raise LagError("bridge /api/status carried no preview.strip")
    return float(sum(strip)), t


def device_brightness(host: str) -> tuple[float, float]:
    """Total brightness of the frame the strip is displaying, and its time."""
    data, t = _get(f"http://{host}/json/live")
    leds = data.get("leds") or []
    if not leds:
        raise LagError("WLED /json/live carried no leds (is JSONLIVE enabled?)")
    total = 0
    for hexrgb in leds:
        v = int(hexrgb, 16)
        total += (v >> 16 & 0xFF) + (v >> 8 & 0xFF) + (v & 0xFF)
    return float(total), t


def collect(wled: str, bridge: str, seconds: float) -> tuple[list, list]:
    """Alternate between the two endpoints, returning (bridge, device) series.

    Alternating rather than threading keeps one request in flight at a time, so
    neither side is answering two overlapping polls and inflating the other's
    apparent latency.
    """
    bridge_pts, device_pts = [], []
    t0 = time.monotonic()
    errors = 0
    while time.monotonic() - t0 < seconds:
        for fn, sink, arg in ((bridge_brightness, bridge_pts, bridge),
                              (device_brightness, device_pts, wled)):
            try:
                value, t = fn(arg)
                sink.append((t - t0, value))
            except (urllib.error.URLError, OSError, ValueError, LagError):
                errors += 1
                if errors > 20:
                    raise LagError(f"too many failed reads ({errors}) during probe")
    return bridge_pts, device_pts


def _normalise(points: list) -> list:
    """Z-score the values, which cancels the brightness scale and downsample."""
    values = [v for _, v in points]
    mean = statistics.fmean(values)
    try:
        sd = statistics.stdev(values)
    except statistics.StatisticsError:
        sd = 0.0
    if sd == 0:
        raise LagError("a series was perfectly flat — the pattern carried no "
                       "timing information; probe during something that moves")
    return [(t, (v - mean) / sd) for t, v in points]


def _resample(points: list, grid: list) -> list:
    """Linear interpolation onto a common time grid."""
    out = []
    i = 0
    for t in grid:
        while i + 2 < len(points) and points[i + 1][0] < t:
            i += 1
        (t0, v0), (t1, v1) = points[i], points[i + 1]
        if t1 == t0:
            out.append(v0)
        else:
            frac = (t - t0) / (t1 - t0)
            out.append(v0 + frac * (v1 - v0))
    return out


def estimate_lag(bridge_pts: list, device_pts: list, max_lag: float = 2.0,
                 step: float = 0.01) -> dict:
    """Cross-correlate the two series; return the best lag and its strength.

    A positive lag means the device is showing what the bridge rendered that
    many seconds ago — the strip is behind, which is the direction that would
    read as "out of sync" in the room.
    """
    if len(bridge_pts) < 20 or len(device_pts) < 20:
        raise LagError(f"not enough samples to correlate "
                       f"(bridge {len(bridge_pts)}, device {len(device_pts)})")

    bridge_n = _normalise(bridge_pts)
    device_n = _normalise(device_pts)

    start = max(bridge_n[0][0], device_n[0][0]) + max_lag
    end = min(bridge_n[-1][0], device_n[-1][0]) - max_lag
    if end - start < 5.0:
        raise LagError("probe too short to search the lag window; use --seconds 30+")

    grid = []
    t = start
    while t < end:
        grid.append(t)
        t += step
    device_grid = _resample(device_n, grid)

    best = None
    curve = []
    lag = -max_lag
    while lag <= max_lag:
        shifted = _resample(bridge_n, [t - lag for t in grid])
        # Pearson over *this window*. Z-scoring earlier used whole-series
        # statistics, so a mean-product over a higher-variance subwindow could
        # exceed 1 and stop being a correlation at all. Normalising here keeps
        # the score in [-1, 1] and comparable between probes.
        ma = statistics.fmean(shifted)
        mb = statistics.fmean(device_grid)
        num = sum((a - ma) * (b - mb) for a, b in zip(shifted, device_grid))
        da = sum((a - ma) ** 2 for a in shifted) ** 0.5
        db = sum((b - mb) ** 2 for b in device_grid) ** 0.5
        corr = num / (da * db) if da and db else 0.0
        curve.append((round(lag, 3), round(corr, 4)))
        if best is None or corr > best[1]:
            best = (lag, corr)
        lag += step

    # Aliasing check done properly: count genuine *local maxima*, not points on
    # the shoulders of the single true peak. A periodic stimulus produces
    # several comparable humps a beat apart; aperiodic content produces one.
    peaks = [(l, v) for i, (l, v) in enumerate(curve[1:-1], 1)
             if v > curve[i - 1][1] and v > curve[i + 1][1]]
    peaks.sort(key=lambda pk: -pk[1])
    rivals = [pk for pk in peaks[1:] if pk[1] > best[1] * 0.9]

    return {
        "peaks": peaks[:5],
        "rivals": len(rivals),
        "lag_s": round(best[0], 3),
        "lag_ms": round(best[0] * 1000, 1),
        "correlation": round(best[1], 4),
        "bridge_samples": len(bridge_pts),
        "device_samples": len(device_pts),
        "window_s": round(end - start, 1),
        "curve": curve,
    }


def describe(result: dict) -> list[str]:
    """Report lines, including whether the estimate should be believed."""
    lines = [
        f"lag: {result['lag_ms']:+.1f} ms "
        f"(correlation {result['correlation']:.3f})",
        f"  from {result['bridge_samples']} bridge and "
        f"{result['device_samples']} device samples over {result['window_s']}s",
    ]
    if result.get("rivals"):
        lines.append(
            f"  !! {result['rivals']} rival correlation peak(s) within 10% of the "
            f"best. The stimulus is too periodic to pin the lag — a whole cycle "
            f"of error cannot be excluded. Use the step probe instead."
        )
    if result["correlation"] < MIN_CORRELATION:
        lines.append(
            f"  !! correlation below {MIN_CORRELATION} — do not trust this "
            f"number. The pattern was probably too static; probe while "
            f"something is moving on the strip."
        )
    else:
        lines.append(
            "  positive = the strip is behind the bridge. Compare against "
            "probes taken earlier in the soak: a stable number is constant "
            "latency, a growing one is drift."
        )
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.wled_lag",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe", help="measure bridge-to-strip lag now")
    probe.add_argument("--wled", default=DEFAULT_WLED)
    probe.add_argument("--bridge", default=DEFAULT_BRIDGE)
    probe.add_argument("--seconds", type=float, default=45.0)
    probe.add_argument("--max-lag", type=float, default=2.0,
                       help="widest lag to search, seconds")
    probe.add_argument("--out", help="write the full result (incl. curve) as JSON")

    step = sub.add_parser("step", help="alias-proof latency via randomised cue steps")
    step.add_argument("--wled", default=DEFAULT_WLED)
    step.add_argument("--bridge-host", default="192.168.0.230",
                      help="host running the bridge's YARG listener")
    step.add_argument("--yarg-port", type=int, default=36107)
    step.add_argument("--cycles", type=int, default=6)
    step.add_argument("--out", help="write per-step results as JSON")
    step.add_argument("--direct", action="store_true",
                      help="send DDP straight to WLED, bypassing the bridge "
                           "(stop the bridge's own DDP output first)")
    step.add_argument("--leds", type=int, default=120)

    args = parser.parse_args(argv)

    if args.command == "step":
        try:
            if args.direct:
                results = step_probe_direct(args.wled, led_count=args.leds,
                                            cycles=args.cycles)
            else:
                results = step_probe(args.wled, args.bridge_host,
                                     args.yarg_port, cycles=args.cycles)
        except (urllib.error.URLError, OSError, LagError) as exc:
            print(f"step probe failed: {exc}", file=sys.stderr)
            return 1
        for line in summarise_steps(results):
            print(line)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump({"wall": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "steps": results}, fh, indent=2)
        return 0

    try:
        bridge_pts, device_pts = collect(args.wled, args.bridge, args.seconds)
        result = estimate_lag(bridge_pts, device_pts, max_lag=args.max_lag)
    except LagError as exc:
        print(f"lag probe failed: {exc}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"lag probe failed: {exc}", file=sys.stderr)
        return 1

    for line in describe(result):
        print(line)
    if args.out:
        result["wall"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        print(f"  full result -> {args.out}")
    return 0




# --- step response ----------------------------------------------------------
#
# Cross-correlation against a periodic pattern cannot tell a small lag from one
# a whole cycle larger. That ambiguity is fatal here, because "40 ms behind" and
# "one second behind" are the difference between fine and broken. The step test
# removes it: drive the strip with randomised dwell times so no shift but the
# true one lines up, and time each transition against the moment we sent it.

import random  # noqa: E402  (kept beside the code that uses it)
import struct  # noqa: E402


def _yarg_packet(cue: int) -> bytes:
    """A valid in-game YARG datagram carrying *cue*.

    Delegates to `test_sender.build_packet` rather than hand-rolling the
    layout — the cue lives at offset 34, and several earlier bytes (scene,
    pause) have to be right or the bridge treats the packet as out-of-game.
    """
    from test_sender import build_packet
    return build_packet(cue=cue)


def step_probe(wled: str, bridge_host: str, yarg_port: int, cycles: int = 6,
               dim_cue: int = 8, bright_cue: int = 12,
               quiet: bool = False) -> list[dict]:
    """Drive cue steps at randomised intervals; time when the strip reacts.

    Returns one record per transition. `latency_ms` is the whole path — our
    send, the bridge's render tick, the network, and the device's next show —
    so a few tens of ms is healthy. What matters is whether it stays there.

    Nothing else may be feeding the bridge while this runs: a second sender
    fighting for the cue makes the strip oscillate, and then no step is
    distinguishable from the noise.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    addr = (bridge_host, yarg_port)
    results = []
    rng = random.Random()

    def hold(cue: int, seconds: float) -> float:
        """Drive one cue for *seconds*, returning the settled brightness."""
        end = time.monotonic() + seconds
        last = None
        while time.monotonic() < end:
            sock.sendto(_yarg_packet(cue), addr)
            time.sleep(1 / 60)
        for _ in range(5):
            try:
                last, _ = device_brightness(wled)
                break
            except (urllib.error.URLError, OSError, ValueError, LagError):
                continue
        return last if last is not None else 0.0

    try:
        settled = hold(dim_cue, 2.0)
        for i in range(cycles):
            cue = bright_cue if i % 2 == 0 else dim_cue
            dwell = rng.uniform(1.5, 3.5)

            t_send = time.monotonic()
            deadline = t_send + dwell
            detected = None
            seen_max = settled
            while time.monotonic() < deadline:
                sock.sendto(_yarg_packet(cue), addr)
                try:
                    value, t_read = device_brightness(wled)
                except (urllib.error.URLError, OSError, ValueError, LagError):
                    continue
                seen_max = max(seen_max, value)
                # A step is a large move away from where the strip had settled,
                # in either direction, scaled to the settled level so it works
                # for both the dark->bright and bright->dark transitions.
                span = max(200.0, abs(settled) * 0.4)
                if detected is None and abs(value - settled) > span:
                    detected = (t_read - t_send) * 1000
                time.sleep(1 / 60)

            settled = hold(cue, 0.6)
            results.append({
                "step": i,
                "cue": cue,
                "settled_before": round(seen_max, 1),
                "settled_after": round(settled, 1),
                "latency_ms": None if detected is None else round(detected, 1),
                "dwell_s": round(dwell, 2),
            })
            if not quiet:
                lat = "not seen" if detected is None else f"{detected:7.1f} ms"
                print(f"  step {i} -> cue {cue:>3}: {lat}")
    finally:
        sock.close()
    return results


def summarise_steps(results: list[dict]) -> list[str]:
    seen = [r["latency_ms"] for r in results if r["latency_ms"] is not None]
    if not seen:
        return ["step probe: the strip never visibly reacted — check the bridge "
                "is connected and the cues differ in brightness."]
    lines = [
        f"step latency: median {statistics.median(seen):.1f} ms "
        f"(min {min(seen):.1f}, max {max(seen):.1f}, n={len(seen)}/{len(results)})",
        "  This one cannot alias: randomised dwell times mean only the true "
        "lag lines up. Compare across a long run — a median that climbs and "
        "stays climbed is the persistent desync.",
    ]
    if max(seen) > 500:
        lines.append(f"  !! worst transition took {max(seen):.0f} ms to reach the "
                     f"strip. That is visible hesitation, not jitter.")
    return lines




def step_probe_direct(wled: str, led_count: int = 120, cycles: int = 12,
                      quiet: bool = False) -> list[dict]:
    """Same step timing, but driving DDP straight at the controller.

    Removes the bridge from the path entirely, so the difference against
    `step_probe` is the bridge's own contribution (its ingest plus one render
    tick) and what remains is network plus device. Detection is identical, so
    the polling overhead is common to both and cancels in the comparison.

    Nothing else may be sending DDP at the same time — two senders write the
    same realtime buffer and the last one wins.
    """
    from protocol.ddp_sender import DDPSender

    sender = DDPSender(wled)
    dark = bytes(led_count * 3)
    bright = bytes([180, 180, 180] * led_count)
    results = []
    rng = random.Random()

    def hold(frame: bytes, seconds: float) -> float:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            sender.send_pixels(frame)
            time.sleep(1 / 60)
        for _ in range(5):
            try:
                value, _ = device_brightness(wled)
                return value
            except (urllib.error.URLError, OSError, ValueError, LagError):
                continue
        return 0.0

    settled = hold(dark, 2.0)
    for i in range(cycles):
        frame = bright if i % 2 == 0 else dark
        dwell = rng.uniform(1.5, 3.5)
        t_send = time.monotonic()
        deadline = t_send + dwell
        detected = None
        while time.monotonic() < deadline:
            sender.send_pixels(frame)
            try:
                value, t_read = device_brightness(wled)
            except (urllib.error.URLError, OSError, ValueError, LagError):
                continue
            span = max(200.0, abs(settled) * 0.4)
            if detected is None and abs(value - settled) > span:
                detected = (t_read - t_send) * 1000
            time.sleep(1 / 60)
        settled = hold(frame, 0.6)
        results.append({
            "step": i,
            "target": "bright" if frame is bright else "dark",
            "latency_ms": None if detected is None else round(detected, 1),
            "dwell_s": round(dwell, 2),
        })
        if not quiet:
            lat = "not seen" if detected is None else f"{detected:7.1f} ms"
            print(f"  step {i} -> {results[-1]['target']:>6}: {lat}")
    return results


if __name__ == "__main__":
    sys.exit(main())
