from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.actions import disable_pack, enable_pack
from core.cache import ModEntry, ModInfoCache, ScanIndex
from core.modinfo import get_mod_info_cached, has_info_json
from core import scanner
from core.utils import human_size
from ui.duplicates_dialog import DuplicatesDialog
from ui.settings_dialog import SettingsDialog, load_settings


LEFT_KIND_ROLE = Qt.UserRole
LEFT_NAME_ROLE = Qt.UserRole + 1
LEFT_PATH_ROLE = Qt.UserRole + 2
LEFT_ACTIVE_ROLE = Qt.UserRole + 3

RIGHT_PATH_ROLE = Qt.UserRole

_VEHICLES_LINE2 = ["Name", "Brand", "Author", "Country", "Body Style", "Type", "Years", "Derby Class"]
_VEHICLES_LINE3 = ["Description", "Slogan"]
_LEVELS_LINE2 = ["title", "authors", "size", "biome", "roads"]
_LEVELS_LINE3 = ["description", "features"]
_MOD_INFO_LINE2 = ["title", "version_string", "prefix_title", "username"]
_MOD_INFO_LINE3 = ["description", "tagline"]
_OTHER_LINE2 = [
    "Name",
    "Brand",
    "Title",
    "Author",
    "Authors",
    "Country",
    "Body Style",
    "Type",
    "Years",
    "Derby Class",
    "size",
    "biome",
    "roads",
    "version_string",
    "prefix_title",
    "username",
]
_OTHER_LINE3 = ["Description", "Slogan", "features", "tagline"]


class WorkerSignals(QObject):
    done = Signal(object)
    error = Signal(str)


