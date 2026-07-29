"""Sample the WLED controller and the bridge onto one timeline, for hours.

Built for the "WLED drifts out of sync after hours of DDP" backlog item, whose
first step is evidence rather than code. Pair it with `python -m replay.cli
capture.jsonl --loop` so a long stream can be soaked without playing a 4-hour
game session, then read the two sides against each other: if bridge-side timing
stays flat while the device's numbers move, the device is proven guilty.

    python -m tools.wled_soak log --out soak.jsonl
    python -m tools.wled_soak log --out soak.jsonl --interval 15 --duration 4h
    python -m tools.wled_soak analyse soak.jsonl

Runs against whatever is already deployed — it only reads `/json/info` and
`/api/status`, and never writes to either. Stdlib only, like the rest of the
bridge's HTTP code.
"""

import argparse
import json
import socket
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_WLED = "192.168.0.53"
DEFAULT_BRIDGE = "192.168.0.230:36180"
DEFAULT_INTERVAL = 30.0

# Series pulled out of each sample for the trend report. The value is a label;
# the key is the flattened field name produced by _flatten().
TRENDS = {
    "wled.freeheap": "free heap (bytes)",
    "wled.fps": "device strip FPS",
    "wled.http_ms": "device HTTP reply (ms)",
    "wled.tcp_ms": "device TCP connect (ms)",
    "wled.rssi": "WiFi RSSI (dBm)",
    "bridge.render_gap_ms_max": "render gap max (ms)",
    "bridge.ddp_send_us_max": "DDP send max (us)",
    "bridge.http_ms": "bridge HTTP reply (ms)",
}

# Series that should only ever climb. A decrease means the far side restarted,
# which invalidates deltas across that boundary.
COUNTERS = ("wled.uptime", "bridge.ddp_frames_sent", "bridge.rendered",
            "bridge.packets_received", "bridge.stalls", "bridge.send_errors")


class SoakError(Exception):
    """A soak log could not be read or made sense of."""


def parse_duration(text: str) -> float:
    """Parse `90`, `30s`, `15m`, `4h` into seconds."""
    text = text.strip().lower()
    if not text:
        raise ValueError("empty duration")
    units = {"s": 1, "m": 60, "h": 3600}
    if text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text)


def _get_json(url: str, timeout: float = 5.0):
    """GET and decode JSON. Returns (data, None) or (None, reason).

    Sets `_last_http_ms` to the round trip. On an ESP32 that is a usable proxy
    for main-loop health: the web server is serviced from the same loop as the
    strip, so a device that has bogged down answers HTTP late.
    """
    global _last_http_ms
    _last_http_ms = None
    start = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
        _last_http_ms = round((time.monotonic() - start) * 1000, 1)
        return json.loads(body), None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


_last_http_ms: float | None = None


def tcp_rtt_ms(host: str, port: int = 80, timeout: float = 3.0) -> float | None:
    """Time a bare TCP connect. None if it fails.

    This is the transport-vs-firmware discriminator. On an ESP32 the TCP
    handshake is completed by lwIP (and, for the async web server, by the
    AsyncTCP task) — largely *below* the Arduino `loop()`. Serving an HTTP
    response needs `loop()` to actually run. So:

      - both climb together  -> the network path is the problem, and moving to
        a wired board would very likely fix it;
      - HTTP climbs while TCP stays flat -> packets are arriving fine and the
        firmware's main loop is blocking, which ethernet does **not** fix.
    """
    sock = socket.socket()
    sock.settimeout(timeout)
    start = time.monotonic()
    try:
        sock.connect((host, port))
        return round((time.monotonic() - start) * 1000, 1)
    except OSError:
        return None
    finally:
        sock.close()


