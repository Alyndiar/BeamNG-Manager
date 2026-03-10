from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow


def main() -> int:
    QCoreApplication.setOrganizationName("BeamNGManager")
    QCoreApplication.setApplicationName("ModPackManager")
    app = QApplication(sys.argv)
    window = MainWindow()
    # Delay maximize until the first event loop tick to avoid inconsistent
    # pseudo-maximized state on some Windows setups.
    window.show()
    QTimer.singleShot(0, window.showMaximized)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