class FnWorker(QRunnable):
    def __init__(self, fn) -> None:
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            value = self.fn()
        except Exception as exc:  # pragma: no cover
            self.signals.error.emit(str(exc))
            return
        self.signals.done.emit(value)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("BeamNG Mod Pack Manager")
        self.resize(1200, 760)

        self.thread_pool = QThreadPool.globalInstance()
        self.mod_info_cache = ModInfoCache()

        self.beam_mods_root = ""
        self.library_root = ""
        self.index: ScanIndex | None = None
        self.current_mod_path: Path | None = None
        self.status_line3_message = ""
        self._workers: set[FnWorker] = set()

        self._build_ui()
        self._build_menu()

        self._load_settings_and_maybe_scan()

    def _build_ui(self) -> None:
        self.left_tree = QTreeWidget(self)
        self.left_tree.setHeaderHidden(True)
        self.left_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.left_tree.customContextMenuRequested.connect(self._show_left_context_menu)
        self.left_tree.itemSelectionChanged.connect(self._on_left_selection_changed)
        self.left_tree.itemDoubleClicked.connect(self._on_left_double_clicked)

        self.mods_table = QTableWidget(self)
        self.mods_table.setColumnCount(3)
        self.mods_table.setHorizontalHeaderLabels(["Filename", "Size", "info.json"])
        self.mods_table.horizontalHeader().setStretchLastSection(True)
        self.mods_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.mods_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.mods_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.mods_table.itemSelectionChanged.connect(self._on_mod_selection_changed)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self.left_tree)
        splitter.addWidget(self.mods_table)
        splitter.setSizes([360, 820])

        self.status_box = QPlainTextEdit(self)
        self.status_box.setReadOnly(True)
        self.status_box.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.status_box.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.status_box.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.status_box.horizontalScrollBar().rangeChanged.connect(self._update_status_box_height)
        self._update_status_box_height()

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.addWidget(splitter)
        layout.addWidget(self.status_box)
        self.setCentralWidget(central)

        self._set_status("Active mods: 0 / Total mods: 0", "Packs active: 0/0 | Loose: 0 | Repo: 0", "")

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        tools_menu = self.menuBar().addMenu("Tools")

        settings_action = QAction("Settings...", self)
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)

        refresh_action = QAction("Refresh", self)
        refresh_action.triggered.connect(self.full_refresh)
        file_menu.addAction(refresh_action)

        find_dupes_action = QAction("Find duplicates...", self)
        find_dupes_action.triggered.connect(self._open_duplicates)
        tools_menu.addAction(find_dupes_action)

    def _load_settings_and_maybe_scan(self) -> None:
        beam_mods, library = load_settings()
        self.beam_mods_root = beam_mods
        self.library_root = library
        if not self._settings_valid():
            self._open_settings(force=True)
            if not self._settings_valid():
                self._set_status(
                    "Active mods: 0 / Total mods: 0",
                    "Packs active: 0/0 | Loose: 0 | Repo: 0",
                    "Configure BeamNG Mod Folder and Library Root in Settings.",
                )
                return
        self.full_refresh()

    def _settings_valid(self) -> bool:
        return bool(self.beam_mods_root and self.library_root and Path(self.beam_mods_root).is_dir() and Path(self.library_root).is_dir())

    def _open_settings(self, force: bool = False) -> None:
        while True:
            dlg = SettingsDialog(self)
            accepted = dlg.exec()
            self.beam_mods_root, self.library_root = load_settings()
            if self._settings_valid():
                self.full_refresh()
                return
            if not force or accepted == 0:
                break
            QMessageBox.warning(self, "Settings Required", "Both folders must be configured before scanning.")

    def full_refresh(self) -> None:
        if not self._settings_valid():
            self._set_status(
                "Active mods: 0 / Total mods: 0",
                "Packs active: 0/0 | Loose: 0 | Repo: 0",
                "Invalid settings. Open File -> Settings...",
            )
            return

        self._set_status_line3("Scanning...")

        worker = FnWorker(lambda: scanner.build_full_index(self.beam_mods_root, self.library_root))
        worker.signals.done.connect(self._apply_index)
        worker.signals.error.connect(lambda e: self._set_status_line3(f"Scan error: {e}"))
        self._start_worker(worker)

    def _quick_refresh(self) -> None:
        if self.index is None:
            self.full_refresh()
            return
        worker = FnWorker(lambda: scanner.refresh_after_toggle(self.index))
        worker.signals.done.connect(self._apply_index)
        worker.signals.error.connect(lambda e: self._set_status_line3(f"Refresh error: {e}"))
        self._start_worker(worker)

    def _start_worker(self, worker: FnWorker) -> None:
        self._workers.add(worker)

        def _cleanup(*_args) -> None:
            self._workers.discard(worker)

        worker.signals.done.connect(_cleanup)
        worker.signals.error.connect(_cleanup)
        self.thread_pool.start(worker)

    def _apply_index(self, index: ScanIndex) -> None:
        self.index = index
        self._rebuild_left_tree()
        self.mods_table.setRowCount(0)
        self.current_mod_path = None
        self._update_summary_status()

    def _rebuild_left_tree(self) -> None:
        if self.index is None:
            return

        self.left_tree.clear()
        style = QApplication.style()
        folder_icon = style.standardIcon(QStyle.SP_DirIcon)
        warn_icon = style.standardIcon(QStyle.SP_MessageBoxWarning)
        on_icon = style.standardIcon(QStyle.SP_DialogApplyButton)
        off_icon = style.standardIcon(QStyle.SP_DialogCancelButton)

        mods_item = QTreeWidgetItem(["Mods folder"])
        mods_item.setIcon(0, folder_icon)
        mods_item.setData(0, LEFT_KIND_ROLE, "mods_root")
        mods_item.setData(0, LEFT_PATH_ROLE, str(self.index.beam_mods_root))
        self.left_tree.addTopLevelItem(mods_item)

        repo_item = QTreeWidgetItem(["Mods/Repo folder"])
        repo_item.setIcon(0, folder_icon)
        repo_item.setData(0, LEFT_KIND_ROLE, "repo")
        repo_item.setData(0, LEFT_PATH_ROLE, str(self.index.beam_repo_root))
        self.left_tree.addTopLevelItem(repo_item)

        for name in sorted(self.index.unknown_junctions.keys(), key=str.lower):
            item = QTreeWidgetItem([f"Unknown junction: {name}"])
            item.setIcon(0, warn_icon)
            item.setData(0, LEFT_KIND_ROLE, "unknown")
            item.setData(0, LEFT_NAME_ROLE, name)
            unk = self.index.unknown_junctions[name]
            item.setData(0, LEFT_PATH_ROLE, str(unk.path))
            self.left_tree.addTopLevelItem(item)

        for name in sorted(self.index.orphan_folders.keys(), key=str.lower):
            item = QTreeWidgetItem([f"Orphan folder: {name}"])
            item.setIcon(0, folder_icon)
            item.setData(0, LEFT_KIND_ROLE, "orphan")
            item.setData(0, LEFT_NAME_ROLE, name)
            item.setData(0, LEFT_PATH_ROLE, str(self.index.beam_mods_root / name))
            self.left_tree.addTopLevelItem(item)

        for pack_name in sorted(self.index.packs, key=str.lower):
            active = pack_name in self.index.active_packs
            item = QTreeWidgetItem([pack_name])
            item.setIcon(0, on_icon if active else off_icon)
            item.setData(0, LEFT_KIND_ROLE, "pack")
            item.setData(0, LEFT_NAME_ROLE, pack_name)
            item.setData(0, LEFT_ACTIVE_ROLE, active)
            item.setData(0, LEFT_PATH_ROLE, str(self.index.library_root / pack_name))
            self.left_tree.addTopLevelItem(item)

        self.left_tree.expandAll()

    def _mods_for_left_item(self, item: QTreeWidgetItem | None) -> list[ModEntry]:
        if self.index is None or item is None:
            return []

        kind = item.data(0, LEFT_KIND_ROLE)
        if kind == "mods_root":
            return list(self.index.loose_mods)
        if kind == "repo":
            return list(self.index.repo_mods)
        if kind == "unknown":
            name = item.data(0, LEFT_NAME_ROLE)
            unknown = self.index.unknown_junctions.get(name)
            return list(unknown.mods if unknown else [])
        if kind == "orphan":
            name = item.data(0, LEFT_NAME_ROLE)
            return list(self.index.orphan_folders.get(name, []))
        if kind == "pack":
            name = item.data(0, LEFT_NAME_ROLE)
            return list(self.index.pack_mods.get(name, []))
        return []

    def _on_left_selection_changed(self) -> None:
        items = self.left_tree.selectedItems()
        if not items:
            self.mods_table.setRowCount(0)
            self._update_summary_status()
            return

        left_item = items[0]
        mods = self._mods_for_left_item(left_item)
        self._populate_mods_table(mods)
        self._status_for_folder(left_item, len(mods))

    def _populate_mods_table(self, mods: list[ModEntry]) -> None:
        self.mods_table.setRowCount(len(mods))
        for row, mod in enumerate(sorted(mods, key=lambda m: m.path.name.lower())):
            info_state = "Yes" if has_info_json(mod.path) else "No"
            row_items = [
                QTableWidgetItem(mod.path.name),
                QTableWidgetItem(human_size(mod.size)),
                QTableWidgetItem(info_state),
            ]
            for col, cell in enumerate(row_items):
                cell.setData(RIGHT_PATH_ROLE, str(mod.path))
                self.mods_table.setItem(row, col, cell)
        self.mods_table.resizeColumnsToContents()

    def _on_mod_selection_changed(self) -> None:
        rows = self.mods_table.selectionModel().selectedRows()
        if not rows:
            return

        row = rows[0].row()
        cell = self.mods_table.item(row, 0)
        if cell is None:
            return

        path = Path(cell.data(RIGHT_PATH_ROLE))
        self.current_mod_path = path
        try:
            size = path.stat().st_size
        except OSError:
            size = 0

        line1 = f"Mod: {path.name}  Size: {human_size(size)}  |  Path: {path}"
        self._set_status(line1, "Loading info.json...", "")

        worker = FnWorker(lambda: get_mod_info_cached(path, self.mod_info_cache))
        worker.signals.done.connect(lambda data, p=path: self._on_mod_info_ready(p, data))
        worker.signals.error.connect(lambda e: self._set_status(line1, f"info.json parse error: {e}", ""))
        self._start_worker(worker)

    def _on_mod_info_ready(self, mod_path: Path, data: dict[str, str] | None) -> None:
        if self.current_mod_path != mod_path:
            return

        try:
            size = mod_path.stat().st_size
        except OSError:
            size = 0

        line1 = f"Mod: {mod_path.name}  Size: {human_size(size)}  |  Path: {mod_path}"
        if data is None:
            self._set_status(line1, "info.json not found", "")
            return

        category = data.get("__category", "other")
        if category == "vehicles":
            line2_keys, line3_keys = _VEHICLES_LINE2, _VEHICLES_LINE3
        elif category == "levels":
            line2_keys, line3_keys = _LEVELS_LINE2, _LEVELS_LINE3
        elif category == "mod_info":
            line2_keys, line3_keys = _MOD_INFO_LINE2, _MOD_INFO_LINE3
        else:
            line2_keys, line3_keys = _OTHER_LINE2, _OTHER_LINE3

        def present_field(label: str) -> str | None:
            value = (data.get(label) or "").strip()
            if not value:
                return None
            return f"{label}={value}"

        line2_fields = [present_field(label) for label in line2_keys]
        line3_fields = [present_field(label) for label in line3_keys]

        line2 = " | ".join(field for field in line2_fields if field)
        line3 = " | ".join(field for field in line3_fields if field)
        self._set_status(line1, line2, line3)

    def _status_for_folder(self, item: QTreeWidgetItem, count: int) -> None:
        kind = item.data(0, LEFT_KIND_ROLE)
        path = item.data(0, LEFT_PATH_ROLE)
        label = item.text(0)
        state = ""

        if kind == "pack":
            state = "ACTIVE" if item.data(0, LEFT_ACTIVE_ROLE) else "INACTIVE"
        elif kind == "unknown":
            state = "CAUTION"
        elif kind == "orphan":
            state = "UNASSOCIATED"

        suffix = f"  ({state})" if state else ""
        line1 = f"{label}{suffix}  |  Path: {path}"
        self._set_status(line1, f"Mods: {count}", self.status_line3_message)

    def _update_summary_status(self) -> None:
        if self.index is None:
            self._set_status("Active mods: 0 / Total mods: 0", "Packs active: 0/0 | Loose: 0 | Repo: 0", self.status_line3_message)
            return

        t = self.index.totals
        line1 = f"Active mods: {t.active_mods} / Total mods: {t.total_mods}"
        line2 = f"Packs active: {t.packs_active}/{t.packs_total} | Loose: {t.loose_mods} | Repo: {t.repo_mods}"
        self._set_status(line1, line2, self.status_line3_message)

    def _set_status(self, line1: str, line2: str, line3: str) -> None:
        self.status_box.setPlainText(f"{line1}\n{line2}\n{line3}")
        self._update_status_box_height()

    def _update_status_box_height(self, *_args) -> None:
        fm = self.status_box.fontMetrics()
        base_height = int(fm.lineSpacing() * 3 + 12)
        extra = self.status_box.horizontalScrollBar().sizeHint().height() if self.status_box.horizontalScrollBar().isVisible() else 0
        self.status_box.setFixedHeight(base_height + extra)

    def _set_status_line3(self, message: str) -> None:
        self.status_line3_message = message
        items = self.left_tree.selectedItems()
        if self.mods_table.selectionModel().selectedRows():
            return
        if items:
            self._status_for_folder(items[0], len(self._mods_for_left_item(items[0])))
        else:
            self._update_summary_status()

    def _show_left_context_menu(self, pos) -> None:
        item = self.left_tree.itemAt(pos)
        if item is None or item.data(0, LEFT_KIND_ROLE) != "pack":
            return

        menu = QMenu(self)
        pack_name = item.data(0, LEFT_NAME_ROLE)
        active = bool(item.data(0, LEFT_ACTIVE_ROLE))

        enable_action = menu.addAction("Enable")
        disable_action = menu.addAction("Disable")
        enable_action.setEnabled(not active)
        disable_action.setEnabled(active)

        selected = menu.exec(self.left_tree.viewport().mapToGlobal(pos))
        if selected == enable_action:
            ok, msg = enable_pack(pack_name, self.beam_mods_root, self.library_root)
            self._set_status_line3(msg)
            if ok:
                self._quick_refresh()
        elif selected == disable_action:
            if QMessageBox.question(self, "Disable Pack", f"Disable pack '{pack_name}'?") != QMessageBox.Yes:
                return
            ok, msg = disable_pack(pack_name, self.beam_mods_root, self.library_root)
            self._set_status_line3(msg)
            if ok:
                self._quick_refresh()

    def _on_left_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        del column
        if item.data(0, LEFT_KIND_ROLE) != "pack":
            return
        pack_name = item.data(0, LEFT_NAME_ROLE)
        active = bool(item.data(0, LEFT_ACTIVE_ROLE))
        if active:
            ok, msg = disable_pack(pack_name, self.beam_mods_root, self.library_root)
        else:
            ok, msg = enable_pack(pack_name, self.beam_mods_root, self.library_root)
        self._set_status_line3(msg)
        if ok:
            self._quick_refresh()

    def _open_duplicates(self) -> None:
        if self.index is None:
            QMessageBox.information(self, "Duplicates", "No scan data available yet.")
            return
        dlg = DuplicatesDialog(self.index, self)
        dlg.exec()
