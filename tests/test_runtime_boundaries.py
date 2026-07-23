"""Regression tests for runtime boundaries not covered by effect tests."""

import asyncio
import math
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import RenderThread, WLEDPowerManager
from status_server import StatusServer, StatusTracker


class _Api:
    def __init__(self, initial=True):
        self.initial = initial
        self.reachable = True
        self.wifi_info = {}
        self.power_calls = []

    def is_on(self):
        return self.initial

    def fetch_wifi_info(self):
        return {}

    def set_power(self, on):
        self.power_calls.append(on)
        return True


def test_initial_wled_state_is_adopted_before_watchdog_sleep():
    api = _Api(initial=True)
    manager = WLEDPowerManager(api, idle_timeout=0)

    async def exercise():
        task = asyncio.create_task(manager._watchdog_run())
        for _ in range(100):
            if manager.is_on:
                break
            await asyncio.sleep(0.001)
        assert manager.is_on
        assert api.power_calls == []
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())


def test_manual_off_cancels_pending_power_on():
    manager = WLEDPowerManager(_Api(initial=False), idle_timeout=30)
    manager.on_activity()
    assert manager._power_on_pending
    manager.manual_power(False)
    assert not manager._power_on_pending


def test_latest_manual_power_command_wins():
    manager = WLEDPowerManager(_Api(initial=False), idle_timeout=30)
    manager._wled_on = True
    manager.manual_power(False)
    manager.manual_power(True)
    assert not manager._power_off_pending


def test_test_api_rejects_bad_and_non_finite_bpm():
    engine = SimpleNamespace()
    server = StatusServer(StatusTracker(), engine=engine)
    for bpm in ("bad", float("nan"), float("inf"), 29, 401):
        status, _ = server._handle_test_action({"action": "bpm", "bpm": bpm})
        assert status == 400
    status, _ = server._handle_test_action({"action": "bpm", "bpm": 120})
    assert status == 200
    assert math.isfinite(engine.bpm)


def test_render_thread_records_uncaught_failure():
    render = RenderThread.__new__(RenderThread)
    threading.Thread.__init__(render)
    render._active = True
    render._fatal_error = None
    render._sender = SimpleNamespace(send_pixels=lambda _pixels: None)
    render._black = b"\x00\x00\x00"

    def fail():
        raise RuntimeError("mapper failed")

    render._run_loop = fail
    render.run()
    assert not render._active
    assert render.failed
    assert render._fatal_error == "RuntimeError: mapper failed"


def test_environment_controls_initial_runtime_defaults():
    env = dict(os.environ)
    env.update(TARGET_FPS="35", GLOBAL_BRIGHTNESS="123")
    code = (
        "from settings import BridgeSettings; "
        "s=BridgeSettings(path='/proc/unwritable/settings.json'); "
        "print(s.fps, s.brightness)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "35 123"
