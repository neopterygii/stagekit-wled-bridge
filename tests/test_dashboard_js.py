"""The dashboard's JavaScript parses, loads and survives real snapshots.

`status_server.STATUS_HTML` is one Python string holding the whole page. A
syntax error in its script passes every other test and ships a blank
dashboard. These tests run the script in Node: a syntax check, then a smoke
test against stand-in browser objects (`tests/fixtures/dashboard_stub.js`) fed
with snapshots built by the real `StatusTracker.snapshot()`.

Skipped, with the reason, when `node` is not installed; on CI that is a failure.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anomalies import AnomalyLog  # noqa: E402
from config import LED_COUNT  # noqa: E402
from effects.cue_engine import CueEngine  # noqa: E402
from effects.mapper import LEDMapper  # noqa: E402
from main import DeviceMode, RenderThread, WLEDPowerManager  # noqa: E402
from protocol.yarg_packet import CueByte  # noqa: E402
from settings import BridgeSettings  # noqa: E402
from status_server import STATUS_HTML, StatusTracker
from version import BUILD_INFO  # noqa: E402

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None, reason="node is not on PATH; the dashboard script cannot be checked")

STUB = Path(__file__).resolve().parent / "fixtures" / "dashboard_stub.js"
_NO_SETTINGS_FILE = "/proc/dashboard-js-tests-have-no-settings/settings.json"
_SCRIPT = re.compile(r"<script\b[^>]*>(.*?)</script>", re.DOTALL)


def page_scripts() -> list[str]:
    return _SCRIPT.findall(STATUS_HTML)


# ── snapshots ───────────────────────────────────────────────────────────


class _Api:
    """WLEDApi stand-in: the attributes power_snapshot() and capabilities() read."""

    reachable = True
    wifi_info = {"rssi": -58, "signal": 84, "channel": 6, "bssid": "00:00:00:00:00:00"}
    effects = ["Solid", "Blink", "Breathe"]
    palettes = ["Default", "Party"]
    realtime_timeout_ms = 2500


class _Sink:
    frames_sent = 0

    def send_pixels(self, data):
        self.frames_sent += 1

    def stats(self):
        return {"send_us_avg": 0.0, "send_us_max": 0.0,
                "send_errors": 0, "frames_sent": self.frames_sent}


def _bridge(mode: str):
    """A tracker wired to real collaborators, with the strip in `mode`."""
    clock = lambda: 1000.0  # noqa: E731
    tracker = StatusTracker()
    tracker._sse_queues.append(object())    # a dashboard is watching: build previews
    settings = BridgeSettings(path=_NO_SETTINGS_FILE, warn_unwritable=False)
    power = WLEDPowerManager(_Api(), idle_timeout=1800, settings=settings)
    power._wled_on = mode != DeviceMode.OFF
    power._output_live = mode == DeviceMode.LIVE
    assert power.mode == mode

    engine = CueEngine(clock=clock)
    engine.bpm = 120.0
    thread = RenderThread(engine, LEDMapper(LED_COUNT, clock=clock), _Sink(),
                          tracker, settings, power)
    tracker.render_thread = thread
    tracker.anomalies = AnomalyLog(directory=None, clock=lambda: 1_700_000_000.0)
    return tracker, settings, power, engine, thread


def live_snapshot() -> dict:
    """Mid-song: a cue, a strobe, star power, a venue, and one open warning."""
    tracker, settings, power, engine, thread = _bridge(DeviceMode.LIVE)
    tracker.on_packet()
    tracker.on_cue(CueByte.CHORUS)
    engine.on_cue(CueByte.CHORUS)
    tracker.on_beat(1)
    tracker.on_scene(2)
    tracker.on_venue_size(1)
    tracker.on_song_section(2)
    tracker.on_camera_cut(1, 1)
    tracker.on_star_power(True, 0.5, 2)
    thread.render_frame(1000.0, 40)
    thread.render_frame(1001.0, 40)     # past the cue cross-fade from black
    log = tracker.anomalies
    log.start("for the dashboard test")
    log.det.cond(log.clock(), "wled_slow_http", True, msg="WLED took 900 ms <to> answer")
    log._flush()
    snap = tracker.snapshot(power, settings)
    assert snap["output_live"] is True and any(snap["preview"]["strip"])
    assert snap["anomalies"]["open"] and snap["anomalies"]["recent"]
    return snap


def idle_snapshot() -> dict:
    tracker, settings, power, _, thread = _bridge(DeviceMode.IDLE)
    thread.render_frame(1000.0, 40)
    snap = tracker.snapshot(power, settings)
    assert snap["output_live"] is False and snap["preview"]["source"] == DeviceMode.IDLE
    return snap


def off_snapshot() -> dict:
    tracker, settings, power, _, thread = _bridge(DeviceMode.OFF)
    thread.render_frame(1000.0, 40)
    snap = tracker.snapshot(power, settings)
    assert snap["wled_power"]["on"] is False
    return snap


def older_bridge_snapshot() -> dict:
    """What a bridge from before the version line and the Health card sends."""
    snap = idle_snapshot()
    del snap["version"], snap["anomalies"]
    return snap


def bare_snapshot() -> dict:
    """No power manager, settings or render thread: StatusTracker on its own."""
    return StatusTracker().snapshot()


# ── tests ───────────────────────────────────────────────────────────────


def test_the_page_has_exactly_one_inline_script():
    """The checks below cover every script on the page; say so if that changes."""
    scripts = page_scripts()
    assert len(scripts) == 1
    assert "new EventSource('/events')" in scripts[0]
    assert "<script src" not in STATUS_HTML


def test_ci_does_not_skip_these_tests():
    """A skip is right on a laptop without node. On CI it would hide the check."""
    if os.environ.get("CI"):
        assert NODE is not None, "node is missing on the CI runner; add actions/setup-node"


@needs_node
def test_script_is_valid_javascript(tmp_path):
    for n, source in enumerate(page_scripts()):
        path = tmp_path / f"dashboard_{n}.js"
        path.write_text(source, encoding="utf-8")
        result = subprocess.run([NODE, "--check", str(path)],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr


def _run(tmp_path, snapshots: list[tuple[str, dict]], script: str | None = None):
    (tmp_path / "dashboard.js").write_text(
        page_scripts()[0] if script is None else script, encoding="utf-8")
    (tmp_path / "page.html").write_text(STATUS_HTML, encoding="utf-8")
    _, _, power, _, _ = _bridge(DeviceMode.IDLE)
    (tmp_path / "input.json").write_text(json.dumps({
        "snapshots": [{"name": name, "data": data} for name, data in snapshots],
        "routes": {"/api/wled": power.capabilities()},
    }), encoding="utf-8")
    return subprocess.run(
        [NODE, str(STUB), str(tmp_path / "dashboard.js"), str(tmp_path / "page.html"),
         str(tmp_path / "input.json")],
        capture_output=True, text=True, timeout=60)


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    """The page after each snapshot in turn: {snapshot name: {element id: state}}."""
    if NODE is None:
        pytest.skip("node is not on PATH; the dashboard script cannot be run")
    result = _run(tmp_path_factory.mktemp("dashboard"), [
        ("live", live_snapshot()),
        ("idle", idle_snapshot()),
        ("off", off_snapshot()),
        ("older", older_bridge_snapshot()),
        ("bare", bare_snapshot()),
        ("live_again", live_snapshot()),
    ])
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def older_page(tmp_path_factory):
    """A freshly loaded page whose first snapshots come from an older bridge.

    Separate from `page` because the script keeps state between snapshots: the
    build line is written once, so after a live snapshot the code that reads
    `version` never runs again.
    """
    if NODE is None:
        pytest.skip("node is not on PATH; the dashboard script cannot be run")
    result = _run(tmp_path_factory.mktemp("older"), [
        ("older", older_bridge_snapshot()),
        ("bare", bare_snapshot()),
    ])
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_script_loads_and_survives_every_snapshot(page):
    assert set(page) == {"live", "idle", "off", "older", "bare", "live_again", "after_error"}


def test_live_snapshot_reaches_the_page(page):
    live = page["live"]
    assert live["cue"]["text"] == "CHORUS"
    assert live["conn"]["text"] == "Connected"
    assert live["venue-pill"]["text"] == "SMALL VENUE"
    assert live["sp-state"]["text"] == "2 players"
    assert "hidden" in live["strip-note"]["cls"]
    assert "not-sending" not in live["strip-wrap"]["cls"]


def test_build_label_is_written_from_the_snapshot(page):
    assert page["live"]["build-label"]["text"] == BUILD_INFO["label"]


def test_not_sending_note_names_the_reason(page):
    assert "hidden" not in page["idle"]["strip-note"]["cls"]
    assert "idle look" in page["idle"]["strip-note"]["html"]
    assert "powered off" in page["off"]["strip-note"]["html"]
    assert "not-sending" in page["off"]["strip-wrap"]["cls"]
    assert "hidden" in page["live_again"]["strip-note"]["cls"]


def test_strip_preview_is_painted_only_while_live(page):
    """`lit` is how many pixels of the last canvas paint had any colour."""
    assert page["live"]["strip-canvas"]["lit"] > 0
    assert page["idle"]["strip-canvas"]["lit"] == 0
    assert page["off"]["strip-canvas"]["lit"] == 0
    assert page["live_again"]["strip-canvas"]["lit"] > 0


def test_health_card_lists_the_open_warning_escaped(page):
    live = page["live"]
    assert "warn" in live["anomaly-card"]["cls"].split()
    assert "wled_slow_http" in live["anomaly-open"]["html"]
    assert "&lt;to&gt;" in live["anomaly-open"]["html"]
    assert "<to>" not in live["anomaly-open"]["html"]
    assert "bridge_start" in live["anomaly-recent"]["html"]


def test_older_bridge_payload_does_not_break_the_page(page, older_page):
    """No `version` and no `anomalies`: the Health card says detection is off."""
    assert "Anomaly detection is off" in page["older"]["anomaly-state"]["html"]
    assert "Anomaly detection is off" in older_page["older"]["anomaly-state"]["html"]
    assert older_page["older"]["build-label"]["text"] == ""
    assert older_page["bare"]["build-label"]["text"] == BUILD_INFO["label"]


def test_idle_editor_is_filled_from_the_wled_capabilities(page):
    """/api/wled is fetched once and its names become the select options."""
    assert page["live"]["idle-effect"]["kids"] == len(_Api.effects)
    assert page["live"]["idle-palette"]["kids"] == len(_Api.palettes)


def test_no_element_ever_reads_undefined_or_nan(page, older_page):
    for pages in (page, older_page):
        for name, elements in pages.items():
            for element_id, state in elements.items():
                for field in ("text", "html"):
                    assert not re.search(r"\bundefined\b|\bNaN\b", state[field]), (
                        f"#{element_id} after the {name} snapshot: {state[field]!r}")


def test_stream_error_shows_reconnecting(page):
    assert page["after_error"]["conn"]["text"] == "Reconnecting..."
    assert page["after_error"]["dot"]["cls"] == "status-dot off"


@needs_node
def test_the_harness_fails_when_the_script_throws(tmp_path):
    """The smoke test can fail: a reference to a missing element is caught."""
    broken = page_scripts()[0].replace(
        "document.getElementById('conn')", "document.getElementById('no-such-element')", 1)
    assert broken != page_scripts()[0]
    result = _run(tmp_path, [("live", live_snapshot())], script=broken)
    assert result.returncode != 0
    assert "TypeError" in result.stderr
