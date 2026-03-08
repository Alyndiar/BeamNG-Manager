from __future__ import annotations

import ast
import hashlib
import itertools
import json
import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import (
    QByteArray,
    QBuffer,
    QDateTime,
    QEvent,
    QEventLoop,
    QIODevice,
    QMimeData,
    QObject,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    QSettings,
    Signal,
)
from PySide6.QtGui import QAction, QDesktopServices, QDrag, QIcon, QImage, QImageWriter, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QSplitter,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.actions import (
    beamng_is_running,
    create_pack,
    delete_empty_pack,
    disable_pack,
    enable_pack,
    move_mod_to_mods_root,
    move_mod_to_pack,
    rename_pack,
)
from core.cache import ModEntry, ModInfoCache, ScanIndex
from core.online_repo import OnlineRepoClient, is_beamng_resource_download_url, parse_beamng_protocol_uri
from core import profiles as profile_store
from core.state_sync import (
    collect_profile_snapshot,
    extract_active_by_db_fullpath,
    load_beam_db,
    mod_db_fullpath,
    sync_db_from_index,
)
from core.modpreview import read_preview_image
from core.modinfo import get_mod_info_cached, has_info_json, parse_mod_info
from core import scanner
from core.utils import human_size
from ui.duplicates_dialog import DuplicatesDialog
from ui.settings_dialog import (
    SettingsDialog,
    load_online_cache_preferences,
    load_settings,
    load_view_preferences,
    save_view_preferences,
)
from ui.beamng_status_poller import BeamNGStatusPoller

try:
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
except Exception:  # pragma: no cover - environment dependent
    QWebEnginePage = object  # type: ignore[assignment]
    QWebEngineProfile = object  # type: ignore[assignment]
    QWebEngineView = object  # type: ignore[assignment]
    WEBENGINE_AVAILABLE = False


LEFT_KIND_ROLE = Qt.UserRole
LEFT_NAME_ROLE = Qt.UserRole + 1
LEFT_PATH_ROLE = Qt.UserRole + 2
LEFT_ACTIVE_ROLE = Qt.UserRole + 3

RIGHT_PATH_ROLE = Qt.UserRole
MOD_PATHS_MIME = "application/x-beamng-mod-paths"
_MISS = object()
_WEBENGINE_HTTP_CACHE_MAX_BYTES = 2_147_483_647
_WEBENGINE_HTTP_CACHE_MAX_MB = _WEBENGINE_HTTP_CACHE_MAX_BYTES // (1024 * 1024)
_ONLINE_REQUEST_PROMPT_TIMEOUT_SECONDS = 30
_BEAMNG_POLL_INTERVAL_SECONDS = 15.0
_BEAMNG_POLLER_STOP_TIMEOUT_MS = 1500
_TABLE_POPULATE_BATCH_SIZE = 120
_TABLE_INFO_BATCH_SIZE = 24
_ICON_POPULATE_BATCH_SIZE = 24
_ICON_DETAIL_BATCH_SIZE = 4
_ICON_ACTIVE_INDICATOR_SIZE = 22
_DB_WRITE_DEBOUNCE_MS = 900
_DB_WRITE_FLUSH_WAIT_SECONDS = 6.0
_PROFILE_SAVE_TIMEOUT_SECONDS = 45.0


def _webengine_cache_bytes(cache_mb: int) -> int:
    mb = max(64, min(_WEBENGINE_HTTP_CACHE_MAX_MB, int(cache_mb)))
    return mb * 1024 * 1024

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

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.itemAt(event.position().toPoint()) is None:
            self.clearSelection()
            self.setCurrentItem(None)
        super().mousePressEvent(event)

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


