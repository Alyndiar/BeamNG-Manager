from __future__ import annotations

import hashlib
import html
import json
import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QRunnable, QThreadPool, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QResizeEvent, QShowEvent
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.modinfo import InfoJsonAnalysisResult
from core.utils import app_root_dir


_MESSAGE_IMAGE_CACHE_MAX_WIDTH = 896
_MESSAGE_IMAGE_CACHE_MAX_HEIGHT = 504
_MESSAGE_IMAGE_CACHE_VERSION = "v2"
_MESSAGE_IMAGE_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
_MESSAGE_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".avif",
}
_WINDOWS_CERT_CONTEXT: ssl.SSLContext | None = None
_DISCARDED_MESSAGE_HOSTS = {"pp.userapi.com"}


class _WorkerSignals(QObject):
    done = Signal(object)
    error = Signal(str)


class _FnWorker(QRunnable):
    def __init__(self, fn) -> None:
        super().__init__()
        self.fn = fn
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            value = self.fn()
        except Exception as exc:  # pragma: no cover
            self.signals.error.emit(str(exc))
            return
        self.signals.done.emit(value)


def _extract_anchor_hrefs(message_html: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r'<a\b[^>]*\bhref="([^"]+)"[^>]*>', message_html, flags=re.IGNORECASE):
        href = html.unescape(match.group(1)).strip()
        if href and href not in seen:
            seen.add(href)
            urls.append(href)
    return urls


