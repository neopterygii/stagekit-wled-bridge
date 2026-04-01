"""Test packet sender — generates fake YARG UDP packets for development.

Usage:
  python test_sender.py --pattern chase_blue
  python test_sender.py --pattern all_on
  python test_sender.py --pattern strobe_fast
  python test_sender.py --pattern warm_loop
  python test_sender.py --pattern cool_loop
  python test_sender.py --pattern sweep
  python test_sender.py --pattern big_rock_ending
  python test_sender.py --pattern cycle_cues
"""

import argparse
import socket
import struct
import time

from protocol.yarg_packet import CueByte, BeatByte, StrobeSpeed

YARG_HEADER = 0x59415247  # "YARG"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 36107
DEFAULT_BPM = 120.0


def build_packet(
    cue: int = CueByte.DEFAULT,
    strobe: int = StrobeSpeed.OFF,
    beat: int = BeatByte.OFF,
    bpm: float = DEFAULT_BPM,
    keyframe: int = 0,
    drum_notes: int = 0,
) -> bytes:
    """Build a minimal valid YARG UDP packet (44+ bytes)."""
    buf = bytearray(47)

    # Header (big-endian)
    struct.pack_into(">I", buf, 0, YARG_HEADER)

    # Version, platform
    buf[4] = 3   # datagram version
    buf[5] = 1   # platform (Windows)

    # Scene, pause, venue
    buf[6] = 1   # in-game scene
    buf[7] = 0   # not paused
    buf[8] = 1   # large venue

    # BPM (little-endian float)
    struct.pack_into("<f", buf, 9, bpm)

    # Song section
    buf[13] = 0

    # Instruments
    buf[14] = 0  # guitar
    buf[15] = 0  # bass
    buf[16] = drum_notes  # drums
    buf[17] = 0  # keys
    struct.pack_into("<f", buf, 18, 0.0)  # vocals
    struct.pack_into("<f", buf, 22, 0.0)  # harmony0
    struct.pack_into("<f", buf, 26, 0.0)  # harmony1
    struct.pack_into("<f", buf, 30, 0.0)  # harmony2

    # Lighting
    buf[34] = cue
    buf[35] = 0  # post processing
    buf[36] = 0  # fog
    buf[37] = strobe
    buf[38] = beat
    buf[39] = keyframe
    buf[40] = 0  # bonus
    buf[41] = 0  # autogen
    buf[42] = 0  # spotlight
    buf[43] = 0  # singalong

    # Camera (optional, padding to 47)
    buf[44] = 0  # camera constraint
    buf[45] = 0  # camera priority
    buf[46] = 0  # camera subject

    return bytes(buf)


def send_loop(sock: socket.socket, addr: tuple, pattern: str, bpm: float):
    beat_interval = 60.0 / bpm
    beat_count = 0

    print(f"Sending pattern '{pattern}' to {addr[0]}:{addr[1]} at {bpm} BPM")
    print("Press Ctrl+C to stop\n")

    try:
        if pattern == "all_on":
            pkt = build_packet(cue=CueByte.SCORE, bpm=bpm)
            while True:
                beat_count += 1
                beat_type = BeatByte.MEASURE if beat_count % 4 == 0 else BeatByte.STRONG
                pkt_with_beat = build_packet(cue=CueByte.SCORE, bpm=bpm, beat=beat_type)
                sock.sendto(pkt_with_beat, addr)
                time.sleep(beat_interval / 4)

        elif pattern == "warm_loop":
            while True:
                beat_count += 1
                beat_type = BeatByte.MEASURE if beat_count % 4 == 0 else BeatByte.STRONG
                sock.sendto(build_packet(cue=CueByte.WARM_AUTOMATIC, bpm=bpm, beat=beat_type), addr)
                time.sleep(beat_interval / 4)

        elif pattern == "cool_loop":
            while True:
                beat_count += 1
                beat_type = BeatByte.MEASURE if beat_count % 4 == 0 else BeatByte.STRONG
                sock.sendto(build_packet(cue=CueByte.COOL_AUTOMATIC, bpm=bpm, beat=beat_type), addr)
                time.sleep(beat_interval / 4)

        elif pattern == "sweep":
            while True:
                beat_count += 1
                beat_type = BeatByte.MEASURE if beat_count % 4 == 0 else BeatByte.STRONG
                sock.sendto(build_packet(cue=CueByte.SWEEP, bpm=bpm, beat=beat_type), addr)
                time.sleep(beat_interval / 4)

        elif pattern == "big_rock_ending":
            while True:
                beat_count += 1
                beat_type = BeatByte.MEASURE if beat_count % 4 == 0 else BeatByte.STRONG
                sock.sendto(build_packet(cue=CueByte.BIG_ROCK_ENDING, bpm=bpm, beat=beat_type), addr)
                time.sleep(beat_interval / 4)

        elif pattern == "strobe_fast":
            while True:
                sock.sendto(build_packet(cue=CueByte.WARM_AUTOMATIC, strobe=StrobeSpeed.FAST, bpm=bpm, beat=BeatByte.STRONG), addr)
                time.sleep(beat_interval / 4)

        elif pattern == "cycle_cues":
            cues = [
                ("Warm Auto", CueByte.WARM_AUTOMATIC),
                ("Cool Auto", CueByte.COOL_AUTOMATIC),
                ("Sweep", CueByte.SWEEP),
                ("Frenzy", CueByte.FRENZY),
                ("Harmony", CueByte.HARMONY),
                ("Searchlights", CueByte.SEARCHLIGHTS),
                ("Big Rock Ending", CueByte.BIG_ROCK_ENDING),
                ("Blackout", CueByte.BLACKOUT_FAST),
                ("Score", CueByte.SCORE),
            ]
            while True:
                for name, cue in cues:
                    print(f"  → {name}")
                    for i in range(int(4 * bpm / 60 * 4)):  # ~4 seconds per cue
                        beat_count += 1
                        beat_type = BeatByte.MEASURE if beat_count % 4 == 0 else BeatByte.STRONG
                        sock.sendto(build_packet(cue=cue, bpm=bpm, beat=beat_type), addr)
                        time.sleep(beat_interval / 4)

        else:
            print(f"Unknown pattern: {pattern}")
            return

    except KeyboardInterrupt:
        # Send no-cue on exit
        sock.sendto(build_packet(cue=CueByte.NO_CUE), addr)
        print("\nStopped.")


def main():
    parser = argparse.ArgumentParser(description="YARG test packet sender")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Target host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Target port")
    parser.add_argument("--bpm", type=float, default=DEFAULT_BPM, help="Beats per minute")
    parser.add_argument("--pattern", default="cycle_cues",
                        choices=["all_on", "warm_loop", "cool_loop", "sweep",
                                 "big_rock_ending", "strobe_fast", "cycle_cues"],
                        help="Test pattern to send")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    send_loop(sock, (args.host, args.port), args.pattern, args.bpm)
    sock.close()


if __name__ == "__main__":
    main()
