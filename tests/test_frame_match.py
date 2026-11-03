"""Tests for the bridge-vs-device content check.

Pins the two ways it gave a wrong verdict: an empty bridge preview matching
vacuously, and the brightness scale depending on which live view answered.

Run: python -m pytest tests/test_frame_match.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import frame_match  # noqa: E402


class _View:
    def __init__(self, leds, prescaled):
        self._leds, self.prescaled = leds, prescaled
        self.source = "/json/live" if prescaled else "websocket peek"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def leds(self):
        return self._leds


def _run(monkeypatch, capsys, cells, leds, prescaled=False, sender=(False, "", "")):
    monkeypatch.setattr(frame_match, "open_live",
                        lambda host, prefer="auto": _View(leds, prescaled))
    monkeypatch.setattr(frame_match, "bridge_cells",
                        lambda addr: (cells, {"cue": "X", "paused": False}))
    monkeypatch.setattr(frame_match, "realtime_source", lambda host: sender)
    code = frame_match.main([])
    return code, capsys.readouterr()


def test_an_empty_bridge_preview_is_refused_not_matched(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, cells=[], leds=[(1, 2, 3)] * 120)
    assert code == 2
    assert "preview is empty" in out.err


def test_peek_values_compare_unscaled(monkeypatch, capsys):
    cells = [(100, 50, 0)] * 60
    code, out = _run(monkeypatch, capsys, cells, [(100, 50, 0)] * 120)
    assert code == 0 and "MATCH" in out.out


def test_a_different_frame_diverges(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, [(100, 50, 0)] * 60, [(0, 0, 200)] * 120)
    assert code == 1 and "DIVERGED" in out.out


def test_a_second_sender_is_named(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, [(100, 50, 0)] * 60, [(0, 0, 200)] * 120,
                     sender=(True, "DDP", "192.168.0.15"))
    assert "written by 192.168.0.15" in out.out