def _is_remote_message_link(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    return parsed.scheme.lower() in {"http", "https"} and host not in _DISCARDED_MESSAGE_HOSTS


def _message_image_cache_path(cache_dir: Path, source_key: str, url: str) -> Path:
    source_digest = hashlib.sha1(str(source_key).encode("utf-8")).hexdigest()
    url_digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return cache_dir / _MESSAGE_IMAGE_CACHE_VERSION / source_digest / f"{url_digest}.png"


def _gallery_tile_width_px(viewport_width: int, columns: int, spacing: int = 8) -> int:
    available_width = max(1, int(viewport_width))
    cols = max(1, int(columns))
    gap = max(0, int(spacing))
    total_gap = gap * max(0, cols - 1)
    return max(72, (available_width - total_gap) // cols)


def _gallery_tile_height_px(tile_width: int) -> int:
    return max(40, int(round(tile_width * _MESSAGE_IMAGE_CACHE_MAX_HEIGHT / _MESSAGE_IMAGE_CACHE_MAX_WIDTH)))


def _gallery_item_dimensions_px(cached_path: Path, tile_width: int, tile_height: int) -> tuple[int, int]:
    fallback_width = max(72, int(tile_width))
    fallback_height = max(40, int(tile_height))
    image = QImage(str(cached_path))
    if image.isNull() or image.width() <= 0 or image.height() <= 0:
        return fallback_width, fallback_height

    width_ratio = fallback_width / image.width()
    height_ratio = fallback_height / image.height()
    scale = min(width_ratio, height_ratio, 1.0)
    fitted_width = max(1, int(round(image.width() * scale)))
    fitted_height = max(1, int(round(image.height() * scale)))
    return fitted_width, fitted_height


def _is_gallery_separator(fragment: str) -> bool:
    stripped = re.sub(r"<br\s*/?>", "", fragment, flags=re.IGNORECASE)
    stripped = stripped.replace("&nbsp;", "").strip()
    return stripped == ""


def _render_gallery_item(
    href: str,
    cached_path: Path,
    box_width: int,
    box_height: int,
    display_width: int,
    display_height: int,
) -> str:
    file_url = QUrl.fromLocalFile(str(cached_path)).toString()
    return (
        '<table cellspacing="0" cellpadding="0" border="0">'
        "<tbody><tr>"
        f'<td width="{box_width}" height="{box_height}" align="center" valign="middle">'
        f'<a href="{html.escape(href, quote=True)}">'
        f'<img src="{html.escape(file_url, quote=True)}" alt="Linked image preview" '
        f'width="{display_width}" height="{display_height}"/>'
        "</a>"
        "</td>"
        "</tr></tbody></table>"
    )


def _render_gallery_table(items: list[str], columns: int, tile_width: int, spacing: int = 8) -> str:
    if not items:
        return ""
    rows: list[str] = []
    cell_style = f"padding:0 {spacing}px {spacing}px 0; width:{tile_width}px;"
    for start in range(0, len(items), max(1, int(columns))):
        row_items = items[start : start + max(1, int(columns))]
        cells = "".join(f'<td style="{cell_style}" valign="top">{item}</td>' for item in row_items)
        rows.append(f"<tr>{cells}</tr>")
    return '<table cellspacing="0" cellpadding="0" border="0"><tbody>' + "".join(rows) + "</tbody></table>"


def _inject_cached_image_previews(
    message_html: str,
    cached_paths: dict[str, Path],
    columns: int = 4,
    viewport_width: int = 960,
) -> str:
    if not cached_paths:
        return message_html

    tile_width = _gallery_tile_width_px(viewport_width, columns)
    tile_height = _gallery_tile_height_px(tile_width)
    anchor_pattern = re.compile(r'(<a\b[^>]*\bhref="([^"]+)"[^>]*>.*?</a>)', flags=re.IGNORECASE | re.DOTALL)
    parts: list[str] = []
    gallery_items: list[str] = []
    last_end = 0

    def _flush_gallery() -> None:
        nonlocal gallery_items
        if not gallery_items:
            return
        parts.append(_render_gallery_table(gallery_items, columns, tile_width))
        gallery_items = []

    for match in anchor_pattern.finditer(message_html):
        anchor_html = match.group(1)
        href = html.unescape(match.group(2)).strip()
        between = message_html[last_end : match.start()]
        cached = cached_paths.get(href)
        if cached is None or not cached.is_file():
            _flush_gallery()
            parts.append(between)
            parts.append(anchor_html)
            last_end = match.end()
            continue
        if between:
            if gallery_items and _is_gallery_separator(between):
                pass
            else:
                _flush_gallery()
                parts.append(between)
        display_width, display_height = _gallery_item_dimensions_px(cached, tile_width, tile_height)
        gallery_items.append(_render_gallery_item(href, cached, tile_width, tile_height, display_width, display_height))
        last_end = match.end()

    trailing = message_html[last_end:]
    _flush_gallery()
    parts.append(trailing)
    return "".join(parts)


def _download_remote_image(url: str) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": "BeamNG-Manager/1.0"})
    contexts: list[ssl.SSLContext | None] = [None]
    if hasattr(ssl, "enum_certificates"):
        contexts.append(_windows_cert_store_context())
    should_try_qt_fallback = False
    for index, context in enumerate(contexts):
        try:
            open_kwargs = {"timeout": 12}
            if context is not None:
                open_kwargs["context"] = context
            with urllib.request.urlopen(request, **open_kwargs) as response:
                content_type = str(response.headers.get("Content-Type", "") or "").strip().lower()
                if content_type and not content_type.startswith("image/"):
                    return None
                content_length = response.headers.get("Content-Length", "").strip()
                if content_length.isdigit() and int(content_length) > _MESSAGE_IMAGE_MAX_DOWNLOAD_BYTES:
                    return None
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MESSAGE_IMAGE_MAX_DOWNLOAD_BYTES:
                        return None
                    chunks.append(chunk)
            return b"".join(chunks)
        except (ValueError, OSError, urllib.error.URLError) as exc:
            if index + 1 < len(contexts) and _should_retry_with_windows_cert_context(exc):
                should_try_qt_fallback = True
                continue
            if _should_retry_with_windows_cert_context(exc):
                should_try_qt_fallback = True
                break
            return None
    if should_try_qt_fallback:
        return _download_remote_image_qt(url)
    return None


def _should_retry_with_windows_cert_context(exc: Exception) -> bool:
    reason = exc
    if isinstance(exc, urllib.error.URLError) and exc.reason is not None:
        reason = exc.reason
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    return "CERTIFICATE_VERIFY_FAILED" in str(reason).upper()


def _windows_cert_store_context() -> ssl.SSLContext | None:
    global _WINDOWS_CERT_CONTEXT
    if _WINDOWS_CERT_CONTEXT is not None:
        return _WINDOWS_CERT_CONTEXT

    context = ssl.create_default_context()
    loaded_any = False
    for store_name in ("ROOT", "CA"):
        try:
            certs = ssl.enum_certificates(store_name)
        except (AttributeError, OSError, ssl.SSLError):
            continue
        for cert_bytes, encoding_type, _trust in certs:
            if encoding_type != "x509_asn":
                continue
            try:
                context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(cert_bytes))
                loaded_any = True
            except ssl.SSLError:
                continue
    _WINDOWS_CERT_CONTEXT = context if loaded_any else None
    return _WINDOWS_CERT_CONTEXT


def _download_remote_image_qt(url: str) -> bytes | None:
    if QCoreApplication.instance() is None:
        return None
    manager = QNetworkAccessManager()
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"User-Agent", b"BeamNG-Manager/1.0")
    reply = manager.get(request)
    loop = QEventLoop()
    timeout = QTimer()
    timeout.setSingleShot(True)
    timed_out = {"value": False}

    def _on_timeout() -> None:
        timed_out["value"] = True
        reply.abort()
        loop.quit()

    timeout.timeout.connect(_on_timeout)
    reply.finished.connect(loop.quit)
    timeout.start(12000)
    loop.exec()
    timeout.stop()

    if timed_out["value"]:
        reply.deleteLater()
        manager.deleteLater()
        return None
    if reply.error() != QNetworkReply.NoError:
        reply.deleteLater()
        manager.deleteLater()
        return None

    content_type = str(reply.header(QNetworkRequest.ContentTypeHeader) or "").strip().lower()
    if content_type and not content_type.startswith("image/"):
        reply.deleteLater()
        manager.deleteLater()
        return None
    content_length = reply.header(QNetworkRequest.ContentLengthHeader)
    if isinstance(content_length, int) and content_length > _MESSAGE_IMAGE_MAX_DOWNLOAD_BYTES:
        reply.deleteLater()
        manager.deleteLater()
        return None
    data = bytes(reply.readAll())
    reply.deleteLater()
    manager.deleteLater()
    if not data or len(data) > _MESSAGE_IMAGE_MAX_DOWNLOAD_BYTES:
        return None
    return data


