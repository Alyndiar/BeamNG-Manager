from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow


def main() -> int:
    QCoreApplication.setOrganizationName("BeamNGManager")
    QCoreApplication.setApplicationName("ModPackManager")
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
