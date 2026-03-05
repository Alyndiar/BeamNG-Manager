from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)


class SettingsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = QSettings("BeamNGManager", "ModPackManager")

        self.beam_mods_edit = QLineEdit(self)
        self.library_root_edit = QLineEdit(self)

        form = QFormLayout()
        form.addRow("BeamNG Mod Folder", self._path_row(self.beam_mods_edit, self._browse_beam_mods))
        form.addRow("Library Root Folder", self._path_row(self.library_root_edit, self._browse_library_root))

        save_btn = QPushButton("Save", self)
        cancel_btn = QPushButton("Cancel", self)
        save_btn.clicked.connect(self._save)
        cancel_btn.clicked.connect(self.reject)

        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(save_btn)
        actions.addWidget(cancel_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(actions)

        self._load()

    def _path_row(self, edit: QLineEdit, callback) -> QHBoxLayout:
        browse = QPushButton("Browse...", self)
        browse.clicked.connect(callback)
        row = QHBoxLayout()
        row.addWidget(edit)
        row.addWidget(browse)
        return row

    def _browse_beam_mods(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select BeamNG Mod Folder", self.beam_mods_edit.text())
        if path:
            self.beam_mods_edit.setText(path)

    def _browse_library_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Library Root Folder", self.library_root_edit.text())
        if path:
            self.library_root_edit.setText(path)

    def _load(self) -> None:
        self.beam_mods_edit.setText(self.settings.value("beam_mods_root", "", str))
        self.library_root_edit.setText(self.settings.value("library_root", "", str))

    def _save(self) -> None:
        beam_mods = Path(self.beam_mods_edit.text().strip())
        library = Path(self.library_root_edit.text().strip())

        if not beam_mods.exists() or not beam_mods.is_dir():
            QMessageBox.warning(self, "Invalid Folder", "BeamNG Mod Folder must be an existing directory.")
            return
        if not library.exists() or not library.is_dir():
            QMessageBox.warning(self, "Invalid Folder", "Library Root Folder must be an existing directory.")
            return

        self.settings.setValue("beam_mods_root", str(beam_mods))
        self.settings.setValue("library_root", str(library))
        self.accept()


def load_settings() -> tuple[str, str]:
    settings = QSettings("BeamNGManager", "ModPackManager")
    return (
        settings.value("beam_mods_root", "", str),
        settings.value("library_root", "", str),
    )
