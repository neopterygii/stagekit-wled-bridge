"""Replay a capture over UDP, at its original timing, to a running bridge.

The companion to `replay.player`: that one renders in process for tests, this
one puts the recorded datagrams back on the wire so a real strip lights up.
Useful for judging a change by eye, and for the visual acceptance checks after a
WLED firmware upgrade — the same performance, on demand, without playing the
song again.

    python -m replay.cli capture.jsonl                    # to localhost
    python -m replay.cli capture.jsonl --host 192.168.0.230
    python -m replay.cli capture.jsonl --speed 2 --loop

Datagrams are sent verbatim, so what the bridge parses is exactly what YARG
sent. `test_sender.py` remains the tool for synthetic patterns with no capture.
"""

import argparse
import socket
import sys
import time

from config import YARG_LISTEN_PORT
from replay.capture import CaptureError, read_capture


def replay_over_udp(path, host: str = "127.0.0.1", port: int = YARG_LISTEN_PORT,
                    speed: float = 1.0, loop: bool = False,
                    quiet: bool = False) -> int:
    """Send a capture's datagrams to *host*:*port*, preserving relative timing.

    Returns the number of datagrams sent. Sleeps against a monotonic deadline
    rather than sleeping the gap between packets, so scheduling jitter doesn't
    accumulate into drift across a long capture.
    """
    header, packets = read_capture(path)
    if not packets:
        print(f"{path}: no packets in capture", file=sys.stderr)
        return 0

    if speed <= 0:
        raise ValueError("--speed must be greater than 0")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    addr = (host, port)
    duration = packets[-1].t
    sent = 0

    if not quiet:
        print(f"Replaying {len(packets)} datagrams ({duration:.1f}s at 1×) "
              f"to {host}:{port} at {speed}× — Ctrl+C to stop")
        if header.note:
            print(f"  note: {header.note}")
        if header.started:
            print(f"  recorded: {header.started}")

    try:
        while True:
            start = time.monotonic()
            for pkt in packets:
                target = start + pkt.t / speed
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                sock.sendto(pkt.data, addr)
                sent += 1
            if not loop:
                break
            if not quiet:
                print(f"  … looping ({sent} sent)")
    except KeyboardInterrupt:
        if not quiet:
            print(f"\nStopped after {sent} datagrams.")
    finally:
        sock.close()

    if not quiet and sent == len(packets):
        print(f"Done — {sent} datagrams sent.")
    return sent


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Replay a YARG capture over UDP to a running bridge")
    ap.add_argument("capture", help="path to a .jsonl capture")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bridge address (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=YARG_LISTEN_PORT,
                    help=f"bridge UDP port (default: {YARG_LISTEN_PORT})")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="playback rate multiplier (default: 1.0)")
    ap.add_argument("--loop", action="store_true", help="repeat until stopped")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        replay_over_udp(args.capture, host=args.host, port=args.port,
                        speed=args.speed, loop=args.loop, quiet=args.quiet)
    except (CaptureError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