def _write_message_image_cache(cache_path: Path, raw_bytes: bytes) -> bool:
    source = QImage()
    if not source.loadFromData(raw_bytes):
        return False

    target = source
    if source.width() > _MESSAGE_IMAGE_CACHE_MAX_WIDTH or source.height() > _MESSAGE_IMAGE_CACHE_MAX_HEIGHT:
        target = source.scaled(
            _MESSAGE_IMAGE_CACHE_MAX_WIDTH,
            _MESSAGE_IMAGE_CACHE_MAX_HEIGHT,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return target.save(str(cache_path), "PNG")


class InfoJsonViewerDialog(QDialog):
    def __init__(
        self,
        mod_display_name: str,
        mod_path: Path,
        analysis: InfoJsonAnalysisResult,
        image_columns: int = 4,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._analysis = analysis
        self._mod_path = mod_path
        self._message_image_cache_key = str(mod_path)
        self._thread_pool = QThreadPool.globalInstance()
        self._content_splitter: QSplitter | None = None
        self._base_message_html = ""
        self._cached_message_image_paths: dict[str, Path] = {}
        self._pending_message_image_urls: set[str] = set()
        self._image_fetch_workers: set[_FnWorker] = set()
        self._message_image_columns = max(1, int(image_columns))
        self._last_gallery_layout: tuple[int, int] | None = None
        self._initial_geometry_applied = False
        self._gallery_refresh_timer = QTimer(self)
        self._gallery_refresh_timer.setSingleShot(True)
        self._gallery_refresh_timer.timeout.connect(self._refresh_message_html)

        title_suffix = mod_display_name.strip() if mod_display_name else mod_path.name
        self.setWindowTitle(f"info.json - {title_suffix}" if title_suffix else "info.json")
        self.resize(980, 680)
        self.setWindowModality(Qt.NonModal)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)

        self._message_image_cache_dir = app_root_dir() / ".cache" / "message_image_cache"

        layout = QVBoxLayout(self)
        content_splitter = QSplitter(Qt.Vertical, self)
        content_splitter.setChildrenCollapsible(False)
        content_splitter.splitterMoved.connect(lambda *_args: self._schedule_message_gallery_refresh())
        self._content_splitter = content_splitter

        self.message_label = QLabel("Message", self)
        self.message_view = QTextBrowser(self)
        self.message_view.setReadOnly(True)
        self.message_view.setOpenLinks(False)
        self.message_view.setOpenExternalLinks(False)
        self.message_view.anchorClicked.connect(self._on_message_link_clicked)
        self.message_view.viewport().installEventFilter(self)

        message_text = (analysis.message_clean or "").strip()
        message_html = (analysis.message_html or "").strip()
        self._base_message_html = message_html
        if message_text:
            if message_html:
                self._set_message_html(message_html)
            else:
                self.message_view.setPlainText(message_text)

            message_panel = QWidget(self)
            message_layout = QVBoxLayout(message_panel)
            message_layout.setContentsMargins(0, 0, 0, 0)
            message_layout.addWidget(self.message_label)
            message_layout.addWidget(self.message_view, 1)
            content_splitter.addWidget(message_panel)

        controls = QHBoxLayout()
        self.expand_all_btn = QPushButton("Expand All", self)
        self.collapse_all_btn = QPushButton("Collapse All", self)
        self.expand_top_btn = QPushButton("Expand Top Level", self)
        self.copy_json_btn = QPushButton("Copy JSON", self)
        self.copy_message_btn = QPushButton("Copy Message", self)
        self.copy_message_btn.setEnabled(bool(message_text))
        controls.addWidget(self.expand_all_btn)
        controls.addWidget(self.collapse_all_btn)
        controls.addWidget(self.expand_top_btn)
        controls.addStretch(1)
        controls.addWidget(self.copy_json_btn)
        controls.addWidget(self.copy_message_btn)

        self.state_label = QLabel("", self)
        self.state_label.setWordWrap(True)

        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Key / Index", "Value", "Type"])
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)

        json_panel = QWidget(self)
        json_layout = QVBoxLayout(json_panel)
        json_layout.setContentsMargins(0, 0, 0, 0)
        json_layout.addLayout(controls)
        json_layout.addWidget(self.state_label)
        json_layout.addWidget(self.tree, 1)

        if message_text:
            content_splitter.addWidget(json_panel)
            content_splitter.setStretchFactor(0, 5)
            content_splitter.setStretchFactor(1, 2)
            layout.addWidget(content_splitter, 1)
        else:
            layout.addWidget(json_panel, 1)

        self.expand_all_btn.clicked.connect(self.tree.expandAll)
        self.collapse_all_btn.clicked.connect(self.tree.collapseAll)
        self.expand_top_btn.clicked.connect(self._expand_top_level)
        self.copy_json_btn.clicked.connect(self._copy_json)
        self.copy_message_btn.clicked.connect(self._copy_message)

        self._populate()
        self._prime_message_image_previews()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._initial_geometry_applied:
            return
        self._initial_geometry_applied = True

        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(available.width(), max(980, int(available.width() * 0.82)))
            height = min(available.height(), max(680, int(available.height() * 0.82)))
            self.resize(width, height)

        if self._content_splitter is not None and self._content_splitter.count() == 2:
            total = max(1, self._content_splitter.height() or self.height())
            json_height = max(220, min(300, total // 3))
            self._content_splitter.setSizes([max(1, total - json_height), json_height])
        self._schedule_message_gallery_refresh(delay_ms=0)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._schedule_message_gallery_refresh()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.message_view.viewport() and event.type() == QEvent.Resize:
            self._schedule_message_gallery_refresh()
        return super().eventFilter(watched, event)

    def _status_text(self) -> str:
        source = "No info.json found"
        if self._analysis.path:
            source = f"{self._mod_path} :: {self._analysis.path}"
        base = f"Source: {source} | Status: {self._analysis.status}"
        if self._analysis.error_text:
            return f"{base} | Error: {self._analysis.error_text}"
        return base

    def _set_message_html(self, html_text: str) -> None:
        scroll_bar = self.message_view.verticalScrollBar()
        previous_pos = scroll_bar.value()
        self.message_view.setHtml(html_text)
        scroll_bar.setValue(previous_pos)

    def _prime_message_image_previews(self) -> None:
        if not self._base_message_html:
            return
        for url in _extract_anchor_hrefs(self._base_message_html):
            if not _is_remote_message_link(url):
                continue
            cache_path = _message_image_cache_path(self._message_image_cache_dir, self._message_image_cache_key, url)
            if cache_path.is_file():
                self._cached_message_image_paths[url] = cache_path
                continue
            self._queue_message_image_fetch(url)
        if self._cached_message_image_paths:
            self._refresh_message_html(force=True)

    def _schedule_message_gallery_refresh(self, delay_ms: int = 40) -> None:
        if not self._base_message_html or not self._cached_message_image_paths:
            return
        self._gallery_refresh_timer.start(max(0, int(delay_ms)))

    def _message_gallery_viewport_width(self) -> int:
        return max(1, int(self.message_view.viewport().width()))

    def _refresh_message_html(self, force: bool = False) -> None:
        if not self._base_message_html:
            return
        layout_key = (self._message_gallery_viewport_width(), self._message_image_columns)
        if not force and self._last_gallery_layout == layout_key:
            return
        rendered = _inject_cached_image_previews(
            self._base_message_html,
            self._cached_message_image_paths,
            self._message_image_columns,
            viewport_width=layout_key[0],
        )
        self._last_gallery_layout = layout_key
        self._set_message_html(rendered)

    def set_message_image_columns(self, columns: int) -> None:
        self._message_image_columns = max(1, int(columns))
        self._refresh_message_html(force=True)

    def _queue_message_image_fetch(self, url: str) -> None:
        if url in self._pending_message_image_urls:
            return
        self._pending_message_image_urls.add(url)
        worker = _FnWorker(lambda target=url: (target, _download_remote_image(target)))
        self._image_fetch_workers.add(worker)
        worker.signals.done.connect(lambda payload, w=worker: self._on_message_image_fetch_done(payload, w))
        worker.signals.error.connect(lambda _error, target=url, w=worker: self._on_message_image_fetch_error(target, w))
        self._thread_pool.start(worker)

    def _on_message_image_fetch_done(self, payload: object, worker: _FnWorker) -> None:
        self._image_fetch_workers.discard(worker)
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        url, raw_bytes = payload
        if not isinstance(url, str):
            return
        self._pending_message_image_urls.discard(url)
        if not isinstance(raw_bytes, bytes) or not raw_bytes:
            return
        cache_path = _message_image_cache_path(self._message_image_cache_dir, self._message_image_cache_key, url)
        if not cache_path.is_file() and not _write_message_image_cache(cache_path, raw_bytes):
            return
        self._cached_message_image_paths[url] = cache_path
        self._refresh_message_html(force=True)

    def _on_message_image_fetch_error(self, url: str, worker: _FnWorker) -> None:
        self._image_fetch_workers.discard(worker)
        self._pending_message_image_urls.discard(url)

    def _on_message_link_clicked(self, url: QUrl) -> None:
        if url.isEmpty():
            return
        QDesktopServices.openUrl(url)

    def _populate(self) -> None:
        self.state_label.setText(self._status_text())
        self.tree.clear()

        if self._analysis.parsed_data is None:
            if self._analysis.status == "missing":
                placeholder = QTreeWidgetItem(["(root)", "No info.json found", "missing"])
            elif self._analysis.raw_text:
                placeholder = QTreeWidgetItem(["(root)", "JSON parse failed; raw text available", "invalid"])
            else:
                placeholder = QTreeWidgetItem(["(root)", "No parsed structure available", "invalid"])
            self.tree.addTopLevelItem(placeholder)
            self.copy_json_btn.setEnabled(bool(self._analysis.raw_text))
            self.tree.resizeColumnToContents(0)
            self.tree.resizeColumnToContents(2)
            return

        self.copy_json_btn.setEnabled(True)
        self._add_children(None, self._analysis.parsed_data)
        self._expand_top_level()
        self.tree.resizeColumnToContents(0)
        self.tree.resizeColumnToContents(2)

    def _type_name(self, value: Any) -> str:
        if isinstance(value, dict):
            return "object"
        if isinstance(value, list):
            return "array"
        if isinstance(value, str):
            return "string"
        if isinstance(value, bool):
            return "bool"
        if value is None:
            return "null"
        if isinstance(value, (int, float)):
            return "number"
        return type(value).__name__

    def _preview_text(self, value: Any) -> str:
        if isinstance(value, dict):
            key_count = len(value)
            return f"{key_count} key" if key_count == 1 else f"{key_count} keys"
        if isinstance(value, list):
            item_count = len(value)
            return f"{item_count} item" if item_count == 1 else f"{item_count} items"
        if isinstance(value, str):
            full = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
            if len(full) > 180:
                return f"{full[:177]}..."
            return full
        if value is None:
            return "null"
        return str(value)

    def _add_children(self, parent: QTreeWidgetItem | None, value: Any) -> None:
        if isinstance(value, dict):
            for key, child_value in value.items():
                item = QTreeWidgetItem([str(key), self._preview_text(child_value), self._type_name(child_value)])
                if isinstance(child_value, str):
                    item.setToolTip(1, child_value)
                if parent is None:
                    self.tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                self._add_children(item, child_value)
            return

        if isinstance(value, list):
            for idx, child_value in enumerate(value):
                label = f"[{idx}]"
                item = QTreeWidgetItem([label, self._preview_text(child_value), self._type_name(child_value)])
                if isinstance(child_value, str):
                    item.setToolTip(1, child_value)
                if parent is None:
                    self.tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                self._add_children(item, child_value)
            return

        if parent is None:
            self.tree.addTopLevelItem(QTreeWidgetItem(["(root)", self._preview_text(value), self._type_name(value)]))

    def _expand_top_level(self) -> None:
        self.tree.collapseAll()
        for idx in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(idx)
            if item is not None:
                item.setExpanded(True)

    def _copy_json(self) -> None:
        text = ""
        if self._analysis.parsed_data is not None:
            try:
                text = json.dumps(self._analysis.parsed_data, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                text = str(self._analysis.parsed_data)
        elif self._analysis.raw_text:
            text = self._analysis.raw_text
        elif self._analysis.error_text:
            text = self._analysis.error_text
        if text:
            QApplication.clipboard().setText(text)

    def _copy_message(self) -> None:
        text = (self._analysis.message_clean or "").strip()
        if text:
            QApplication.clipboard().setText(text)