def sample_wled(host: str) -> dict:
    """Read the device fields that could plausibly move over a long stream."""
    tcp_ms = tcp_rtt_ms(host)
    data, err = _get_json(f"http://{host}/json/info")
    if data is None:
        return {"error": err, "tcp_ms": tcp_ms}
    leds = data.get("leds", {})
    wifi = data.get("wifi", {})
    return {
        "http_ms": _last_http_ms,
        "tcp_ms": tcp_ms,
        "uptime": data.get("uptime"),
        "freeheap": data.get("freeheap"),
        "fps": leds.get("fps"),
        "pwr": leds.get("pwr"),
        "live": data.get("live"),
        "lm": data.get("lm"),
        "lip": data.get("lip"),
        "rssi": wifi.get("rssi"),
        "signal": wifi.get("signal"),
        "channel": wifi.get("channel"),
        "ws": data.get("ws"),
        "ndc": data.get("ndc"),
    }


def sample_bridge(addr: str) -> dict:
    """Read the bridge-side timing that rules the host in or out."""
    data, err = _get_json(f"http://{addr}/api/status")
    if data is None:
        return {"error": err}
    render = data.get("render", {})
    ddp = render.get("ddp", {})
    return {
        "http_ms": _last_http_ms,
        "connected": data.get("connected"),
        "paused": data.get("paused"),
        "cue": data.get("cue"),
        "packets_received": data.get("packets_received"),
        "packets_per_sec": data.get("packets_per_sec"),
        "ddp_frames_sent": data.get("ddp_frames_sent"),
        "rendered": render.get("rendered"),
        "skipped": render.get("skipped"),
        "stalls": render.get("stalls"),
        "render_work_ms_max": render.get("work_ms_max"),
        "render_gap_ms_avg": render.get("gap_ms_avg"),
        "render_gap_ms_max": render.get("gap_ms_max"),
        "ddp_send_us_avg": ddp.get("send_us_avg"),
        "ddp_send_us_max": ddp.get("send_us_max"),
        "send_errors": ddp.get("send_errors"),
    }


def run_log(wled: str, bridge: str, out_path: str, interval: float,
            duration: float | None, quiet: bool = False) -> int:
    """Append one JSONL sample per interval until duration elapses or Ctrl+C.

    Each line is flushed as it is written, so a soak killed by a reboot or a
    dropped SSH session still leaves everything it had gathered up to that
    point. Sleeps against a monotonic deadline so sampling does not drift.
    """
    if interval <= 0:
        raise ValueError("--interval must be greater than 0")

    start = time.monotonic()
    samples = 0
    if not quiet:
        end_note = f"for {duration:.0f}s" if duration else "until Ctrl+C"
        print(f"Soaking {wled} + {bridge} every {interval:g}s {end_note} "
              f"-> {out_path}")

    with open(out_path, "a", encoding="utf-8") as fh:
        try:
            while True:
                now = time.monotonic()
                row = {
                    "t": round(now - start, 3),
                    "wall": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "wled": sample_wled(wled),
                    "bridge": sample_bridge(bridge),
                }
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                samples += 1
                if not quiet:
                    print(f"  {row['t']:8.1f}s  {_one_line(row)}")

                if duration is not None and time.monotonic() - start >= duration:
                    break
                delay = start + samples * interval - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        except KeyboardInterrupt:
            if not quiet:
                print(f"\nStopped after {samples} samples.")

    if not quiet:
        print(f"Wrote {samples} samples to {out_path}. "
              f"Read them with: python -m tools.wled_soak analyse {out_path}")
    return samples


def _one_line(row: dict) -> str:
    w, b = row["wled"], row["bridge"]
    if "error" in w:
        wled_part = f"wled unreachable ({w['error']})"
    else:
        live = f"live={w['lm'] or 'off'}" if w.get("live") else "live=no"
        wled_part = (f"heap={w['freeheap']} fps={w['fps']} rssi={w['rssi']} "
                     f"http={w['http_ms']}ms tcp={w['tcp_ms']}ms {live}")
    if "error" in b:
        bridge_part = f"bridge unreachable ({b['error']})"
    else:
        bridge_part = (f"ddp={b['ddp_frames_sent']} gap_max={b['render_gap_ms_max']} "
                       f"send_max={b['ddp_send_us_max']}us")
    return f"{wled_part} | {bridge_part}"


