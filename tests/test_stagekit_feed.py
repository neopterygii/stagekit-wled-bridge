"""Tests for the Stage Kit visualizer feed on the status snapshot.

The dashboard pushes at ~10 Hz but cue patterns step faster, so the feed is a
log of every mask change with its time rather than a sample of the current
masks. These pin that it logs changes (not frames), keeps them in order with
usable times, and windows what each snapshot carries.

Run: python -m pytest tests/test_stagekit_feed.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import status_server  # noqa: E402
from status_server import STAGEKIT_WINDOW_S, StatusTracker  # noqa: E402


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _tracker(monkeypatch, clock):
    monkeypatch.setattr(status_server.time, "monotonic", clock)
    return StatusTracker()


def test_logs_each_change_not_each_frame(monkeypatch):
    clock = _Clock()
    tr = _tracker(monkeypatch, clock)
    for masks in ([1, 0, 0, 0], [1, 0, 0, 0], [2, 0, 0, 0], [2, 0, 0, 0], [4, 0, 0, 0]):
        tr.on_render(masks, 0.0, 120.0)
        clock.t += 1 / 60
    events = tr.stagekit_snapshot(clock.t)["events"]
    assert [e[1:] for e in events] == [[1, 0, 0, 0], [2, 0, 0, 0], [4, 0, 0, 0]]


def test_a_chase_faster_than_the_push_keeps_every_step(monkeypatch):
    # 12 steps a second against a 10 Hz push: sampling would drop steps; the
    # log must hold all of them, in order, with their real spacing.
    clock = _Clock()
    tr = _tracker(monkeypatch, clock)
    steps = [1 << (i % 8) for i in range(12)]
    for mask in steps:
        for _ in range(5):                      # 5 frames per step at 60 fps
            tr.on_render([0, 0, mask, 0], 0.0, 180.0)
            clock.t += 1 / 60
    events = tr.stagekit_snapshot(clock.t)["events"]
    assert [e[3] for e in events] == steps
    gaps = [b[0] - a[0] for a, b in zip(events, events[1:])]
    assert all(abs(g - 83) <= 1 for g in gaps)  # 5/60 s = 83 ms


def test_snapshot_carries_only_the_recent_window(monkeypatch):
    clock = _Clock()
    tr = _tracker(monkeypatch, clock)
    tr.on_render([1, 0, 0, 0], 0.0, 120.0)
    clock.t += STAGEKIT_WINDOW_S + 0.5
    tr.on_render([2, 0, 0, 0], 0.0, 120.0)
    sk = tr.stagekit_snapshot(clock.t)
    assert [e[1] for e in sk["events"]] == [2]
    assert sk["now_ms"] == round(clock.t * 1000)


def test_full_snapshot_includes_the_feed_and_fog(monkeypatch):
    clock = _Clock()
    tr = _tracker(monkeypatch, clock)
    tr.on_render([0, 255, 0, 0], 0.0, 120.0)
    tr.on_fog(True)
    d = tr.snapshot()
    assert d["stagekit"]["fog"] is True
    assert d["stagekit"]["events"][-1][1:] == [0, 255, 0, 0]
    assert d["zones_raw"] == [0, 255, 0, 0]


def test_the_log_is_bounded(monkeypatch):
    clock = _Clock()
    tr = _tracker(monkeypatch, clock)
    for i in range(status_server.STAGEKIT_LOG_MAX * 2):
        tr.on_render([i % 256, 0, 0, 1], 0.0, 120.0)
        clock.t += 0.001
    assert len(tr._zone_log) == status_server.STAGEKIT_LOG_MAX