class ProfileDbConflictDialog(QDialog):
    def __init__(self, conflicts: list[tuple[str, bool, bool]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Profile / db.json Active-State Conflicts")
        self.resize(980, 500)
        self._conflicts = list(conflicts)
        self._choice_boxes: list[QComboBox] = []

        layout = QVBoxLayout(self)
        summary = QLabel(
            "The selected profile and db.json disagree on active state for these mods.\n"
            "Choose which source should win for each mod.",
            self,
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        self.table = QTableWidget(len(self._conflicts), 4, self)
        self.table.setHorizontalHeaderLabels(["Mod fullpath", "Profile", "db.json", "Use"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)

        for row, (fullpath, profile_state, db_state) in enumerate(self._conflicts):
            self.table.setItem(row, 0, QTableWidgetItem(fullpath))
            self.table.setItem(row, 1, QTableWidgetItem("Active" if profile_state else "Inactive"))
            self.table.setItem(row, 2, QTableWidgetItem("Active" if db_state else "Inactive"))
            choice = QComboBox(self.table)
            choice.addItem("Profile")
            choice.addItem("db.json")
            choice.setCurrentIndex(0)
            self.table.setCellWidget(row, 3, choice)
            self._choice_boxes.append(choice)

        self.table.resizeColumnsToContents()
        if self.table.columnWidth(0) < 380:
            self.table.setColumnWidth(0, 380)
        layout.addWidget(self.table, 1)

        source_buttons_row = QWidget(self)
        source_buttons_layout = QHBoxLayout(source_buttons_row)
        source_buttons_layout.setContentsMargins(0, 0, 0, 0)
        source_buttons_layout.setSpacing(8)
        all_profile_btn = QPushButton("All profile", source_buttons_row)
        all_db_btn = QPushButton("All db.json", source_buttons_row)
        all_profile_btn.clicked.connect(lambda: self._set_all_choices(use_profile=True))
        all_db_btn.clicked.connect(lambda: self._set_all_choices(use_profile=False))
        source_buttons_layout.addWidget(all_profile_btn)
        source_buttons_layout.addWidget(all_db_btn)
        source_buttons_layout.addStretch(1)
        layout.addWidget(source_buttons_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_all_choices(self, use_profile: bool) -> None:
        target_index = 0 if use_profile else 1
        for combo in self._choice_boxes:
            combo.setCurrentIndex(target_index)

    def selected_source_by_mod_fullpath(self) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for row, (fullpath, _profile_state, _db_state) in enumerate(self._conflicts):
            use_profile = self._choice_boxes[row].currentIndex() == 0
            out[fullpath] = bool(use_profile)
        return out


class WorkerSignals(QObject):
    done = Signal(object)
    error = Signal(str)
    progress = Signal(object)


class FnWorker(QRunnable):
    def __init__(self, fn, with_progress: bool = False) -> None:
        super().__init__()
        self.fn = fn
        self.with_progress = bool(with_progress)
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            if self.with_progress:
                value = self.fn(self.signals.progress.emit)
            else:
                value = self.fn()
        except Exception as exc:  # pragma: no cover
            self.signals.error.emit(str(exc))
            return
        self.signals.done.emit(value)


if WEBENGINE_AVAILABLE:

    class OnlineWebPage(QWebEnginePage):
        def __init__(self, profile, navigate_handler, parent=None) -> None:
            super().__init__(profile, parent)
            self._navigate_handler = navigate_handler

        def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
            if self._navigate_handler is not None and self._navigate_handler(url, nav_type, is_main_frame):
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)


class MainWindow(QMainWindow):
    onlineConsoleLog = Signal(str)
    onlineRequestErrorPrompt = Signal(int, str)

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
        self._all_mod_by_path: dict[str, ModEntry] = {}
        self.preview_cache_dir = Path()
        self.preview_cache_index_file = Path()
        self._loading_view_preferences = False
        self._updating_icon_grid_metrics = False
        self.status_line3_message = ""
        self._local_status_lines: tuple[str, str, str] = ("", "", "")
        self._workers: set[FnWorker] = set()
        self._online_error_prompt_lock = threading.Lock()
        self._online_error_prompt_waiters: dict[int, tuple[threading.Event, dict[str, bool]]] = {}
        self._online_error_prompt_seq = itertools.count(1)
        self.online_client: OnlineRepoClient | None = None
        self.online_profile = None
        self.online_view = None
        self.online_page = None
        self.online_available = WEBENGINE_AVAILABLE
        self.online_current_url = ""
        self.online_hover_url = ""
        self.online_status_line2 = ""
        self.settings_store = QSettings("BeamNGManager", "ModPackManager")
        self.online_dark_mode_enabled = bool(self.settings_store.value("online_dark_mode", True, bool))
        self.online_debug_enabled = bool(self.settings_store.value("online_debug_enabled", False, bool))
        self.confirm_actions_enabled = bool(self.settings_store.value("confirm_actions_enabled", True, bool))
        self.info_caption_enabled = bool(self.settings_store.value("info_caption_enabled", False, bool))
        _startup_last_profile = str(self.settings_store.value("last_profile_path", "", str) or "").strip()
        self._startup_last_profile_to_apply: Path | None = Path(_startup_last_profile) if _startup_last_profile else None
        self._startup_profile_apply_pending = True
        self.online_cache_max_mb, self.online_cache_ttl_hours = load_online_cache_preferences()
        self.online_cache_max_mb = max(64, min(_WEBENGINE_HTTP_CACHE_MAX_MB, int(self.online_cache_max_mb)))
        self.project_root = Path(__file__).resolve().parents[1]
        self.db_path = Path()
        self.active_by_db_fullpath: dict[str, bool] = {}
        self._updating_mod_table = False
        self._mod_row_by_path: dict[str, int] = {}
        self._icon_holder_by_path: dict[str, QWidget] = {}
        self._mod_prefix_by_path: dict[str, str] = {}
        self._table_population_token = 0
        self._table_info_token = 0
        self._icon_population_token = 0
        self._icon_detail_token = 0
        self.current_profile_path: Path | None = None
        self.last_saved_profile_snapshot: dict[str, object] | None = None
        self.profile_dirty = False
        self._setting_splitter_sizes = False
        self._left_splitter_user_resized = False
        self._left_splitter_initialized = False
        self._pending_left_selection: tuple[str, str] | None = None
        self._index_apply_context = ""
        self._index_worker_error_prefix = "Scan"
        self._interaction_lock_depth = 0
        self._interaction_lock_reason = ""
        self._interaction_lock_popup_cooldown_until_ms = 0
        self._interaction_lock_popup: QMessageBox | None = None
        self._interaction_lock_widgets: list[QWidget] = []
        self._db_write_pending = False
        self._db_write_in_flight = False
        self._db_write_show_progress = False
        self._db_write_generation = 0
        self._db_write_in_flight_generation = 0
        self._beamng_running = bool(beamng_is_running())
        self._beamng_status_poller = BeamNGStatusPoller(
            self,
            check_fn=beamng_is_running,
            poll_interval_seconds=_BEAMNG_POLL_INTERVAL_SECONDS,
        )
        self._icon_metrics_timer = QTimer(self)
        self._icon_metrics_timer.setSingleShot(True)
        self._icon_metrics_timer.timeout.connect(self._update_icon_grid_metrics)
        self._db_write_timer = QTimer(self)
        self._db_write_timer.setSingleShot(True)
        self._db_write_timer.timeout.connect(self._flush_deferred_db_write)

        self._init_preview_cache_storage()
        self._init_online_client()
        self._build_ui()
        self._build_menu()
        self._update_beamng_status_indicator()
        self.onlineConsoleLog.connect(self._append_online_console_line)
        self.onlineRequestErrorPrompt.connect(self._on_online_request_error_prompt)
        self._beamng_status_poller.stateChanged.connect(self._on_beamng_runtime_state_changed)

        self._load_settings_and_maybe_scan()
        if self._beamng_running:
            self._set_status_line3("BeamNG is running. File-mutating actions are blocked until it closes.")
            self._show_silent_warning(
                "BeamNG Running",
                "BeamNG.drive.exe is currently running.\n\nPack/mod/profile actions are blocked while BeamNG is open.",
            )
        self._beamng_status_poller.start(initial_state=self._beamng_running)

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

    def _init_online_client(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        cache_root = project_root / ".cache" / "online"
        self.online_client = OnlineRepoClient(
            beam_mods_root=self.beam_mods_root or ".",
            library_root=self.library_root or ".",
            cache_root=cache_root,
            ttl_hours=self.online_cache_ttl_hours,
            cache_max_mb=self.online_cache_max_mb,
        )
        self.online_client.set_debug_logging(
            self.online_debug_enabled,
            self._emit_online_console_log if self.online_debug_enabled else None,
        )
        self.online_client.set_request_error_handler(self._decide_online_request_error)

    def _update_online_client_roots(self) -> None:
        if self.online_client is None:
            return
        if self.beam_mods_root:
            self.online_client.beam_mods_root = Path(self.beam_mods_root)
        if self.library_root:
            self.online_client.library_root = Path(self.library_root)
        self.online_client.update_cache_policy(self.online_cache_ttl_hours, self.online_cache_max_mb)
        self._refresh_online_installed_indicators()

    def _show_silent_message(
        self,
        title: str,
        text: str,
        informative_text: str = "",
        buttons=QMessageBox.Ok,
        default_button=QMessageBox.NoButton,
    ) -> QMessageBox.StandardButton:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.NoIcon)
        box.setWindowTitle(str(title))
        box.setText(str(text))
        if informative_text:
            box.setInformativeText(str(informative_text))
        box.setStandardButtons(buttons)
        if default_button != QMessageBox.NoButton:
            box.setDefaultButton(default_button)
        return QMessageBox.StandardButton(box.exec())

    def _show_silent_warning(self, title: str, text: str, informative_text: str = "") -> None:
        self._show_silent_message(title, text, informative_text, QMessageBox.Ok, QMessageBox.Ok)

    def _show_silent_information(self, title: str, text: str, informative_text: str = "") -> None:
        self._show_silent_message(title, text, informative_text, QMessageBox.Ok, QMessageBox.Ok)

    def _ask_silent_yes_no(self, title: str, question: str, default_yes: bool = True) -> bool:
        default_button = QMessageBox.Yes if default_yes else QMessageBox.No
        result = self._show_silent_message(
            title=title,
            text=question,
            informative_text="",
            buttons=QMessageBox.Yes | QMessageBox.No,
            default_button=default_button,
        )
        return result == QMessageBox.Yes

    def _update_beamng_status_indicator(self) -> None:
        if not hasattr(self, "beamng_status_indicator") or self.beamng_status_indicator is None:
            return
        if self._beamng_running:
            self.beamng_status_indicator.setStyleSheet(
                "QFrame#beamng_status_indicator { border: 1px solid #666; background-color: #d42b2b; }"
            )
            self.beamng_status_indicator.setToolTip("BeamNG is running.")
            return
        self.beamng_status_indicator.setStyleSheet(
            "QFrame#beamng_status_indicator { border: 1px solid #666; background-color: transparent; }"
        )
        self.beamng_status_indicator.setToolTip("BeamNG is not running.")

    def _beamng_mutation_blocked(self, action: str, show_dialog: bool = False) -> bool:
        if not self._beamng_running:
            return False
        message = f"BeamNG is running. Close BeamNG.drive.exe to {action}."
        self._set_status_line3(message)
        if show_dialog:
            self._show_silent_warning(
                "BeamNG Running",
                "BeamNG.drive.exe is currently running.\nClose the game before file-mutating actions.",
            )
        return True

    def _on_beamng_runtime_state_changed(self, is_running: bool) -> None:
        next_state = bool(is_running)
        if next_state == self._beamng_running:
            return
        self._beamng_running = next_state
        self._update_beamng_status_indicator()
        if next_state:
            timer = getattr(self, "_db_write_timer", None)
            if timer is not None and timer.isActive():
                timer.stop()
            self._db_write_pending = False
            self._db_write_show_progress = False
            if self.online_client is not None:
                self.online_client.request_cancel()
            self._set_status_line3("BeamNG started. File-mutating actions are now blocked.")
            return
        self._set_status_line3("BeamNG closed. Reloading state and re-enabling actions...")
        self.full_refresh()

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
            self._all_mod_by_path = {}
            return

        names: set[str] = set()
        paths: dict[str, Path] = {}
        all_by_path: dict[str, ModEntry] = {}
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
                all_by_path[str(entry.path)] = entry
        self.known_mod_names = names
        self.known_mod_paths = paths
        self._all_mod_by_path = all_by_path

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
            self._schedule_icon_grid_metrics_update(delay_ms=0)

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

        self.profile_combo = QComboBox(self)
        self.profile_new_btn = QPushButton("New", self)
        self.profile_save_btn = QPushButton("Save", self)
        self.profile_load_btn = QPushButton("Load", self)
        for btn in (self.profile_new_btn, self.profile_save_btn, self.profile_load_btn):
            btn.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Fixed)
            btn.setMinimumWidth(btn.fontMetrics().horizontalAdvance(btn.text()) + 18)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_combo_changed)
        self.profile_new_btn.clicked.connect(self._create_profile_from_current)
        self.profile_save_btn.clicked.connect(self._save_selected_profile)
        self.profile_load_btn.clicked.connect(self._load_selected_profile)

        self.profile_buttons_row = QWidget(self)
        profile_buttons_layout = QHBoxLayout(self.profile_buttons_row)
        profile_buttons_layout.setContentsMargins(0, 0, 0, 0)
        profile_buttons_layout.addWidget(self.profile_new_btn)
        profile_buttons_layout.addWidget(self.profile_save_btn)
        profile_buttons_layout.addWidget(self.profile_load_btn)

        self.profile_combo_row = QWidget(self)
        profile_combo_layout = QHBoxLayout(self.profile_combo_row)
        profile_combo_layout.setContentsMargins(0, 0, 0, 0)
        profile_combo_layout.addWidget(self.profile_combo, 1)

        left_panel = QWidget(self)
        self.left_panel = left_panel
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.profile_buttons_row)
        left_layout.addWidget(self.profile_combo_row)
        left_layout.addWidget(self.left_tree, 1)

        self.mods_table = ModsTableWidget(self)
        self.mods_table.setColumnCount(4)
        self.mods_table.setHorizontalHeaderLabels(["Filename", "Update", "Size", "info.json"])
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
        self.mods_table.itemChanged.connect(self._on_mod_table_item_changed)

        self.mods_icons = ModsIconListWidget(self)
        self.mods_icons.setViewMode(QListWidget.IconMode)
        self.mods_icons.setFlow(QListWidget.LeftToRight)
        self.mods_icons.setWrapping(True)
        self.mods_icons.setResizeMode(QListWidget.Adjust)
        self.mods_icons.setMovement(QListWidget.Static)
        self.mods_icons.setSpacing(0)
        self.mods_icons.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.mods_icons.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.mods_icons.setDragEnabled(True)
        self.mods_icons.setDragDropMode(QAbstractItemView.DragOnly)
        self.mods_icons.setDefaultDropAction(Qt.MoveAction)
        self.mods_icons.setStyleSheet("QListWidget::item { margin: 0px; padding: 0px; border: none; }")
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
        self.columns_slider.setFixedWidth(120)
        self.columns_slider.valueChanged.connect(self._on_icon_columns_changed)
        self.columns_label.setVisible(False)
        self.columns_slider.setVisible(False)

        self.info_caption_checkbox = QCheckBox("Show info label", self)
        self.info_caption_checkbox.setChecked(self.info_caption_enabled)
        self.info_caption_checkbox.toggled.connect(self._on_info_caption_toggle)
        self.confirm_actions_checkbox = QCheckBox("Confirm actions", self)
        self.confirm_actions_checkbox.setChecked(self.confirm_actions_enabled)
        self.confirm_actions_checkbox.toggled.connect(self._on_confirm_actions_toggled)
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
        mods_toolbar_layout.addWidget(self.confirm_actions_checkbox)
        mods_toolbar_layout.addSpacing(12)
        mods_toolbar_layout.addWidget(self.columns_label)
        mods_toolbar_layout.addWidget(self.columns_slider, 0)
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
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([300, 900])
        splitter.setChildrenCollapsible(False)
        splitter.splitterMoved.connect(self._on_splitter_moved)
        self.main_splitter = splitter

        self.status_box = QPlainTextEdit(self)
        self.status_box.setReadOnly(True)
        self.status_box.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.status_box.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.status_box.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.status_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.status_box.horizontalScrollBar().rangeChanged.connect(self._update_status_box_height)
        self._update_status_box_height()

        self.beamng_status_indicator = QFrame(self)
        self.beamng_status_indicator.setObjectName("beamng_status_indicator")
        self.beamng_status_indicator.setFixedSize(24, 24)
        self.beamng_status_indicator.setToolTip("BeamNG is not running.")

        self.local_tab = QWidget(self)
        local_layout = QVBoxLayout(self.local_tab)
        local_layout.setContentsMargins(0, 0, 0, 0)
        local_layout.addWidget(splitter)

        self.online_tab = self._build_online_tab()
        self.console_tab = self._build_console_tab()
        self.main_tabs = QTabWidget(self)
        self.main_tabs.setTabPosition(QTabWidget.North)
        self.main_tabs.addTab(self.local_tab, "Local")
        self.main_tabs.addTab(self.online_tab, "Online")
        if self.online_debug_enabled:
            self.main_tabs.addTab(self.console_tab, "Console")
        self.main_tabs.currentChanged.connect(self._on_main_tab_changed)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.addWidget(self.main_tabs, 1)
        status_row = QWidget(central)
        status_row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        status_row_layout = QHBoxLayout(status_row)
        status_row_layout.setContentsMargins(0, 0, 0, 0)
        status_row_layout.setSpacing(6)
        status_row_layout.addWidget(self.status_box, 1)
        status_row_layout.addWidget(self.beamng_status_indicator, 0, Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(status_row)
        self.setCentralWidget(central)

        self._set_status("Active mods: 0 / Total mods: 0", "Packs active: 0/0 | Loose: 0 | Repo: 0", "")
        self._load_view_preferences()
        self._install_interaction_filters()

    def _build_online_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)

        toolbar = QWidget(tab)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)

        self.online_repo_btn = QPushButton("Repo", toolbar)
        self.online_forums_btn = QPushButton("Forums", toolbar)
        self.online_back_btn = QPushButton("Back", toolbar)
        self.online_forward_btn = QPushButton("Forward", toolbar)
        self.online_refresh_btn = QPushButton("Refresh", toolbar)
        self.online_dark_toggle_btn = QToolButton(toolbar)
        self.online_dark_toggle_btn.setText("Dark Web")
        self.online_dark_toggle_btn.setCheckable(True)
        self.online_dark_toggle_btn.setChecked(self.online_dark_mode_enabled)
        self.online_debug_checkbox = QCheckBox("Debug", toolbar)
        self.online_debug_checkbox.setChecked(self.online_debug_enabled)
        self.online_address_edit = QLineEdit(toolbar)
        self.online_address_edit.setPlaceholderText("Enter URL...")
        self.online_address_edit.setClearButtonEnabled(True)
        self.online_go_btn = QPushButton("Go", toolbar)

        for btn in [
            self.online_repo_btn,
            self.online_forums_btn,
            self.online_back_btn,
            self.online_forward_btn,
            self.online_refresh_btn,
            self.online_dark_toggle_btn,
            self.online_debug_checkbox,
        ]:
            toolbar_layout.addWidget(btn)
        toolbar_layout.addWidget(self.online_address_edit, 1)
        toolbar_layout.addWidget(self.online_go_btn)
        layout.addWidget(toolbar)

        self.online_repo_btn.clicked.connect(self._open_online_repo)
        self.online_forums_btn.clicked.connect(self._open_online_forums)
        self.online_back_btn.clicked.connect(self._online_go_back)
        self.online_forward_btn.clicked.connect(self._online_go_forward)
        self.online_refresh_btn.clicked.connect(self._online_refresh)
        self.online_dark_toggle_btn.toggled.connect(self._on_online_dark_toggled)
        self.online_debug_checkbox.toggled.connect(self._on_online_debug_toggled)
        self.online_go_btn.clicked.connect(self._navigate_online_from_address_bar)
        self.online_address_edit.returnPressed.connect(self._navigate_online_from_address_bar)

        if not self.online_available:
            placeholder = QLabel(
                "Qt WebEngine is not available in this environment.\n"
                "Install a PySide6 distribution that includes QtWebEngine.",
                tab,
            )
            placeholder.setAlignment(Qt.AlignCenter)
            layout.addWidget(placeholder, 1)
            for btn in [
                self.online_repo_btn,
                self.online_forums_btn,
                self.online_back_btn,
                self.online_forward_btn,
                self.online_refresh_btn,
                self.online_dark_toggle_btn,
                self.online_address_edit,
                self.online_go_btn,
            ]:
                btn.setEnabled(False)
            return tab

        project_root = Path(__file__).resolve().parents[1]
        webview_cache_root = project_root / ".cache" / "webview"
        cache_path = webview_cache_root / "http_cache"
        storage_path = webview_cache_root / "storage"
        cache_path.mkdir(parents=True, exist_ok=True)
        storage_path.mkdir(parents=True, exist_ok=True)

        profile = QWebEngineProfile("BeamNGManagerOnline", self)
        profile.setCachePath(str(cache_path))
        profile.setPersistentStoragePath(str(storage_path))
        profile.setHttpCacheMaximumSize(_webengine_cache_bytes(self.online_cache_max_mb))
        self.online_profile = profile

        self.online_view = QWebEngineView(tab)
        # Keep page parented to profile so profile teardown can safely dispose it first.
        self.online_page = OnlineWebPage(profile, self._handle_online_navigation, profile)
        self.online_view.setPage(self.online_page)
        self.online_view.urlChanged.connect(self._on_online_url_changed)
        self.online_page.linkHovered.connect(self._on_online_link_hovered)
        self.online_view.loadFinished.connect(self._on_online_page_load_finished)
        default_url = "https://www.beamng.com/resources/"
        self.online_current_url = default_url
        self.online_address_edit.setText(default_url)
        self.online_view.setUrl(QUrl(default_url))
        self._update_online_nav_buttons()
        layout.addWidget(self.online_view, 1)
        return tab

    def _build_console_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)

        controls = QWidget(tab)
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        clear_btn = QPushButton("Clear", controls)
        clear_btn.clicked.connect(self._clear_online_console)
        controls_layout.addWidget(clear_btn)
        controls_layout.addStretch(1)

        self.online_console_box = QPlainTextEdit(tab)
        self.online_console_box.setReadOnly(True)
        self.online_console_box.setLineWrapMode(QPlainTextEdit.NoWrap)

        layout.addWidget(controls)
        layout.addWidget(self.online_console_box, 1)
        return tab

    def _open_online_repo(self) -> None:
        if self.online_view is None:
            return
        self.online_view.setUrl(QUrl("https://www.beamng.com/resources/"))

    def _open_online_forums(self) -> None:
        if self.online_view is None:
            return
        self.online_view.setUrl(QUrl("https://www.beamng.com/forums/"))

    def _online_go_back(self) -> None:
        if self.online_view is None:
            return
        try:
            history = self.online_view.history()
            if history.canGoBack():
                history.back()
                return
        except Exception:
            pass
        if self.online_page is not None:
            self.online_page.runJavaScript("if (window.history.length > 1) { history.back(); }")
            return
        self._set_status_line3("No previous page in history.")

    def _online_go_forward(self) -> None:
        if self.online_view is None:
            return
        try:
            history = self.online_view.history()
            if history.canGoForward():
                history.forward()
                return
        except Exception:
            pass
        if self.online_page is not None:
            self.online_page.runJavaScript("history.forward();")
            return
        self._set_status_line3("No next page in history.")

    def _online_refresh(self) -> None:
        if self.online_view is None:
            return
        self.online_view.reload()

    def _normalize_online_input_url(self, raw_value: str) -> str:
        value = str(raw_value or "").strip()
        if not value:
            return ""
        lower = value.lower()
        if lower.startswith("http://") or lower.startswith("https://"):
            return value
        if lower.startswith("www."):
            return f"https://{value}"
        if "://" in value:
            return value
        return f"https://{value}"

    def _navigate_online_from_address_bar(self) -> None:
        if self.online_view is None:
            return
        if not hasattr(self, "online_address_edit"):
            return
        target_text = self._normalize_online_input_url(self.online_address_edit.text())
        if not target_text:
            return
        target_url = QUrl(target_text)
        if not target_url.isValid():
            self._set_status_line3(f"Invalid URL: {target_text}")
            return
        self.online_view.setUrl(target_url)

    def _is_online_tab_active(self) -> bool:
        return hasattr(self, "main_tabs") and self.main_tabs.currentWidget() is self.online_tab

    def _render_status(self, line1: str, line2: str, line3: str) -> None:
        self.status_box.setPlainText(f"{line1}\n{line2}\n{line3}")
        self._update_status_box_height()

    def _set_online_status(self) -> None:
        self._render_status(self.online_current_url, self.online_status_line2, self.online_hover_url)

    def _set_online_status_line2(self, message: str) -> None:
        self.online_status_line2 = message
        if self._is_online_tab_active():
            self._set_online_status()

    def _refresh_local_status_display(self) -> None:
        if self._selected_mod_paths():
            self._on_mod_selection_changed()
            return
        items = self.left_tree.selectedItems()
        if items:
            self._status_for_folder(items[0], len(self._mods_for_left_item(items[0])))
        else:
            self._update_summary_status()

    def _on_main_tab_changed(self, _index: int) -> None:
        if self._is_online_tab_active():
            self._set_online_status()
            return
        self._refresh_local_status_display()

    def _left_item_signature(self, item: QTreeWidgetItem | None) -> tuple[str, str]:
        if item is None:
            return "", ""
        kind = str(item.data(0, LEFT_KIND_ROLE) or "")
        name = str(item.data(0, LEFT_NAME_ROLE) or "")
        if kind in {"mods_root", "repo"}:
            name = kind
        if not name:
            name = item.text(0)
        return kind, name

    def _save_last_left_selection(self, item: QTreeWidgetItem | None) -> None:
        kind, name = self._left_item_signature(item)
        if not kind:
            return
        self.settings_store.setValue("local_last_kind", kind)
        self.settings_store.setValue("local_last_name", name)

    def _saved_left_selection(self) -> tuple[str, str]:
        kind = str(self.settings_store.value("local_last_kind", "mods_root", str) or "mods_root")
        name = str(self.settings_store.value("local_last_name", "mods_root", str) or "mods_root")
        return kind, name

    def _find_left_item(self, target_kind: str, target_name: str) -> QTreeWidgetItem | None:
        for idx in range(self.left_tree.topLevelItemCount()):
            item = self.left_tree.topLevelItem(idx)
            kind, name = self._left_item_signature(item)
            if kind != target_kind:
                continue
            if target_kind in {"mods_root", "repo"}:
                return item
            if name == target_name:
                return item
        return None

    def _capture_pending_left_selection(self) -> None:
        items = self.left_tree.selectedItems() if hasattr(self, "left_tree") else []
        if not items:
            self._pending_left_selection = None
            return
        kind, name = self._left_item_signature(items[0])
        self._pending_left_selection = (kind, name) if kind else None

    def _restore_left_selection(self, preferred: tuple[str, str] | None = None) -> bool:
        if self.left_tree.topLevelItemCount() == 0:
            return False
        target = None
        preferred_kind = str((preferred or ("", ""))[0] or "")
        preferred_name = str((preferred or ("", ""))[1] or "")
        if preferred_kind:
            target = self._find_left_item(preferred_kind, preferred_name)
        if target is None:
            saved_kind, saved_name = self._saved_left_selection()
            target = self._find_left_item(saved_kind, saved_name)
        if target is None:
            target = self._find_left_item("mods_root", "mods_root")
        if target is None:
            target = self.left_tree.topLevelItem(0)
        if target is None:
            return False
        self.left_tree.setCurrentItem(target)
        target.setSelected(True)
        self.left_tree.scrollToItem(target)
        self._save_last_left_selection(target)
        return True

    def _recommended_left_pane_width(self) -> int:
        tree_col = max(0, self.left_tree.sizeHintForColumn(0))
        indent = max(0, int(self.left_tree.indentation()))
        scroll_extent = self.left_tree.style().pixelMetric(QStyle.PM_ScrollBarExtent, None, self.left_tree)
        tree_width = tree_col + indent + scroll_extent + 36

        controls_width = 0
        if hasattr(self, "profile_buttons_row"):
            controls_width = max(controls_width, self.profile_buttons_row.minimumSizeHint().width())
        if hasattr(self, "profile_combo_row"):
            controls_width = max(controls_width, self.profile_combo_row.minimumSizeHint().width())
        if hasattr(self, "profile_combo"):
            fm = self.profile_combo.fontMetrics()
            longest = 0
            for i in range(self.profile_combo.count()):
                longest = max(longest, fm.horizontalAdvance(self.profile_combo.itemText(i)))
            controls_width = max(controls_width, longest + 56)

        return max(220, tree_width, controls_width + 10)

    def _apply_initial_left_pane_width(self, force: bool = False) -> None:
        if not hasattr(self, "main_splitter"):
            return
        if self._left_splitter_user_resized and not force:
            return
        left_width = self._recommended_left_pane_width()
        total_width = max(1, self.main_splitter.width())
        if total_width <= 1:
            total_width = max(left_width + 500, 1000)
        right_width = max(320, total_width - left_width)
        if right_width + left_width > total_width:
            left_width = max(200, total_width - right_width)
        self._setting_splitter_sizes = True
        try:
            self.main_splitter.setSizes([left_width, right_width])
        finally:
            self._setting_splitter_sizes = False
        self._left_splitter_initialized = True

    def _on_online_url_changed(self, url: QUrl) -> None:
        self.online_current_url = url.toString()
        if hasattr(self, "online_address_edit"):
            self.online_address_edit.blockSignals(True)
            self.online_address_edit.setText(self.online_current_url)
            self.online_address_edit.blockSignals(False)
        self._update_online_nav_buttons()
        if self._is_online_tab_active():
            self._set_online_status()

    def _update_online_nav_buttons(self) -> None:
        if not hasattr(self, "online_back_btn") or not hasattr(self, "online_forward_btn"):
            return
        if self.online_view is None:
            self.online_back_btn.setEnabled(False)
            self.online_forward_btn.setEnabled(False)
            return
        can_back = False
        can_forward = False
        try:
            history = self.online_view.history()
            can_back = bool(history.canGoBack())
            can_forward = bool(history.canGoForward())
        except Exception:
            pass
        self.online_back_btn.setEnabled(can_back)
        self.online_forward_btn.setEnabled(can_forward)

    def _on_online_link_hovered(self, url: str) -> None:
        self.online_hover_url = (url or "").strip()
        if self._is_online_tab_active():
            self._set_online_status()

    def _emit_online_console_log(self, message: str) -> None:
        self.onlineConsoleLog.emit(str(message))

    def _show_online_request_error_dialog(self, message: str) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.NoIcon)
        box.setWindowTitle("Online Request Error")
        box.setText("A web request failed.")
        box.setInformativeText(str(message))
        stop_btn = box.addButton("Stop", QMessageBox.RejectRole)
        continue_btn = box.addButton("Continue", QMessageBox.AcceptRole)
        box.setDefaultButton(continue_btn)
        box.exec()
        return box.clickedButton() is continue_btn

    def _decide_online_request_error(self, message: str) -> bool:
        if threading.current_thread() is threading.main_thread():
            return self._show_online_request_error_dialog(message)
        token = next(self._online_error_prompt_seq)
        waiter = threading.Event()
        holder = {"continue": True}
        with self._online_error_prompt_lock:
            self._online_error_prompt_waiters[token] = (waiter, holder)
        self.onlineRequestErrorPrompt.emit(token, str(message))
        if waiter.wait(timeout=_ONLINE_REQUEST_PROMPT_TIMEOUT_SECONDS):
            return bool(holder.get("continue", True))
        with self._online_error_prompt_lock:
            pair = self._online_error_prompt_waiters.pop(token, None)
        if pair is not None:
            timeout_waiter, timeout_holder = pair
            timeout_holder["continue"] = False
            timeout_waiter.set()
            return False
        return bool(holder.get("continue", False))

    def _flush_online_error_waiters(self, default_continue: bool = False) -> None:
        with self._online_error_prompt_lock:
            waiters = list(self._online_error_prompt_waiters.values())
            self._online_error_prompt_waiters.clear()
        for waiter, holder in waiters:
            holder["continue"] = bool(default_continue)
            waiter.set()

    def _on_online_request_error_prompt(self, token: int, message: str) -> None:
        decision = self._show_online_request_error_dialog(message)
        with self._online_error_prompt_lock:
            pair = self._online_error_prompt_waiters.pop(int(token), None)
        if pair is None:
            return
        waiter, holder = pair
        holder["continue"] = bool(decision)
        waiter.set()

    def _append_online_console_line(self, message: str) -> None:
        if not self.online_debug_enabled or not hasattr(self, "online_console_box"):
            return
        stamp = QDateTime.currentDateTime().toString("HH:mm:ss")
        self.online_console_box.appendPlainText(f"[{stamp}] {message}")

    def _clear_online_console(self) -> None:
        if hasattr(self, "online_console_box"):
            self.online_console_box.clear()

    def _set_console_tab_visible(self, visible: bool) -> None:
        if not hasattr(self, "main_tabs") or not hasattr(self, "console_tab"):
            return
        idx = self.main_tabs.indexOf(self.console_tab)
        if visible and idx < 0:
            self.main_tabs.addTab(self.console_tab, "Console")
            return
        if not visible and idx >= 0:
            was_current = self.main_tabs.currentWidget() is self.console_tab
            self.main_tabs.removeTab(idx)
            if was_current:
                self.main_tabs.setCurrentWidget(self.online_tab)

    def _on_online_debug_toggled(self, checked: bool) -> None:
        self.online_debug_enabled = bool(checked)
        self.settings_store.setValue("online_debug_enabled", bool(checked))
        if self.online_client is not None:
            self.online_client.set_debug_logging(
                self.online_debug_enabled,
                self._emit_online_console_log if self.online_debug_enabled else None,
            )
        if self.online_debug_enabled:
            self._set_console_tab_visible(True)
            self._emit_online_console_log("Debug logging enabled.")
        else:
            self._set_console_tab_visible(False)

    def _db_entry_tag_id(self, value: dict[str, object]) -> str:
        direct = str(value.get("modID") or value.get("tagid") or "").strip()
        if direct:
            return direct
        mod_data = value.get("modData")
        if isinstance(mod_data, dict):
            nested = str(mod_data.get("tagid") or mod_data.get("modID") or "").strip()
            if nested:
                return nested
        return ""

    def _online_installed_marker_sets(self) -> tuple[set[str], set[str], set[str], set[str]]:
        subscribed_tokens: set[str] = set()
        manual_tokens: set[str] = set()
        subscribed_tag_ids: set[str] = set()
        manual_tag_ids: set[str] = set()

        if self.db_path:
            payload = load_beam_db(self.db_path)
            mods_payload = payload.get("mods", {})
            if isinstance(mods_payload, dict):
                for value in mods_payload.values():
                    if not isinstance(value, dict):
                        continue
                    fullpath = str(value.get("fullpath") or value.get("dirname") or "").strip().replace("\\", "/").lower()
                    is_subscribed = fullpath.startswith("/mods/repo/")
                    tag_id = self._db_entry_tag_id(value)
                    if tag_id:
                        if is_subscribed:
                            subscribed_tag_ids.add(tag_id.lower())
                        else:
                            manual_tag_ids.add(tag_id.lower())
                    mod_data = value.get("modData")
                    if isinstance(mod_data, dict):
                        resource_id = str(mod_data.get("resource_id") or mod_data.get("resourceId") or "").strip()
                        if resource_id:
                            if is_subscribed:
                                subscribed_tokens.add(resource_id.lower())
                            else:
                                manual_tokens.add(resource_id.lower())

        # If both exist, prefer subscribed presentation.
        manual_tokens.difference_update(subscribed_tokens)
        manual_tag_ids.difference_update(subscribed_tag_ids)
        return subscribed_tokens, manual_tokens, subscribed_tag_ids, manual_tag_ids

    def _online_installed_indicator_script(
        self,
        subscribed_tokens: set[str],
        manual_tokens: set[str],
        subscribed_tag_ids: set[str],
        manual_tag_ids: set[str],
    ) -> str:
        subscribed_tokens_json = json.dumps(
            sorted({str(token).strip().lower() for token in subscribed_tokens if str(token).strip()})
        )
        manual_tokens_json = json.dumps(sorted({str(token).strip().lower() for token in manual_tokens if str(token).strip()}))
        subscribed_tag_ids_json = json.dumps(
            sorted({str(tag_id).strip().lower() for tag_id in subscribed_tag_ids if str(tag_id).strip()})
        )
        manual_tag_ids_json = json.dumps(sorted({str(tag_id).strip().lower() for tag_id in manual_tag_ids if str(tag_id).strip()}))
        return (
            "(() => {\n"
            f"  const subscribedTokens = new Set({subscribed_tokens_json});\n"
            f"  const manualTokens = new Set({manual_tokens_json});\n"
            f"  const subscribedTagIds = new Set({subscribed_tag_ids_json});\n"
            f"  const manualTagIds = new Set({manual_tag_ids_json});\n"
            "  const styleId = 'beamng-manager-installed-style';\n"
            "  const markerAttr = 'data-beamng-manager-install-kind';\n"
            "  const cardAttr = 'data-beamng-manager-install-kind-card';\n"
            "  const pageBadgeId = 'beamng-manager-installed-page-badge';\n"
            "  const listCardSelector = '.resourceListItem, .structItem--resource, .structItem, .resourceRow';\n"
            "  const titleTagPattern = /\\s*\\|\\s*(Installed on this PC|Subscribed|Manually Installed)\\s*$/i;\n"
            "  const statusLabel = (kind) => (kind === 'subscribed' ? 'Subscribed' : (kind === 'manual' ? 'Manually Installed' : ''));\n"
            "  const isResourceDetailPage = (() => {\n"
            "    const parts = (window.location.pathname || '').split('/').filter(Boolean).map((p) => p.toLowerCase());\n"
            "    if (parts.length < 2) return false;\n"
            "    if (parts[0] !== 'resources') return false;\n"
            "    const second = parts[1] || '';\n"
            "    if (!second) return false;\n"
            "    if (new Set(['authors', 'categories', 'reviews']).has(second)) return false;\n"
            "    return true;\n"
            "  })();\n"
            "  const ensureStyle = () => {\n"
            "    let style = document.getElementById(styleId);\n"
            "    if (style) return;\n"
            "    style = document.createElement('style');\n"
            "    style.id = styleId;\n"
            "    style.textContent = `\n"
            "a[data-beamng-manager-install-kind=\"subscribed\"] {\n"
            "  box-shadow: inset 0 0 0 2px rgba(255, 216, 77, 0.92) !important;\n"
            "  border-radius: 4px !important;\n"
            "}\n"
            "a[data-beamng-manager-install-kind=\"manual\"] {\n"
            "  box-shadow: inset 0 0 0 2px rgba(225, 68, 68, 0.92) !important;\n"
            "  border-radius: 4px !important;\n"
            "}\n"
            "[data-beamng-manager-install-kind-card=\"subscribed\"] {\n"
            "  position: relative !important;\n"
            "  box-shadow: inset 0 0 0 1px rgba(255, 216, 77, 0.75) !important;\n"
            "  border-radius: 6px !important;\n"
            "}\n"
            "[data-beamng-manager-install-kind-card=\"manual\"] {\n"
            "  position: relative !important;\n"
            "  box-shadow: inset 0 0 0 1px rgba(225, 68, 68, 0.8) !important;\n"
            "  border-radius: 6px !important;\n"
            "}\n"
            "#beamng-manager-installed-page-badge {\n"
            "  display: inline-block !important;\n"
            "  margin: 8px 0 10px 0 !important;\n"
            "  padding: 4px 10px !important;\n"
            "  border-radius: 999px !important;\n"
            "  font-size: 12px !important;\n"
            "  font-weight: 700 !important;\n"
            "}\n"
            "#beamng-manager-installed-page-badge[data-kind=\"subscribed\"] {\n"
            "  background: rgba(255, 216, 77, 0.2) !important;\n"
            "  border: 1px solid rgba(255, 216, 77, 0.7) !important;\n"
            "  color: #ffd84d !important;\n"
            "}\n"
            "#beamng-manager-installed-page-badge[data-kind=\"manual\"] {\n"
            "  background: rgba(225, 68, 68, 0.22) !important;\n"
            "  border: 1px solid rgba(225, 68, 68, 0.78) !important;\n"
            "  color: #ff8c8c !important;\n"
            "}\n"
            "    `;\n"
            "    (document.head || document.documentElement).appendChild(style);\n"
            "  };\n"
            "  const extractDirectResourceToken = (value) => {\n"
            "    const raw = String(value || '').trim();\n"
            "    if (!raw) return '';\n"
            "    let parsed;\n"
            "    try {\n"
            "      parsed = new URL(raw, document.baseURI || window.location.href);\n"
            "    } catch (_err) {\n"
            "      return '';\n"
            "    }\n"
            "    const parts = parsed.pathname.split('/').filter(Boolean);\n"
            "    const idx = parts.findIndex((part) => part.toLowerCase() === 'resources');\n"
            "    if (idx < 0 || idx + 1 >= parts.length) return '';\n"
            "    if (idx + 2 !== parts.length) return '';\n"
            "    const token = String(parts[idx + 1] || '').trim().toLowerCase();\n"
            "    if (!token || token === 'download') return '';\n"
            "    if (new Set(['authors', 'categories', 'reviews']).has(token)) return '';\n"
            "    return token;\n"
            "  };\n"
            "  const kindFromToken = (token) => {\n"
            "    const value = String(token || '').trim().toLowerCase();\n"
            "    if (!value) return '';\n"
            "    if (subscribedTokens.has(value)) return 'subscribed';\n"
            "    if (manualTokens.has(value)) return 'manual';\n"
            "    const maybeId = value.match(/\\.([0-9]+)$/);\n"
            "    if (maybeId) {\n"
            "      const id = maybeId[1];\n"
            "      if (subscribedTokens.has(id)) return 'subscribed';\n"
            "      if (manualTokens.has(id)) return 'manual';\n"
            "    }\n"
            "    return '';\n"
            "  };\n"
            "  const installedKind = (href) => {\n"
            "    const raw = String(href || '').trim();\n"
            "    if (!raw) return '';\n"
            "    const protocolMatch = raw.match(/^beamng:v1\\/(?:subscriptionMod|showMod)\\/([A-Za-z0-9_]+)/i);\n"
            "    if (protocolMatch && protocolMatch[1]) {\n"
            "      const tag = protocolMatch[1].toLowerCase();\n"
            "      if (subscribedTagIds.has(tag)) return 'subscribed';\n"
            "      if (manualTagIds.has(tag)) return 'manual';\n"
            "    }\n"
            "    const token = extractDirectResourceToken(raw);\n"
            "    if (!token) return '';\n"
            "    return kindFromToken(token);\n"
            "  };\n"
            "  const isListBadgeEligibleAnchor = (anchor) => {\n"
            "    if (!(anchor instanceof HTMLAnchorElement)) return false;\n"
            "    if (anchor.classList.contains('resourceIcon')) return true;\n"
            "    if (anchor.closest('h3.title, .resourceTitle, .structItem-title, .resourceBody h3, .resourceBody .title')) return true;\n"
            "    return false;\n"
            "  };\n"
            "  const syncAnchor = (anchor) => {\n"
            "    if (!(anchor instanceof HTMLAnchorElement)) return;\n"
            "    if (isResourceDetailPage || !isListBadgeEligibleAnchor(anchor)) {\n"
            "      anchor.removeAttribute(markerAttr);\n"
            "      anchor.title = String(anchor.title || '').replace(titleTagPattern, '').trim();\n"
            "      return;\n"
            "    }\n"
            "    const kind = installedKind(anchor.getAttribute('href'));\n"
            "    if (kind) {\n"
            "      anchor.setAttribute(markerAttr, kind);\n"
            "      const cleanedTitle = String(anchor.title || '').replace(titleTagPattern, '').trim();\n"
            "      const label = statusLabel(kind);\n"
            "      anchor.title = cleanedTitle ? `${cleanedTitle} | ${label}` : label;\n"
            "      return;\n"
            "    }\n"
            "    anchor.removeAttribute(markerAttr);\n"
            "    anchor.title = String(anchor.title || '').replace(titleTagPattern, '').trim();\n"
            "  };\n"
            "  const syncPageBadge = () => {\n"
            "    const existing = document.getElementById(pageBadgeId);\n"
            "    if (existing) existing.remove();\n"
            "    const kind = installedKind(window.location.href);\n"
            "    if (!kind) return;\n"
            "    const title = document.querySelector('h1, .p-title-value, .resourceTitle, .PageTitle');\n"
            "    if (!title || !title.parentElement) return;\n"
            "    const badge = document.createElement('div');\n"
            "    badge.id = pageBadgeId;\n"
            "    badge.setAttribute('data-kind', kind);\n"
            "    badge.textContent = statusLabel(kind);\n"
            "    title.parentElement.insertBefore(badge, title.nextSibling);\n"
            "  };\n"
            "  const syncCard = (card) => {\n"
            "    if (!(card instanceof Element)) return;\n"
            "    if (isResourceDetailPage) {\n"
            "      card.removeAttribute(cardAttr);\n"
            "      return;\n"
            "    }\n"
            "    const links = card.querySelectorAll('a[href]');\n"
            "    let kind = '';\n"
            "    for (const anchor of Array.from(links)) {\n"
            "      const current = installedKind(anchor.getAttribute('href'));\n"
            "      if (current === 'subscribed') {\n"
            "        kind = 'subscribed';\n"
            "        break;\n"
            "      }\n"
            "      if (current === 'manual') {\n"
            "        kind = 'manual';\n"
            "      }\n"
            "    }\n"
            "    if (kind) {\n"
            "      card.setAttribute(cardAttr, kind);\n"
            "      return;\n"
            "    }\n"
            "    card.removeAttribute(cardAttr);\n"
            "  };\n"
            "  const scan = (scope) => {\n"
            "    const root = scope || document;\n"
            "    if (!root.querySelectorAll) return;\n"
            "    root.querySelectorAll('a[href]').forEach((anchor) => syncAnchor(anchor));\n"
            "    root.querySelectorAll(listCardSelector).forEach((card) => syncCard(card));\n"
            "  };\n"
            "  ensureStyle();\n"
            "  scan(document);\n"
            "  syncPageBadge();\n"
            "  try {\n"
            "    const prev = window.__beamngManagerInstalledObserver;\n"
            "    if (prev && typeof prev.disconnect === 'function') prev.disconnect();\n"
            "  } catch (_err) {}\n"
            "  const observer = new MutationObserver((mutations) => {\n"
            "    for (const mutation of mutations) {\n"
            "      for (const node of Array.from(mutation.addedNodes || [])) {\n"
            "        if (!(node instanceof Element)) continue;\n"
            "        if (node.matches && node.matches('a[href]')) syncAnchor(node);\n"
            "        scan(node);\n"
            "      }\n"
            "    }\n"
            "    syncPageBadge();\n"
            "  });\n"
            "  if (document.body || document.documentElement) {\n"
            "    observer.observe(document.body || document.documentElement, { childList: true, subtree: true });\n"
            "    window.__beamngManagerInstalledObserver = observer;\n"
            "  }\n"
            "})();\n"
        )

    def _refresh_online_installed_indicators(self) -> None:
        if self.online_page is None or not self.online_available:
            return
        subscribed_tokens, manual_tokens, subscribed_tag_ids, manual_tag_ids = self._online_installed_marker_sets()
        self.online_page.runJavaScript(
            self._online_installed_indicator_script(
                subscribed_tokens,
                manual_tokens,
                subscribed_tag_ids,
                manual_tag_ids,
            )
        )

    def _online_dark_mode_script(self, enabled: bool) -> str:
        state = "true" if enabled else "false"
        return f"""
(() => {{
  const enabled = {state};
  const styleId = "beamng-manager-dark-style";
  const attrName = "data-beamng-manager-dark";
  const ensureStyle = () => {{
    let style = document.getElementById(styleId);
    if (!style) {{
      style = document.createElement("style");
      style.id = styleId;
      style.textContent = `
html[${{attrName}}="1"] {{
  color-scheme: dark !important;
  background: #0f1218 !important;
}}
html[${{attrName}}="1"] body {{
  background: #0f1218 !important;
  color: #e7ecf3 !important;
  text-shadow: none !important;
  -webkit-font-smoothing: antialiased !important;
  text-rendering: optimizeLegibility !important;
}}
html[${{attrName}}="1"] :is(main, section, article, aside, nav, header, footer, form, fieldset, details, summary, ul, ol, li, table, tr, td, th, blockquote, pre, code) {{
  background-color: transparent !important;
  color: inherit !important;
}}
html[${{attrName}}="1"] :is(
  [role="dialog"],
  [role="menu"],
  [role="listbox"],
  dialog,
  .modal,
  .overlay,
  .popup,
  .popover,
  .tooltip,
  .menu,
  .dropdown-menu,
  .xenOverlay,
  .xenOverlayContainer
) {{
  background: #1a2130 !important;
  color: #edf2ff !important;
  border-color: #3d4b63 !important;
  opacity: 1 !important;
}}
html[${{attrName}}="1"] :is(h1, h2, h3, h4, h5, h6, strong, b) {{
  color: #f5f8ff !important;
}}
html[${{attrName}}="1"] :is(p, span, li, dt, dd, label, small, cite, em) {{
  color: #d8dee8 !important;
}}
html[${{attrName}}="1"] a {{
  color: #7db3ff !important;
}}
html[${{attrName}}="1"] a:visited {{
  color: #b59bff !important;
}}
html[${{attrName}}="1"] a:hover {{
  color: #9ec8ff !important;
}}
html[${{attrName}}="1"] :is(input, textarea, select, button) {{
  background: #1a2130 !important;
  color: #edf2ff !important;
  border: 1px solid #3d4b63 !important;
}}
html[${{attrName}}="1"] :is(input::placeholder, textarea::placeholder) {{
  color: #9ba7bd !important;
}}
html[${{attrName}}="1"] :is(pre, code, kbd, samp) {{
  background: #161d29 !important;
  color: #e5ecfa !important;
  border-color: #334159 !important;
}}
html[${{attrName}}="1"] :is(table, th, td) {{
  border-color: #36445c !important;
}}
html[${{attrName}}="1"] :is(hr) {{
  border-color: #2f3b4e !important;
}}
html[${{attrName}}="1"] :is(.primaryContent, .secondaryContent, .messageContent, .sidebar, .resourceBody, .resourceTabs, .tabs, .section, .block) {{
  background: #111826 !important;
  color: #e1e8f5 !important;
  border-color: #334159 !important;
}}
html[${{attrName}}="1"] img,
html[${{attrName}}="1"] picture,
html[${{attrName}}="1"] video,
html[${{attrName}}="1"] canvas,
html[${{attrName}}="1"] svg {{
  filter: none !important;
}}
      `;
      (document.head || document.documentElement).appendChild(style);
    }}
  }};
  if (enabled) {{
    ensureStyle();
    document.documentElement.setAttribute(attrName, "1");
    document.documentElement.style.colorScheme = "dark";
    document.body && (document.body.style.backgroundColor = "#0f1218");
    document.body && (document.body.style.color = "#e7ecf3");
  }} else {{
    document.documentElement.removeAttribute(attrName);
    document.documentElement.style.colorScheme = "";
    if (document.body) {{
      document.body.style.backgroundColor = "";
      document.body.style.color = "";
    }}
  }}
}})();
"""

    def _apply_online_dark_mode(self, enabled: bool, persist: bool = True) -> None:
        self.online_dark_mode_enabled = bool(enabled)
        if persist:
            self.settings_store.setValue("online_dark_mode", bool(enabled))
        if self.online_page is None:
            return
        self.online_page.runJavaScript(self._online_dark_mode_script(bool(enabled)))

    def _on_online_dark_toggled(self, checked: bool) -> None:
        self._apply_online_dark_mode(checked, persist=True)
        state = "enabled" if checked else "disabled"
        self._set_status_line3(f"Online dark mode {state}.")

    def _on_online_page_load_finished(self, ok: bool) -> None:
        if not ok:
            return
        self._apply_online_dark_mode(self.online_dark_mode_enabled, persist=False)
        self._refresh_online_installed_indicators()
        self._update_online_nav_buttons()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "mods_stack") and self.mods_stack.currentWidget() is self.mods_icons:
            self._schedule_icon_grid_metrics_update()

    def closeEvent(self, event) -> None:
        if not self._prompt_save_profile_before_exit():
            event.ignore()
            return
        self._flush_db_write_if_pending(show_progress=True)
        if self._workers:
            self._set_status_line3_progress("Waiting for background operations to finish...")
            if not self.thread_pool.waitForDone(int(_DB_WRITE_FLUSH_WAIT_SECONDS * 1000)):
                self._show_silent_warning(
                    "Pending Operations",
                    "Background operations are still running.\nPlease wait for current tasks to complete before closing.",
                )
                event.ignore()
                return
        self._flush_online_error_waiters(default_continue=False)
        self._shutdown_beamng_status_poller()
        # Ensure page is detached before profile deletion to avoid WebEngine lifecycle warnings.
        if self.online_view is not None and self.online_page is not None and WEBENGINE_AVAILABLE:
            try:
                self.online_view.setPage(QWebEnginePage(self.online_view))
            except Exception:
                pass
            try:
                self.online_page.deleteLater()
            except Exception:
                pass
            self.online_page = None
        if self.online_profile is not None:
            try:
                self.online_profile.deleteLater()
            except Exception:
                pass
            self.online_profile = None
        super().closeEvent(event)

    def _shutdown_beamng_status_poller(self) -> None:
        poller = self._beamng_status_poller
        if poller is None:
            return
        try:
            poller.stateChanged.disconnect(self._on_beamng_runtime_state_changed)
        except (TypeError, RuntimeError):
            pass
        stopped = poller.stop(timeout_ms=_BEAMNG_POLLER_STOP_TIMEOUT_MS)
        if not stopped:
            poller.force_terminate(timeout_ms=_BEAMNG_POLLER_STOP_TIMEOUT_MS)
        self._beamng_status_poller = None

    def _prompt_save_profile_before_exit(self) -> bool:
        if not self.profile_dirty:
            return True
        return self._prompt_save_profile_changes(
            none_status_message="Closing without saving profile changes.",
            cancel_status_message="Exit cancelled.",
        )

    def _prompt_save_profile_before_profile_change(self) -> bool:
        if not self.profile_dirty:
            return True
        return self._prompt_save_profile_changes(
            none_status_message="Continuing without saving current profile changes.",
            cancel_status_message="Profile load cancelled.",
        )

    def _prompt_save_profile_changes(self, none_status_message: str, cancel_status_message: str) -> bool:
        if not self.profile_dirty:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.NoIcon)
        box.setWindowTitle("Unsaved Profile Changes")
        box.setText("Save to :")
        box.setInformativeText("")
        save_current_btn = box.addButton("Current", QMessageBox.AcceptRole)
        save_other_btn = box.addButton("Existing", QMessageBox.ActionRole)
        save_new_btn = box.addButton("New", QMessageBox.ActionRole)
        dont_save_btn = box.addButton("None", QMessageBox.DestructiveRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(save_current_btn)
        box.exec()

        clicked = box.clickedButton()
        if clicked == cancel_btn:
            self._set_status_line3(cancel_status_message)
            return False
        if clicked == dont_save_btn:
            self._set_status_line3(none_status_message)
            return True

        if clicked == save_current_btn:
            path = self._selected_profile_path()
            if path is None:
                path = self._profiles_folder() / "default.json"
            return self._save_profile_snapshot_to_path(path)

        if clicked == save_other_btn:
            paths = self._profile_paths()
            if not paths:
                self._show_silent_information("Save Profile", "No existing profiles available.")
                return False
            labels = [path.stem for path in paths]
            selected_label, ok = QInputDialog.getItem(
                self,
                "Save to Other Profile",
                "Choose profile:",
                labels,
                0,
                False,
            )
            if not ok or not selected_label:
                return False
            for path in paths:
                if path.stem == selected_label:
                    return self._save_profile_snapshot_to_path(path)
            return True

        if clicked == save_new_btn:
            raw_name, ok = QInputDialog.getText(self, "Save to New Profile", "New profile name:")
            if not ok:
                return False
            name = profile_store.sanitize_profile_name(raw_name)
            if not name:
                self._show_silent_warning("Save Profile", "Invalid profile name.")
                return False
            return self._save_profile_snapshot_to_path(self._profiles_folder() / f"{name}.json")

        return False

    def _save_profile_payload_responsive(
        self,
        path: Path,
        snapshot: dict[str, object],
        profile_name: str | None,
        status_prefix: str,
    ) -> bool:
        done_event = threading.Event()
        outcome: dict[str, str] = {"error": ""}
        started_at = time.monotonic()
        progress_lock = threading.Lock()
        progress_state: dict[str, object] = {"current": 0, "total": 0, "phase": "Preparing profile entries"}
        last_display: tuple[int, int, str] | None = None

        def _on_progress(current: int, total: int, phase: str) -> None:
            with progress_lock:
                progress_state["current"] = max(0, int(current))
                progress_state["total"] = max(0, int(total))
                progress_state["phase"] = str(phase or "Preparing profile entries")

        def _run_save() -> None:
            try:
                profile_store.save_profile(path, snapshot, profile_name=profile_name, progress_cb=_on_progress)
            except Exception as exc:  # pragma: no cover - defensive
                outcome["error"] = str(exc)
            finally:
                done_event.set()

        thread = threading.Thread(target=_run_save, name="profile-save", daemon=True)
        thread.start()

        while not done_event.wait(0.05):
            now = time.monotonic()
            with progress_lock:
                current = int(progress_state.get("current", 0))
                total = int(progress_state.get("total", 0))
                phase = str(progress_state.get("phase", "") or "")
            display = (current, total, phase)
            if display != last_display:
                phase_suffix = f" ({phase})" if phase else ""
                self._set_status_line3_progress(f"{status_prefix}... {current}/{total}{phase_suffix}")
                last_display = display
            QApplication.processEvents(QEventLoop.AllEvents, 50)
            if now - started_at >= _PROFILE_SAVE_TIMEOUT_SECONDS:
                self._set_status_line3(
                    f"Profile save timed out after {int(_PROFILE_SAVE_TIMEOUT_SECONDS)}s. "
                    "Try again when background activity is lower."
                )
                return False

        with progress_lock:
            current = int(progress_state.get("current", 0))
            total = int(progress_state.get("total", 0))
        self._set_status_line3_progress(f"{status_prefix}... {current}/{total} (done)")

        if outcome["error"]:
            self._set_status_line3(f"Profile save error: {outcome['error']}")
            return False
        return True

    def _save_profile_snapshot_to_path(self, path: Path) -> bool:
        if self.index is None:
            return True
        snapshot = self._current_profile_snapshot()
        packs = snapshot.get("packs", {})
        mods = snapshot.get("mods", {})
        packs_count = len(packs) if isinstance(packs, dict) else 0
        mods_count = len(mods) if isinstance(mods, dict) else 0
        total = packs_count + mods_count
        self._set_status_line3_progress(f"Writing profile '{path.stem}'... 0/{total}")
        if not self._save_profile_payload_responsive(
            path,
            snapshot,
            profile_name=path.stem,
            status_prefix=f"Writing profile '{path.stem}'",
        ):
            return False
        self.current_profile_path = path
        self.last_saved_profile_snapshot = self._current_profile_snapshot()
        self.profile_dirty = False
        self._refresh_profile_combo()
        self._set_status_line3_progress(f"Writing profile '{path.stem}' done ({total}/{total})")
        return True

    def _on_splitter_moved(self, _pos: int, _index: int) -> None:
        if not self._setting_splitter_sizes:
            self._left_splitter_user_resized = True
        if self._is_icon_view_active():
            self._schedule_icon_grid_metrics_update()

    def _on_icon_geometry_changed(self) -> None:
        if self._is_icon_view_active():
            self._schedule_icon_grid_metrics_update()

    def _on_icon_scroll_range_changed(self, _min_value: int, _max_value: int) -> None:
        if self._is_icon_view_active() and not self._updating_icon_grid_metrics:
            self._schedule_icon_grid_metrics_update()

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
        is_icon = not checked
        self.columns_label.setVisible(is_icon)
        self.columns_slider.setVisible(is_icon)
        self._persist_view_preferences()
        self._on_mod_selection_changed()

    def _on_icon_columns_changed(self, _value: int) -> None:
        self._schedule_icon_grid_metrics_update()
        self._persist_view_preferences()

    def _on_info_caption_toggle(self, checked: bool) -> None:
        self.info_caption_enabled = bool(checked)
        self.settings_store.setValue("info_caption_enabled", self.info_caption_enabled)
        if self._is_icon_view_active():
            self._populate_mods_icons(self.current_mod_entries)

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

    def _cancel_mod_population_jobs(self) -> None:
        self._table_population_token += 1
        self._table_info_token += 1
        self._icon_population_token += 1
        self._icon_detail_token += 1

    def _repopulate_current_mod_view(self) -> None:
        self._cancel_mod_population_jobs()
        self._populate_mods_table(self.current_mod_entries)
        if self._is_icon_view_active():
            self._populate_mods_icons(self.current_mod_entries)

    def _icon_caption_height(self) -> int:
        return max(18, int(self.mods_icons.fontMetrics().lineSpacing() + 2))

    def _icon_row_height(self, image_height: int) -> int:
        # holder margins (3+3) + layout spacing (4) + single-line caption height.
        return image_height + 10 + self._icon_caption_height()

    def _schedule_icon_grid_metrics_update(self, delay_ms: int = 25) -> None:
        if not self._is_icon_view_active():
            return
        self._icon_metrics_timer.start(max(0, int(delay_ms)))

    def _update_icon_grid_metrics(self) -> None:
        if not self.current_mod_entries or self._updating_icon_grid_metrics:
            return
        self._updating_icon_grid_metrics = True
        try:
            gap = max(0, int(self.mods_icons.spacing()))
            scrollbar_extent = self.mods_icons.style().pixelMetric(QStyle.PM_ScrollBarExtent, None, self.mods_icons)
            viewport_width = int(self.mods_icons.viewport().width())
            # Stabilize layout: compute as if one vertical scrollbar column is always reserved.
            full_content_width = viewport_width + (scrollbar_extent if self.mods_icons.verticalScrollBar().isVisible() else 0)
            viewport_width = max(1, full_content_width - scrollbar_extent)
            cols = max(2, min(8, self.columns_slider.value()))
            total_gaps = gap * (cols - 1)
            item_width = max(1, (viewport_width - total_gaps) // cols)
            # Account for per-item border to avoid overflow-triggered extra wrapping.
            item_width = max(1, item_width - 1)
            image_height = int(item_width * 9 / 16)
            caption_height = self._icon_caption_height()
            row_height = self._icon_row_height(image_height)

            # In IconMode, spacing is handled by QListWidget itself.
            self.mods_icons.setGridSize(QSize(item_width, row_height))
            for i in range(self.mods_icons.count()):
                item = self.mods_icons.item(i)
                item.setSizeHint(QSize(item_width, row_height))
                holder = self.mods_icons.itemWidget(item)
                if holder is None:
                    continue
                image_container = holder.findChild(QWidget, "preview_image_container")
                if image_container is not None:
                    image_container.setFixedHeight(image_height)
                preview_width = max(1, item_width - 8)
                image_label = holder.findChild(QLabel, "preview_image_label")
                if image_label is not None:
                    image_label.setFixedHeight(image_height)
                    image_label.setGeometry(0, 0, preview_width, image_height)
                name_label = holder.findChild(ElidedLabel, "icon_name_label")
                if name_label is not None:
                    name_label.setFixedHeight(caption_height)
                update_badge = holder.findChild(QLabel, "update_indicator_label")
                if update_badge is not None:
                    update_badge.adjustSize()
                    update_badge.move(max(6, preview_width - update_badge.width() - 6), 6)
                prefix_badge = holder.findChild(QLabel, "prefix_badge_label")
                if prefix_badge is not None and prefix_badge.isVisible():
                    prefix_badge.adjustSize()
                    prefix_badge.move(max(6, preview_width - prefix_badge.width() - 6), max(6, image_height - prefix_badge.height() - 6))
                active_btn = holder.findChild(QToolButton, "active_indicator_btn")
                if active_btn is not None:
                    active_btn.move(6, 6)
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
        self._lock_interaction("Image recheck is in progress.")
        try:
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
            total = len(selected_by_name)
            if total > 0:
                self._set_status_line3_progress(f"Rechecking mod images... 0/{total}")
            for key, mod_path in selected_by_name.items():
                if not mod_path.exists() or not mod_path.is_file():
                    missing_files += 1
                    rescanned += 1
                    if rescanned == total or rescanned % 10 == 0:
                        self._set_status_line3_progress(f"Rechecking mod images... {rescanned}/{total}")
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
                if rescanned == total or rescanned % 10 == 0:
                    self._set_status_line3_progress(f"Rechecking mod images... {rescanned}/{total}")

            self._save_preview_cache_index()
            if self._is_icon_view_active():
                self._populate_mods_icons(self.current_mod_entries)
                self._schedule_icon_grid_metrics_update(delay_ms=0)
            self._set_status_line3(
                "Image recheck complete. "
                f"Rescanned: {rescanned} | Updated image selection: {updated} | "
                f"Unchanged selection: {unchanged} | Found image: {found} | Missing file: {missing_files}"
            )
        finally:
            self._unlock_interaction()

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

    def _extract_prefix_value(self, info: dict[str, str] | None) -> str:
        if not info:
            return ""
        for key in ("prefix_title", "prefix", "Prefix", "status_prefix"):
            value = str(info.get(key, "")).strip()
            if value:
                return value
        return ""

    def _format_mod_name_with_prefix(self, file_name: str, prefix: str) -> str:
        prefix_text = str(prefix).strip()
        if not prefix_text:
            return file_name
        return f"{file_name} [{prefix_text}]"

    def _prefix_badge_stylesheet(self, prefix: str) -> str:
        key = str(prefix).strip().lower()
        if key == "alpha":
            bg, fg, border = "rgba(147, 84, 20, 0.90)", "#fff1de", "#ffb25b"
        elif key == "beta":
            bg, fg, border = "rgba(35, 84, 148, 0.90)", "#e5f0ff", "#6fb2ff"
        elif key == "experimental":
            bg, fg, border = "rgba(110, 55, 150, 0.90)", "#f1e6ff", "#c59aff"
        elif key == "outdated":
            bg, fg, border = "rgba(134, 36, 36, 0.92)", "#ffe9e9", "#ff8f8f"
        elif key == "unsupported":
            bg, fg, border = "rgba(125, 30, 30, 0.92)", "#ffe7e7", "#ff7d7d"
        else:
            bg, fg, border = "rgba(26, 26, 26, 0.82)", "#f0f0f0", "#777777"
        return (
            "QLabel#prefix_badge_label { "
            f"background: {bg}; "
            f"color: {fg}; "
            f"border: 1px solid {border}; "
            "border-radius: 3px; "
            "padding: 1px 4px; "
            "font-weight: 600; "
            "}"
        )

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

    def _build_icon_card_holder(
        self,
        mod: ModEntry,
        image_width: int,
        image_height: int,
        caption_height: int,
        item_bg_name: str,
    ) -> QWidget:
        holder = QWidget(self.mods_icons)
        holder.setObjectName("icon_card")
        holder.setProperty("mod_path", str(mod.path))
        holder.setStyleSheet(f"QWidget#icon_card {{ border: 1px solid #000000; background-color: {item_bg_name}; }}")
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(3, 3, 3, 3)
        holder_layout.setSpacing(4)

        image_container = QWidget(holder)
        image_container.setObjectName("preview_image_container")
        image_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        image_container.setFixedHeight(image_height)

        image_label = QLabel(image_container)
        image_label.setObjectName("preview_image_label")
        image_label.setAlignment(Qt.AlignCenter)
        image_label.setGeometry(0, 0, image_width - 8, image_height)
        image_label.setFixedHeight(image_height)
        placeholder = self.missing_preview_pixmap.scaled(
            QSize(max(1, image_width - 8), max(1, image_height)),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        image_label.setPixmap(placeholder)

        active_btn = QToolButton(image_container)
        active_btn.setObjectName("active_indicator_btn")
        active_btn.setCheckable(True)
        active_btn.setFixedSize(_ICON_ACTIVE_INDICATOR_SIZE, _ICON_ACTIVE_INDICATOR_SIZE)
        active_state = self._mod_active(mod)
        active_btn.setChecked(active_state)
        active_btn.setText("✓" if active_state else " ")
        active_btn.setToolTip("Toggle mod active state")
        active_btn.setAutoRaise(True)
        active_btn.setStyleSheet(
            "QToolButton { background: rgba(0,0,0,0.55); color: #f0f0f0; border: 1px solid #666; padding: 0px; }"
            "QToolButton:checked { color: #31d843; border-color: #31d843; }"
        )
        active_btn.move(6, 6)
        active_btn.raise_()
        active_btn.toggled.connect(lambda checked, p=mod.path, b=active_btn: self._on_icon_active_toggled(p, checked, b))

        prefix_badge = QLabel(image_container)
        prefix_badge.setObjectName("prefix_badge_label")
        prefix_value = self._mod_prefix_by_path.get(str(mod.path), "")
        prefix_badge.setStyleSheet(self._prefix_badge_stylesheet(prefix_value))
        prefix_badge.setText(prefix_value)
        prefix_badge.setVisible(bool(prefix_value))
        prefix_badge.adjustSize()
        prefix_badge.move(max(6, image_width - prefix_badge.width() - 10), max(6, image_height - prefix_badge.height() - 6))
        prefix_badge.raise_()

        name_label = ElidedLabel(parent=holder)
        name_label.setObjectName("icon_name_label")
        name_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        name_label.setFixedHeight(caption_height)
        name_label.set_full_text(mod.path.name)
        name_label.setStyleSheet("")

        holder_layout.addWidget(image_container)
        holder_layout.addWidget(name_label)
        return holder

    def _populate_mods_icons(self, mods: list[ModEntry]) -> None:
        self._icon_population_token += 1
        self._icon_detail_token += 1
        population_token = self._icon_population_token
        detail_token = self._icon_detail_token
        mods_list = list(mods)
        self.mods_icons.clear()
        self._icon_holder_by_path = {}
        if not mods_list:
            return

        image_width = 200
        image_height = int(image_width * 9 / 16)
        caption_height = self._icon_caption_height()
        row_height = self._icon_row_height(image_height)
        item_bg_name = self.palette().color(self.backgroundRole()).name()
        total = len(mods_list)
        cursor = {"value": 0}
        self._set_status_line3_progress(f"Loading icon cards... 0/{total}")

        def _populate_batch() -> None:
            if population_token != self._icon_population_token:
                return
            start = cursor["value"]
            end = min(total, start + _ICON_POPULATE_BATCH_SIZE)
            for idx in range(start, end):
                mod = mods_list[idx]
                item = QListWidgetItem()
                item.setData(RIGHT_PATH_ROLE, str(mod.path))
                item.setToolTip(str(mod.path))
                item.setSizeHint(QSize(image_width, row_height))
                self.mods_icons.addItem(item)
                holder = self._build_icon_card_holder(mod, image_width, image_height, caption_height, item_bg_name)
                self._icon_holder_by_path[str(mod.path)] = holder
                self.mods_icons.setItemWidget(item, holder)
            cursor["value"] = end
            if end == total or end % 200 == 0:
                self._set_status_line3_progress(f"Loading icon cards... {end}/{total}")
            if end < total:
                QTimer.singleShot(0, _populate_batch)
                return
            self._schedule_icon_grid_metrics_update(delay_ms=0)
            QTimer.singleShot(0, lambda: self._populate_icon_details(mods_list, detail_token))

        _populate_batch()

    def _populate_icon_details(self, mods: list[ModEntry], detail_token: int) -> None:
        total = len(mods)
        if total <= 0:
            return
        show_info = self.info_caption_checkbox.isChecked()
        cursor = {"value": 0}
        self._set_status_line3_progress(f"Loading icon previews... 0/{total}")

        def _details_batch() -> None:
            if detail_token != self._icon_detail_token:
                return
            start = cursor["value"]
            end = min(total, start + _ICON_DETAIL_BATCH_SIZE)
            for idx in range(start, end):
                mod = mods[idx]
                holder = self._icon_holder_by_path.get(str(mod.path))
                if holder is None:
                    continue
                self._set_icon_prefix_badge_for_mod(mod.path, self._mod_prefix_by_path.get(str(mod.path), ""))
                image_label = holder.findChild(QLabel, "preview_image_label")
                if image_label is not None:
                    target_size = QSize(max(1, image_label.width()), max(1, image_label.height()))
                    image_label.setPixmap(self._build_preview_pixmap(mod.path, target_size))
                if show_info:
                    name_label = holder.findChild(ElidedLabel, "icon_name_label")
                    if name_label is not None:
                        name_label.set_full_text(self._icon_caption(mod))
            cursor["value"] = end
            if end == total or end % 100 == 0:
                self._set_status_line3_progress(f"Loading icon previews... {end}/{total}")
            if end < total:
                QTimer.singleShot(0, _details_batch)
                return
            self._set_status_line3_progress(f"Loading icon previews done ({total}/{total})")

        _details_batch()

    def _on_icon_active_toggled(self, mod_path: Path, checked: bool, button: QToolButton) -> None:
        if self._beamng_mutation_blocked("change mod activation", show_dialog=True):
            previous = not bool(checked)
            button.blockSignals(True)
            button.setChecked(previous)
            button.blockSignals(False)
            button.setText("✓" if previous else " ")
            return
        action_word = "enable" if checked else "disable"
        if not self._confirm_action(
            f"{action_word.capitalize()} Mod",
            f"{action_word.capitalize()} '{mod_path.name}'?",
        ):
            previous = not bool(checked)
            button.blockSignals(True)
            button.setChecked(previous)
            button.blockSignals(False)
            button.setText("✓" if previous else " ")
            self._set_status_line3(f"{action_word.capitalize()} mod cancelled.")
            return
        button.setText("✓" if checked else " ")
        self._set_mod_active(mod_path, checked)

    def _icon_item_at_pos(self, pos) -> QListWidgetItem | None:
        return self.mods_icons.itemAt(pos)

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()
        menu_bar.setNativeMenuBar(False)
        menu_bar.setAutoFillBackground(True)
        menu_bar.setAttribute(Qt.WA_StyledBackground, True)
        menu_bar.setStyleSheet(
            "QMenuBar {"
            "  background-color: rgba(28, 28, 28, 255);"
            "  border: 0px;"
            "}"
            "QMenuBar::item {"
            "  background: transparent;"
            "  padding: 4px 10px;"
            "}"
            "QMenuBar::item:selected {"
            "  background-color: rgba(60, 60, 60, 255);"
            "}"
            "QMenu {"
            "  background-color: rgba(28, 28, 28, 255);"
            "  border: 1px solid rgba(80, 80, 80, 255);"
            "}"
        )

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
        self.db_path = Path(self.beam_mods_root) / "db.json" if self.beam_mods_root else Path()
        self.online_cache_max_mb, self.online_cache_ttl_hours = load_online_cache_preferences()
        self.online_cache_max_mb = max(64, min(_WEBENGINE_HTTP_CACHE_MAX_MB, int(self.online_cache_max_mb)))
        self._update_online_client_roots()
        if self.online_profile is not None:
            self.online_profile.setHttpCacheMaximumSize(_webengine_cache_bytes(self.online_cache_max_mb))
        if not self._settings_valid():
            self._open_settings(force=True)
            if not self._settings_valid():
                self._set_status(
                    "Active mods: 0 / Total mods: 0",
                    "Packs active: 0/0 | Loose: 0 | Repo: 0",
                    "Configure BeamNG Mod Folder and Library Root in Settings.",
                )
                return
        self._refresh_now_or_when_shown()

    def _settings_valid(self) -> bool:
        return bool(self.beam_mods_root and self.library_root and Path(self.beam_mods_root).is_dir() and Path(self.library_root).is_dir())

    def _open_settings(self, force: bool = False) -> None:
        while True:
            dlg = SettingsDialog(self)
            accepted = dlg.exec()
            self.beam_mods_root, self.library_root = load_settings()
            self.db_path = Path(self.beam_mods_root) / "db.json" if self.beam_mods_root else Path()
            self.online_cache_max_mb, self.online_cache_ttl_hours = load_online_cache_preferences()
            self.online_cache_max_mb = max(64, min(_WEBENGINE_HTTP_CACHE_MAX_MB, int(self.online_cache_max_mb)))
            self._update_online_client_roots()
            if self.online_profile is not None:
                self.online_profile.setHttpCacheMaximumSize(_webengine_cache_bytes(self.online_cache_max_mb))
            if self._settings_valid():
                self._refresh_now_or_when_shown()
                return
            if not force or accepted == 0:
                break
            self._show_silent_warning("Settings Required", "Both folders must be configured before scanning.")

    def _refresh_now_or_when_shown(self) -> None:
        if self.isVisible():
            self.full_refresh()
            return
        self._set_status_line3_progress("Preparing startup scan...")
        QTimer.singleShot(0, self.full_refresh)

    def _install_interaction_filters(self) -> None:
        widgets: list[QWidget] = []
        if hasattr(self, "main_tabs") and isinstance(self.main_tabs, QWidget):
            widgets.append(self.main_tabs)
        menu = self.menuBar()
        if menu is not None:
            widgets.append(menu)
        self._interaction_lock_widgets = widgets

    def _show_or_update_interaction_lock_popup(self) -> None:
        reason = self._interaction_lock_reason.strip() or "an operation is in progress"
        title = "Interaction Temporarily Disabled"
        text = "Interaction is currently disabled."
        info = f"Reason: {reason}\n\nProgress continues in the status box."
        box = self._interaction_lock_popup
        if box is None:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.NoIcon)
            box.setWindowTitle(title)
            box.setText(text)
            box.setInformativeText(info)
            box.setStandardButtons(QMessageBox.NoButton)
            box.setModal(False)
            box.setWindowModality(Qt.NonModal)
            box.show()
            self._interaction_lock_popup = box
            return
        box.setWindowTitle(title)
        box.setText(text)
        box.setInformativeText(info)
        if not box.isVisible():
            box.show()

    def _close_interaction_lock_popup(self) -> None:
        box = self._interaction_lock_popup
        if box is None:
            return
        box.close()
        box.deleteLater()
        self._interaction_lock_popup = None

    def _lock_interaction(self, reason: str) -> None:
        normalized_reason = str(reason or "").strip() or "an operation is in progress"
        self._interaction_lock_depth += 1
        self._interaction_lock_reason = normalized_reason
        if self._interaction_lock_depth > 1:
            self._show_or_update_interaction_lock_popup()
            return
        for widget in self._interaction_lock_widgets:
            widget.setEnabled(False)
        QApplication.setOverrideCursor(Qt.BusyCursor)
        self._show_or_update_interaction_lock_popup()

    def _unlock_interaction(self) -> None:
        if self._interaction_lock_depth <= 0:
            self._interaction_lock_depth = 0
            self._interaction_lock_reason = ""
            self._close_interaction_lock_popup()
            return
        self._interaction_lock_depth -= 1
        if self._interaction_lock_depth > 0:
            return
        self._interaction_lock_reason = ""
        for widget in self._interaction_lock_widgets:
            widget.setEnabled(True)
        self._close_interaction_lock_popup()
        QApplication.restoreOverrideCursor()

    def full_refresh(self) -> None:
        if not self._settings_valid():
            self._set_status(
                "Active mods: 0 / Total mods: 0",
                "Packs active: 0/0 | Loose: 0 | Repo: 0",
                "Invalid settings. Open File -> Settings...",
            )
            return

        self._lock_interaction("Scanning is in progress.")
        self._index_apply_context = "Scanning"
        self._index_worker_error_prefix = "Scan"
        self._capture_pending_left_selection()
        self._set_status_line3_progress("Scanning...")

        worker = FnWorker(
            lambda progress_emit: scanner.build_full_index(
                self.beam_mods_root,
                self.library_root,
                progress_cb=progress_emit,
            ),
            with_progress=True,
        )
        worker.signals.progress.connect(self._on_scan_progress)
        worker.signals.done.connect(self._on_index_worker_done)
        worker.signals.error.connect(self._on_index_worker_error)
        self._start_worker(worker)

    def _quick_refresh(self) -> None:
        if self.index is None:
            self.full_refresh()
            return
        self._lock_interaction("Refresh is in progress.")
        self._index_apply_context = "Refreshing"
        self._index_worker_error_prefix = "Refresh"
        self._capture_pending_left_selection()
        self._set_status_line3_progress("Refreshing pack and mod index...")
        worker = FnWorker(lambda: scanner.refresh_after_toggle(self.index))
        worker.signals.done.connect(self._on_index_worker_done)
        worker.signals.error.connect(self._on_index_worker_error)
        self._start_worker(worker)

    def _start_worker(self, worker: FnWorker) -> None:
        self._workers.add(worker)

        def _cleanup(*_args) -> None:
            self._workers.discard(worker)

        worker.signals.done.connect(_cleanup)
        worker.signals.error.connect(_cleanup)
        self.thread_pool.start(worker)

    def _on_scan_progress(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        message = str(payload.get("message") or "").strip()
        if not message:
            return
        current_raw = payload.get("current")
        total_raw = payload.get("total")
        try:
            current = int(current_raw) if current_raw is not None else None
        except (TypeError, ValueError):
            current = None
        try:
            total = int(total_raw) if total_raw is not None else None
        except (TypeError, ValueError):
            total = None
        if current is not None and total is not None and total > 0:
            self._set_status_line3_progress(f"{message}: {current}/{total}")
            return
        if current is not None:
            self._set_status_line3_progress(f"{message}: {current}")
            return
        self._set_status_line3_progress(message)

    def _on_index_worker_done(self, index: ScanIndex) -> None:
        try:
            self._apply_index(index)
        finally:
            self._unlock_interaction()

    def _on_index_worker_error(self, error_text: str) -> None:
        try:
            self._set_status_line3(f"{self._index_worker_error_prefix} error: {error_text}")
        finally:
            self._unlock_interaction()

    def _apply_index(self, index: ScanIndex) -> None:
        preferred_left_selection = self._pending_left_selection
        self._pending_left_selection = None
        context = self._index_apply_context
        self._index_apply_context = ""
        self.index = index
        self._flush_db_write_if_pending(show_progress=True)
        self._set_status_line3_progress("Loading and analyzing db.json active states...")
        self._load_active_states_from_db(show_progress=True)
        self._set_status_line3_progress("Refreshing pack and mod lists...")
        self._rebuild_known_mod_names()
        self._rebuild_left_tree()
        self._ensure_profiles_initialized()
        self._refresh_profile_combo()
        if self._startup_profile_apply_pending:
            self._startup_profile_apply_pending = False
            startup_profile = self._startup_last_profile_to_apply
            self._startup_last_profile_to_apply = None
            selected_profile = self._selected_profile_path()
            if (
                startup_profile is not None
                and selected_profile is not None
                and startup_profile == selected_profile
                and startup_profile.is_file()
                and not self._beamng_running
            ):
                self._set_status_line3_progress(f"Applying last selected profile '{startup_profile.stem}'...")
                self._load_profile_path(startup_profile, require_confirm=False)
                return
        self._update_profile_dirty_state()
        self.current_mod_path = None
        self._set_status_line3_progress("Queueing active-state sync to db.json...")
        self._persist_db_from_current_state(show_progress=False)
        if not self._restore_left_selection(preferred_left_selection):
            self._cancel_mod_population_jobs()
            self._mod_row_by_path = {}
            self._icon_holder_by_path = {}
            self.mods_table.clearContents()
            self.mods_table.setRowCount(0)
            self.mods_icons.clear()
            self.current_mod_entries = []
            self._update_summary_status()
        if not self._left_splitter_initialized and not self._left_splitter_user_resized:
            self._apply_initial_left_pane_width()
        if context:
            self._set_status_line3(f"{context} done")

    def _all_scanned_mod_entries(self) -> list[ModEntry]:
        if self.index is None:
            return []
        mods: list[ModEntry] = []
        mods.extend(self.index.loose_mods)
        mods.extend(self.index.repo_mods)
        for pack_mods in self.index.pack_mods.values():
            mods.extend(pack_mods)
        return mods

    def _is_repo_mod_path(self, mod_path: Path) -> bool:
        if not self.beam_mods_root:
            return False
        repo_root = Path(self.beam_mods_root) / "repo"
        try:
            resolved_mod = mod_path.resolve()
            resolved_repo = repo_root.resolve()
        except OSError:
            return False
        try:
            resolved_mod.relative_to(resolved_repo)
            return True
        except ValueError:
            return False

    def _load_active_states_from_db(self, show_progress: bool = False) -> None:
        if show_progress:
            self._set_status_line3_progress("Loading and analyzing db.json...")
        if self.index is None or not self.db_path:
            self.active_by_db_fullpath = {}
            if show_progress:
                self._set_status_line3_progress("No db.json found. Using in-memory active states.")
            return
        previous_map = dict(self.active_by_db_fullpath)
        payload = load_beam_db(self.db_path)
        active_map = extract_active_by_db_fullpath(payload)
        all_mods = self._all_scanned_mod_entries()
        total_mods = len(all_mods)
        started_at = time.monotonic()
        if show_progress and total_mods > 0:
            self._set_status_line3_progress(f"Loading and analyzing db.json... 0/{total_mods}")
        for index, mod in enumerate(all_mods, start=1):
            fp = mod_db_fullpath(self.index, mod)
            # Preserve in-memory states for mods without db rows (profile-driven states included).
            active_map.setdefault(fp, bool(previous_map.get(fp, True)))
            if not show_progress:
                continue
            if index == total_mods or index % 100 == 0:
                elapsed = max(0.001, time.monotonic() - started_at)
                rate = index / elapsed
                remaining = max(0, total_mods - index)
                eta_seconds = int(remaining / rate) if rate > 0 else 0
                self._set_status_line3_progress(
                    f"Loading and analyzing db.json... {index}/{total_mods} (ETA ~{eta_seconds}s)"
                )
        self.active_by_db_fullpath = active_map
        if show_progress:
            self._set_status_line3_progress(f"Loaded active states for {len(active_map)} db.json entries.")

    def _mod_active(self, mod: ModEntry) -> bool:
        if self.index is None:
            return True
        fp = mod_db_fullpath(self.index, mod)
        return bool(self.active_by_db_fullpath.get(fp, True))

    def _set_mod_active(self, mod_path: Path, active: bool) -> None:
        if self._beamng_mutation_blocked("change mod activation"):
            return
        if self.index is None:
            return
        target = self._all_mod_by_path.get(str(mod_path))
        if target is None:
            return
        fp = mod_db_fullpath(self.index, target)
        self.active_by_db_fullpath[fp] = bool(active)
        self._apply_mod_active_to_views(mod_path, active)
        self._queue_db_write(show_progress=False)
        self._update_profile_dirty_state()

    def _bulk_set_selected_mods_active(self, active: bool) -> None:
        if self._beamng_mutation_blocked("change selected mod activation", show_dialog=True):
            return
        if self.index is None:
            self._set_status_line3("No scan data available.")
            return
        mod_paths = self._selected_mod_paths()
        if not mod_paths:
            self._set_status_line3("No mods selected.")
            return

        selected_mods = [self._all_mod_by_path[str(path)] for path in mod_paths if str(path) in self._all_mod_by_path]
        if not selected_mods:
            self._set_status_line3("Selected mods are no longer available.")
            return

        to_change = [mod for mod in selected_mods if self._mod_active(mod) != active]
        action_word = "enable" if active else "disable"
        changed_word = "enabled" if active else "disabled"
        if not to_change:
            self._set_status_line3(f"No selected mods needed to be {changed_word}.")
            return

        question = (
            f"{action_word.capitalize()} {len(to_change)} selected mod(s)?\n"
            f"{len(selected_mods) - len(to_change)} already in target state."
        )
        if not self._confirm_action(f"{action_word.capitalize()} Selected Mods", question):
            self._set_status_line3(f"{action_word.capitalize()} selected mods cancelled.")
            return

        total = len(to_change)
        self._set_status_line3_progress(f"{action_word.capitalize()} selected mods... 0/{total}")
        changed = 0
        for index, mod in enumerate(to_change, start=1):
            fp = mod_db_fullpath(self.index, mod)
            previous_state = bool(self.active_by_db_fullpath.get(fp, not active))
            self.active_by_db_fullpath[fp] = bool(active)
            if previous_state != active:
                changed += 1
            self._apply_mod_active_to_views(mod.path, active)
            if index == total or index % 10 == 0:
                self._set_status_line3_progress(f"{action_word.capitalize()} selected mods... {index}/{total}")

        self._set_status_line3_progress("Queueing db.json active-state update...")
        self._queue_db_write(show_progress=True, delay_ms=80)
        self._update_profile_dirty_state()
        self._set_status_line3(f"{changed}/{total} selected mod(s) {changed_word}.")

    def _set_table_check_state_for_mod(self, mod_path: Path, active: bool) -> None:
        row = self._mod_row_by_path.get(str(mod_path))
        if row is None or row < 0 or row >= self.mods_table.rowCount():
            return
        cell = self.mods_table.item(row, 0)
        if cell is None:
            return
        desired = Qt.Checked if active else Qt.Unchecked
        if cell.checkState() == desired:
            return
        self._updating_mod_table = True
        try:
            cell.setCheckState(desired)
        finally:
            self._updating_mod_table = False

    def _set_icon_button_state_for_mod(self, mod_path: Path, active: bool) -> None:
        holder = self._icon_holder_by_path.get(str(mod_path))
        if holder is None:
            return
        button = holder.findChild(QToolButton, "active_indicator_btn")
        if button is None:
            return
        if button.isChecked() != bool(active):
            button.blockSignals(True)
            button.setChecked(bool(active))
            button.blockSignals(False)
        button.setText("✓" if active else " ")

    def _set_icon_prefix_badge_for_mod(self, mod_path: Path, prefix: str) -> None:
        holder = self._icon_holder_by_path.get(str(mod_path))
        if holder is None:
            return
        badge = holder.findChild(QLabel, "prefix_badge_label")
        image_label = holder.findChild(QLabel, "preview_image_label")
        if badge is None or image_label is None:
            return
        text = str(prefix).strip()
        badge.setStyleSheet(self._prefix_badge_stylesheet(text))
        badge.setText(text)
        badge.setVisible(bool(text))
        if not text:
            return
        badge.adjustSize()
        x = max(6, image_label.width() - badge.width() - 6)
        y = max(6, image_label.height() - badge.height() - 6)
        badge.move(x, y)
        badge.raise_()

    def _apply_mod_active_to_views(self, mod_path: Path, active: bool) -> None:
        self._set_table_check_state_for_mod(mod_path, active)
        self._set_icon_button_state_for_mod(mod_path, active)

    def _refresh_current_view_active_states(self, show_progress: bool = False, prefix: str = "Refreshing current view states") -> None:
        mods = list(self.current_mod_entries)
        total = len(mods)
        if total <= 0:
            return
        if show_progress:
            self._set_status_line3_progress(f"{prefix}... 0/{total}")
        for index, mod in enumerate(mods, start=1):
            active = self._mod_active(mod)
            self._apply_mod_active_to_views(mod.path, active)
            if show_progress and (index == total or index % 200 == 0):
                self._set_status_line3_progress(f"{prefix}... {index}/{total}")

    def _queue_db_write(self, show_progress: bool = False, delay_ms: int = _DB_WRITE_DEBOUNCE_MS) -> None:
        if self._beamng_running:
            return
        if self.index is None or not self._settings_valid() or not self.db_path:
            return
        self._db_write_pending = True
        if show_progress:
            self._db_write_show_progress = True
        if self._db_write_in_flight:
            return
        self._db_write_timer.start(max(0, int(delay_ms)))

    def _persist_db_from_current_state(self, show_progress: bool = False) -> None:
        delay = 80 if show_progress else _DB_WRITE_DEBOUNCE_MS
        self._queue_db_write(show_progress=show_progress, delay_ms=delay)

    def _flush_deferred_db_write(self) -> None:
        if self._db_write_in_flight or not self._db_write_pending:
            return
        if self._beamng_running or self.index is None or not self._settings_valid() or not self.db_path:
            self._db_write_pending = False
            self._db_write_show_progress = False
            return

        self._db_write_pending = False
        show_progress = bool(self._db_write_show_progress)
        self._db_write_show_progress = False
        self._db_write_in_flight = True
        self._db_write_generation += 1
        generation = self._db_write_generation
        self._db_write_in_flight_generation = generation
        index_snapshot = self.index
        db_path_snapshot = Path(self.db_path)
        active_snapshot = dict(self.active_by_db_fullpath)
        started_at = time.monotonic()
        if show_progress:
            self._set_status_line3_progress("Writing db.json active states... 0/?")

        def _worker_fn(progress_emit):
            def _on_progress(current: int, total: int) -> None:
                progress_emit((int(current), int(total)))

            payload = sync_db_from_index(
                index_snapshot,
                db_path_snapshot,
                active_snapshot,
                repo_mod_id_map=None,
                progress_cb=_on_progress,
            )
            return {"payload": payload}

        worker = FnWorker(_worker_fn, with_progress=True)
        worker.signals.progress.connect(
            lambda payload, gen=generation, visible=show_progress, started=started_at: self._on_db_write_progress(
                gen,
                payload,
                visible,
                started,
            )
        )
        worker.signals.done.connect(lambda result, gen=generation, visible=show_progress: self._on_db_write_done(gen, result, visible))
        worker.signals.error.connect(lambda error, gen=generation: self._on_db_write_error(gen, error))
        self._start_worker(worker)

    def _on_db_write_progress(self, generation: int, payload, show_progress: bool, started_at: float) -> None:
        if generation != self._db_write_in_flight_generation or not show_progress:
            return
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        try:
            current = int(payload[0])
            total = int(payload[1])
        except (TypeError, ValueError):
            return
        if total <= 0:
            self._set_status_line3_progress("Writing db.json active states... 0/0")
            return
        if current == total or current == 0 or current % 100 == 0:
            elapsed = max(0.001, time.monotonic() - started_at)
            rate = max(0.001, current / elapsed) if current > 0 else 0.0
            remaining = max(0, total - current)
            eta_seconds = int(remaining / rate) if rate > 0 else 0
            self._set_status_line3_progress(
                f"Writing db.json active states... {current}/{total} (ETA ~{eta_seconds}s)"
            )

    def _on_db_write_done(self, generation: int, result, show_progress: bool) -> None:
        if generation != self._db_write_in_flight_generation:
            return
        self._db_write_in_flight = False
        if isinstance(result, dict):
            payload = result.get("payload")
            if isinstance(payload, dict):
                db_active = extract_active_by_db_fullpath(payload)
                for fullpath, state in db_active.items():
                    if fullpath not in self.active_by_db_fullpath:
                        self.active_by_db_fullpath[fullpath] = bool(state)
        if show_progress:
            self._set_status_line3_progress("Writing db.json active states done")
        if self._db_write_pending:
            self._db_write_timer.start(50)

    def _on_db_write_error(self, generation: int, error_text: str) -> None:
        if generation != self._db_write_in_flight_generation:
            return
        self._db_write_in_flight = False
        self._set_status_line3(f"db.json write error: {error_text}")
        if self._db_write_pending:
            self._db_write_timer.start(250)

    def _flush_db_write_if_pending(self, show_progress: bool = False) -> None:
        if self._db_write_timer.isActive():
            self._db_write_timer.stop()
        deadline = time.monotonic() + _DB_WRITE_FLUSH_WAIT_SECONDS
        if self._db_write_in_flight and show_progress:
            self._set_status_line3_progress("Waiting for pending db.json write to finish...")
        while self._db_write_in_flight and time.monotonic() < deadline:
            QApplication.processEvents(QEventLoop.AllEvents, 50)
            time.sleep(0.01)
        if self._db_write_in_flight:
            self._set_status_line3("Pending db.json write is still running.")
            return
        if self._db_write_pending:
            self._db_write_pending = False
            self._db_write_show_progress = False
            self._persist_db_from_current_state_sync(show_progress=show_progress)

    def _persist_db_from_current_state_sync(self, show_progress: bool = False) -> None:
        if self._beamng_running:
            return
        if self.index is None or not self._settings_valid() or not self.db_path:
            return
        acquired_lock = False
        if show_progress and self._interaction_lock_depth <= 0:
            acquired_lock = True
            self._lock_interaction("db.json update is in progress.")
        progress_total = {"value": 0}
        started_at = time.monotonic()

        def _on_progress(current: int, total: int) -> None:
            progress_total["value"] = max(progress_total["value"], int(total))
            if not show_progress:
                return
            if total <= 0:
                self._set_status_line3_progress("Writing db.json active states... 0/0")
                return
            if current == total or current == 0 or current % 100 == 0:
                elapsed = max(0.001, time.monotonic() - started_at)
                rate = max(0.001, current / elapsed) if current > 0 else 0.0
                remaining = max(0, total - current)
                eta_seconds = int(remaining / rate) if rate > 0 else 0
                self._set_status_line3_progress(
                    f"Writing db.json active states... {current}/{total} (ETA ~{eta_seconds}s)"
                )

        if show_progress:
            self._set_status_line3_progress("Writing db.json active states... 0/?")
        try:
            previous_map = dict(self.active_by_db_fullpath)
            payload = sync_db_from_index(
                self.index,
                self.db_path,
                self.active_by_db_fullpath,
                repo_mod_id_map=None,
                progress_cb=_on_progress,
            )
            db_active_map = extract_active_by_db_fullpath(payload)
            for mod in self._all_scanned_mod_entries():
                fp = mod_db_fullpath(self.index, mod)
                if fp in previous_map:
                    db_active_map.setdefault(fp, bool(previous_map[fp]))
            self.active_by_db_fullpath = db_active_map
            if show_progress:
                total = progress_total["value"]
                if total > 0:
                    self._set_status_line3_progress(f"Writing db.json active states done ({total}/{total})")
                else:
                    self._set_status_line3_progress("Writing db.json active states done")
        finally:
            if acquired_lock:
                self._unlock_interaction()

    def _current_profile_snapshot(self) -> dict[str, object]:
        if self.index is None:
            return {"packs": {}, "mods": {}}
        return collect_profile_snapshot(self.index, self.active_by_db_fullpath)

    def _profiles_folder(self) -> Path:
        return profile_store.profiles_dir(self.project_root)

    def _ensure_profiles_initialized(self) -> None:
        if self.index is None:
            return
        profile_store.ensure_default_profile(self.project_root, self._current_profile_snapshot())

    def _profile_paths(self) -> list[Path]:
        return profile_store.list_profiles(self.project_root)

    def _refresh_profile_combo(self) -> None:
        paths = self._profile_paths()
        current = self.current_profile_path
        remembered_raw = str(self.settings_store.value("last_profile_path", "", str) or "").strip()
        remembered = Path(remembered_raw) if remembered_raw else None
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for path in paths:
            self.profile_combo.addItem(path.stem, str(path))
        self.profile_combo.blockSignals(False)
        if not paths:
            self.current_profile_path = None
            self.settings_store.setValue("last_profile_path", "")
            return
        target: Path | None = None
        if current is not None and current in paths:
            target = current
        elif remembered is not None and remembered in paths:
            target = remembered
        else:
            target = paths[0]

        target_index = 0
        for i in range(self.profile_combo.count()):
            candidate = Path(str(self.profile_combo.itemData(i)))
            if candidate == target:
                target_index = i
                break
        self.profile_combo.setCurrentIndex(target_index)
        self.current_profile_path = Path(str(self.profile_combo.itemData(target_index)))
        self.settings_store.setValue("last_profile_path", str(self.current_profile_path))
        loaded = profile_store.load_profile(self.current_profile_path)
        if loaded is not None:
            self.last_saved_profile_snapshot = {
                "packs": dict(loaded.get("packs", {})),
                "mods": dict(loaded.get("mods", {})),
            }

    def _selected_profile_path(self) -> Path | None:
        if self.profile_combo.count() == 0:
            return None
        raw = self.profile_combo.currentData()
        if not raw:
            return None
        return Path(str(raw))

    def _on_profile_combo_changed(self, _index: int) -> None:
        selected = self._selected_profile_path()
        if selected is None:
            self.current_profile_path = None
            self.settings_store.setValue("last_profile_path", "")
            return
        self.current_profile_path = selected
        self.settings_store.setValue("last_profile_path", str(selected))

    def _create_profile_from_current(self) -> None:
        if self._beamng_mutation_blocked("create profiles", show_dialog=True):
            return
        if self.index is None:
            self._set_status_line3("No scan data to create profile.")
            return
        raw_name, ok = QInputDialog.getText(self, "New Profile", "Profile name:")
        if not ok:
            return
        name = profile_store.sanitize_profile_name(raw_name)
        if not name:
            self._set_status_line3("Invalid profile name.")
            return
        if not self._confirm_action("Create Profile", f"Create profile '{name}' from current state?"):
            self._set_status_line3("Create profile cancelled.")
            return
        path = self._profiles_folder() / f"{name}.json"
        profile_store.save_profile(path, self._current_profile_snapshot(), profile_name=name)
        self.current_profile_path = path
        self.last_saved_profile_snapshot = self._current_profile_snapshot()
        self.profile_dirty = False
        self._refresh_profile_combo()
        self._set_status_line3(f"Created profile: {name}")

    def _save_selected_profile(self) -> None:
        if self._beamng_mutation_blocked("save profiles", show_dialog=True):
            return
        if self.index is None:
            self._set_status_line3("No scan data to save profile.")
            return
        path = self._selected_profile_path()
        if path is None:
            self._set_status_line3("No profile selected.")
            return
        if not self._confirm_action("Save Profile", f"Save current state to profile '{path.stem}'?"):
            self._set_status_line3("Save profile cancelled.")
            return
        self._save_profile_snapshot_to_path(path)
        self._set_status_line3(f"Saved profile: {path.stem}")

    def _load_selected_profile(self) -> None:
        path = self._selected_profile_path()
        if path is None:
            self._set_status_line3("No profile selected.")
            return
        self._load_profile_path(path, require_confirm=True)

    def _load_profile_path(self, path: Path, require_confirm: bool = True) -> None:
        if self._beamng_mutation_blocked("load profiles", show_dialog=True):
            return
        if self.index is None:
            self._set_status_line3("No scan data to load profile.")
            return
        if (
            self.current_profile_path is not None
            and path != self.current_profile_path
            and not self._prompt_save_profile_before_profile_change()
        ):
            return
        if require_confirm and not self._confirm_action(
            "Load Profile",
            f"Load profile '{path.stem}' and apply its pack/mod activation states?",
        ):
            self._set_status_line3("Load profile cancelled.")
            return
        self._flush_db_write_if_pending(show_progress=True)
        self._lock_interaction("Profile loading is in progress.")
        try:
            self._set_status_line3_progress(f"Loading profile '{path.stem}'...")
            profile = profile_store.load_profile(path)
            if profile is None:
                self._set_status_line3(f"Invalid profile file: {path.name}")
                return

            packs_cfg = profile.get("packs", {})
            mods_cfg = profile.get("mods", {})
            if not isinstance(packs_cfg, dict) or not isinstance(mods_cfg, dict):
                self._set_status_line3(f"Invalid profile content: {path.name}")
                return
            loaded_profile_snapshot = {
                "packs": {str(k): bool(v) for k, v in packs_cfg.items() if isinstance(k, str)},
                "mods": {str(k).strip(): bool(v) for k, v in mods_cfg.items() if isinstance(k, str) and str(k).strip()},
            }

            self._set_status_line3_progress("Loading db.json for comparison...")
            db_payload = load_beam_db(self.db_path) if self.db_path else {"header": {"version": 1.1}, "mods": {}}
            db_listed_packs = self._db_listed_pack_names(db_payload)

            self._set_status_line3_progress("Applying pack states from profile...")
            missing_packs: list[str] = []
            pack_action_failures: list[str] = []
            packs_items = [(pack_name, should_enable) for pack_name, should_enable in packs_cfg.items() if isinstance(pack_name, str)]
            total_pack_items = len(packs_items)
            for pack_index, (pack_name, should_enable) in enumerate(packs_items, start=1):
                if not isinstance(pack_name, str):
                    continue
                if pack_name not in self.index.packs:
                    missing_packs.append(pack_name)
                    if pack_index == total_pack_items or pack_index % 25 == 0:
                        self._set_status_line3_progress(
                            f"Applying pack states from profile... {pack_index}/{total_pack_items}"
                        )
                    continue
                currently_enabled = pack_name in self.index.active_packs
                if bool(should_enable) == currently_enabled:
                    if pack_index == total_pack_items or pack_index % 25 == 0:
                        self._set_status_line3_progress(
                            f"Applying pack states from profile... {pack_index}/{total_pack_items}"
                        )
                    continue
                if bool(should_enable):
                    ok, msg = enable_pack(pack_name, self.beam_mods_root, self.library_root)
                else:
                    ok, msg = disable_pack(pack_name, self.beam_mods_root, self.library_root)
                if not ok:
                    pack_action_failures.append(f"{pack_name}: {msg}")
                if pack_index == total_pack_items or pack_index % 25 == 0:
                    self._set_status_line3_progress(
                        f"Applying pack states from profile... {pack_index}/{total_pack_items}"
                    )

            for pack_name in self.index.packs:
                if pack_name in packs_cfg or pack_name in db_listed_packs:
                    continue
                if pack_name not in self.index.active_packs:
                    continue
                ok, msg = disable_pack(pack_name, self.beam_mods_root, self.library_root)
                if not ok:
                    pack_action_failures.append(f"{pack_name}: {msg}")

            self._set_status_line3_progress("Refreshing scan after pack changes...")
            def _profile_scan_progress(payload: dict[str, object]) -> None:
                if not isinstance(payload, dict):
                    return
                current = payload.get("current")
                total = payload.get("total")
                message = str(payload.get("message") or "Scanning").strip()
                prefixed: dict[str, object] = {"message": f"Profile refresh - {message}"}
                if current is not None:
                    prefixed["current"] = current
                if total is not None:
                    prefixed["total"] = total
                self._on_scan_progress(prefixed)

            refreshed = scanner.build_full_index(
                self.beam_mods_root,
                self.library_root,
                progress_cb=_profile_scan_progress,
            )
            self._index_apply_context = "Refreshing scan"
            self._apply_index(refreshed)

            self._set_status_line3_progress("Comparing profile states with db.json states...")
            available_mod_paths: set[str] = set()
            for mod in self._all_scanned_mod_entries():
                available_mod_paths.add(mod_db_fullpath(self.index, mod))

            profile_mod_states: dict[str, bool] = {}
            missing_mods: list[str] = []
            for raw_fp, raw_state in mods_cfg.items():
                if not isinstance(raw_fp, str):
                    continue
                fp = raw_fp.strip()
                if not fp:
                    continue
                if fp not in available_mod_paths:
                    missing_mods.append(fp)
                    continue
                profile_mod_states[fp] = bool(raw_state)

            db_active_map: dict[str, bool] = {}
            if self.db_path:
                db_active_map = extract_active_by_db_fullpath(db_payload)

            profile_effective_states, conflicts = self._effective_profile_states_and_conflicts(
                available_mod_paths,
                profile_mod_states,
                db_active_map,
            )

            # Default behavior is profile-winning unless the user explicitly chooses db.json.
            resolved_by_mod = dict(profile_effective_states)
            selected_source: dict[str, bool] = {fp: True for fp in profile_effective_states}
            cancelled_conflict_dialog = False
            if conflicts:
                self._set_status_line3_progress(
                    f"{len(conflicts)} active-state conflicts found. Waiting for Apply/Cancel..."
                )
                self._unlock_interaction()
                try:
                    dialog = ProfileDbConflictDialog(conflicts, self)
                    accepted = dialog.exec() == QDialog.Accepted
                finally:
                    self._lock_interaction("Profile loading is in progress.")
                if accepted:
                    self._set_status_line3_progress("Apply selected. Resolving conflicts...")
                    selected_source.update(dialog.selected_source_by_mod_fullpath())
                else:
                    cancelled_conflict_dialog = True
                    self._set_status_line3_progress("Cancel selected. Keeping db.json for conflicting mods...")
                    for fp, _profile_state, _db_state in conflicts:
                        selected_source[fp] = False

                for fp, profile_state, db_state in conflicts:
                    use_profile = bool(selected_source.get(fp, False))
                    resolved_by_mod[fp] = profile_state if use_profile else db_state

            self._set_status_line3_progress("Applying resolved mod active states...")
            resolved_items = list(resolved_by_mod.items())
            resolved_total = len(resolved_items)
            if resolved_total > 0:
                self._set_status_line3_progress(f"Applying resolved mod active states... 0/{resolved_total}")
            for resolved_index, (fp, resolved_state) in enumerate(resolved_items, start=1):
                self.active_by_db_fullpath[fp] = bool(resolved_state)
                if resolved_index == resolved_total or resolved_index % 200 == 0:
                    self._set_status_line3_progress(
                        f"Applying resolved mod active states... {resolved_index}/{resolved_total}"
                    )

            self._set_status_line3_progress("Writing active states to db.json...")
            self._persist_db_from_current_state(show_progress=True)
            self._set_status_line3_progress("Updating current view active states...")
            self._refresh_current_view_active_states(show_progress=True, prefix="Updating current view active states")

            self.current_profile_path = path
            self.last_saved_profile_snapshot = loaded_profile_snapshot
            self._refresh_profile_combo()
            self._update_profile_dirty_state()

            self._set_status_line3_progress("Profile load complete. Preparing summary...")
            info_lines: list[str] = []
            if missing_packs:
                info_lines.append(f"Missing packs: {', '.join(sorted(missing_packs))}")
            if pack_action_failures:
                info_lines.append(
                    f"Pack state changes failed: {', '.join(sorted(pack_action_failures)[:6])}"
                    + (" ..." if len(pack_action_failures) > 6 else "")
                )
            if missing_mods:
                sorted_missing_mods = sorted(missing_mods, key=str.lower)
                info_lines.append(
                    f"Missing mods: {', '.join(sorted_missing_mods[:8])}"
                    + (" ..." if len(sorted_missing_mods) > 8 else "")
                )
            if conflicts:
                profile_wins = sum(1 for fp, _profile_state, _db_state in conflicts if bool(selected_source.get(fp, False)))
                db_wins = len(conflicts) - profile_wins
                if cancelled_conflict_dialog:
                    info_lines.append(
                        f"Conflict resolution cancelled: db.json won for all {len(conflicts)} conflicting mods."
                    )
                else:
                    info_lines.append(
                        f"Resolved {len(conflicts)} active-state conflicts (Profile: {profile_wins}, db.json: {db_wins})."
                    )
            if info_lines:
                self._unlock_interaction()
                try:
                    self._show_silent_information("Profile Load Information", "\n".join(info_lines))
                finally:
                    self._lock_interaction("Profile loading is in progress.")
            self._set_status_line3(f"Loaded profile: {path.stem}")
        finally:
            self._unlock_interaction()

    def _effective_profile_states_and_conflicts(
        self,
        available_mod_paths: set[str],
        profile_mod_states: dict[str, bool],
        db_active_map: dict[str, bool],
    ) -> tuple[dict[str, bool], list[tuple[str, bool, bool]]]:
        profile_effective: dict[str, bool] = {}

        # Explicit profile entries always apply for scanned mods.
        for fp, profile_state in profile_mod_states.items():
            if fp in available_mod_paths:
                profile_effective[fp] = bool(profile_state)

        # If a scanned mod is missing from profile, assume profile wants it inactive.
        for fp in available_mod_paths:
            if fp in profile_effective:
                continue
            profile_effective[fp] = False

        conflicts: list[tuple[str, bool, bool]] = []
        for fp, profile_state in profile_effective.items():
            if fp not in db_active_map:
                continue
            db_state = bool(db_active_map[fp])
            if bool(profile_state) != db_state:
                conflicts.append((fp, bool(profile_state), db_state))
        conflicts.sort(key=lambda item: item[0].lower())
        return profile_effective, conflicts

    def _db_listed_pack_names(self, payload: dict[str, object]) -> set[str]:
        out: set[str] = set()
        mods_payload = payload.get("mods", {})
        if not isinstance(mods_payload, dict):
            return out
        for value in mods_payload.values():
            if not isinstance(value, dict):
                continue
            fullpath = str(value.get("fullpath") or "").strip().replace("\\", "/")
            dirname = str(value.get("dirname") or "").strip().replace("\\", "/")
            for raw in (fullpath, dirname):
                if not raw.startswith("/mods/"):
                    continue
                rel = raw[len("/mods/") :].strip("/")
                if not rel:
                    continue
                head = rel.split("/", 1)[0].strip()
                if not head or head.lower() == "repo":
                    continue
                if "/" not in rel and raw == fullpath and head.lower().endswith(".zip"):
                    continue
                out.add(head)
        return out

    def _update_profile_dirty_state(self) -> None:
        if self.last_saved_profile_snapshot is None or self.index is None:
            self.profile_dirty = False
            return
        self.profile_dirty = self._current_profile_snapshot() != self.last_saved_profile_snapshot

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
            self.status_line3_message = ""
            self._cancel_mod_population_jobs()
            self._mod_row_by_path = {}
            self._icon_holder_by_path = {}
            self.mods_table.clearContents()
            self.mods_table.setRowCount(0)
            self.mods_icons.clear()
            self.current_mod_entries = []
            self._update_summary_status()
            return
        if self.status_line3_message.endswith(" done"):
            self.status_line3_message = ""

        left_item = items[0]
        self._save_last_left_selection(left_item)
        mods = self._mods_for_left_item(left_item)
        self.current_mod_entries = sorted(mods, key=lambda m: m.path.name.lower())
        self._status_for_folder(left_item, len(mods))
        self._repopulate_current_mod_view()

    def _populate_mods_table(self, mods: list[ModEntry]) -> None:
        self._table_population_token += 1
        self._table_info_token += 1
        table_token = self._table_population_token
        info_token = self._table_info_token
        mods_list = list(mods)
        total = len(mods_list)
        self._mod_row_by_path = {}
        self._updating_mod_table = True
        try:
            self.mods_table.clearContents()
            self.mods_table.setRowCount(0)
        finally:
            self._updating_mod_table = False
        if total <= 0:
            return
        cursor = {"value": 0}
        self._set_status_line3_progress(f"Loading mod list... 0/{total}")

        def _populate_batch() -> None:
            if table_token != self._table_population_token:
                return
            start = cursor["value"]
            end = min(total, start + _TABLE_POPULATE_BATCH_SIZE)
            self._updating_mod_table = True
            try:
                self.mods_table.setRowCount(end)
                for row in range(start, end):
                    mod = mods_list[row]
                    mod_path_raw = str(mod.path)
                    self._mod_row_by_path[mod_path_raw] = row
                    prefix = self._mod_prefix_by_path.get(mod_path_raw, "")
                    name_cell = QTableWidgetItem(self._format_mod_name_with_prefix(mod.path.name, prefix))
                    name_cell.setFlags(name_cell.flags() | Qt.ItemIsUserCheckable)
                    name_cell.setCheckState(Qt.Checked if self._mod_active(mod) else Qt.Unchecked)
                    update_cell = QTableWidgetItem("")
                    update_cell.setTextAlignment(int(Qt.AlignCenter | Qt.AlignVCenter))
                    row_items = [
                        name_cell,
                        update_cell,
                        QTableWidgetItem(human_size(mod.size)),
                        QTableWidgetItem("..."),
                    ]
                    for col, cell in enumerate(row_items):
                        cell.setData(RIGHT_PATH_ROLE, mod_path_raw)
                        self.mods_table.setItem(row, col, cell)
            finally:
                self._updating_mod_table = False
            cursor["value"] = end
            if end == total or end % 250 == 0:
                self._set_status_line3_progress(f"Loading mod list... {end}/{total}")
            if end < total:
                QTimer.singleShot(0, _populate_batch)
                return
            self.mods_table.resizeColumnsToContents()
            self._probe_info_json_states(mods_list, table_token, info_token)

        _populate_batch()

    def _probe_info_json_states(self, mods: list[ModEntry], table_token: int, info_token: int) -> None:
        total = len(mods)
        if total <= 0:
            return
        self._set_status_line3_progress(f"Inspecting info.json presence... 0/{total}")

        def _worker_fn(progress_emit):
            batch: list[tuple[str, str, str, int, int]] = []
            for idx, mod in enumerate(mods, start=1):
                if info_token != self._table_info_token or table_token != self._table_population_token:
                    return total
                state = "Yes" if has_info_json(mod.path) else "No"
                prefix = ""
                if state == "Yes":
                    prefix = self._extract_prefix_value(parse_mod_info(mod.path))
                batch.append((str(mod.path), state, prefix, idx, total))
                if len(batch) >= _TABLE_INFO_BATCH_SIZE:
                    progress_emit(batch)
                    batch = []
            if batch:
                progress_emit(batch)
            return total

        worker = FnWorker(_worker_fn, with_progress=True)
        worker.signals.progress.connect(
            lambda payload, tt=table_token, it=info_token: self._on_info_json_probe_progress(tt, it, payload)
        )
        worker.signals.done.connect(lambda _value, tt=table_token, it=info_token: self._on_info_json_probe_done(tt, it))
        worker.signals.error.connect(lambda error, tt=table_token, it=info_token: self._on_info_json_probe_error(tt, it, error))
        self._start_worker(worker)

    def _on_info_json_probe_progress(self, table_token: int, info_token: int, payload) -> None:
        if table_token != self._table_population_token or info_token != self._table_info_token:
            return
        if not isinstance(payload, list):
            return
        last_index = 0
        total = 0
        self._updating_mod_table = True
        try:
            for item in payload:
                if not isinstance(item, tuple) or len(item) != 5:
                    continue
                path_raw, state, prefix, idx, total_count = item
                row = self._mod_row_by_path.get(str(path_raw))
                if row is None or row >= self.mods_table.rowCount():
                    continue
                mod_path = Path(str(path_raw))
                prefix_text = str(prefix).strip()
                self._mod_prefix_by_path[str(path_raw)] = prefix_text
                info_cell = self.mods_table.item(row, 3)
                if info_cell is None:
                    continue
                info_cell.setText(str(state))
                name_cell = self.mods_table.item(row, 0)
                if name_cell is not None:
                    name_cell.setText(self._format_mod_name_with_prefix(mod_path.name, prefix_text))
                self._set_icon_prefix_badge_for_mod(mod_path, prefix_text)
                last_index = max(last_index, int(idx))
                total = max(total, int(total_count))
        finally:
            self._updating_mod_table = False
        if last_index > 0 and total > 0 and (last_index == total or last_index % 200 == 0):
            self._set_status_line3_progress(f"Inspecting info.json presence... {last_index}/{total}")

    def _on_info_json_probe_done(self, table_token: int, info_token: int) -> None:
        if table_token != self._table_population_token or info_token != self._table_info_token:
            return
        total = len(self._mod_row_by_path)
        self._set_status_line3_progress(f"Inspecting info.json presence done ({total}/{total})")

    def _on_info_json_probe_error(self, table_token: int, info_token: int, error_text: str) -> None:
        if table_token != self._table_population_token or info_token != self._table_info_token:
            return
        self._set_status_line3(f"info.json inspection error: {error_text}")

    def _on_mod_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_mod_table:
            return
        if item.column() != 0:
            return
        if self._beamng_mutation_blocked("change mod activation", show_dialog=True):
            previous = Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
            self._updating_mod_table = True
            try:
                item.setCheckState(previous)
            finally:
                self._updating_mod_table = False
            return
        raw_path = str(item.data(RIGHT_PATH_ROLE) or "")
        if not raw_path:
            return
        next_active = item.checkState() == Qt.Checked
        action_word = "enable" if next_active else "disable"
        if not self._confirm_action(
            f"{action_word.capitalize()} Mod",
            f"{action_word.capitalize()} '{Path(raw_path).name}'?",
        ):
            previous = Qt.Unchecked if next_active else Qt.Checked
            self._updating_mod_table = True
            try:
                item.setCheckState(previous)
            finally:
                self._updating_mod_table = False
            self._set_status_line3(f"{action_word.capitalize()} mod cancelled.")
            return
        self._set_mod_active(Path(raw_path), next_active)

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
        self._local_status_lines = (line1, line2, line3)
        if self._is_online_tab_active():
            self._set_online_status()
            return
        self._render_status(line1, line2, line3)

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

    def _set_status_line3_progress(self, message: str) -> None:
        self.status_line3_message = message
        if self._is_online_tab_active():
            self._set_online_status()
        else:
            line1, line2, _line3 = self._local_status_lines
            self._render_status(line1, line2, message)
        QApplication.processEvents(
            QEventLoop.AllEvents,
            50,
        )

    def _on_confirm_actions_toggled(self, checked: bool) -> None:
        self.confirm_actions_enabled = bool(checked)
        self.settings_store.setValue("confirm_actions_enabled", self.confirm_actions_enabled)
        state = "enabled" if self.confirm_actions_enabled else "disabled"
        self._set_status_line3(f"Action confirmations {state}.")

    def _confirm_action(self, title: str, question: str, default_yes: bool = True) -> bool:
        if not self.confirm_actions_enabled:
            return True
        return self._ask_silent_yes_no(title, question, default_yes=default_yes)

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
            if self._beamng_mutation_blocked("enable packs", show_dialog=True):
                return
            if not self._confirm_action("Enable Pack", f"Enable pack '{pack_name}'?"):
                self._set_status_line3("Enable pack cancelled.")
                return
            ok, msg = enable_pack(pack_name, self.beam_mods_root, self.library_root)
            self._set_status_line3(msg)
            if ok:
                self._quick_refresh()
        elif selected == disable_action:
            if self._beamng_mutation_blocked("disable packs", show_dialog=True):
                return
            if not self._confirm_action("Disable Pack", f"Disable pack '{pack_name}'?"):
                self._set_status_line3("Disable pack cancelled.")
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
        if self._beamng_mutation_blocked("toggle pack state", show_dialog=True):
            return
        pack_name = item.data(0, LEFT_NAME_ROLE)
        active = bool(item.data(0, LEFT_ACTIVE_ROLE))
        if active:
            if not self._confirm_action("Disable Pack", f"Disable pack '{pack_name}'?"):
                self._set_status_line3("Disable pack cancelled.")
                return
        else:
            if not self._confirm_action("Enable Pack", f"Enable pack '{pack_name}'?"):
                self._set_status_line3("Enable pack cancelled.")
                return
        if active:
            ok, msg = disable_pack(pack_name, self.beam_mods_root, self.library_root)
        else:
            ok, msg = enable_pack(pack_name, self.beam_mods_root, self.library_root)
        self._set_status_line3(msg)
        if ok:
            self._quick_refresh()

    def _open_duplicates(self) -> None:
        if self.index is None:
            self._show_silent_information("Duplicates", "No scan data available yet.")
            return
        self._set_status_line3_progress("Analyzing duplicates...")
        dlg = DuplicatesDialog(self.index, self)
        dlg.exec()
        self._set_status_line3("Duplicate analysis closed.")

    def _selected_pack_name(self) -> str | None:
        items = self.left_tree.selectedItems()
        if not items:
            return None
        item = items[0]
        if item.data(0, LEFT_KIND_ROLE) != "pack":
            return None
        return item.data(0, LEFT_NAME_ROLE)

    def _create_pack_dialog(self) -> None:
        if self._beamng_mutation_blocked("create packs", show_dialog=True):
            return
        name, ok = QInputDialog.getText(self, "Create Pack", "Pack name:")
        if not ok:
            return
        pack_name = name.strip()
        if not pack_name:
            self._set_status_line3("Pack name cannot be empty.")
            return
        if not self._confirm_action("Create Pack", f"Create pack '{pack_name}'?"):
            self._set_status_line3("Create pack cancelled.")
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
        if self._beamng_mutation_blocked("rename packs", show_dialog=True):
            return
        new_name, ok = QInputDialog.getText(self, "Rename Pack", "New pack name:", text=old_name)
        if not ok:
            return
        cleaned_name = new_name.strip()
        if not self._confirm_action("Rename Pack", f"Rename pack '{old_name}' to '{cleaned_name}'?"):
            self._set_status_line3("Rename pack cancelled.")
            return
        done, msg = rename_pack(old_name, cleaned_name, self.beam_mods_root, self.library_root)
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
        if self._beamng_mutation_blocked("delete packs", show_dialog=True):
            return
        if not self._confirm_action("Delete Empty Pack", f"Delete empty pack '{pack_name}'?", default_yes=False):
            self._set_status_line3("Delete pack cancelled.")
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
        enable_selected_action = menu.addAction("Enable selected")
        disable_selected_action = menu.addAction("Disable selected")
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
        elif chosen == enable_selected_action:
            self._bulk_set_selected_mods_active(True)
        elif chosen == disable_selected_action:
            self._bulk_set_selected_mods_active(False)
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
        if self._beamng_mutation_blocked("move mods to packs", show_dialog=True):
            return
        if any(self._is_repo_mod_path(mod_path) for mod_path in mod_paths):
            self._set_status_line3("Moving mods to or from the repo folder is disabled.")
            return
        if not self._confirm_action(
            "Move Mods",
            f"Move {len(mod_paths)} selected mod(s) to '{target_pack}'?",
        ):
            self._set_status_line3("Move to pack cancelled.")
            return
        ok_count = 0
        fail_messages: list[str] = []
        total = len(mod_paths)
        if total > 0:
            self._set_status_line3_progress(f"Moving mods to '{target_pack}'... 0/{total}")
        for index, mod_path in enumerate(mod_paths, start=1):
            done, msg = move_mod_to_pack(mod_path, target_pack, self.library_root)
            if done:
                ok_count += 1
            else:
                fail_messages.append(msg)
            if index == total or index % 10 == 0:
                self._set_status_line3_progress(f"Moving mods to '{target_pack}'... {index}/{total}")
        if ok_count > 0:
            self.full_refresh()
        if fail_messages:
            self._set_status_line3(f"Transférés: {ok_count}/{len(mod_paths)} | Erreurs: {len(fail_messages)} | {fail_messages[0]}")
        else:
            self._set_status_line3(f"Transférés: {ok_count}/{len(mod_paths)} vers '{target_pack}'.")

    def _move_mods_to_root(self, mod_paths: list[Path]) -> None:
        if self._beamng_mutation_blocked("move mods to Mods root", show_dialog=True):
            return
        if any(self._is_repo_mod_path(mod_path) for mod_path in mod_paths):
            self._set_status_line3("Moving mods to or from the repo folder is disabled.")
            return
        if not self._confirm_action(
            "Move Mods",
            f"Move {len(mod_paths)} selected mod(s) to Mods root?",
        ):
            self._set_status_line3("Move to Mods root cancelled.")
            return
        ok_count = 0
        fail_messages: list[str] = []
        total = len(mod_paths)
        if total > 0:
            self._set_status_line3_progress(f"Moving mods to Mods root... 0/{total}")
        for index, mod_path in enumerate(mod_paths, start=1):
            done, msg = move_mod_to_mods_root(mod_path, self.beam_mods_root)
            if done:
                ok_count += 1
            else:
                fail_messages.append(msg)
            if index == total or index % 10 == 0:
                self._set_status_line3_progress(f"Moving mods to Mods root... {index}/{total}")
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

    # ------------------------
    # Online actions and hooks
    # ------------------------
    def _online_mutation_blocked(self) -> bool:
        return self._beamng_mutation_blocked("mutate online mods", show_dialog=True)

    def _start_online_task(self, busy_message: str, fn, done_handler, online_line2: str | None = None) -> None:
        if self.online_client is not None:
            self.online_client.clear_cancel_request()
        self._set_status_line3_progress(busy_message)
        if self.online_debug_enabled:
            self._emit_online_console_log(f"TASK start: {busy_message}")
        if online_line2 is not None:
            self._set_online_status_line2(online_line2)
        worker = FnWorker(fn)

        def _on_done(result) -> None:
            if online_line2 is not None:
                self._set_online_status_line2("")
            if self.online_debug_enabled:
                if isinstance(result, list):
                    summary = f"list(len={len(result)})"
                elif isinstance(result, tuple):
                    summary = f"tuple(len={len(result)})"
                elif hasattr(result, "ok"):
                    summary = f"ok={getattr(result, 'ok')}"
                else:
                    summary = type(result).__name__
                self._emit_online_console_log(f"TASK done: {busy_message} result={summary}")
            done_handler(result)

        def _on_error(error_text: str) -> None:
            if online_line2 is not None:
                self._set_online_status_line2("")
            if self.online_debug_enabled:
                self._emit_online_console_log(f"TASK error: {busy_message} error={error_text}")
            self._set_status_line3(f"Online action failed: {error_text}")

        worker.signals.done.connect(_on_done)
        worker.signals.error.connect(_on_error)
        self._start_worker(worker)

    def _current_online_url(self) -> str:
        if self.online_view is None:
            return ""
        return self.online_view.url().toString()

    def _handle_online_navigation(self, url: QUrl, _nav_type, _is_main_frame: bool) -> bool:
        if self.online_client is None:
            return False
        url_text = url.toString().strip()
        if not url_text:
            return False
        if self.online_debug_enabled:
            self._emit_online_console_log(f"WEBVIEW request: {url_text}")

        parsed_protocol = parse_beamng_protocol_uri(url_text)
        if parsed_protocol:
            command, _mod_id = parsed_protocol
            if self.online_debug_enabled:
                self._emit_online_console_log(f"WEBVIEW protocol forwarded: command={command}")
            opened = QDesktopServices.openUrl(url)
            if opened:
                self._set_status_line3("Forwarded beamng:v1 link to BeamNG.")
            else:
                self._set_status_line3("Could not forward beamng:v1 link to BeamNG.")
            return True

        if is_beamng_resource_download_url(url_text):
            if self.online_debug_enabled:
                self._emit_online_console_log(f"WEBVIEW download intercept: {url_text}")
            if self._online_mutation_blocked():
                return True
            destination = self._choose_online_download_destination()
            if destination is None:
                self._set_status_line3("Download cancelled.")
                return True
            destination_label, destination_path = destination

            def _download() -> object:
                assert self.online_client is not None
                return self.online_client.direct_download(url_text, destination_path, overwrite=False)

            self._start_online_task(
                f"Downloading to {destination_label}...",
                _download,
                lambda result: self._on_online_direct_download_done(result, destination_label, destination_path, url_text),
            )
            return True

        return False

    def _available_packs_for_download(self) -> list[str]:
        if self.index is not None:
            return sorted(self.index.packs, key=str.lower)
        library_root = Path(self.library_root)
        if not library_root.exists() or not library_root.is_dir():
            return []
        return sorted([p.name for p in library_root.iterdir() if p.is_dir()], key=str.lower)

    def _choose_online_download_destination(self) -> tuple[str, Path] | None:
        if not self._settings_valid():
            self._set_status_line3("Configure settings before online downloads.")
            return None
        mods_root = Path(self.beam_mods_root)
        choices: list[tuple[str, Path]] = [
            ("Mods folder", mods_root),
        ]
        for pack in self._available_packs_for_download():
            choices.append((f"Pack: {pack}", Path(self.library_root) / pack))

        labels = [label for label, _ in choices]
        selected, ok = QInputDialog.getItem(self, "Download Destination", "Choose destination:", labels, 0, False)
        if not ok or not selected:
            return None
        for label, path in choices:
            if label == selected:
                return label, path
        return None

    def _on_online_direct_download_done(self, result, destination_label: str, destination_path: Path, original_url: str) -> None:
        if not hasattr(result, "ok"):
            self._set_status_line3("Unexpected download response.")
            return
        if result.ok:
            self._set_status_line3(f"Downloaded to {destination_label}: {result.file_name}")
            self.full_refresh()
            return
        if isinstance(result.message, str) and "Destination already exists" in result.message:
            if self._confirm_action(
                "File Exists",
                f"{result.file_name} already exists in {destination_label}.\nReplace it?",
            ):

                def _download_overwrite() -> object:
                    assert self.online_client is not None
                    return self.online_client.direct_download(original_url, destination_path, overwrite=True)

                self._start_online_task(
                    f"Replacing {result.file_name} in {destination_label}...",
                    _download_overwrite,
                    lambda retry: self._on_online_direct_download_done(retry, destination_label, destination_path, original_url),
                )
                return
            self._set_status_line3("Replace download cancelled.")
            return
        self._set_status_line3(result.message)
