"""Regression tests for runtime boundaries not covered by effect tests."""

import asyncio
import math
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as bridge_main
from main import (RENDER_MAX_FAILED_RESTARTS, RENDER_RESTART_BACKOFF_S,
                  RENDER_STABLE_S, RenderSupervisor, RenderThread, WLEDPowerManager)
from settings import BridgeSettings
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

    def fetch_capabilities(self):
        return True

    def set_power(self, on, transition_ms=None):
        self.power_calls.append(on)
        return True

    def assert_live_baseline(self, led_count):
        return True

    def apply_idle_look(self, spec, led_count, transition_ms=700):
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


# ── render-thread supervisor ────────────────────────────────────────────


class _FakeThread:
    """A render thread that is alive until told to die."""

    def __init__(self):
        self.restarts = 0
        self.started = False
        self.stopped = False
        self._fatal_error = None
        self._alive = False

    def start(self):
        self.started = True
        self._alive = True

    def is_alive(self):
        return self._alive

    @property
    def failed(self):
        return self._fatal_error is not None

    def crash(self, why="RuntimeError: boom"):
        self._fatal_error = why
        self._alive = False

    def stop(self):
        self.stopped = True
        self._alive = False

    def join(self, timeout=None):
        pass


class _Supervised:
    """A supervisor on a fake clock. `script(n, thread)` runs on every poll."""

    def __init__(self, script):
        self.now = 1000.0
        self.sleeps = []
        self.made = []
        self.tracker = SimpleNamespace(render_thread=None)
        self.script = script
        self.sup = RenderSupervisor(self.tracker, self._make,
                                    sleep=self._sleep, clock=lambda: self.now)

    def _make(self):
        thread = _FakeThread()
        self.made.append(thread)
        return thread

    async def _sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if seconds == bridge_main.RENDER_POLL_S:
            self.script(self)

    def run(self):
        self.sup.start()
        asyncio.run(self.sup.watch())
        return self.sup

    @property
    def backoffs(self):
        return [s for s in self.sleeps if s != bridge_main.RENDER_POLL_S]


def test_supervisor_replaces_a_crashed_render_thread():
    def script(h):
        if len(h.made) == 1:
            h.made[0].crash()
        elif h.now > 1100:
            h.sup._stopping = True

    h = _Supervised(script)
    sup = h.run()
    assert len(h.made) == 2
    assert h.made[1].started and h.made[1].is_alive()
    assert h.tracker.render_thread is h.made[1]
    assert sup.thread is h.made[1]
    assert sup.restarts == 1 and h.made[1].restarts == 1
    assert not sup.gave_up
    assert h.backoffs == [RENDER_RESTART_BACKOFF_S[0]]


def test_supervisor_notices_a_thread_that_exited_without_an_error():
    def script(h):
        if len(h.made) == 1:
            h.made[0]._alive = False
        else:
            h.sup._stopping = True

    h = _Supervised(script)
    h.run()
    assert len(h.made) == 2


def test_supervisor_backs_off_then_gives_up():
    def script(h):
        h.made[-1].crash()

    h = _Supervised(script)
    sup = h.run()
    assert sup.gave_up
    assert sup.restarts == RENDER_MAX_FAILED_RESTARTS
    assert len(h.made) == RENDER_MAX_FAILED_RESTARTS + 1
    assert h.backoffs == list(RENDER_RESTART_BACKOFF_S[:RENDER_MAX_FAILED_RESTARTS])
    assert h.backoffs == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_a_stable_run_clears_the_failure_streak():
    crashes = {"left": 3}

    def script(h):
        thread = h.made[-1]
        if len(h.made) <= 3:                       # first three die at once
            thread.crash()
        elif len(h.made) == 4:                     # the fourth survives a while…
            if h.now - h.sup._started_at >= RENDER_STABLE_S + 1:
                thread.crash()                     # …then dies
        else:
            h.sup._stopping = True

    h = _Supervised(script)
    sup = h.run()
    assert not sup.gave_up
    assert sup.restarts == 4                       # the lifetime count keeps climbing
    assert sup.failed_restarts == 1                # the streak started over
    assert h.backoffs == [1.0, 2.0, 4.0, 1.0]      # and so did the backoff


def test_a_replacement_that_cannot_start_counts_as_a_failed_restart():
    def script(h):
        if h.made:
            h.made[-1].crash()

    h = _Supervised(script)
    real_make = h._make

    def make():
        if h.made:
            raise RuntimeError("can't start new thread")
        return real_make()

    h.sup._make_thread = make
    sup = h.run()
    assert sup.gave_up
    assert len(h.made) == 1
    assert sup.restarts == RENDER_MAX_FAILED_RESTARTS
    assert h.made[0].restarts == RENDER_MAX_FAILED_RESTARTS


