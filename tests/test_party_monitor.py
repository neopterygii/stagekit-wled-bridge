"""The party monitor's Detector, driven through synthetic 10 Hz timelines."""

import json

from tools import party_monitor as pm

BRIDGE_IP = "192.168.0.230"


def snap(t, *, scene="Gameplay", cue="VERSE", connected=True, paused=False, frames=None, level=120,
         strobe=0, bpm=120.0, beat=None, pps=90.0, stalls=0, gap=17.0, commit="abc", pkts=None,
         live=True, held=False):
    n = int(t * 10)
    frames = n if frames is None else frames
    strip = [level] * 180 if live else []
    if live and level and level >= pm.BLACK_LEVEL:
        strip[n % 180] = 255  # something on the strip moves, as every real cue does
    return {
        "scene": scene, "scene_id": 2, "cue": cue, "connected": connected, "paused": paused,
        "bpm": bpm, "strobe_hz": strobe, "packets_per_sec": pps if connected else 0.0,
        "packets_received": pkts if pkts is not None else n, "output_live": live,
        "last_beat": 1, "bar_beat": (n // 5) % 4 if beat is None else beat, "beat_phase": 0.0,
        "cue_held": held, "auto_gen": False,
        "version": {"commit": commit, "label": f"v · {commit}"},
        "wled_power": {"grace": 15, "mode": "live" if live else "off"},
        "render": {"alive": True, "fatal_error": None, "stalls": stalls, "skipped": 0,
                   "gap_ms_max": gap, "target_ms": 16.7, "work_ms_max": 1.0,
                   "ddp": {"frames_sent": frames, "send_errors": 0}},
        "preview": {"source": "live" if live else "off", "strip": strip},
    }


def run(det, seconds, start=0.0, **kw):
    """Feed 10 Hz snapshots; kw values may be callables of t."""
    evs = []
    for i in range(int(seconds * 10)):
        t = start + i / 10
        det.bridge(t, snap(t, **{k: (v(t) if callable(v) else v) for k, v in kw.items()}))
        det.silence(t)
        evs += det.drain()
    return evs


def kinds(evs, level=None):
    return [e["kind"] for e in evs if e.get("phase") in ("start", "point") and (level is None or e["level"] == level)]


def test_healthy_song_is_quiet_and_summarised():
    det = pm.Detector(BRIDGE_IP)
    det.title(0, "Song A")
    evs = run(det, 120, cue=lambda t: "VERSE" if t < 60 else "CHORUS")
    evs += run(det, 2, start=120, scene="Menu", cue="MENU")
    assert kinds(evs) == []
    song = [e for e in evs if e["kind"] == "song"]
    assert len(song) == 1 and song[0]["label"] == "Song A"
    assert song[0]["cues"] == {"VERSE": 1, "CHORUS": 1}
    assert "2.0 min" in song[0]["msg"]


def test_beat_pulse_is_not_flicker_but_frame_alternation_is():
    pulse = lambda t: 200 if int(t * 10) % 5 == 0 else 120 - 20 * (int(t * 10) % 5)  # once a beat at 120
    det = pm.Detector(BRIDGE_IP)
    assert "flicker" not in kinds(run(det, 10, level=pulse))
    alt = lambda t: 200 if int(t * 10) % 2 else 20
    det = pm.Detector(BRIDGE_IP)
    assert "flicker" in kinds(run(det, 10, level=alt))
    det = pm.Detector(BRIDGE_IP)
    assert "flicker" not in kinds(run(det, 10, level=alt, cue="STROBE_FAST", strobe=8))


def test_dark_while_playing_and_clear():
    det = pm.Detector(BRIDGE_IP)
    evs = run(det, 2)
    evs += run(det, 8, start=2, frames=20)  # frames stop advancing
    assert kinds(evs, "ALERT") == ["dark_playing"]
    evs = run(det, 1, start=10)
    assert [e for e in evs if e["kind"] == "dark_playing" and e["level"] == "CLEAR"]


def test_black_strip_only_under_lit_cues():
    det = pm.Detector(BRIDGE_IP)
    assert "black_strip" in kinds(run(det, 6, level=0, cue="CHORUS"))
    det = pm.Detector(BRIDGE_IP)
    assert "black_strip" not in kinds(run(det, 6, level=0, cue="BLACKOUT_SLOW"))


def test_frozen_strip():
    det = pm.Detector(BRIDGE_IP)
    evs = []
    for i in range(400):
        t = i / 10
        s = snap(t)
        s["preview"]["strip"] = [100] * 180
        det.bridge(t, s)
        evs += det.drain()
    assert "frozen_strip" in kinds(evs)


def test_parser_garbage_and_beat_stall():
    det = pm.Detector(BRIDGE_IP)
    assert "parser_garbage" in kinds(run(det, 3, cue="UNKNOWN(77)"), "ALERT")
    det = pm.Detector(BRIDGE_IP)
    assert "beat_stalled" in kinds(run(det, 12, beat=2))


def test_yarg_lost_mid_song_but_not_after_menu():
    det = pm.Detector(BRIDGE_IP)
    evs = run(det, 5) + run(det, 12, start=5, connected=False)
    assert "yarg_lost" in kinds(evs, "ALERT")
    det = pm.Detector(BRIDGE_IP)
    evs = run(det, 5, scene="Menu") + run(det, 12, start=5, connected=False, scene="Menu")
    assert "yarg_lost" not in kinds(evs)


def test_strobe_stuck_and_outside_gameplay():
    det = pm.Detector(BRIDGE_IP)
    assert "strobe_stuck" in kinds(run(det, 50, cue="STROBE_FAST", strobe=8))
    det = pm.Detector(BRIDGE_IP)
    assert "strobe_outside" in kinds(run(det, 5, scene="Menu", strobe=8))


def test_restart_and_stutter():
    det = pm.Detector(BRIDGE_IP)
    evs = run(det, 2, pkts=1000) + run(det, 1, start=2, pkts=5, commit="def")
    assert "bridge_restart" in kinds(evs, "NOTE")
    det = pm.Detector(BRIDGE_IP)
    assert "render_stutter" in kinds(run(det, 2, stalls=lambda t: int(t)))


def test_bridge_silence():
    det = pm.Detector(BRIDGE_IP)
    run(det, 1)
    det.silence(7.0)
    assert kinds(det.drain(), "ALERT") == ["bridge_down"]


def test_wled_checks():
    det = pm.Detector(BRIDGE_IP)
    run(det, 2)
    ok = {"live": True, "lip": BRIDGE_IP, "lm": "DDP", "uptime": 100, "freeheap": 110000, "rssi": -48,
          "http_ms": 40, "tcp_ms": 5}
    det.wled(2.0, ok)
    assert det.drain() == []
    det.wled(3.0, dict(ok, lip="192.168.0.15", uptime=5))
    got = kinds(det.drain())
    assert "second_sender" in got and "wled_reboot" in got
    det.wled(4.0, {"error": "timeout", "tcp_ms": None})
    assert "wled_down" in kinds(det.drain(), "ALERT")
    det.wled(5.0, dict(ok, uptime=2))  # back after a reboot: compared with the last good sample
    assert "wled_reboot" in kinds(det.drain(), "NOTE")


def test_latch_after_bridge_stops():
    det = pm.Detector(BRIDGE_IP)
    run(det, 2)
    run(det, 20, start=2, scene="Menu", frames=20, connected=False, live=False)
    det.wled(22.0, {"live": True, "lip": BRIDGE_IP, "lm": "DDP", "uptime": 100})
    assert "wled_latched" in kinds(det.drain(), "ALERT")


def test_pids_high():
    det = pm.Detector(BRIDGE_IP)
    det.pids(0, 1900, 2048)
    assert kinds(det.drain(), "ALERT") == ["pids_high"]


def test_session_writes_evidence_and_report(tmp_path, capsys):
    session = pm.Session(str(tmp_path))
    det = pm.Detector(BRIDGE_IP)
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
