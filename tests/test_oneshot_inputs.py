"""One-shot inputs must survive a stuck value.

On 2026-08-23 a YARG layout change (datagram v5) put the Beat byte — whose
dequeue fallback is a non-zero 3 — under the offset the bridge read as
`bonus_effect`. It read true on every datagram, the white bonus burst re-armed
~88 times a second, and the strip sat solid white for a whole song while the
dashboard showed correct cues.

Nothing here prevents the next misparse. It bounds one: a one-shot input that
reads "set" on every datagram is treated as stuck, stops reaching the engine,
and is named on the status snapshot so the `parser_garbage` anomaly can say so.

Run: python -m pytest tests/test_oneshot_inputs.py -v
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as bridge_main  # noqa: E402
from anomalies import Detector  # noqa: E402
from main import STUCK_ONESHOT_RUN, YARGProtocol  # noqa: E402
from protocol.yarg_packet import BeatByte, KeyframeByte  # noqa: E402
from status_server import StatusTracker  # noqa: E402


class _Engine:
    bpm = 120.0

    def __init__(self):
        self.bonus = 0
        self.beats = []
        self.keyframes = []

    def on_bonus(self):
        self.bonus += 1

    def on_beat(self, value):
        self.beats.append(value)

    def on_keyframe(self, value):
        self.keyframes.append(value)

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Sink:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _packet(beat=0, keyframe=0, bonus=False):
    return SimpleNamespace(
        datagram_version=5, bpm=120.0, venue_size=0, lighting_cue=0,
        strobe_state=0, beat=beat, keyframe=keyframe,
        guitar_notes=0, bass_notes=0, drum_notes=0, keys_notes=0,
        vocal_note=0.0, harmony0_note=0.0, harmony1_note=0.0,
        harmony2_note=0.0, spotlight=0, singalong=0, post_processing=0,
        fog_state=False, song_section=0, bonus_effect=bonus, paused=1,
        sp_active=False, sp_amount=0.0, sp_charge=0.0, sp_active_count=0,
        camera_cut_subject=0, camera_cut_priority=0, scene=2, auto_gen=False,
    )


def _feed(monkeypatch, protocol, packets):
    """Push pre-parsed packets through the real datagram path."""
    queue = list(packets)
    monkeypatch.setattr(bridge_main, "parse_packet", lambda _d: queue.pop(0))
    for _ in range(len(queue)):
        protocol.datagram_received(b"", ("127.0.0.1", 0))


def _protocol(tracker=None):
    engine = _Engine()
    return engine, YARGProtocol(engine, tracker or _Sink(), _Sink())


# ── bonus ───────────────────────────────────────────────────────────────


def test_a_stuck_bonus_fires_exactly_once(monkeypatch):
    engine, protocol = _protocol()
    _feed(monkeypatch, protocol, [_packet(bonus=True)] * 100)
    assert engine.bonus == 1


def test_two_separate_bonuses_both_fire(monkeypatch):
    engine, protocol = _protocol()
    _feed(monkeypatch, protocol,
          [_packet(bonus=True), _packet(), _packet(bonus=True)])
    assert engine.bonus == 2


def test_a_bonus_fires_again_after_a_release(monkeypatch):
    engine, protocol = _protocol()
    _feed(monkeypatch, protocol, [_packet(bonus=True)] * 20)
    protocol.release_latched_state()
    _feed(monkeypatch, protocol, [_packet(bonus=True)])
    assert engine.bonus == 2


# ── beat ────────────────────────────────────────────────────────────────


def test_a_stuck_measure_beat_stops_reaching_the_engine(monkeypatch):
    tracker = StatusTracker()
    engine, protocol = _protocol(tracker)
    _feed(monkeypatch, protocol, [_packet(beat=BeatByte.MEASURE)] * 100)
    forwarded = [b for b in engine.beats if b == BeatByte.MEASURE]
    assert len(forwarded) < STUCK_ONESHOT_RUN
    assert "beat" in tracker.stuck_inputs
    assert "beat" in tracker.snapshot()["stuck_inputs"]


def test_alternating_measure_and_strong_still_counts_as_stuck(monkeypatch):
    engine, protocol = _protocol()
    packets = [_packet(beat=BeatByte.MEASURE if i % 2 else BeatByte.STRONG)
               for i in range(100)]
    _feed(monkeypatch, protocol, packets)
    real = [b for b in engine.beats if b in (BeatByte.MEASURE, BeatByte.STRONG)]
    assert len(real) < STUCK_ONESHOT_RUN


def test_a_normal_beat_pattern_is_forwarded_every_time(monkeypatch):
    tracker = StatusTracker()
    engine, protocol = _protocol(tracker)
    packets = []
    for _ in range(20):
        packets.append(_packet(beat=BeatByte.MEASURE))
        packets.extend([_packet(beat=BeatByte.WEAK)] * 43)  # YARG's "no beat" 3
    _feed(monkeypatch, protocol, packets)
    assert engine.beats.count(BeatByte.MEASURE) == 20
    assert tracker.stuck_inputs == []


def test_a_short_burst_of_queued_beats_is_not_stuck(monkeypatch):
    # YARG queues beats, so a hitch can release a few on consecutive datagrams.
    engine, protocol = _protocol()
    _feed(monkeypatch, protocol,
          [_packet(beat=BeatByte.STRONG)] * (STUCK_ONESHOT_RUN - 1) + [_packet()])
    assert engine.beats.count(BeatByte.STRONG) == STUCK_ONESHOT_RUN - 1


def test_a_stuck_input_recovers_when_the_value_clears(monkeypatch):
    tracker = StatusTracker()
    engine, protocol = _protocol(tracker)
    _feed(monkeypatch, protocol, [_packet(beat=BeatByte.MEASURE)] * 50)
    assert tracker.stuck_inputs == ["beat"]
    _feed(monkeypatch, protocol, [_packet(), _packet(beat=BeatByte.MEASURE)])
    assert tracker.stuck_inputs == []
    assert engine.beats[-1] == BeatByte.MEASURE


# ── keyframe ────────────────────────────────────────────────────────────


def test_a_stuck_keyframe_stops_reaching_the_engine(monkeypatch):
    tracker = StatusTracker()
    engine, protocol = _protocol(tracker)
    _feed(monkeypatch, protocol, [_packet(keyframe=KeyframeByte.NEXT)] * 100)
    assert engine.keyframes.count(KeyframeByte.NEXT) < STUCK_ONESHOT_RUN
    assert tracker.stuck_inputs == ["keyframe"]


def test_release_clears_the_stuck_list(monkeypatch):
    tracker = StatusTracker()
    _engine, protocol = _protocol(tracker)
    _feed(monkeypatch, protocol,
          [_packet(beat=BeatByte.MEASURE, bonus=True)] * 50)
    assert tracker.stuck_inputs == ["beat", "bonus_effect"]
    protocol.release_latched_state()
    assert tracker.stuck_inputs == []


# ── the anomaly ─────────────────────────────────────────────────────────


def _snap(**over):
    snap = {"connected": True, "scene_id": 2, "cue": "DEFAULT", "bpm": 120.0,
            "packets_per_sec": 88.0, "output_live": True, "stuck_inputs": []}
    snap.update(over)
    return snap


def _kinds(det):
    return [ev["kind"] for ev in det.drain()]


def test_stuck_inputs_raise_parser_garbage():
    det = Detector()
    for i in range(30):
        det.bridge(100.0 + i * 0.1, _snap(stuck_inputs=["bonus_effect"]))
    assert "parser_garbage" in _kinds(det)


def test_no_stuck_inputs_raise_nothing():
    det = Detector()
    for i in range(30):
        det.bridge(100.0 + i * 0.1, _snap())
    assert "parser_garbage" not in _kinds(det)