def test_supervisor_stop_stops_the_current_thread_and_ends_the_watch():
    def script(h):
        h.sup.stop()

    h = _Supervised(script)
    sup = h.run()
    assert h.made[0].stopped
    assert len(h.made) == 1 and not sup.gave_up


def test_real_render_thread_is_replaced_with_a_fresh_mapper():
    """End to end with real RenderThreads whose loop raises once."""
    mappers = []
    tracker = StatusTracker()
    sender = SimpleNamespace(send_pixels=lambda _p: None, stats=lambda: {})
    settings = SimpleNamespace(fps=60)
    release = threading.Event()

    def make():
        mapper = object()
        mappers.append(mapper)
        thread = RenderThread(None, mapper, sender, tracker, settings, None)
        first = len(mappers) == 1

        def loop():
            if first:
                raise RuntimeError("mapper failed")
            thread._active = True
            release.wait(5)

        thread._run_loop = loop
        return thread

    async def no_wait(_seconds):
        await asyncio.sleep(0.005)

    sup = RenderSupervisor(tracker, make, sleep=no_wait)
    sup.start()
    first = sup.thread

    async def exercise():
        task = asyncio.create_task(sup.watch())
        for _ in range(400):
            if sup.restarts:
                break
            await asyncio.sleep(0.005)
        task.cancel()

    try:
        asyncio.run(exercise())
        assert first.failed
        assert sup.restarts == 1
        assert sup.thread is not first and sup.thread.is_alive()
        assert tracker.render_thread is sup.thread
        assert sup.thread._mapper is mappers[1] and mappers[1] is not mappers[0]
        assert not tracker.render_thread.failed          # /api/status is 200 again
        assert tracker.render_thread.render_stats()["restarts"] == 1
    finally:
        release.set()
        sup.stop()


def test_main_exits_nonzero_when_the_supervisor_gives_up(monkeypatch, tmp_path):
    """main() must return 1, having still blacked out and powered the strip off."""
    calls = []

    class Sender:
        def __init__(self, *_a):
            pass

        def send_pixels(self, pixels):
            calls.append(("black", any(pixels)))

        def stats(self):
            return {}

        def close(self):
            calls.append(("close",))

    class Api(_Api):
        def __init__(self, *_a):
            super().__init__(initial=False)
            self.device_info = None

        def set_power(self, on, transition_ms=None):
            calls.append(("power", on))
            return True

    class Server:
        def __init__(self, *_a, **_k):
            pass

        async def start(self):
            pass

    class Doomed(_FakeThread):
        def __init__(self, *_a, **_k):
            super().__init__()

        def start(self):
            super().start()
            self.crash()

    async def fast(self):
        self._sleep = lambda _s: asyncio.sleep(0)
        return await real_watch(self)

    real_watch = RenderSupervisor.watch
    monkeypatch.setattr(bridge_main, "DDPSender", Sender)
    monkeypatch.setattr(bridge_main, "WLEDApi", Api)
    monkeypatch.setattr(bridge_main, "StatusServer", Server)
    monkeypatch.setattr(bridge_main, "RenderThread", Doomed)
    monkeypatch.setattr(RenderSupervisor, "watch", fast)
    monkeypatch.setattr(bridge_main, "ANOMALY_DETECTION", False)
    monkeypatch.setattr(bridge_main, "YARG_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setattr(bridge_main, "YARG_LISTEN_PORT", 0)
    monkeypatch.setattr(bridge_main, "BridgeSettings",
                        lambda: BridgeSettings(path=str(tmp_path / "settings.json")))
    monkeypatch.setattr(bridge_main, "CAPTURE_DIR", str(tmp_path / "captures"))

    assert asyncio.run(bridge_main.main()) == 1
    assert ("black", False) in calls
    assert ("power", False) in calls

    # The ordinary way out is unchanged: a signal, a healthy thread, status 0.
    class Healthy(_FakeThread):
        def __init__(self, *_a, **_k):
            super().__init__()

    async def signalled():
        asyncio.get_running_loop().call_later(
            0.05, os.kill, os.getpid(), signal.SIGINT)
        return await bridge_main.main()

    calls.clear()
    monkeypatch.setattr(bridge_main, "RenderThread", Healthy)
    assert asyncio.run(signalled()) == 0
    assert ("power", False) in calls


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
