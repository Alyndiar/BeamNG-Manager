from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from ui.main_window import MainWindow


def test_runtime_transition_to_running_relinquishes_without_refresh() -> None:
    class DummyWindow:
        def __init__(self) -> None:
            self._beamng_running = False
            self.messages: list[str] = []
            self.refresh_count = 0

        def _set_status_line3(self, message: str) -> None:
            self.messages.append(str(message))

        def _update_beamng_status_indicator(self) -> None:
            return

        def full_refresh(self) -> None:
            self.refresh_count += 1

    window = DummyWindow()
    MainWindow._on_beamng_runtime_state_changed(window, True)  # type: ignore[arg-type]
    assert window._beamng_running is True
    assert window.refresh_count == 0


def test_runtime_transition_to_stopped_reclaims_and_refreshes() -> None:
    class DummyWindow:
        def __init__(self) -> None:
            self._beamng_running = True
            self.messages: list[str] = []
            self.refresh_count = 0

        def _set_status_line3(self, message: str) -> None:
            self.messages.append(str(message))

        def _update_beamng_status_indicator(self) -> None:
            return

        def full_refresh(self) -> None:
            self.refresh_count += 1

    window = DummyWindow()
    MainWindow._on_beamng_runtime_state_changed(window, False)  # type: ignore[arg-type]
    assert window._beamng_running is False
    assert window.refresh_count == 1


def test_shutdown_poller_force_terminates_when_stop_times_out() -> None:
    class FakeSignal:
        def __init__(self) -> None:
            self.disconnected = False

        def disconnect(self, _slot) -> None:
            self.disconnected = True

    class FakePoller:
        def __init__(self) -> None:
            self.stateChanged = FakeSignal()
            self.stop_calls = 0
            self.force_calls = 0

        def stop(self, timeout_ms: int = 0) -> bool:
            self.stop_calls += 1
            return False

        def force_terminate(self, timeout_ms: int = 0) -> bool:
            self.force_calls += 1
            return True

    class DummyWindow:
        def __init__(self) -> None:
            self._beamng_status_poller = FakePoller()

        def _on_beamng_runtime_state_changed(self, _running: bool) -> None:
            return

    window = DummyWindow()
    MainWindow._shutdown_beamng_status_poller(window)  # type: ignore[arg-type]
    assert window._beamng_status_poller is None
