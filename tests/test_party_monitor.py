"""tools/party_monitor.py: the watcher's session files, end-of-night report and relay mode."""

import json

import anomalies as an
from tests.test_anomalies import BRIDGE_IP, snap
from tools import party_monitor as pm


def test_session_writes_evidence_and_report(tmp_path, capsys):
    session = pm.Session(str(tmp_path))
    det = an.Detector(BRIDGE_IP)
    det.title(0, "Song B")
    for i in range(100):
        t = i / 10
        s = snap(t, frames=10 if t > 2 else None)
        session.record(t, s)
        det.bridge(t, s)
        for ev in det.drain():
            session.emit(ev)
    s = snap(10.0, scene="Menu", cue="MENU")
    det.bridge(10.0, s)
    for ev in det.drain():
        session.emit(ev)
    out = capsys.readouterr().out
    assert "ALERT" in out and "dark_playing" in out and "SONG" in out
    evs = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    files = [e["evidence_file"] for e in evs if e.get("evidence_file")]
    assert files and (tmp_path / files[0]).exists()
    text = pm.report(str(tmp_path))
    assert "### bridge" in text and "`dark_playing`" in text and "Song B" in text
    assert (tmp_path / "report.md").exists()
