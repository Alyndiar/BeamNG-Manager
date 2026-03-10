from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

FIREFOX_BRIDGE_PORT_DEFAULT = 49441
OPEN_IN_BROWSER_MODE_DEFAULT = "bridge"
OPEN_IN_BROWSER_MODE_OPTIONS = ("default", "bridge")
BRIDGE_DEBUG_DEFAULT = False


class SettingsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = QSettings("BeamNGManager", "ModPackManager")

        self.beam_mods_edit = QLineEdit(self)
        self.library_root_edit = QLineEdit(self)
        self.firefox_bridge_port_spin = QSpinBox(self)
        self.firefox_bridge_port_spin.setRange(1024, 65535)
        self.open_in_browser_mode_combo = QComboBox(self)
        self.open_in_browser_mode_combo.addItem("Default browser", "default")
        self.open_in_browser_mode_combo.addItem("Bridge", "bridge")
        self.bridge_debug_checkbox = QCheckBox("Enable bridge debug logs in console", self)

        form = QFormLayout()
        form.addRow("BeamNG Mod Folder", self._path_row(self.beam_mods_edit, self._browse_beam_mods))
        form.addRow("Library Root Folder", self._path_row(self.library_root_edit, self._browse_library_root))
        form.addRow("Open Repo URL via", self.open_in_browser_mode_combo)
        form.addRow("Browser Bridge Port", self.firefox_bridge_port_spin)
        form.addRow("Bridge Debug", self.bridge_debug_checkbox)

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
        open_mode = load_browser_open_mode()
        index = 0 if open_mode == "default" else 1
        self.open_in_browser_mode_combo.setCurrentIndex(index)
        bridge_port = int(self.settings.value("firefox_bridge_port", FIREFOX_BRIDGE_PORT_DEFAULT, int))
        self.firefox_bridge_port_spin.setValue(max(1024, min(65535, bridge_port)))
        bridge_debug = bool(self.settings.value("bridge_debug_enabled", BRIDGE_DEBUG_DEFAULT, bool))
        self.bridge_debug_checkbox.setChecked(bridge_debug)

    def _show_silent_warning(self, title: str, text: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.NoIcon)
        box.setWindowTitle(str(title))
        box.setText(str(text))
        box.setStandardButtons(QMessageBox.Ok)
        box.setDefaultButton(QMessageBox.Ok)
        box.exec()

    def _save(self) -> None:
        beam_mods = Path(self.beam_mods_edit.text().strip())
        library = Path(self.library_root_edit.text().strip())

        if not beam_mods.exists() or not beam_mods.is_dir():
            self._show_silent_warning("Invalid Folder", "BeamNG Mod Folder must be an existing directory.")
            return
        if not library.exists() or not library.is_dir():
            self._show_silent_warning("Invalid Folder", "Library Root Folder must be an existing directory.")
            return

        self.settings.setValue("beam_mods_root", str(beam_mods))
        self.settings.setValue("library_root", str(library))
        self.settings.setValue("open_in_browser_mode", str(self.open_in_browser_mode_combo.currentData()))
        self.settings.setValue("firefox_bridge_port", int(self.firefox_bridge_port_spin.value()))
        self.settings.setValue("bridge_debug_enabled", bool(self.bridge_debug_checkbox.isChecked()))
        self.accept()


def load_settings() -> tuple[str, str]:
    settings = QSettings("BeamNGManager", "ModPackManager")
    return (
        settings.value("beam_mods_root", "", str),
        settings.value("library_root", "", str),
    )


def load_view_preferences() -> tuple[str, int]:
    settings = QSettings("BeamNGManager", "ModPackManager")
    view_mode = settings.value("mods_view_mode", "text", str)
    raw_cols = settings.value("mods_icon_columns", 4, int)
    cols = max(2, min(8, int(raw_cols)))
    if view_mode not in {"text", "icons"}:
        view_mode = "text"
    return view_mode, cols


def load_firefox_bridge_port() -> int:
    settings = QSettings("BeamNGManager", "ModPackManager")
    value = int(settings.value("firefox_bridge_port", FIREFOX_BRIDGE_PORT_DEFAULT, int))
    return max(1024, min(65535, value))


def load_browser_open_mode() -> str:
    settings = QSettings("BeamNGManager", "ModPackManager")
    value = str(settings.value("open_in_browser_mode", OPEN_IN_BROWSER_MODE_DEFAULT, str) or OPEN_IN_BROWSER_MODE_DEFAULT)
    if value not in OPEN_IN_BROWSER_MODE_OPTIONS:
        return OPEN_IN_BROWSER_MODE_DEFAULT
    return value


def load_bridge_debug_enabled() -> bool:
    settings = QSettings("BeamNGManager", "ModPackManager")
    return bool(settings.value("bridge_debug_enabled", BRIDGE_DEBUG_DEFAULT, bool))


def save_view_preferences(view_mode: str, icon_columns: int) -> None:
    settings = QSettings("BeamNGManager", "ModPackManager")
    mode = "icons" if view_mode == "icons" else "text"
    cols = max(2, min(8, int(icon_columns)))
    settings.setValue("mods_view_mode", mode)
    settings.setValue("mods_icon_columns", cols)