def _flatten(row: dict) -> dict:
    """Flatten one sample to `wled.freeheap`-style keys, dropping errors."""
    flat = {"t": row.get("t")}
    for side in ("wled", "bridge"):
        section = row.get(side) or {}
        if "error" in section:
            continue
        for key, value in section.items():
            flat[f"{side}.{key}"] = value
    return flat


def read_soak(path: str) -> list[dict]:
    """Read a soak log into flattened samples. Raises SoakError if unusable."""
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise SoakError(f"{path}:{lineno}: {exc}") from exc
    except OSError as exc:
        raise SoakError(f"{path}: {exc}") from exc
    if not rows:
        raise SoakError(f"{path}: no samples")
    return rows


def _series(flat_rows: list[dict], key: str) -> list[tuple[float, float]]:
    """(t, value) pairs for one key, skipping samples that lack it."""
    out = []
    for row in flat_rows:
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out.append((row["t"], float(value)))
    return out


def _slope_per_hour(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope in units per hour. None if the span is too short."""
    if len(points) < 2:
        return None
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    denom = sum((t - mean_t) ** 2 for t, _ in points)
    if denom == 0:
        return None
    num = sum((t - mean_t) * (v - mean_v) for t, v in points)
    return (num / denom) * 3600.0


def analyse(path: str) -> list[str]:
    """Summarise a soak log as report lines.

    The questions this is meant to answer, in order: did the device restart
    under us (which voids every delta), did anything on the device trend over
    the run, and did the bridge stay flat while it did.
    """
    rows = read_soak(path)
    flat = [_flatten(r) for r in rows]
    span = flat[-1]["t"] - flat[0]["t"]
    lines = [
        f"{path}: {len(rows)} samples over {span / 3600:.2f} h "
        f"({rows[0].get('wall')} -> {rows[-1].get('wall')})",
    ]

    unreachable = {
        side: sum(1 for r in rows if "error" in (r.get(side) or {}))
        for side in ("wled", "bridge")
    }
    for side, count in unreachable.items():
        if count:
            lines.append(f"  !! {side} unreachable in {count}/{len(rows)} samples")

    resets = []
    for key in COUNTERS:
        points = _series(flat, key)
        for (_, prev), (t, cur) in zip(points, points[1:]):
            if cur < prev:
                resets.append(f"{key} at t={t:.0f}s ({prev:.0f} -> {cur:.0f})")
    if resets:
        lines.append("  !! counter went backwards — something restarted mid-run:")
        lines.extend(f"       {r}" for r in resets)
        lines.append("     Deltas spanning that point are meaningless.")

    lines.append("  trends (first -> last, min/max, least-squares slope per hour):")
    for key, label in TRENDS.items():
        points = _series(flat, key)
        if not points:
            lines.append(f"    {label:<24} no data")
            continue
        values = [v for _, v in points]
        slope = _slope_per_hour(points)
        slope_text = f"{slope:+.1f}/h" if slope is not None else "n/a"
        lines.append(
            f"    {label:<24} {values[0]:.1f} -> {values[-1]:.1f}  "
            f"(min {min(values):.1f}, max {max(values):.1f})  {slope_text}"
        )

    lines.extend(_rate_lines(flat, span))
    lines.extend(_hourly_lines(flat))
    lines.extend(_transport_lines(flat))
    lines.extend(_latch_lines(flat))
    return lines


def _pearson(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3 or len(a) != len(b):
        return None
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((y - mb) ** 2 for y in b) ** 0.5
    if da == 0 or db == 0:
        return None
    return num / (da * db)


def _paired(flat: list[dict], *keys: str) -> tuple[list, ...]:
    """Rows where every key is numeric, as parallel lists."""
    cols: tuple[list, ...] = tuple([] for _ in keys)
    for row in flat:
        values = [row.get(k) for k in keys]
        if all(isinstance(v, (int, float)) and not isinstance(v, bool)
               for v in values):
            for col, value in zip(cols, values):
                col.append(float(value))
    return cols


def _transport_lines(flat: list[dict]) -> list[str]:
    """Say whether the device's stalls look like network or like firmware.

    The question this answers is a purchasing one: a wired controller removes
    the WiFi hop, so it fixes transport problems and does nothing at all for a
    blocked `loop()`. Splitting TCP connect time (below the sketch) from HTTP
    reply time (needs the sketch) is what separates them.
    """
    http, tcp = _paired(flat, "wled.http_ms", "wled.tcp_ms")
    if len(http) < 10:
        return ["  transport vs firmware: not enough paired TCP/HTTP samples yet."]

    lines = ["  transport vs firmware (does a wired board help?):"]
    lines.append(f"    device TCP connect   median {statistics.median(tcp):6.1f} ms   "
                 f"p95 {_pct(tcp, 0.95):7.1f} ms")
    lines.append(f"    device HTTP reply    median {statistics.median(http):6.1f} ms   "
                 f"p95 {_pct(http, 0.95):7.1f} ms")

    corr = _pearson(http, tcp)
    if corr is not None:
        lines.append(f"    corr(HTTP, TCP) = {corr:+.3f}")

    http_rssi, rssi = _paired(flat, "wled.http_ms", "wled.rssi")
    corr_rssi = _pearson(http_rssi, rssi) if http_rssi else None
    if corr_rssi is not None:
        lines.append(f"    corr(HTTP, RSSI) = {corr_rssi:+.3f}")

    # Compare the two during the worst HTTP samples specifically. Correlation
    # over the whole run can be dominated by the calm majority. Split by rank,
    # not by value: a run where most samples sit at one number would put the
    # whole population on one side of a value threshold.
    pairs = sorted(zip(http, tcp), key=lambda p: p[0])
    cut = max(1, len(pairs) // 10)
    spike_tcp = [t for _, t in pairs[-cut:]]
    calm_tcp = [t for _, t in pairs[:-cut]]
    if spike_tcp and calm_tcp:
        ratio = statistics.fmean(spike_tcp) / max(statistics.fmean(calm_tcp), 0.1)
        lines.append(
            f"    during the worst 10% of HTTP samples, TCP connect averaged "
            f"{statistics.fmean(spike_tcp):.1f} ms vs {statistics.fmean(calm_tcp):.1f} ms "
            f"otherwise ({ratio:.1f}x)"
        )
        if ratio >= 2.0:
            lines.append("    => TCP rose with HTTP: the network path is implicated. "
                         "A wired board would likely help.")
        else:
            lines.append("    => TCP stayed flat while HTTP spiked: packets are "
                         "arriving fine and the firmware's main loop is blocking. "
                         "A wired board would NOT fix this on its own.")
    lines.append("     RSSI is link strength only — it cannot see channel "
                 "congestion, so a flat RSSI correlation does not by itself "
                 "clear WiFi.")
    return lines


def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile. Small n here, so no interpolation games."""
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def _hourly_lines(flat: list[dict]) -> list[str]:
    """Bucket the device's health by hour.

    A whole-run slope hides the shape that matters here: occasional stalls that
    become frequent stalls look almost identical in a least-squares fit, but
    only the second one is progressive drift. Worst-case per hour shows it.
    """
    buckets: dict[int, list[dict]] = {}
    for row in flat:
        t = row.get("t")
        if t is None:
            continue
        buckets.setdefault(int(t // 3600), []).append(row)
    if len(buckets) < 1:
        return []

    lines = ["  per hour (device fps worst/median, device HTTP median/p95 ms, "
             "n samples):"]
    for hour in sorted(buckets):
        rows = buckets[hour]
        fps = [r["wled.fps"] for r in rows
               if isinstance(r.get("wled.fps"), (int, float))]
        http = [r["wled.http_ms"] for r in rows
                if isinstance(r.get("wled.http_ms"), (int, float))]
        if not fps or not http:
            continue
        lines.append(
            f"    h{hour}  fps {min(fps):>3.0f}/{statistics.median(fps):>4.1f}   "
            f"http {statistics.median(http):>6.1f}/{_pct(http, 0.95):>7.1f}   "
            f"n={len(rows)}"
        )
    lines.append("     A worsening run shows the fps worst-case falling and the "
                 "HTTP p95 climbing, hour over hour.")
    return lines


def _rate_lines(flat: list[dict], span: float) -> list[str]:
    """Effective DDP send rate and error/stall growth across the run."""
    lines = []
    if span <= 0:
        return lines
    for key, label in (("bridge.ddp_frames_sent", "DDP frames sent"),
                       ("bridge.rendered", "frames rendered")):
        points = _series(flat, key)
        if len(points) < 2:
            continue
        delta = points[-1][1] - points[0][1]
        elapsed = points[-1][0] - points[0][0]
        if elapsed > 0 and delta >= 0:
            lines.append(f"  {label}: {delta:.0f} over {elapsed / 3600:.2f} h "
                         f"= {delta / elapsed:.1f}/s")
    for key, label in (("bridge.send_errors", "DDP send errors"),
                       ("bridge.stalls", "render stalls"),
                       ("bridge.skipped", "frames skipped")):
        points = _series(flat, key)
        if len(points) < 2:
            continue
        delta = points[-1][1] - points[0][1]
        marker = "  !!" if delta > 0 else "  "
        lines.append(f"{marker} {label} gained during the run: {delta:.0f}")
    return lines


def _latch_lines(flat: list[dict]) -> list[str]:
    """Flag the realtime latch: device live while the bridge sent nothing.

    See `evidence/wled-realtime-latch-2026-07-28.md`. With the realtime timeout
    at WLED's 65000 ms sentinel the controller never leaves realtime mode, so
    `strip.service()` — and with it every state change and OTA — stays blocked
    until the device is power-cycled.
    """
    points = [(r["t"], r.get("wled.live"), r.get("bridge.ddp_frames_sent"))
              for r in flat]
    latched = 0
    for (_, _, prev_frames), (_, live, frames) in zip(points, points[1:]):
        if live and prev_frames is not None and frames == prev_frames:
            latched += 1
    if not latched:
        return []
    return [
        f"  !! WLED reported live realtime across {latched} interval(s) in which "
        f"the bridge sent no DDP frames.",
        "     That is the realtime latch — see "
        "evidence/wled-realtime-latch-2026-07-28.md.",
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.wled_soak",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    log_cmd = sub.add_parser("log", help="sample both sides onto a JSONL file")
    log_cmd.add_argument("--out", default="soak.jsonl", help="output JSONL path")
    log_cmd.add_argument("--wled", default=DEFAULT_WLED, help="WLED host")
    log_cmd.add_argument("--bridge", default=DEFAULT_BRIDGE,
                         help="bridge host:port serving /api/status")
    log_cmd.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                         help="seconds between samples")
    log_cmd.add_argument("--duration", help="stop after e.g. 30m, 4h (default: run until Ctrl+C)")
    log_cmd.add_argument("--quiet", action="store_true")

    analyse_cmd = sub.add_parser("analyse", help="summarise a soak log")
    analyse_cmd.add_argument("path")

    args = parser.parse_args(argv)

    if args.command == "log":
        try:
            duration = parse_duration(args.duration) if args.duration else None
        except ValueError:
            print(f"bad --duration {args.duration!r}: use e.g. 90, 30s, 15m, 4h",
                  file=sys.stderr)
            return 2
        try:
            run_log(args.wled, args.bridge, args.out, args.interval, duration,
                    quiet=args.quiet)
        except (ValueError, OSError) as exc:
            print(f"soak failed: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        for line in analyse(args.path):
            print(line)
    except SoakError as exc:
        print(f"cannot analyse: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
