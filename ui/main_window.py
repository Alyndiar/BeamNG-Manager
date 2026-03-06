from __future__ import annotations

import ast
import hashlib
import itertools
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import (
    QByteArray,
    QBuffer,
    QDateTime,
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
from PySide6.QtGui import QAction, QColor, QDesktopServices, QDrag, QIcon, QImage, QImageWriter, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QCheckBox,
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
from core.modinfo import get_mod_info_cached, has_info_json
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
_TAGID_RESOLUTION_RETRY_SECONDS = 300


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
        self.repo_updates_by_mod_id: dict[str, dict[str, object]] = {}
        self.settings_store = QSettings("BeamNGManager", "ModPackManager")
        self.online_dark_mode_enabled = bool(self.settings_store.value("online_dark_mode", True, bool))
        self.online_debug_enabled = bool(self.settings_store.value("online_debug_enabled", False, bool))
        self.online_cache_max_mb, self.online_cache_ttl_hours = load_online_cache_preferences()
        self.online_cache_max_mb = max(64, min(_WEBENGINE_HTTP_CACHE_MAX_MB, int(self.online_cache_max_mb)))
        self.project_root = Path(__file__).resolve().parents[1]
        self.tagid_resource_tokens_cache_file = self.project_root / ".cache" / "online" / "tagid_resource_tokens.json"
        self.tagid_resource_tokens_cache: dict[str, list[str]] = {}
        self._tagid_resolution_pending: set[str] = set()
        self._tagid_resolution_retry_at: dict[str, int] = {}
        self._tagid_resolution_running = False
        self.db_path = Path()
        self.active_by_db_fullpath: dict[str, bool] = {}
        self._updating_mod_table = False
        self.current_profile_path: Path | None = None
        self.last_saved_profile_snapshot: dict[str, object] | None = None
        self.profile_dirty = False
        self._setting_splitter_sizes = False
        self._left_splitter_user_resized = False
        self._left_splitter_initialized = False
        self._pending_left_selection: tuple[str, str] | None = None

        self._init_preview_cache_storage()
        self._load_tagid_resource_tokens_cache()
        self._init_online_client()
        self._build_ui()
        self._build_menu()
        self.onlineConsoleLog.connect(self._append_online_console_line)
        self.onlineRequestErrorPrompt.connect(self._on_online_request_error_prompt)

        self._load_settings_and_maybe_scan()
        self._warn_if_beamng_running()

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

    def _load_tagid_resource_tokens_cache(self) -> None:
        path = self.tagid_resource_tokens_cache_file
        self.tagid_resource_tokens_cache = {}
        if not path.is_file():
            return
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(parsed, dict):
            return
        for key, value in parsed.items():
            tag_id = str(key).strip().lower()
            if not tag_id:
                continue
            if isinstance(value, list):
                tokens = [str(token).strip().lower() for token in value if str(token).strip()]
                self.tagid_resource_tokens_cache[tag_id] = sorted(set(tokens))

    def _save_tagid_resource_tokens_cache(self) -> None:
        path = self.tagid_resource_tokens_cache_file
        payload = {
            str(tag_id).strip().lower(): sorted(
                {str(token).strip().lower() for token in tokens if str(token).strip()}
            )
            for tag_id, tokens in self.tagid_resource_tokens_cache.items()
            if str(tag_id).strip()
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _enqueue_tagid_resolution(self, tag_ids: set[str]) -> None:
        normalized = {str(tag_id).strip().lower() for tag_id in tag_ids if str(tag_id).strip()}
        if not normalized:
            return
        self._tagid_resolution_pending.update(normalized)
        self._start_tagid_resolution_batch_if_needed()

    def _start_tagid_resolution_batch_if_needed(self) -> None:
        if self._tagid_resolution_running or not self._tagid_resolution_pending or self.online_client is None:
            return
        now = int(QDateTime.currentSecsSinceEpoch())
        due = [tag_id for tag_id in sorted(self._tagid_resolution_pending) if int(self._tagid_resolution_retry_at.get(tag_id, 0)) <= now]
        if not due:
            retry_times = [int(self._tagid_resolution_retry_at.get(tag_id, 0)) for tag_id in self._tagid_resolution_pending]
            retry_times = [retry_at for retry_at in retry_times if retry_at > now]
            if retry_times:
                delay_ms = max(250, (min(retry_times) - now) * 1000)
                QTimer.singleShot(int(delay_ms), self._start_tagid_resolution_batch_if_needed)
            return
        batch = due[:10]
        for tag_id in batch:
            self._tagid_resolution_pending.discard(tag_id)
        self._tagid_resolution_running = True

        def _resolve_batch() -> tuple[dict[str, list[str]], list[str]]:
            assert self.online_client is not None
            resolved: dict[str, list[str]] = {}
            failed: list[str] = []
            for tag_id in batch:
                tokens: set[str] = set()
                try:
                    meta = self.online_client.resolve_subscription(tag_id, "")
                except OSError:
                    failed.append(tag_id)
                    continue
                if isinstance(meta, dict):
                    tokens.update(self._resource_tokens_from_url(str(meta.get("resource_url") or "")))
                    tokens.update(self._resource_tokens_from_url(str(meta.get("download_url") or "")))
                resolved[tag_id] = sorted(tokens)
            return resolved, failed

        worker = FnWorker(_resolve_batch)

        def _on_done(resolved_map) -> None:
            self._tagid_resolution_running = False
            resolved_payload: dict[str, list[str]] = {}
            failed_tags: list[str] = []
            if (
                isinstance(resolved_map, tuple)
                and len(resolved_map) == 2
                and isinstance(resolved_map[0], dict)
                and isinstance(resolved_map[1], list)
            ):
                resolved_payload = resolved_map[0]
                failed_tags = [str(tag_id).strip().lower() for tag_id in resolved_map[1] if str(tag_id).strip()]
            elif isinstance(resolved_map, dict):
                resolved_payload = resolved_map

            now = int(QDateTime.currentSecsSinceEpoch())
            for tag_id, tokens in resolved_payload.items():
                key = str(tag_id).strip().lower()
                if not key:
                    continue
                token_list = tokens if isinstance(tokens, list) else []
                self.tagid_resource_tokens_cache[key] = sorted(
                    {str(token).strip().lower() for token in token_list if str(token).strip()}
                )
                self._tagid_resolution_retry_at.pop(key, None)
            if resolved_payload:
                self._save_tagid_resource_tokens_cache()
                self._refresh_online_installed_indicators()

            for failed_tag in failed_tags:
                if not failed_tag:
                    continue
                self._tagid_resolution_retry_at[failed_tag] = now + _TAGID_RESOLUTION_RETRY_SECONDS
                self._tagid_resolution_pending.add(failed_tag)
            self._start_tagid_resolution_batch_if_needed()

        def _on_error(_error_text: str) -> None:
            self._tagid_resolution_running = False
            retry_at = int(QDateTime.currentSecsSinceEpoch()) + _TAGID_RESOLUTION_RETRY_SECONDS
            for tag_id in batch:
                normalized = str(tag_id).strip().lower()
                if not normalized:
                    continue
                self._tagid_resolution_retry_at[normalized] = retry_at
                self._tagid_resolution_pending.add(normalized)
            self._start_tagid_resolution_batch_if_needed()

        worker.signals.done.connect(_on_done)
        worker.signals.error.connect(_on_error)
        self._start_worker(worker)

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

    def _warn_if_beamng_running(self) -> None:
        if not beamng_is_running():
            return
        self._set_status_line3("BeamNG is running. Online mutating actions are blocked until it closes.")
        QMessageBox.warning(
            self,
            "BeamNG Running",
            "BeamNG.drive.exe is currently running.\n\n"
            "Subscription/download/update/unsubscribe actions are blocked while BeamNG is open.",
        )

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

        self.status_box.horizontalScrollBar().rangeChanged.connect(self._update_status_box_height)
        self._update_status_box_height()

        self.local_tab = QWidget(self)
        local_layout = QVBoxLayout(self.local_tab)
        local_layout.setContentsMargins(0, 0, 0, 0)
        local_layout.addWidget(splitter)

        self.online_tab = self._build_online_tab()
        self.console_tab = self._build_console_tab()
        self.main_tabs = QTabWidget(self)
        self.main_tabs.addTab(self.local_tab, "Local")
        self.main_tabs.addTab(self.online_tab, "Online")
        if self.online_debug_enabled:
            self.main_tabs.addTab(self.console_tab, "Console")
        self.main_tabs.currentChanged.connect(self._on_main_tab_changed)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.addWidget(self.main_tabs)
        layout.addWidget(self.status_box)
        self.setCentralWidget(central)

        self._set_status("Active mods: 0 / Total mods: 0", "Packs active: 0/0 | Loose: 0 | Repo: 0", "")
        self._load_view_preferences()

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
        self.online_check_updates_btn = QPushButton("Check Updates", toolbar)
        self.online_update_all_btn = QPushButton("Update All", toolbar)
        self.online_subscriptions_btn = QPushButton("Subscriptions", toolbar)
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
            self.online_check_updates_btn,
            self.online_update_all_btn,
            self.online_subscriptions_btn,
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
        self.online_check_updates_btn.clicked.connect(self._online_check_updates)
        self.online_update_all_btn.clicked.connect(self._online_update_all)
        self.online_subscriptions_btn.clicked.connect(self._online_manage_subscriptions)
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
                self.online_check_updates_btn,
                self.online_update_all_btn,
                self.online_subscriptions_btn,
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
        if parse_beamng_protocol_uri(value) is not None:
            return value
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
        if parse_beamng_protocol_uri(target_text) is not None:
            self._handle_online_navigation(target_url, None, True)
            return
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
        box.setIcon(QMessageBox.Warning)
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

    def _resource_tokens_from_url(self, url_text: str) -> set[str]:
        value = str(url_text or "").strip()
        if not value:
            return set()
        try:
            parsed = urlparse(value)
            path = parsed.path or value
        except Exception:
            path = value
        if not path:
            return set()
        segments = [segment.strip() for segment in path.split("/") if segment.strip()]
        for idx, segment in enumerate(segments):
            if segment.lower() != "resources":
                continue
            if idx + 1 >= len(segments):
                return set()
            token = segments[idx + 1].strip().lower()
            if not token or token == "download":
                return set()
            out: set[str] = {token}
            if token.isdigit():
                out.add(token)
            else:
                match = re.search(r"\.(\d+)$", token)
                if match:
                    out.add(match.group(1))
            return out
        return set()

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

    def _online_installed_marker_sets(self) -> tuple[set[str], set[str]]:
        resource_tokens: set[str] = set()
        tag_ids: set[str] = set()

        if self.online_client is not None:
            for mod_id, entry in self.online_client.list_subscriptions():
                if not isinstance(entry, dict):
                    continue
                mod_id_text = str(mod_id or "").strip()
                if mod_id_text and not mod_id_text.lower().startswith("repo_file:"):
                    tag_ids.add(mod_id_text.lower())
                entry_mod_id = str(entry.get("mod_id") or "").strip()
                if entry_mod_id and not entry_mod_id.lower().startswith("repo_file:"):
                    tag_ids.add(entry_mod_id.lower())
                resource_tokens.update(self._resource_tokens_from_url(str(entry.get("resource_url") or "")))
                resource_tokens.update(self._resource_tokens_from_url(str(entry.get("download_url") or "")))

        if self.db_path:
            payload = load_beam_db(self.db_path)
            mods_payload = payload.get("mods", {})
            if isinstance(mods_payload, dict):
                for value in mods_payload.values():
                    if not isinstance(value, dict):
                        continue
                    tag_id = self._db_entry_tag_id(value)
                    if tag_id:
                        tag_ids.add(tag_id.lower())
                    mod_data = value.get("modData")
                    if isinstance(mod_data, dict):
                        resource_id = str(mod_data.get("resource_id") or mod_data.get("resourceId") or "").strip()
                        if resource_id:
                            resource_tokens.add(resource_id.lower())

        unresolved_tag_ids: set[str] = set()
        for tag_id in tag_ids:
            cached = self.tagid_resource_tokens_cache.get(tag_id, None)
            if cached is None:
                unresolved_tag_ids.add(tag_id)
                continue
            for token in cached:
                value = str(token).strip().lower()
                if value:
                    resource_tokens.add(value)

        if unresolved_tag_ids:
            self._enqueue_tagid_resolution(unresolved_tag_ids)

        return resource_tokens, tag_ids

    def _online_installed_indicator_script(self, installed_tokens: set[str], installed_tag_ids: set[str]) -> str:
        tokens_json = json.dumps(sorted({str(token).strip().lower() for token in installed_tokens if str(token).strip()}))
        tag_ids_json = json.dumps(sorted({str(tag_id).strip().lower() for tag_id in installed_tag_ids if str(tag_id).strip()}))
        return (
            "(() => {\n"
            f"  const installedTokens = new Set({tokens_json});\n"
            f"  const installedTagIds = new Set({tag_ids_json});\n"
            "  const styleId = 'beamng-manager-installed-style';\n"
            "  const markerAttr = 'data-beamng-manager-installed';\n"
            "  const cardAttr = 'data-beamng-manager-installed-card';\n"
            "  const pageBadgeId = 'beamng-manager-installed-page-badge';\n"
            "  const listCardSelector = '.resourceListItem, .structItem--resource, .structItem, .resourceRow';\n"
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
            "a[data-beamng-manager-installed=\"1\"] {\n"
            "  box-shadow: inset 0 0 0 2px rgba(255, 216, 77, 0.92) !important;\n"
            "  border-radius: 4px !important;\n"
            "}\n"
            "[data-beamng-manager-installed-card=\"1\"] {\n"
            "  position: relative !important;\n"
            "  box-shadow: inset 0 0 0 1px rgba(255, 216, 77, 0.75) !important;\n"
            "  border-radius: 6px !important;\n"
            "}\n"
            "#beamng-manager-installed-page-badge {\n"
            "  display: inline-block !important;\n"
            "  margin: 8px 0 10px 0 !important;\n"
            "  padding: 4px 10px !important;\n"
            "  border-radius: 999px !important;\n"
            "  background: rgba(255, 216, 77, 0.2) !important;\n"
            "  border: 1px solid rgba(255, 216, 77, 0.7) !important;\n"
            "  color: #ffd84d !important;\n"
            "  font-size: 12px !important;\n"
            "  font-weight: 700 !important;\n"
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
            "  const isInstalled = (href) => {\n"
            "    const raw = String(href || '').trim();\n"
            "    if (!raw) return false;\n"
            "    const protocolMatch = raw.match(/^beamng:v1\\/(?:subscriptionMod|showMod)\\/([A-Za-z0-9_]+)/i);\n"
            "    if (protocolMatch && protocolMatch[1] && installedTagIds.has(protocolMatch[1].toLowerCase())) return true;\n"
            "    const token = extractDirectResourceToken(raw);\n"
            "    if (!token) return false;\n"
            "    if (installedTokens.has(token)) return true;\n"
            "    const maybeId = token.match(/\\.([0-9]+)$/);\n"
            "    if (maybeId && installedTokens.has(maybeId[1])) return true;\n"
            "    return false;\n"
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
            "      return;\n"
            "    }\n"
            "    if (isInstalled(anchor.getAttribute('href'))) {\n"
            "      anchor.setAttribute(markerAttr, '1');\n"
            "      anchor.title = (anchor.title || '').includes('Installed on this PC')\n"
            "        ? anchor.title\n"
            "        : ((anchor.title ? `${anchor.title} | ` : '') + 'Installed on this PC');\n"
            "      return;\n"
            "    }\n"
            "    anchor.removeAttribute(markerAttr);\n"
            "  };\n"
            "  const syncPageBadge = () => {\n"
            "    const existing = document.getElementById(pageBadgeId);\n"
            "    if (existing) existing.remove();\n"
            "    if (!isInstalled(window.location.href)) return;\n"
            "    const title = document.querySelector('h1, .p-title-value, .resourceTitle, .PageTitle');\n"
            "    if (!title || !title.parentElement) return;\n"
            "    const badge = document.createElement('div');\n"
            "    badge.id = pageBadgeId;\n"
            "    badge.textContent = 'Installed on this PC';\n"
            "    title.parentElement.insertBefore(badge, title.nextSibling);\n"
            "  };\n"
            "  const syncCard = (card) => {\n"
            "    if (!(card instanceof Element)) return;\n"
            "    if (isResourceDetailPage) {\n"
            "      card.removeAttribute(cardAttr);\n"
            "      return;\n"
            "    }\n"
            "    const links = card.querySelectorAll('a[href]');\n"
            "    let installed = false;\n"
            "    for (const anchor of Array.from(links)) {\n"
            "      if (isInstalled(anchor.getAttribute('href'))) {\n"
            "        installed = true;\n"
            "        break;\n"
            "      }\n"
            "    }\n"
            "    if (installed) {\n"
            "      card.setAttribute(cardAttr, '1');\n"
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
        resource_tokens, tag_ids = self._online_installed_marker_sets()
        self.online_page.runJavaScript(self._online_installed_indicator_script(resource_tokens, tag_ids))

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
html[${{attrName}}="1"] :is(main, section, article, aside, nav, header, footer, div, form, fieldset, details, summary, ul, ol, li, table, tr, td, th, blockquote, pre, code) {{
  background-color: transparent !important;
  color: inherit !important;
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
            QTimer.singleShot(0, self._update_icon_grid_metrics)

    def closeEvent(self, event) -> None:
        if not self._prompt_save_profile_before_exit():
            event.ignore()
            return
        self._flush_online_error_waiters(default_continue=False)
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

    def _prompt_save_profile_before_exit(self) -> bool:
        if not self.profile_dirty:
            return True
        choice = QMessageBox.question(
            self,
            "Unsaved Profile Changes",
            "Packs/mod states changed since the last profile save.\nSave before exiting?",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if choice == QMessageBox.Cancel:
            return False
        if choice == QMessageBox.No:
            return True

        if self.index is None:
            return True
        path = self._selected_profile_path()
        if path is None:
            path = self._profiles_folder() / "default.json"
        profile_store.save_profile(path, self._current_profile_snapshot(), profile_name=path.stem)
        self.current_profile_path = path
        self.last_saved_profile_snapshot = self._current_profile_snapshot()
        self.profile_dirty = False
        return True

    def _on_splitter_moved(self, _pos: int, _index: int) -> None:
        if not self._setting_splitter_sizes:
            self._left_splitter_user_resized = True
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

    def _icon_caption_height(self) -> int:
        return max(18, int(self.mods_icons.fontMetrics().lineSpacing() + 2))

    def _icon_row_height(self, image_height: int) -> int:
        # holder margins (3+3) + layout spacing (4) + single-line caption height.
        return image_height + 10 + self._icon_caption_height()

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
                    mod_path = Path(str(holder.property("mod_path")))
                    image_label.setPixmap(self._build_preview_pixmap(mod_path, QSize(preview_width, image_height)))
                name_label = holder.findChild(ElidedLabel, "icon_name_label")
                if name_label is not None:
                    name_label.setFixedHeight(caption_height)
                update_badge = holder.findChild(QLabel, "update_indicator_label")
                if update_badge is not None:
                    update_badge.adjustSize()
                    update_badge.move(max(6, preview_width - update_badge.width() - 6), 6)
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
        repo_name_to_id = self._repo_mod_id_map_for_ui()
        image_width = 200
        image_height = int(image_width * 9 / 16)
        caption_height = self._icon_caption_height()
        row_height = self._icon_row_height(image_height)
        item_bg = self.palette().color(self.backgroundRole())
        for mod in mods:
            update_available = self._mod_update_available(mod, repo_name_to_id)
            item = QListWidgetItem()
            item.setData(RIGHT_PATH_ROLE, str(mod.path))
            item.setToolTip(str(mod.path))
            item.setSizeHint(QSize(image_width, row_height))
            self.mods_icons.addItem(item)

            holder = QWidget(self.mods_icons)
            holder.setObjectName("icon_card")
            holder.setProperty("mod_path", str(mod.path))
            holder.setStyleSheet(f"QWidget#icon_card {{ border: 1px solid #000000; background-color: {item_bg.name()}; }}")
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
            image_label.setPixmap(self._build_preview_pixmap(mod.path, QSize(image_width - 8, image_height)))

            update_badge = QLabel("★", image_container)
            update_badge.setObjectName("update_indicator_label")
            update_badge.setStyleSheet(
                "QLabel { background: rgba(0,0,0,0.55); color: #ffd84d; border: 1px solid #aa8d10; "
                "font-weight: 700; padding: 1px 4px; border-radius: 3px; }"
            )
            update_badge.setVisible(update_available)
            update_badge.adjustSize()
            update_badge.move(max(6, image_width - 8 - update_badge.width() - 6), 6)
            update_badge.raise_()

            active_btn = QToolButton(image_container)
            active_btn.setObjectName("active_indicator_btn")
            active_btn.setCheckable(True)
            active_state = self._mod_active(mod)
            active_btn.setChecked(active_state)
            active_btn.setText("✓" if active_state else "□")
            active_btn.setToolTip("Toggle mod active state")
            active_btn.setAutoRaise(True)
            active_btn.setStyleSheet(
                "QToolButton { background: rgba(0,0,0,0.55); color: #f0f0f0; border: 1px solid #666; padding: 2px; }"
                "QToolButton:checked { color: #31d843; border-color: #31d843; }"
            )
            active_btn.move(6, 6)
            active_btn.raise_()
            active_btn.toggled.connect(lambda checked, p=mod.path, b=active_btn: self._on_icon_active_toggled(p, checked, b))

            name_label = ElidedLabel(parent=holder)
            name_label.setObjectName("icon_name_label")
            name_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            name_label.setFixedHeight(caption_height)
            name_label.set_full_text(self._icon_caption(mod))
            name_label.setStyleSheet("")

            holder_layout.addWidget(image_container)
            holder_layout.addWidget(name_label)
            self.mods_icons.setItemWidget(item, holder)

    def _on_icon_active_toggled(self, mod_path: Path, checked: bool, button: QToolButton) -> None:
        button.setText("✓" if checked else "□")
        self._set_mod_active(mod_path, checked)

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
        self.full_refresh()

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

        self._capture_pending_left_selection()
        self._set_status_line3("Scanning...")

        worker = FnWorker(lambda: scanner.build_full_index(self.beam_mods_root, self.library_root))
        worker.signals.done.connect(self._apply_index)
        worker.signals.error.connect(lambda e: self._set_status_line3(f"Scan error: {e}"))
        self._start_worker(worker)

    def _quick_refresh(self) -> None:
        if self.index is None:
            self.full_refresh()
            return
        self._capture_pending_left_selection()
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
        preferred_left_selection = self._pending_left_selection
        self._pending_left_selection = None
        self.index = index
        self._load_active_states_from_db()
        self._rebuild_known_mod_names()
        self._rebuild_left_tree()
        self._ensure_profiles_initialized()
        self._refresh_profile_combo()
        self._update_profile_dirty_state()
        self.current_mod_path = None
        self._persist_db_from_current_state()
        if not self._restore_left_selection(preferred_left_selection):
            self.mods_table.setRowCount(0)
            self.mods_icons.clear()
            self.current_mod_entries = []
            self._update_summary_status()
        if not self._left_splitter_initialized and not self._left_splitter_user_resized:
            self._apply_initial_left_pane_width()

    def _repo_mod_id_map_from_subscriptions(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.online_client is not None:
            for mod_id, entry in self.online_client.load_subscriptions().items():
                name = str(entry.get("installed_name") or "").strip().lower()
                if name:
                    out[name] = mod_id
        if self.db_path:
            payload = load_beam_db(self.db_path)
            mods_payload = payload.get("mods", {})
            if isinstance(mods_payload, dict):
                for value in mods_payload.values():
                    if not isinstance(value, dict):
                        continue
                    mod_data = value.get("modData")
                    mod_id = str(value.get("modID") or value.get("tagid") or "").strip()
                    if not mod_id and isinstance(mod_data, dict):
                        mod_id = str(mod_data.get("tagid") or mod_data.get("modID") or "").strip()
                    if not mod_id:
                        continue
                    dirname = str(value.get("dirname") or "")
                    fullpath = str(value.get("fullpath") or "")
                    if not (dirname.startswith("/mods/repo/") or fullpath.startswith("/mods/repo/")):
                        continue
                    file_name = str(value.get("filename") or Path(fullpath).name).strip().lower()
                    if file_name:
                        out.setdefault(file_name, mod_id)
        return out

    def _repo_mod_id_map_for_ui(self) -> dict[str, str]:
        out = self._repo_mod_id_map_from_subscriptions()
        if self.online_client is None:
            return out
        for mod_id, entry in self.online_client.list_subscriptions():
            provider = str(entry.get("provider") or "beamng").strip().lower()
            if provider != "beamng":
                continue
            installed_name = str(entry.get("installed_name") or "").strip().lower()
            if installed_name:
                out.setdefault(installed_name, mod_id)
        return out

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

    def _mod_update_available(self, mod: ModEntry, repo_name_to_id: dict[str, str] | None = None) -> bool:
        if not self._is_repo_mod_path(mod.path):
            return False
        mod_name = mod.path.name.lower()
        name_to_id = repo_name_to_id if repo_name_to_id is not None else self._repo_mod_id_map_for_ui()
        mod_id = str(name_to_id.get(mod_name, "")).strip()
        return bool(mod_id and mod_id in self.repo_updates_by_mod_id)

    def _selected_repo_mod_ids(self, mod_paths: list[Path]) -> list[str]:
        repo_name_to_id = self._repo_mod_id_map_for_ui()
        selected_ids: list[str] = []
        seen: set[str] = set()
        for mod_path in mod_paths:
            if not self._is_repo_mod_path(mod_path):
                continue
            mod_id = str(repo_name_to_id.get(mod_path.name.lower(), "")).strip()
            if not mod_id or mod_id in seen:
                continue
            seen.add(mod_id)
            selected_ids.append(mod_id)
        return selected_ids

    def _all_scanned_mod_entries(self) -> list[ModEntry]:
        if self.index is None:
            return []
        mods: list[ModEntry] = []
        mods.extend(self.index.loose_mods)
        mods.extend(self.index.repo_mods)
        for pack_mods in self.index.pack_mods.values():
            mods.extend(pack_mods)
        return mods

    def _load_active_states_from_db(self) -> None:
        if self.index is None or not self.db_path:
            self.active_by_db_fullpath = {}
            return
        previous_map = dict(self.active_by_db_fullpath)
        payload = load_beam_db(self.db_path)
        active_map = extract_active_by_db_fullpath(payload)
        for mod in self.index.loose_mods:
            fp = mod_db_fullpath(self.index, mod)
            active_map.setdefault(fp, True)
        for mod in self.index.repo_mods:
            fp = mod_db_fullpath(self.index, mod)
            active_map.setdefault(fp, True)
        for pack_name, mod_list in self.index.pack_mods.items():
            for mod in mod_list:
                fp = mod_db_fullpath(self.index, mod)
                if pack_name in self.index.active_packs:
                    # Newly enabled packs default to active if db.json has no explicit row yet.
                    active_map.setdefault(fp, True)
                else:
                    # Keep in-memory state for inactive packs so profiles can still capture/apply them.
                    active_map.setdefault(fp, bool(previous_map.get(fp, True)))
        self.active_by_db_fullpath = active_map

    def _mod_active(self, mod: ModEntry) -> bool:
        if self.index is None:
            return True
        fp = mod_db_fullpath(self.index, mod)
        return bool(self.active_by_db_fullpath.get(fp, True))

    def _set_mod_active(self, mod_path: Path, active: bool) -> None:
        if self.index is None:
            return
        target = None
        for mod in self._all_scanned_mod_entries():
            if mod.path == mod_path:
                target = mod
                break
        if target is None:
            return
        fp = mod_db_fullpath(self.index, target)
        self.active_by_db_fullpath[fp] = bool(active)
        self._persist_db_from_current_state()
        self._update_profile_dirty_state()
        if self._is_icon_view_active():
            self._populate_mods_icons(self.current_mod_entries)

    def _persist_db_from_current_state(self) -> None:
        if self.index is None or not self._settings_valid() or not self.db_path:
            return
        previous_map = dict(self.active_by_db_fullpath)
        repo_map = self._repo_mod_id_map_from_subscriptions()
        payload = sync_db_from_index(
            self.index,
            self.db_path,
            self.active_by_db_fullpath,
            repo_mod_id_map=repo_map,
        )
        db_active_map = extract_active_by_db_fullpath(payload)
        for pack_name, mod_list in self.index.pack_mods.items():
            if pack_name in self.index.active_packs:
                continue
            for mod in mod_list:
                fp = mod_db_fullpath(self.index, mod)
                if fp in previous_map:
                    db_active_map.setdefault(fp, bool(previous_map[fp]))
        self.active_by_db_fullpath = db_active_map

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
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for path in paths:
            self.profile_combo.addItem(path.stem, str(path))
        self.profile_combo.blockSignals(False)
        if not paths:
            self.current_profile_path = None
            return
        if current is not None:
            for i in range(self.profile_combo.count()):
                if Path(str(self.profile_combo.itemData(i))) == current:
                    self.profile_combo.setCurrentIndex(i)
                    self.current_profile_path = current
                    return
        self.profile_combo.setCurrentIndex(0)
        self.current_profile_path = Path(str(self.profile_combo.currentData()))
        if self.current_profile_path:
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
            return
        self.current_profile_path = selected

    def _create_profile_from_current(self) -> None:
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
        path = self._profiles_folder() / f"{name}.json"
        profile_store.save_profile(path, self._current_profile_snapshot(), profile_name=name)
        self.current_profile_path = path
        self.last_saved_profile_snapshot = self._current_profile_snapshot()
        self.profile_dirty = False
        self._refresh_profile_combo()
        self._set_status_line3(f"Created profile: {name}")

    def _save_selected_profile(self) -> None:
        if self.index is None:
            self._set_status_line3("No scan data to save profile.")
            return
        path = self._selected_profile_path()
        if path is None:
            self._set_status_line3("No profile selected.")
            return
        profile_store.save_profile(path, self._current_profile_snapshot(), profile_name=path.stem)
        self.current_profile_path = path
        self.last_saved_profile_snapshot = self._current_profile_snapshot()
        self.profile_dirty = False
        self._set_status_line3(f"Saved profile: {path.stem}")

    def _load_selected_profile(self) -> None:
        path = self._selected_profile_path()
        if path is None:
            self._set_status_line3("No profile selected.")
            return
        self._load_profile_path(path)

    def _load_profile_path(self, path: Path) -> None:
        if self.index is None:
            self._set_status_line3("No scan data to load profile.")
            return
        profile = profile_store.load_profile(path)
        if profile is None:
            self._set_status_line3(f"Invalid profile file: {path.name}")
            return

        packs_cfg = profile.get("packs", {})
        mods_cfg = profile.get("mods", {})
        if not isinstance(packs_cfg, dict) or not isinstance(mods_cfg, dict):
            self._set_status_line3(f"Invalid profile content: {path.name}")
            return

        missing_packs: list[str] = []
        pack_action_failures: list[str] = []
        for pack_name, should_enable in packs_cfg.items():
            if not isinstance(pack_name, str):
                continue
            if pack_name not in self.index.packs:
                missing_packs.append(pack_name)
                continue
            currently_enabled = pack_name in self.index.active_packs
            if bool(should_enable) == currently_enabled:
                continue
            if bool(should_enable):
                ok, msg = enable_pack(pack_name, self.beam_mods_root, self.library_root)
            else:
                ok, msg = disable_pack(pack_name, self.beam_mods_root, self.library_root)
            if not ok:
                pack_action_failures.append(f"{pack_name}: {msg}")

        refreshed = scanner.build_full_index(self.beam_mods_root, self.library_root)
        self._apply_index(refreshed)

        available_mod_paths: set[str] = set()
        for mod in self._all_scanned_mod_entries():
            fp = mod_db_fullpath(self.index, mod)
            available_mod_paths.add(fp)
            if fp in mods_cfg:
                self.active_by_db_fullpath[fp] = bool(mods_cfg[fp])
        self._persist_db_from_current_state()
        self._repopulate_current_mod_view()

        missing_mods = sorted([str(fp) for fp in mods_cfg.keys() if fp not in available_mod_paths])

        current_snapshot = self._current_profile_snapshot()
        packs_unlisted = any(pack_name not in packs_cfg for pack_name in current_snapshot.get("packs", {}).keys())
        mods_unlisted = any(mod_path not in mods_cfg for mod_path in current_snapshot.get("mods", {}).keys())
        self.current_profile_path = path
        self.last_saved_profile_snapshot = {
            "packs": dict(packs_cfg),
            "mods": dict(mods_cfg),
        }
        self.profile_dirty = bool(packs_unlisted or mods_unlisted)
        self._refresh_profile_combo()

        info_lines: list[str] = []
        if missing_packs:
            info_lines.append(f"Missing packs: {', '.join(sorted(missing_packs))}")
        if pack_action_failures:
            info_lines.append(
                f"Pack state changes failed: {', '.join(sorted(pack_action_failures)[:6])}"
                + (" ..." if len(pack_action_failures) > 6 else "")
            )
        if missing_mods:
            info_lines.append(f"Missing mods: {', '.join(missing_mods[:8])}" + (" ..." if len(missing_mods) > 8 else ""))
        if packs_unlisted or mods_unlisted:
            info_lines.append("Profile ignored unlisted current packs/mods; profile is marked modified.")
        if info_lines:
            QMessageBox.information(self, "Profile Load Information", "\n".join(info_lines))
        self._set_status_line3(f"Loaded profile: {path.stem}")

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
            self.mods_table.setRowCount(0)
            self.mods_icons.clear()
            self.current_mod_entries = []
            self._update_summary_status()
            return

        left_item = items[0]
        self._save_last_left_selection(left_item)
        mods = self._mods_for_left_item(left_item)
        self.current_mod_entries = sorted(mods, key=lambda m: m.path.name.lower())
        self._repopulate_current_mod_view()
        self._status_for_folder(left_item, len(mods))

    def _populate_mods_table(self, mods: list[ModEntry]) -> None:
        self._updating_mod_table = True
        try:
            repo_name_to_id = self._repo_mod_id_map_for_ui()
            self.mods_table.setRowCount(len(mods))
            for row, mod in enumerate(mods):
                info_state = "Yes" if has_info_json(mod.path) else "No"
                update_available = self._mod_update_available(mod, repo_name_to_id)
                name_cell = QTableWidgetItem(f"{mod.path.name} ⭐" if update_available else mod.path.name)
                name_cell.setFlags(name_cell.flags() | Qt.ItemIsUserCheckable)
                name_cell.setCheckState(Qt.Checked if self._mod_active(mod) else Qt.Unchecked)
                update_cell = QTableWidgetItem("⭐" if update_available else "")
                update_cell.setTextAlignment(int(Qt.AlignCenter | Qt.AlignVCenter))
                if update_available:
                    update_cell.setForeground(QColor("#ffd84d"))
                row_items = [
                    name_cell,
                    update_cell,
                    QTableWidgetItem(human_size(mod.size)),
                    QTableWidgetItem(info_state),
                ]
                for col, cell in enumerate(row_items):
                    cell.setData(RIGHT_PATH_ROLE, str(mod.path))
                    self.mods_table.setItem(row, col, cell)
            self.mods_table.resizeColumnsToContents()
        finally:
            self._updating_mod_table = False

    def _on_mod_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_mod_table:
            return
        if item.column() != 0:
            return
        raw_path = str(item.data(RIGHT_PATH_ROLE) or "")
        if not raw_path:
            return
        self._set_mod_active(Path(raw_path), item.checkState() == Qt.Checked)

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
        update_selected_action = None
        if left_kind in {"mods_root", "pack"}:
            move_to_pack_action = menu.addAction("Move to pack...")
            move_to_root_action = menu.addAction("Move to Mods root")
            move_to_root_action.setEnabled(left_kind == "pack")
        if self.online_client is not None:
            update_selected_action = menu.addAction(
                "Update repo mod" if len(mod_paths) == 1 else "Update selected repo mods"
            )
            selected_repo_ids = self._selected_repo_mod_ids(mod_paths)
            pending_count = sum(1 for mod_id in selected_repo_ids if mod_id in self.repo_updates_by_mod_id)
            if pending_count > 0:
                update_selected_action.setText(
                    ("Update repo mod" if len(mod_paths) == 1 else "Update selected repo mods")
                    + f" ({pending_count} flagged)"
                )
            update_selected_action.setEnabled(bool(selected_repo_ids) and self._settings_valid())

        chosen = menu.exec(global_pos)
        if chosen == open_external_action:
            self._open_mod_externally(mod_paths[0])
        elif chosen == recheck_image_action:
            self._recheck_mod_images(mod_paths)
        elif update_selected_action is not None and chosen == update_selected_action:
            self._update_selected_repo_mods(mod_paths)
        elif move_to_pack_action is not None and chosen == move_to_pack_action:
            source_pack = left_item.data(0, LEFT_NAME_ROLE) if left_kind == "pack" else None
            self._move_selected_mod_to_pack(mod_paths, source_pack)
        elif move_to_root_action is not None and chosen == move_to_root_action:
            self._move_mods_to_root(mod_paths)

    def _update_selected_repo_mods(self, mod_paths: list[Path]) -> None:
        if self.online_client is None:
            self._set_status_line3("Online client is unavailable.")
            return
        if not self._settings_valid():
            self._set_status_line3("Configure valid BeamNG and Library folders before updating.")
            return
        mod_ids = self._selected_repo_mod_ids(mod_paths)
        if not mod_ids:
            self._set_status_line3("No selected repo mods with known subscription IDs.")
            return
        if self._online_mutation_blocked():
            return

        def _update_selected() -> object:
            assert self.online_client is not None
            return self.online_client.update_subscriptions(mod_ids=mod_ids, overwrite=True)

        count = len(mod_ids)
        self._start_online_task(
            f"Updating {count} selected repo mod(s)...",
            _update_selected,
            lambda outcome, ids=list(mod_ids): self._on_online_update_selected_done(outcome, ids),
            online_line2="Updating selected...",
        )

    def _on_online_update_selected_done(self, outcome, updated_mod_ids: list[str]) -> None:
        if not isinstance(outcome, tuple) or len(outcome) != 3:
            self._set_status_line3("Unexpected selected-update response.")
            return
        updated, failed, messages = outcome
        cancelled = any("Cancelled by user." in str(msg) for msg in (messages or []))
        if cancelled:
            self._set_status_line3(f"Selected update cancelled. Updated: {updated} | Failed: {failed}")
        else:
            self._set_status_line3(f"Selected update complete. Updated: {updated} | Failed: {failed}")
            for mod_id in updated_mod_ids:
                self.repo_updates_by_mod_id.pop(mod_id, None)
        if self.online_debug_enabled:
            self._emit_online_console_log(f"UPDATE_SELECTED summary: updated={updated} failed={failed}")
            if isinstance(messages, list):
                for msg in messages[:20]:
                    self._emit_online_console_log(f"UPDATE_SELECTED detail: {msg}")
        if failed and isinstance(messages, list) and messages:
            QMessageBox.warning(self, "Update Failures", "\n".join(str(m) for m in messages[:8]))
        if updated > 0:
            self.full_refresh()

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

    # ------------------------
    # Online actions and hooks
    # ------------------------
    def _online_mutation_blocked(self) -> bool:
        if not beamng_is_running():
            return False
        self._set_status_line3("BeamNG is running. Close BeamNG.drive.exe to mutate online mods.")
        QMessageBox.warning(
            self,
            "BeamNG Running",
            "BeamNG.drive.exe is currently running.\n"
            "Close the game before subscription/download/update/unsubscribe actions.",
        )
        return True

    def _start_online_task(self, busy_message: str, fn, done_handler, online_line2: str | None = None) -> None:
        if self.online_client is not None:
            self.online_client.clear_cancel_request()
        self._set_status_line3(busy_message)
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
            command, mod_id = parsed_protocol
            if self.online_debug_enabled:
                self._emit_online_console_log(f"WEBVIEW protocol intercept: command={command} mod_id={mod_id}")
            if command == "subscriptionMod":
                if self._online_mutation_blocked():
                    return True
                if not self._settings_valid():
                    self._set_status_line3("Configure valid BeamNG and Library folders before subscriptions.")
                    return True
                source_url = self._current_online_url()

                def _subscribe() -> object:
                    assert self.online_client is not None
                    return self.online_client.subscribe_from_protocol(mod_id, source_url, overwrite=True)

                self._start_online_task(
                    f"Subscribing {mod_id} into Repo...",
                    _subscribe,
                    lambda result: self._on_online_subscription_done(result, mod_id),
                )
                return True
            if command == "showMod":
                source_url = self._current_online_url()

                def _resolve_show() -> object:
                    assert self.online_client is not None
                    return self.online_client.resolve_subscription(mod_id, source_url)

                self._start_online_task(
                    f"Resolving in-game view for {mod_id}...",
                    _resolve_show,
                    lambda meta: self._on_online_showmod_resolved(meta, mod_id),
                )
                return True
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
        repo_root = mods_root / "repo"
        choices: list[tuple[str, Path]] = [
            ("Repo folder", repo_root),
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

    def _on_online_subscription_done(self, result, mod_id: str) -> None:
        if not hasattr(result, "ok"):
            self._set_status_line3(f"Unexpected subscription response for {mod_id}.")
            return
        if result.ok:
            self.repo_updates_by_mod_id.pop(mod_id, None)
            self._refresh_online_installed_indicators()
            self._set_status_line3(result.message)
            self.full_refresh()
            return
        self._set_status_line3(result.message)

    def _on_online_showmod_resolved(self, metadata, mod_id: str) -> None:
        if self.online_view is None:
            return
        resource_url = ""
        if isinstance(metadata, dict):
            resource_url = str(metadata.get("resource_url") or "")
        if not resource_url:
            resource_url = f"https://www.beamng.com/resources/?q={mod_id}"
        self.online_view.setUrl(QUrl(resource_url))
        self._set_status_line3(f"Opened resource page for {mod_id}.")

    def _on_online_direct_download_done(self, result, destination_label: str, destination_path: Path, original_url: str) -> None:
        if not hasattr(result, "ok"):
            self._set_status_line3("Unexpected download response.")
            return
        if result.ok:
            self._set_status_line3(f"Downloaded to {destination_label}: {result.file_name}")
            self.full_refresh()
            return
        if isinstance(result.message, str) and "Destination already exists" in result.message:
            if QMessageBox.question(
                self,
                "File Exists",
                f"{result.file_name} already exists in {destination_label}.\nReplace it?",
            ) == QMessageBox.Yes:

                def _download_overwrite() -> object:
                    assert self.online_client is not None
                    return self.online_client.direct_download(original_url, destination_path, overwrite=True)

                self._start_online_task(
                    f"Replacing {result.file_name} in {destination_label}...",
                    _download_overwrite,
                    lambda retry: self._on_online_direct_download_done(retry, destination_label, destination_path, original_url),
                )
                return
        self._set_status_line3(result.message)

    def _online_check_updates(self) -> None:
        if self.online_client is None:
            self._set_status_line3("Online client is unavailable.")
            return
        if not self._settings_valid():
            self._set_status_line3("Configure valid BeamNG and Library folders before checking updates.")
            return

        def _check() -> object:
            assert self.online_client is not None
            return self.online_client.check_updates()

        self._start_online_task(
            "Checking subscriptions for updates...",
            _check,
            self._on_online_check_updates_done,
            online_line2="Checking updates...",
        )

    def _on_online_check_updates_done(self, updates) -> None:
        if not isinstance(updates, list):
            self._set_status_line3("Unexpected update-check response.")
            return
        cancelled = any(isinstance(item, dict) and item.get("cancelled") for item in updates)
        available = [item for item in updates if isinstance(item, dict) and item.get("update_available")]
        self.repo_updates_by_mod_id = {
            str(item.get("mod_id") or "").strip(): dict(item)
            for item in available
            if str(item.get("mod_id") or "").strip()
        }
        self._repopulate_current_mod_view()
        failed = [item for item in updates if isinstance(item, dict) and not item.get("ok", True)]
        checked_count = len(updates)
        updates_count = len(available)
        failed_count = len(failed)
        if cancelled:
            self._set_status_line3(
                f"Update check cancelled. Updates available: {updates_count} | Errors: {failed_count} | Total: {checked_count}"
            )
        else:
            self._set_status_line3(
                f"Update check complete. Updates available: {updates_count} | Errors: {failed_count} | Total: {checked_count}"
            )
        if available:
            sample = ", ".join(str(item.get("mod_id")) for item in available[:5])
            message = (
                f"Checked: {checked_count} subscribed mod(s)\n"
                f"Updates found: {updates_count}\n"
                f"Errors: {failed_count}\n\n"
                f"Examples: {sample}"
            )
        else:
            message = (
                f"Checked: {checked_count} subscribed mod(s)\n"
                "Updates found: 0\n"
                f"Errors: {failed_count}"
            )
        QMessageBox.information(self, "Check Updates", message)
        if self.online_debug_enabled:
            self._emit_online_console_log(
                f"CHECK_UPDATES summary: checked={checked_count} updates={updates_count} errors={failed_count}"
            )
            for item in failed[:20]:
                mod_id = str(item.get("mod_id") or "")
                msg = str(item.get("message") or "")
                self._emit_online_console_log(f"CHECK_UPDATES error: mod_id={mod_id} message={msg}")

    def _online_update_all(self) -> None:
        if self.online_client is None:
            self._set_status_line3("Online client is unavailable.")
            return
        if not self._settings_valid():
            self._set_status_line3("Configure valid BeamNG and Library folders before updating.")
            return
        if self._online_mutation_blocked():
            return

        def _update() -> object:
            assert self.online_client is not None
            return self.online_client.update_all_subscriptions(overwrite=True)

        self._start_online_task(
            "Updating subscribed mods in Repo...",
            _update,
            self._on_online_update_all_done,
            online_line2="Updating all...",
        )

    def _on_online_update_all_done(self, outcome) -> None:
        if not isinstance(outcome, tuple) or len(outcome) != 3:
            self._set_status_line3("Unexpected update-all response.")
            return
        updated, failed, messages = outcome
        cancelled = any("Cancelled by user." in str(msg) for msg in (messages or []))
        if cancelled:
            self._set_status_line3(f"Update all cancelled. Updated: {updated} | Failed: {failed}")
        else:
            self.repo_updates_by_mod_id.clear()
            self._set_status_line3(f"Update all complete. Updated: {updated} | Failed: {failed}")
        if self.online_debug_enabled:
            self._emit_online_console_log(f"UPDATE_ALL summary: updated={updated} failed={failed}")
            if isinstance(messages, list):
                for msg in messages[:20]:
                    self._emit_online_console_log(f"UPDATE_ALL detail: {msg}")
        if failed and isinstance(messages, list) and messages:
            QMessageBox.warning(self, "Update Failures", "\n".join(str(m) for m in messages[:8]))
        self._refresh_online_installed_indicators()
        if updated > 0:
            self.full_refresh()

    def _online_manage_subscriptions(self) -> None:
        if self.online_client is None:
            self._set_status_line3("Online client is unavailable.")
            return
        if not self._settings_valid():
            self._set_status_line3("Configure valid BeamNG and Library folders before managing subscriptions.")
            return
        items = self.online_client.list_subscriptions()
        if not items:
            QMessageBox.information(self, "Subscriptions", "No subscriptions found.")
            return
        option_map: dict[str, str] = {}
        labels: list[str] = []
        for mod_id, entry in items:
            display_id = str(entry.get("display_id") or mod_id)
            name = str(entry.get("installed_name") or "")
            version = str(entry.get("version_id") or "-")
            label = f"{display_id} | {name} | v={version}"
            labels.append(label)
            option_map[label] = mod_id
        selected, ok = QInputDialog.getItem(
            self,
            "Subscriptions",
            "Select a subscription to unsubscribe:",
            labels,
            0,
            False,
        )
        if not ok or not selected:
            return
        mod_id = option_map.get(selected, "").strip()
        if not mod_id:
            return
        if self._online_mutation_blocked():
            return
        ok_done, msg = self.online_client.unsubscribe(mod_id, remove_file=True)
        self._set_status_line3(msg)
        if ok_done:
            self.repo_updates_by_mod_id.pop(mod_id, None)
            self._refresh_online_installed_indicators()
            self.full_refresh()
