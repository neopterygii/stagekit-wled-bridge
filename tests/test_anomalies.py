"""anomalies.Detector, driven through synthetic 10 Hz timelines (the external watcher's view)."""

import anomalies as pm

BRIDGE_IP = "192.168.0.230"


def snap(t, *, scene="Gameplay", cue="VERSE", connected=True, paused=False, frames=None, level=120,
         strobe=0, bpm=120.0, beat=None, pps=90.0, stalls=0, gap=17.0, commit="abc", pkts=None,
         live=True, held=False, beats=None):
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
        "beats_received": n // 5 if beats is None else beats,
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
    assert "beat_stalled" in kinds(run(det, 12, beats=7))
    det = pm.Detector(BRIDGE_IP)  # an older image without the counter can't tell, so stays quiet
    evs = []
    for i in range(120):
        s = snap(i / 10, beats=7)
        del s["beats_received"]
        det.bridge(i / 10, s)
        evs += det.drain()
    assert "beat_stalled" not in kinds(evs)


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


# ── the bridge's own view: every frame's brightness, not a 10 Hz preview sample ──────────────

def run_frames(det, seconds, level_fn, start=0.0, frame=None, **kw):
    """10 Hz snapshots, each carrying the six 60 fps frame levels since the last one."""
    evs = []
    for i in range(int(seconds * 10)):
        t = start + i / 10
        levels = [level_fn(t + k / 60) for k in range(6)]
        s = snap(t, live=False, **kw)
        s["output_live"] = True
        fr = frame(t) if frame else bytes([int(levels[-1])]) * 360 + bytes([i % 256])
        det.bridge(t, s, levels=levels, frame=fr)
        evs += det.drain()
    return evs


def test_frame_rate_beat_pulse_is_not_flicker():
    # a hit on each beat at 140 bpm, decaying over ~200 ms: one alternation per beat
    def pulse(t):
        since = (t * 140 / 60) % 1 * 60 / 140
        return 40 + 160 * max(0.0, 1 - since / 0.2)
    assert "flicker" not in kinds(run_frames(pm.Detector(BRIDGE_IP), 10, pulse))


def test_frame_rate_alternation_is_flicker():
    alt = lambda t: 200 if int(round(t * 60)) % 2 else 30
    assert "flicker" in kinds(run_frames(pm.Detector(BRIDGE_IP), 3, alt))


def test_frame_rate_black_and_frozen():
    assert "black_strip" in kinds(run_frames(pm.Detector(BRIDGE_IP), 6, lambda t: 0.5, cue="CHORUS"))
    still = bytes([90]) * 360
    evs = run_frames(pm.Detector(BRIDGE_IP), 35, lambda t: 90, frame=lambda t: still)
    assert "frozen_strip" in kinds(evs)


def test_no_frames_means_not_live():
    det = pm.Detector(BRIDGE_IP)
    evs = []
    for i in range(100):
        s = snap(i / 10, cue="CHORUS")
        det.bridge(i / 10, s, levels=[], frame=None)  # the gate is shut: nothing was sent
        evs += det.drain()
    assert "black_strip" not in kinds(evs) and "flicker" not in kinds(evs)


def test_wled_down_only_matters_while_someone_plays():
    det = pm.Detector(BRIDGE_IP)
    det.wled(0, {"error": "unplugged"})
    assert det.drain() == []
    run(det, 1, start=1)
    det.wled(2, {"error": "unplugged"})
    assert "wled_down" in kinds(det.drain())


def test_kind_filter_keeps_songs_but_drops_other_kinds():
    det = pm.Detector(BRIDGE_IP, kinds=pm.EXTERNAL_KINDS)
    det.title(0, "Song C")
    evs = run(det, 3, frames=5) + run(det, 1, start=3, scene="Menu", cue="MENU")
    assert kinds(evs) == []
    assert [e["label"] for e in evs if e["kind"] == "song"] == ["Song C"]


# ── AnomalyLog ──────────────────────────────────────────────────────────────────────────────

class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def drive(log, clock, seconds, **kw):
    for i in range(int(seconds * 10)):
        clock.t += 0.1
        log.tick(snap(clock.t, **kw))


def test_log_numbers_persists_and_reloads(tmp_path):
    clock = Clock()
    log = pm.AnomalyLog(directory=str(tmp_path), bridge_ip=BRIDGE_IP, clock=clock)
    log.start("v-test")
    drive(log, clock, 2)
    drive(log, clock, 8, frames=1)
    s = log.summary()
    assert [e["kind"] for e in s["open"]] == ["dark_playing"]
    assert s["today"] == {"alerts": 1, "warnings": 0}
    assert s["recent"][0]["kind"] == "dark_playing"
    seqs = [e["seq"] for e in log.since(0)]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert all(e["seq"] > seqs[0] for e in log.since(seqs[0]))

    again = pm.AnomalyLog(directory=str(tmp_path), clock=clock)  # a restart mid-party
    assert again.seq == log.seq
    assert again.summary()["today"] == {"alerts": 1, "warnings": 0}


def test_log_caps_evidence_per_kind(tmp_path):
    clock, calls = Clock(), []
    log = pm.AnomalyLog(clock=clock, on_evidence=lambda ev: calls.append(ev["kind"]) or f"x{len(calls)}")
    drive(log, clock, 1)
    for _ in range(pm.EVIDENCE_PER_KIND + 3):
        drive(log, clock, 6, frames=0, cue="UNKNOWN(99)")
        drive(log, clock, 1, cue="UNKNOWN(99)")
    assert calls.count("parser_garbage") == 1  # one long episode
    for _ in range(pm.EVIDENCE_PER_KIND + 3):
        drive(log, clock, 2, cue="UNKNOWN(99)")
        drive(log, clock, 1)
    assert calls.count("parser_garbage") == pm.EVIDENCE_PER_KIND


def test_log_survives_a_detector_bug():
    clock = Clock()
    log = pm.AnomalyLog(clock=clock)
    log.tick({"scene": object(), "render": "not a dict"})  # garbage in
    assert log.errors == 1 and log.last_error
    drive(log, clock, 1)
    assert log.errors == 1


def test_log_reads_each_wled_sample_once():
    clock = Clock()
    log = pm.AnomalyLog(bridge_ip=BRIDGE_IP, clock=clock)
    drive(log, clock, 1)
    sample = {"live": True, "lip": "192.168.0.15", "lm": "DDP", "uptime": 5}
    log.tick(snap(clock.t), wled=sample)
    log.tick(snap(clock.t), wled=sample)
    assert [e["kind"] for e in log.since(0)].count("second_sender") == 1


def test_summary_is_cheap_and_tick_fits_the_status_loop():
    import time as _t
    clock = Clock()
    log = pm.AnomalyLog(bridge_ip=BRIDGE_IP, clock=clock)
    s = snap(0)
    frame = bytes(range(256)) + bytes(104)
    start = _t.perf_counter()
    for i in range(600):  # a minute of status loop
        clock.t += 0.1
        log.tick(s, levels=[50.0, 80.0, 120.0, 90.0, 60.0, 55.0], frame=frame)
        log.summary()
    per_tick_ms = (_t.perf_counter() - start) / 600 * 1000
    assert per_tick_ms < 1.0, per_tick_ms  # the loop's budget is 100 ms
