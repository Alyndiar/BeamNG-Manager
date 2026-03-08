from __future__ import annotations

import threading
from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from core.actions import beamng_is_running


class BeamNGStatusPollWorker(QObject):
    stateChanged = Signal(bool)
    wakeup = Signal()
    finished = Signal()

    def __init__(
        self,
        check_fn: Callable[[], bool] | None = None,
        poll_interval_seconds: float = 15.0,
        wait_step_seconds: float = 0.5,
        initial_state: bool | None = None,
        wait_fn: Callable[[float], bool] | None = None,
    ) -> None:
        super().__init__()
        self._check_fn = check_fn or beamng_is_running
        self._poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self._wait_step_seconds = max(0.01, float(wait_step_seconds))
        self._last_state = initial_state if initial_state is None else bool(initial_state)
        self._stop_event = threading.Event()
        self._wait_fn = wait_fn or self._stop_event.wait

    def stop(self) -> None:
        self._stop_event.set()

    def _wait_until_next_poll(self) -> bool:
        remaining = self._poll_interval_seconds
        while remaining > 0:
            timeout = min(self._wait_step_seconds, remaining)
            if self._wait_fn(timeout):
                return True
            remaining -= timeout
        return bool(self._wait_fn(0))

    @Slot()
    def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    is_running = bool(self._check_fn())
                except Exception:
                    is_running = bool(self._last_state) if self._last_state is not None else False
                if self._last_state is None or is_running != self._last_state:
                    self._last_state = is_running
                    self.stateChanged.emit(is_running)
                self.wakeup.emit()
                if self._wait_until_next_poll():
                    break
        finally:
            self.finished.emit()


class BeamNGStatusPoller(QObject):
    stateChanged = Signal(bool)
    wakeup = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        check_fn: Callable[[], bool] | None = None,
        poll_interval_seconds: float = 15.0,
        wait_step_seconds: float = 0.5,
    ) -> None:
        super().__init__(parent)
        self._check_fn = check_fn
        self._poll_interval_seconds = poll_interval_seconds
        self._wait_step_seconds = wait_step_seconds
        self._thread: QThread | None = None
        self._worker: BeamNGStatusPollWorker | None = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, initial_state: bool | None = None) -> None:
        if self.is_running():
            return
        thread = QThread(self)
        worker = BeamNGStatusPollWorker(
            check_fn=self._check_fn,
            poll_interval_seconds=self._poll_interval_seconds,
            wait_step_seconds=self._wait_step_seconds,
            initial_state=initial_state,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.stateChanged.connect(self.stateChanged)
        worker.wakeup.connect(self.wakeup)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    def stop(self, timeout_ms: int = 1200) -> bool:
        worker = self._worker
        thread = self._thread
        if worker is not None:
            worker.stop()
        if thread is None:
            self._worker = None
            return True
        if not thread.isRunning():
            self._worker = None
            self._thread = None
            return True
        thread.quit()
        stopped = bool(thread.wait(max(1, int(timeout_ms))))
        if stopped:
            self._worker = None
            self._thread = None
        return stopped

    def force_terminate(self, timeout_ms: int = 1200) -> bool:
        thread = self._thread
        if thread is None:
            self._worker = None
            return True
        if not thread.isRunning():
            self._worker = None
            self._thread = None
            return True
        thread.terminate()
        stopped = bool(thread.wait(max(1, int(timeout_ms))))
        self._worker = None
        self._thread = None
        return stopped
