from __future__ import annotations

import threading

import pytest

pytest.importorskip("PySide6")

from ui.beamng_status_poller import BeamNGStatusPollWorker, BeamNGStatusPoller


def test_worker_uses_poll_interval_chunks() -> None:
    wait_calls: list[float] = []

    def fake_wait(timeout: float) -> bool:
        wait_calls.append(float(timeout))
        # Stop after one polling interval cycle.
        return len(wait_calls) >= 4

    states: list[bool] = []
    worker = BeamNGStatusPollWorker(
        check_fn=lambda: False,
        poll_interval_seconds=15.0,
        wait_step_seconds=5.0,
        initial_state=None,
        wait_fn=fake_wait,
    )
    worker.stateChanged.connect(lambda running: states.append(bool(running)))
    worker.run()

    assert states == [False]
    assert wait_calls[:3] == [5.0, 5.0, 5.0]


def test_worker_emits_only_on_state_change() -> None:
    sequence = iter([False, False, True, True])
    wait_calls: list[float] = []

    def fake_wait(timeout: float) -> bool:
        wait_calls.append(float(timeout))
        # Two waits per loop (1.0 then 0), stop after 4 loops.
        return len(wait_calls) >= 8

    emitted: list[bool] = []
    worker = BeamNGStatusPollWorker(
        check_fn=lambda: next(sequence, True),
        poll_interval_seconds=1.0,
        wait_step_seconds=1.0,
        initial_state=False,
        wait_fn=fake_wait,
    )
    worker.stateChanged.connect(lambda running: emitted.append(bool(running)))
    worker.run()

    assert emitted == [True]


def test_worker_stop_request_exits_promptly() -> None:
    worker = BeamNGStatusPollWorker(
        check_fn=lambda: False,
        poll_interval_seconds=10.0,
        wait_step_seconds=0.1,
        initial_state=False,
    )
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    worker.stop()
    t.join(timeout=1.0)
    assert not t.is_alive()


def test_poller_force_terminate_after_failed_stop() -> None:
    class FakeWorker:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    class FakeThread:
        def __init__(self) -> None:
            self.running = True
            self.quit_called = False
            self.terminate_called = False
            self.wait_calls: list[int] = []

        def isRunning(self) -> bool:
            return self.running

        def quit(self) -> None:
            self.quit_called = True

        def wait(self, timeout_ms: int) -> bool:
            self.wait_calls.append(int(timeout_ms))
            if self.terminate_called:
                self.running = False
                return True
            return False

        def terminate(self) -> None:
            self.terminate_called = True

    poller = BeamNGStatusPoller()
    poller._worker = FakeWorker()  # type: ignore[assignment]
    poller._thread = FakeThread()  # type: ignore[assignment]

    assert poller.stop(timeout_ms=10) is False
    assert poller._worker.stopped is True  # type: ignore[union-attr]
    assert poller.force_terminate(timeout_ms=10) is True
    assert poller._thread is None
