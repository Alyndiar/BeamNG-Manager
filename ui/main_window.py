from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path

from PySide6.QtCore import (
    QByteArray,
    QBuffer,
    QIODevice,
    QMimeData,
    QObject,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QDesktopServices, QDrag, QIcon, QImage, QImageWriter, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QStackedWidget,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.actions import (
    create_pack,
    delete_empty_pack,
    disable_pack,
    enable_pack,
    move_mod_to_mods_root,
    move_mod_to_pack,
    rename_pack,
)
from core.cache import ModEntry, ModInfoCache, ScanIndex
from core.modpreview import read_preview_image
from core.modinfo import get_mod_info_cached, has_info_json
from core import scanner
from core.utils import human_size
from ui.duplicates_dialog import DuplicatesDialog
from ui.settings_dialog import SettingsDialog, load_settings, load_view_preferences, save_view_preferences


LEFT_KIND_ROLE = Qt.UserRole
LEFT_NAME_ROLE = Qt.UserRole + 1
LEFT_PATH_ROLE = Qt.UserRole + 2
LEFT_ACTIVE_ROLE = Qt.UserRole + 3

RIGHT_PATH_ROLE = Qt.UserRole
MOD_PATHS_MIME = "application/x-beamng-mod-paths"
_MISS = object()

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


class ModsTableWidget(QTableWidget):
    def startDrag(self, supported_actions) -> None:
        rows = self.selectionModel().selectedRows()
        if not rows:
            return
        paths: list[str] = []
        seen: set[str] = set()
        for idx in rows:
            cell = self.item(idx.row(), 0)
            if cell is None:
                continue
            value = str(cell.data(RIGHT_PATH_ROLE))
            if value and value not in seen:
                seen.add(value)
                paths.append(value)
        if not paths:
            return

        mime = QMimeData()
        mime.setData(MOD_PATHS_MIME, "\n".join(paths).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.MoveAction)


class PackTreeWidget(QTreeWidget):
    modsDropped = Signal(str, str, object)

    def _drop_target_item(self, pos) -> QTreeWidgetItem | None:
        return self.itemAt(pos)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(MOD_PATHS_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if not event.mimeData().hasFormat(MOD_PATHS_MIME):
            super().dragMoveEvent(event)
            return
        item = self._drop_target_item(event.position().toPoint())
        if item is None:
            event.ignore()
            return
        kind = item.data(0, LEFT_KIND_ROLE)
        if kind not in {"pack", "mods_root"}:
            event.ignore()
            return
        self.setCurrentItem(item)
        event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        if not event.mimeData().hasFormat(MOD_PATHS_MIME):
            super().dropEvent(event)
            return
        item = self._drop_target_item(event.position().toPoint())
        if item is None:
            event.ignore()
            return
        kind = item.data(0, LEFT_KIND_ROLE)
        if kind not in {"pack", "mods_root"}:
            event.ignore()
            return
        raw = bytes(event.mimeData().data(MOD_PATHS_MIME)).decode("utf-8", errors="replace")
        paths = [Path(v.strip()) for v in raw.splitlines() if v.strip()]
        if not paths:
            event.ignore()
            return
        name = str(item.data(0, LEFT_NAME_ROLE) or "")
        self.modsDropped.emit(kind, name, paths)
        event.acceptProposedAction()


class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = text
        self.setWordWrap(False)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        if text:
            self._update_text()

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self._update_text()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_text()

    def _update_text(self) -> None:
        if not self._full_text:
            self.setText("")
            return
        fm = self.fontMetrics()
        self.setText(fm.elidedText(self._full_text, Qt.ElideRight, max(self.width() - 2, 8)))


class ModsIconListWidget(QListWidget):
    resized = Signal()

    def startDrag(self, supported_actions) -> None:
        del supported_actions
        items = self.selectedItems()
        if not items:
            return
        paths: list[str] = []
        seen: set[str] = set()
        for item in items:
            value = str(item.data(RIGHT_PATH_ROLE))
            if value and value not in seen:
                seen.add(value)
                paths.append(value)
        if not paths:
            return

        mime = QMimeData()
        mime.setData(MOD_PATHS_MIME, "\n".join(paths).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.MoveAction)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.resized.emit()


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
        self.current_mod_entries: list[ModEntry] = []
        self.mod_preview_cache: dict[str, bytes | None] = {}
        self.mod_preview_index: dict[str, dict[str, str | int | None]] = {}
        self.known_mod_names: set[str] = set()
        self.known_mod_paths: dict[str, Path] = {}
        self.preview_cache_dir = Path()
        self.preview_cache_index_file = Path()
        self._loading_view_preferences = False
        self._updating_icon_grid_metrics = False
        self.status_line3_message = ""
        self._workers: set[FnWorker] = set()

        self._init_preview_cache_storage()
        self._build_ui()
        self._build_menu()

        self._load_settings_and_maybe_scan()

    def _cache_filename_for_key(self, key: str) -> str:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return f"{digest}.img"

    def _save_preview_cache_index(self) -> None:
        payload = {"entries": self.mod_preview_index}
        try:
            self.preview_cache_dir.mkdir(parents=True, exist_ok=True)
            self.preview_cache_index_file.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _init_preview_cache_storage(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.preview_cache_dir = project_root / ".cache" / "icon_preview_cache"
        self.preview_cache_index_file = self.preview_cache_dir / "index.json"
        self.preview_cache_dir.mkdir(parents=True, exist_ok=True)
        self.mod_preview_cache = {}
        self.mod_preview_index = {}

        if not self.preview_cache_index_file.is_file():
            return
        try:
            parsed = json.loads(self.preview_cache_index_file.read_text(encoding="utf-8"))
            raw_entries = parsed.get("entries", {})
            if isinstance(raw_entries, dict):
                for key, value in raw_entries.items():
                    key_norm = str(key).strip().lower()
                    if not key_norm:
                        continue
                    if isinstance(value, dict):
                        file_name = value.get("file")
                        size = value.get("size")
                        mtime_ns = value.get("mtime_ns")
                        selected_path = value.get("selected_zip_image_path")
                        self.mod_preview_index[key_norm] = {
                            "file": str(file_name) if file_name else None,
                            "size": int(size) if isinstance(size, int) else None,
                            "mtime_ns": int(mtime_ns) if isinstance(mtime_ns, int) else None,
                            "selected_zip_image_path": str(selected_path) if selected_path else None,
                        }
                        continue
                    # Legacy format support: value was file name or None.
                    self.mod_preview_index[key_norm] = {
                        "file": str(value) if value else None,
                        "size": None,
                        "mtime_ns": None,
                        "selected_zip_image_path": None,
                    }
        except (OSError, json.JSONDecodeError):
            self.mod_preview_index = {}

    def _mod_signature(self, mod_path: Path) -> tuple[int | None, int | None]:
        try:
            stat = mod_path.stat()
        except OSError:
            return None, None
        return int(stat.st_size), int(stat.st_mtime_ns)

    def _cache_entry(self, file_name: str | None, mod_path: Path, selected_zip_image_path: str | None) -> dict[str, str | int | None]:
        size, mtime_ns = self._mod_signature(mod_path)
        return {
            "file": file_name,
            "size": size,
            "mtime_ns": mtime_ns,
            "selected_zip_image_path": selected_zip_image_path,
        }

    def _delete_cache_entry_file(self, file_name: str | None) -> None:
        if not file_name:
            return
        path = self.preview_cache_dir / file_name
        if not path.exists():
            return
        try:
            path.unlink()
        except OSError:
            pass

    def _encode_cache_image(self, data: bytes) -> bytes | None:
        image = QImage.fromData(data)
        if image.isNull():
            return None
        if image.width() > 1024 or image.height() > 576:
            image = image.scaled(QSize(1024, 576), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        output = QByteArray()
        buffer = QBuffer(output)
        if not buffer.open(QIODevice.WriteOnly):
            return None
        writer = QImageWriter()
        writer.setDevice(buffer)
        writer.setFormat(bytearray(b"jpeg"))
        writer.setQuality(75)
        writer.setOptimizedWrite(True)
        ok = writer.write(image)
        buffer.close()
        if not ok or output.isEmpty():
            return None
        return bytes(output)

    def _write_preview_cache_entry(
        self, key: str, data: bytes | None, mod_path: Path, selected_zip_image_path: str | None
    ) -> bytes | None:
        key_norm = key.strip().lower()
        if not key_norm:
            return None
        existing_entry = self.mod_preview_index.get(key_norm, {})
        existing_file = str(existing_entry.get("file")) if existing_entry.get("file") else None
        if data is None:
            self._delete_cache_entry_file(existing_file)
            self.mod_preview_index[key_norm] = self._cache_entry(None, mod_path, selected_zip_image_path)
            self.mod_preview_cache[key_norm] = None
            self._save_preview_cache_index()
            return None

        encoded = self._encode_cache_image(data)
        if encoded is None:
            self._delete_cache_entry_file(existing_file)
            self.mod_preview_index[key_norm] = self._cache_entry(None, mod_path, selected_zip_image_path)
            self.mod_preview_cache[key_norm] = None
            self._save_preview_cache_index()
            return None

        file_name = existing_file or self._cache_filename_for_key(key_norm)
        try:
            self.preview_cache_dir.mkdir(parents=True, exist_ok=True)
            (self.preview_cache_dir / file_name).write_bytes(encoded)
            self.mod_preview_index[key_norm] = self._cache_entry(file_name, mod_path, selected_zip_image_path)
            self.mod_preview_cache[key_norm] = encoded
            self._save_preview_cache_index()
        except OSError:
            # Keep runtime cache even if disk persistence fails.
            self.mod_preview_index[key_norm] = self._cache_entry(None, mod_path, selected_zip_image_path)
            self.mod_preview_cache[key_norm] = encoded
        return encoded

    def _load_preview_cache_entry(self, key: str) -> bytes | None | object:
        key_norm = key.strip().lower()
        if not key_norm:
            return _MISS
        if key_norm in self.mod_preview_cache:
            return self.mod_preview_cache[key_norm]
        if key_norm not in self.mod_preview_index:
            return _MISS

        entry = self.mod_preview_index.get(key_norm, {})
        file_name = str(entry.get("file")) if entry.get("file") else None
        if file_name is None:
            self.mod_preview_cache[key_norm] = None
            return None
        path = self.preview_cache_dir / file_name
        try:
            data = path.read_bytes()
        except OSError:
            self.mod_preview_index[key_norm]["file"] = None
            self.mod_preview_cache.pop(key_norm, None)
            self._save_preview_cache_index()
            return _MISS
        self.mod_preview_cache[key_norm] = data
        return data

    def _rebuild_known_mod_names(self) -> None:
        if self.index is None:
            self.known_mod_names = set()
            self.known_mod_paths = {}
            return

        names: set[str] = set()
        paths: dict[str, Path] = {}
        all_mod_lists: list[list[ModEntry]] = []
        all_mod_lists.extend(self.index.pack_mods.values())
        all_mod_lists.append(self.index.loose_mods)
        all_mod_lists.append(self.index.repo_mods)
        all_mod_lists.extend(self.index.orphan_folders.values())
        all_mod_lists.extend(unknown.mods for unknown in self.index.unknown_junctions.values())
        for mod_list in all_mod_lists:
            for entry in mod_list:
                key = entry.path.name.lower()
                names.add(key)
                # Deterministic representative path per mod name.
                current = paths.get(key)
                if current is None or str(entry.path).lower() < str(current).lower():
                    paths[key] = entry.path
        self.known_mod_names = names
        self.known_mod_paths = paths

    def _clear_preview_cache(self) -> None:
        removed_files = 0
        for entry in list(self.mod_preview_index.values()):
            file_name = str(entry.get("file")) if entry.get("file") else None
            if file_name is None:
                continue
            path = self.preview_cache_dir / file_name
            if path.exists():
                try:
                    path.unlink()
                    removed_files += 1
                except OSError:
                    pass
        self.mod_preview_cache.clear()
        self.mod_preview_index.clear()
        self._save_preview_cache_index()
        self._set_status_line3(f"Icon cache cleared. Removed {removed_files} image files.")
        if self._is_icon_view_active():
            self._populate_mods_icons(self.current_mod_entries)
            self._update_icon_grid_metrics()

    def _verify_preview_cache(self) -> None:
        if self.index is None:
            self._set_status_line3("Cache verify skipped: no mod list available yet.")
            return

        removed = 0
        rescanned = 0
        updated = 0
        unchanged = 0
        metadata_refreshed = 0
        for key in list(self.mod_preview_index.keys()):
            if key in self.known_mod_names:
                mod_path = self.known_mod_paths.get(key)
                if mod_path is None:
                    continue
                entry = self.mod_preview_index.get(key, {})
                current_size, current_mtime_ns = self._mod_signature(mod_path)
                cached_size = entry.get("size")
                cached_mtime_ns = entry.get("mtime_ns")

                # Legacy entries: add metadata without rescanning content.
                if cached_size is None and cached_mtime_ns is None:
                    entry["size"] = current_size
                    entry["mtime_ns"] = current_mtime_ns
                    self.mod_preview_index[key] = entry
                    metadata_refreshed += 1
                    continue

                if cached_size != current_size or cached_mtime_ns != current_mtime_ns:
                    selected_path, data = read_preview_image(mod_path)
                    previous_path = str(entry.get("selected_zip_image_path") or "")
                    has_cache_file = bool(entry.get("file"))
                    if selected_path == previous_path and (selected_path is None or has_cache_file):
                        entry["size"] = current_size
                        entry["mtime_ns"] = current_mtime_ns
                        self.mod_preview_index[key] = entry
                        unchanged += 1
                    else:
                        self._write_preview_cache_entry(key, data, mod_path, selected_path)
                        updated += 1
                    rescanned += 1
                continue

            stale_entry = self.mod_preview_index.get(key, {})
            stale_file = str(stale_entry.get("file")) if stale_entry.get("file") else None
            self._delete_cache_entry_file(stale_file)
            self.mod_preview_index.pop(key, None)
            self.mod_preview_cache.pop(key, None)
            removed += 1
        self._save_preview_cache_index()
        self._set_status_line3(
            "Cache verified. "
            f"Removed stale: {removed} | Rescanned changed mods: {rescanned} | "
            f"Updated image selection: {updated} | Unchanged selection: {unchanged} | "
            f"Metadata refreshed: {metadata_refreshed}"
        )

    def _build_ui(self) -> None:
        self.left_tree = PackTreeWidget(self)
        self.left_tree.setHeaderHidden(True)
        self.left_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.left_tree.setAcceptDrops(True)
        self.left_tree.setDropIndicatorShown(True)
        self.left_tree.customContextMenuRequested.connect(self._show_left_context_menu)
        self.left_tree.itemSelectionChanged.connect(self._on_left_selection_changed)
        self.left_tree.itemDoubleClicked.connect(self._on_left_double_clicked)
        self.left_tree.modsDropped.connect(self._handle_mod_drop)

        self.mods_table = ModsTableWidget(self)
        self.mods_table.setColumnCount(3)
        self.mods_table.setHorizontalHeaderLabels(["Filename", "Size", "info.json"])
        self.mods_table.horizontalHeader().setStretchLastSection(True)
        self.mods_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.mods_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.mods_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.mods_table.setDragEnabled(True)
        self.mods_table.setDragDropMode(QAbstractItemView.DragOnly)
        self.mods_table.setDefaultDropAction(Qt.MoveAction)
        self.mods_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.mods_table.customContextMenuRequested.connect(self._show_mod_context_menu)
        self.mods_table.itemSelectionChanged.connect(self._on_mod_selection_changed)
        self.mods_table.itemDoubleClicked.connect(self._on_mod_table_double_clicked)

        self.mods_icons = ModsIconListWidget(self)
        self.mods_icons.setViewMode(QListWidget.IconMode)
        self.mods_icons.setFlow(QListWidget.LeftToRight)
        self.mods_icons.setWrapping(True)
        self.mods_icons.setResizeMode(QListWidget.Adjust)
        self.mods_icons.setMovement(QListWidget.Static)
        self.mods_icons.setSpacing(4)
        self.mods_icons.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.mods_icons.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.mods_icons.setDragEnabled(True)
        self.mods_icons.setDragDropMode(QAbstractItemView.DragOnly)
        self.mods_icons.setDefaultDropAction(Qt.MoveAction)
        self.mods_icons.setContextMenuPolicy(Qt.CustomContextMenu)
        self.mods_icons.customContextMenuRequested.connect(self._show_mod_context_menu)
        self.mods_icons.itemSelectionChanged.connect(self._on_mod_selection_changed)
        self.mods_icons.itemDoubleClicked.connect(self._on_mod_icon_double_clicked)
        self.mods_icons.resized.connect(self._on_icon_geometry_changed)
        self.mods_icons.verticalScrollBar().rangeChanged.connect(self._on_icon_scroll_range_changed)

        self.text_view_btn = QToolButton(self)
        self.text_view_btn.setCheckable(True)
        self.text_view_btn.setToolTip("Affichage texte")
        self.text_view_btn.setIcon(self._build_text_view_icon())
        self.text_view_btn.setIconSize(QSize(20, 20))

        self.icon_view_btn = QToolButton(self)
        self.icon_view_btn.setCheckable(True)
        self.icon_view_btn.setToolTip("Affichage icônes")
        self.icon_view_btn.setIcon(self._build_grid_view_icon())
        self.icon_view_btn.setIconSize(QSize(20, 20))

        self.view_button_group = QButtonGroup(self)
        self.view_button_group.setExclusive(True)
        self.view_button_group.addButton(self.text_view_btn)
        self.view_button_group.addButton(self.icon_view_btn)
        self.text_view_btn.setChecked(True)
        self.text_view_btn.toggled.connect(self._on_view_mode_toggle)

        self.columns_label = QLabel("Colonnes:", self)
        self.columns_slider = QSlider(Qt.Horizontal, self)
        self.columns_slider.setRange(2, 8)
        self.columns_slider.setValue(4)
        self.columns_slider.valueChanged.connect(self._on_icon_columns_changed)
        self.columns_label.setVisible(False)
        self.columns_slider.setVisible(False)

        self.info_caption_checkbox = QCheckBox("Show info label", self)
        self.info_caption_checkbox.setChecked(False)
        self.info_caption_checkbox.toggled.connect(self._on_info_caption_toggle)
        self.recheck_all_cache_btn = QPushButton("Recheck all images", self)
        self.recheck_all_cache_btn.clicked.connect(self._recheck_all_mod_images)
        self.verify_cache_btn = QPushButton("Verify cache", self)
        self.verify_cache_btn.clicked.connect(self._verify_preview_cache)
        self.clear_cache_btn = QPushButton("Clear cache", self)
        self.clear_cache_btn.clicked.connect(self._clear_preview_cache)

        mods_toolbar = QWidget(self)
        mods_toolbar_layout = QHBoxLayout(mods_toolbar)
        mods_toolbar_layout.setContentsMargins(0, 0, 0, 0)
        mods_toolbar_layout.addWidget(self.text_view_btn)
        mods_toolbar_layout.addWidget(self.icon_view_btn)
        mods_toolbar_layout.addSpacing(12)
        mods_toolbar_layout.addWidget(self.info_caption_checkbox)
        mods_toolbar_layout.addSpacing(12)
        mods_toolbar_layout.addWidget(self.columns_label)
        mods_toolbar_layout.addWidget(self.columns_slider, 1)
        mods_toolbar_layout.addWidget(self.recheck_all_cache_btn)
        mods_toolbar_layout.addWidget(self.verify_cache_btn)
        mods_toolbar_layout.addWidget(self.clear_cache_btn)
        mods_toolbar_layout.addStretch(1)

        self.mods_stack = QStackedWidget(self)
        self.mods_stack.addWidget(self.mods_table)
        self.mods_stack.addWidget(self.mods_icons)
        self.missing_preview_pixmap = self._load_missing_preview_pixmap()

        right_panel = QWidget(self)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(mods_toolbar)
        right_layout.addWidget(self.mods_stack, 1)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(self.left_tree)
        splitter.addWidget(right_panel)
        splitter.setSizes([360, 820])
        splitter.splitterMoved.connect(self._on_splitter_moved)
        self.main_splitter = splitter

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
        self._load_view_preferences()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "mods_stack") and self.mods_stack.currentWidget() is self.mods_icons:
            QTimer.singleShot(0, self._update_icon_grid_metrics)

    def _on_splitter_moved(self, _pos: int, _index: int) -> None:
        if self._is_icon_view_active():
            QTimer.singleShot(0, self._update_icon_grid_metrics)

    def _on_icon_geometry_changed(self) -> None:
        if self._is_icon_view_active():
            QTimer.singleShot(0, self._update_icon_grid_metrics)

    def _on_icon_scroll_range_changed(self, _min_value: int, _max_value: int) -> None:
        if self._is_icon_view_active() and not self._updating_icon_grid_metrics:
            QTimer.singleShot(0, self._update_icon_grid_metrics)

    def _build_text_view_icon(self) -> QIcon:
        pix = QPixmap(24, 24)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(self.palette().text().color())
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawLine(5, 7, 19, 7)
        painter.drawLine(5, 12, 19, 12)
        painter.drawLine(5, 17, 19, 17)
        painter.end()
        return QIcon(pix)

    def _build_grid_view_icon(self) -> QIcon:
        pix = QPixmap(24, 24)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        color = self.palette().text().color()
        painter.setBrush(color)
        painter.setPen(Qt.NoPen)
        for row in range(2):
            for col in range(2):
                painter.drawRoundedRect(5 + col * 8, 5 + row * 8, 6, 6, 1.5, 1.5)
        painter.end()
        return QIcon(pix)

    def _load_missing_preview_pixmap(self) -> QPixmap:
        asset = Path(__file__).resolve().parent / "assets" / "no_preview.png"
        pix = QPixmap(str(asset))
        if not pix.isNull():
            return pix
        fallback = QPixmap(320, 180)
        fallback.fill(Qt.transparent)
        painter = QPainter(fallback)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(Qt.red, 12))
        w = fallback.width()
        h = fallback.height()
        diameter = min(w, h) - 36
        left = (w - diameter) // 2
        top = (h - diameter) // 2
        painter.drawEllipse(left, top, diameter, diameter)
        painter.drawLine(left + 18, top + diameter - 18, left + diameter - 18, top + 18)
        painter.end()
        return fallback

    def _on_view_mode_toggle(self, checked: bool) -> None:
        if checked:
            self.mods_stack.setCurrentWidget(self.mods_table)
        else:
            self.mods_stack.setCurrentWidget(self.mods_icons)
            self._populate_mods_icons(self.current_mod_entries)
            self._update_icon_grid_metrics()
        is_icon = not checked
        self.columns_label.setVisible(is_icon)
        self.columns_slider.setVisible(is_icon)
        self._persist_view_preferences()
        self._on_mod_selection_changed()

    def _on_icon_columns_changed(self, _value: int) -> None:
        self._update_icon_grid_metrics()
        self._persist_view_preferences()

    def _on_info_caption_toggle(self, _checked: bool) -> None:
        if self._is_icon_view_active():
            self._populate_mods_icons(self.current_mod_entries)
            self._update_icon_grid_metrics()

    def _persist_view_preferences(self) -> None:
        if self._loading_view_preferences:
            return
        mode = "icons" if self._is_icon_view_active() else "text"
        save_view_preferences(mode, self.columns_slider.value())

    def _load_view_preferences(self) -> None:
        self._loading_view_preferences = True
        try:
            mode, cols = load_view_preferences()
            self.columns_slider.setValue(cols)
            if mode == "icons":
                self.icon_view_btn.setChecked(True)
            else:
                self.text_view_btn.setChecked(True)
        finally:
            self._loading_view_preferences = False

    def _is_icon_view_active(self) -> bool:
        return self.mods_stack.currentWidget() is self.mods_icons

    def _repopulate_current_mod_view(self) -> None:
        self._populate_mods_table(self.current_mod_entries)
        if self._is_icon_view_active():
            self._populate_mods_icons(self.current_mod_entries)
            self._update_icon_grid_metrics()

    def _update_icon_grid_metrics(self) -> None:
        if not self.current_mod_entries or self._updating_icon_grid_metrics:
            return
        self._updating_icon_grid_metrics = True
        try:
            gap = 4
            scrollbar_extent = self.mods_icons.style().pixelMetric(QStyle.PM_ScrollBarExtent, None, self.mods_icons)
            viewport_width = self.mods_icons.viewport().width()
            # Stabilize layout: compute as if one vertical scrollbar column is always reserved.
            full_content_width = viewport_width + (scrollbar_extent if self.mods_icons.verticalScrollBar().isVisible() else 0)
            viewport_width = max(1, full_content_width - scrollbar_extent)
            cols = max(2, min(8, self.columns_slider.value()))
            total_gaps = gap * (cols - 1)
            item_width = max(1, (viewport_width - total_gaps) // cols)
            image_height = int(item_width * 9 / 16)
            row_height = image_height + 36

            # In IconMode, spacing is handled by QListWidget itself.
            self.mods_icons.setGridSize(QSize(item_width, row_height))
            for i in range(self.mods_icons.count()):
                item = self.mods_icons.item(i)
                item.setSizeHint(QSize(item_width, row_height))
                holder = self.mods_icons.itemWidget(item)
                if holder is None:
                    continue
                image_label = holder.findChild(QLabel, "preview_image_label")
                if image_label is not None:
                    image_label.setFixedHeight(image_height)
                    mod_path = Path(str(holder.property("mod_path")))
                    image_label.setPixmap(self._build_preview_pixmap(mod_path, QSize(item_width - 8, image_height)))
        finally:
            self._updating_icon_grid_metrics = False

    def _preview_image_bytes_cached(self, mod_path: Path) -> bytes | None:
        key = mod_path.name.lower()
        cached = self._load_preview_cache_entry(key)
        if cached is not _MISS:
            return cached  # type: ignore[return-value]
        selected_path, data = read_preview_image(mod_path)
        return self._write_preview_cache_entry(key, data, mod_path, selected_path)

    def _recheck_mod_images(self, mod_paths: list[Path]) -> None:
        if not mod_paths:
            return

        selected_by_name: dict[str, Path] = {}
        for mod_path in sorted(mod_paths, key=lambda p: str(p).lower()):
            key = mod_path.name.lower()
            if key not in selected_by_name:
                selected_by_name[key] = mod_path

        rescanned = 0
        found = 0
        updated = 0
        unchanged = 0
        missing_files = 0
        for key, mod_path in selected_by_name.items():
            if not mod_path.exists() or not mod_path.is_file():
                missing_files += 1
                continue
            selected_path, data = read_preview_image(mod_path)
            entry = self.mod_preview_index.get(key, {})
            previous_path = str(entry.get("selected_zip_image_path") or "")
            has_cache_file = bool(entry.get("file"))
            if selected_path == previous_path and (selected_path is None or has_cache_file):
                entry["size"], entry["mtime_ns"] = self._mod_signature(mod_path)
                self.mod_preview_index[key] = entry
                unchanged += 1
            else:
                self._write_preview_cache_entry(key, data, mod_path, selected_path)
                updated += 1
            rescanned += 1
            if data is not None:
                found += 1

        self._save_preview_cache_index()
        if self._is_icon_view_active():
            self._populate_mods_icons(self.current_mod_entries)
            self._update_icon_grid_metrics()
        self._set_status_line3(
            "Image recheck complete. "
            f"Rescanned: {rescanned} | Updated image selection: {updated} | "
            f"Unchanged selection: {unchanged} | Found image: {found} | Missing file: {missing_files}"
        )

    def _recheck_all_mod_images(self) -> None:
        if self.index is None or not self.known_mod_paths:
            self._set_status_line3("Recheck all skipped: no scan data available.")
            return
        self._recheck_mod_images(list(self.known_mod_paths.values()))

    def _first_value(self, info: dict[str, str], keys: list[str]) -> str:
        for key in keys:
            value = str(info.get(key, "")).strip()
            if value:
                return value
        return ""

    def _format_years(self, value: str) -> str:
        text = value.strip()
        if not text:
            return ""
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(text)
            except Exception:
                continue
            if isinstance(parsed, dict):
                min_year = str(parsed.get("min", "")).strip()
                max_year = str(parsed.get("max", "")).strip()
                if min_year and max_year:
                    return min_year if min_year == max_year else f"{min_year}-{max_year}"
                if min_year:
                    return min_year
                if max_year:
                    return max_year
        return text

    def _icon_caption(self, mod: ModEntry) -> str:
        if not self.info_caption_checkbox.isChecked():
            return mod.path.name

        info = get_mod_info_cached(mod.path, self.mod_info_cache)
        if not info:
            return mod.path.name

        title = self._first_value(info, ["title", "Title"])
        version = self._first_value(info, ["version_string"])
        brand = self._first_value(info, ["Brand"])
        name = self._first_value(info, ["Name", "name"])
        years = self._format_years(self._first_value(info, ["Years", "years"]))

        core_parts: list[str] = []
        if title:
            core_parts.append(title)
            if version:
                core_parts.append(version)
        core_parts.extend(part for part in (brand, name, years) if part)
        if not title and version:
            core_parts.append(version)
        if not core_parts:
            return mod.path.name

        primary = " ".join(core_parts)
        author = self._first_value(info, ["Author", "authors", "Authors", "username"])
        if author:
            return f"{primary} - {author}"
        return primary

    def _build_preview_pixmap(self, mod_path: Path, image_size: QSize) -> QPixmap:
        data = self._preview_image_bytes_cached(mod_path)
        source = QPixmap()
        if data:
            source.loadFromData(data)
        if source.isNull():
            source = self.missing_preview_pixmap

        # Keep a fixed 16:9 slot but preserve source proportions (no deformation).
        canvas = QPixmap(image_size)
        canvas.fill(Qt.transparent)
        fitted = source.scaled(image_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        x = (image_size.width() - fitted.width()) // 2
        y = (image_size.height() - fitted.height()) // 2
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(x, y, fitted)
        painter.end()
        return canvas

    def _populate_mods_icons(self, mods: list[ModEntry]) -> None:
        self.mods_icons.clear()
        image_width = 200
        image_height = int(image_width * 9 / 16)
        for mod in mods:
            item = QListWidgetItem()
            item.setData(RIGHT_PATH_ROLE, str(mod.path))
            item.setToolTip(str(mod.path))
            item.setSizeHint(QSize(image_width, image_height + 36))
            self.mods_icons.addItem(item)

            holder = QWidget(self.mods_icons)
            holder.setProperty("mod_path", str(mod.path))
            holder_layout = QVBoxLayout(holder)
            holder_layout.setContentsMargins(4, 4, 4, 4)
            holder_layout.setSpacing(4)

            image_label = QLabel(holder)
            image_label.setObjectName("preview_image_label")
            image_label.setAlignment(Qt.AlignCenter)
            image_label.setFixedHeight(image_height)
            image_label.setPixmap(self._build_preview_pixmap(mod.path, QSize(image_width - 8, image_height)))

            name_label = ElidedLabel(parent=holder)
            name_label.set_full_text(self._icon_caption(mod))

            holder_layout.addWidget(image_label)
            holder_layout.addWidget(name_label)
            self.mods_icons.setItemWidget(item, holder)

    def _icon_item_at_pos(self, pos) -> QListWidgetItem | None:
        return self.mods_icons.itemAt(pos)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        packs_menu = self.menuBar().addMenu("Packs")
        tools_menu = self.menuBar().addMenu("Tools")

        settings_action = QAction("Settings...", self)
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)

        refresh_action = QAction("Refresh", self)
        refresh_action.triggered.connect(self.full_refresh)
        file_menu.addAction(refresh_action)

        create_pack_action = QAction("Create pack...", self)
        create_pack_action.triggered.connect(self._create_pack_dialog)
        packs_menu.addAction(create_pack_action)

        rename_pack_action = QAction("Rename selected pack...", self)
        rename_pack_action.triggered.connect(self._rename_selected_pack_dialog)
        packs_menu.addAction(rename_pack_action)

        delete_pack_action = QAction("Delete selected empty pack...", self)
        delete_pack_action.triggered.connect(self._delete_selected_pack)
        packs_menu.addAction(delete_pack_action)

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
        self._rebuild_known_mod_names()
        self._rebuild_left_tree()
        self.mods_table.setRowCount(0)
        self.mods_icons.clear()
        self.current_mod_entries = []
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

        ordered_packs = sorted(
            self.index.packs,
            key=lambda name: (0 if name in self.index.active_packs else 1, name.lower()),
        )
        for pack_name in ordered_packs:
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
            self.mods_icons.clear()
            self.current_mod_entries = []
            self._update_summary_status()
            return

        left_item = items[0]
        mods = self._mods_for_left_item(left_item)
        self.current_mod_entries = sorted(mods, key=lambda m: m.path.name.lower())
        self._repopulate_current_mod_view()
        self._status_for_folder(left_item, len(mods))

    def _populate_mods_table(self, mods: list[ModEntry]) -> None:
        self.mods_table.setRowCount(len(mods))
        for row, mod in enumerate(mods):
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
        mod_paths = self._selected_mod_paths()
        displayed_count = len(self.current_mod_entries)
        if not mod_paths:
            items = self.left_tree.selectedItems()
            if items:
                self._status_for_folder(items[0], displayed_count)
            return

        if len(mod_paths) > 1:
            self.current_mod_path = None
            self._set_status(
                f"Mods sélectionnés: {len(mod_paths)} / Affichés: {displayed_count}",
                "Sélection multiple active",
                self.status_line3_message,
            )
            return

        path = mod_paths[0]
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
        if self._selected_mod_paths():
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
        menu.addSeparator()
        rename_action = menu.addAction("Rename...")
        delete_action = menu.addAction("Delete Empty Pack")
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
        elif selected == rename_action:
            self._rename_pack_dialog(pack_name)
        elif selected == delete_action:
            self._delete_pack(pack_name)

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

    def _selected_pack_name(self) -> str | None:
        items = self.left_tree.selectedItems()
        if not items:
            return None
        item = items[0]
        if item.data(0, LEFT_KIND_ROLE) != "pack":
            return None
        return item.data(0, LEFT_NAME_ROLE)

    def _create_pack_dialog(self) -> None:
        name, ok = QInputDialog.getText(self, "Create Pack", "Pack name:")
        if not ok:
            return
        pack_name = name.strip()
        if not pack_name:
            self._set_status_line3("Pack name cannot be empty.")
            return
        done, msg = create_pack(pack_name, self.library_root)
        self._set_status_line3(msg)
        if done:
            self.full_refresh()

    def _rename_selected_pack_dialog(self) -> None:
        pack_name = self._selected_pack_name()
        if not pack_name:
            self._set_status_line3("Select a pack first.")
            return
        self._rename_pack_dialog(pack_name)

    def _rename_pack_dialog(self, old_name: str) -> None:
        new_name, ok = QInputDialog.getText(self, "Rename Pack", "New pack name:", text=old_name)
        if not ok:
            return
        done, msg = rename_pack(old_name, new_name.strip(), self.beam_mods_root, self.library_root)
        self._set_status_line3(msg)
        if done:
            self.full_refresh()

    def _delete_selected_pack(self) -> None:
        pack_name = self._selected_pack_name()
        if not pack_name:
            self._set_status_line3("Select a pack first.")
            return
        self._delete_pack(pack_name)

    def _delete_pack(self, pack_name: str) -> None:
        if QMessageBox.question(self, "Delete Empty Pack", f"Delete empty pack '{pack_name}'?") != QMessageBox.Yes:
            return
        done, msg = delete_empty_pack(pack_name, self.beam_mods_root, self.library_root)
        self._set_status_line3(msg)
        if done:
            self.full_refresh()

    def _selected_mod_paths(self) -> list[Path]:
        paths: list[Path] = []
        seen: set[str] = set()
        if self._is_icon_view_active():
            items = self.mods_icons.selectedItems()
            values = [str(item.data(RIGHT_PATH_ROLE)) for item in items]
        else:
            rows = self.mods_table.selectionModel().selectedRows()
            values = []
            for idx in rows:
                cell = self.mods_table.item(idx.row(), 0)
                if cell is None:
                    continue
                values.append(str(cell.data(RIGHT_PATH_ROLE)))
        for value in values:
            if value and value not in seen:
                seen.add(value)
                paths.append(Path(value))
        return paths

    def _open_mod_externally(self, mod_path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(mod_path))
                return
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(mod_path)))
            if not opened:
                self._set_status_line3(f"Unable to open mod: {mod_path.name}")
        except OSError as exc:
            self._set_status_line3(f"Unable to open mod: {mod_path.name} ({exc})")

    def _on_mod_table_double_clicked(self, item: QTableWidgetItem) -> None:
        mod_path = Path(str(item.data(RIGHT_PATH_ROLE)))
        self._open_mod_externally(mod_path)

    def _on_mod_icon_double_clicked(self, item: QListWidgetItem) -> None:
        mod_path = Path(str(item.data(RIGHT_PATH_ROLE)))
        self._open_mod_externally(mod_path)

    def _show_mod_context_menu(self, pos) -> None:
        if self.index is None:
            return

        if self._is_icon_view_active():
            item = self._icon_item_at_pos(pos)
            if item is not None and not item.isSelected():
                self.mods_icons.setCurrentItem(item)
                item.setSelected(True)
            global_pos = self.mods_icons.viewport().mapToGlobal(pos)
        else:
            idx = self.mods_table.indexAt(pos)
            if idx.isValid():
                if not self.mods_table.selectionModel().isRowSelected(idx.row(), idx.parent()):
                    self.mods_table.selectRow(idx.row())
            global_pos = self.mods_table.viewport().mapToGlobal(pos)

        left_items = self.left_tree.selectedItems()
        if not left_items:
            return
        left_item = left_items[0]
        left_kind = left_item.data(0, LEFT_KIND_ROLE)

        mod_paths = self._selected_mod_paths()
        if not mod_paths:
            return

        menu = QMenu(self)
        open_external_action = menu.addAction("Open externally")
        recheck_image_action = menu.addAction("Recheck images in zip")
        menu.addSeparator()
        move_to_pack_action = None
        move_to_root_action = None
        if left_kind in {"mods_root", "pack"}:
            move_to_pack_action = menu.addAction("Move to pack...")
            move_to_root_action = menu.addAction("Move to Mods root")
            move_to_root_action.setEnabled(left_kind == "pack")

        chosen = menu.exec(global_pos)
        if chosen == open_external_action:
            self._open_mod_externally(mod_paths[0])
        elif chosen == recheck_image_action:
            self._recheck_mod_images(mod_paths)
        elif move_to_pack_action is not None and chosen == move_to_pack_action:
            source_pack = left_item.data(0, LEFT_NAME_ROLE) if left_kind == "pack" else None
            self._move_selected_mod_to_pack(mod_paths, source_pack)
        elif move_to_root_action is not None and chosen == move_to_root_action:
            self._move_mods_to_root(mod_paths)

    def _move_selected_mod_to_pack(self, mod_paths: list[Path], source_pack: str | None = None) -> None:
        if self.index is None:
            return
        items = sorted([p for p in self.index.packs if p != source_pack], key=str.lower)
        if not items:
            self._set_status_line3("No pack available. Create one first.")
            return

        selected_pack, ok = QInputDialog.getItem(self, "Move Mod to Pack", "Destination pack:", items, 0, False)
        if not ok or not selected_pack:
            return
        self._move_mods_to_pack(mod_paths, selected_pack)

    def _move_mods_to_pack(self, mod_paths: list[Path], target_pack: str) -> None:
        ok_count = 0
        fail_messages: list[str] = []
        for mod_path in mod_paths:
            done, msg = move_mod_to_pack(mod_path, target_pack, self.library_root)
            if done:
                ok_count += 1
            else:
                fail_messages.append(msg)
        if ok_count > 0:
            self.full_refresh()
        if fail_messages:
            self._set_status_line3(f"Transférés: {ok_count}/{len(mod_paths)} | Erreurs: {len(fail_messages)} | {fail_messages[0]}")
        else:
            self._set_status_line3(f"Transférés: {ok_count}/{len(mod_paths)} vers '{target_pack}'.")

    def _move_mods_to_root(self, mod_paths: list[Path]) -> None:
        ok_count = 0
        fail_messages: list[str] = []
        for mod_path in mod_paths:
            done, msg = move_mod_to_mods_root(mod_path, self.beam_mods_root)
            if done:
                ok_count += 1
            else:
                fail_messages.append(msg)
        if ok_count > 0:
            self.full_refresh()
        if fail_messages:
            self._set_status_line3(f"Transférés: {ok_count}/{len(mod_paths)} | Erreurs: {len(fail_messages)} | {fail_messages[0]}")
        else:
            self._set_status_line3(f"Transférés: {ok_count}/{len(mod_paths)} vers Mods root.")

    def _handle_mod_drop(self, target_kind: str, target_name: str, mod_paths: list[Path]) -> None:
        if target_kind == "mods_root":
            self._move_mods_to_root(mod_paths)
            return

        if target_kind != "pack" or not target_name:
            self._set_status_line3("Drop target must be a pack or Mods root.")
            return
        self._move_mods_to_pack(mod_paths, target_name)
