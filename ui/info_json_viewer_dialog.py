from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from core.modinfo import InfoJsonAnalysisResult


class InfoJsonViewerDialog(QDialog):
    def __init__(
        self,
        mod_display_name: str,
        mod_path: Path,
        analysis: InfoJsonAnalysisResult,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._analysis = analysis
        self._mod_path = mod_path

        title_suffix = mod_display_name.strip() if mod_display_name else mod_path.name
        self.setWindowTitle(f"info.json - {title_suffix}" if title_suffix else "info.json")
        self.resize(980, 680)
        self.setWindowModality(Qt.NonModal)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        layout = QVBoxLayout(self)

        self.message_label = QLabel("Message", self)
        self.message_view = QTextBrowser(self)
        self.message_view.setReadOnly(True)
        self.message_view.setOpenLinks(False)
        self.message_view.setOpenExternalLinks(False)

        message_text = (analysis.message_clean or "").strip()
        message_html = (analysis.message_html or "").strip()
        if message_text:
            if message_html:
                self.message_view.setHtml(message_html)
            else:
                self.message_view.setPlainText(message_text)
            layout.addWidget(self.message_label)
            layout.addWidget(self.message_view, 0)

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
        layout.addLayout(controls)

        self.state_label = QLabel("", self)
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)

        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Key / Index", "Value", "Type"])
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        layout.addWidget(self.tree, 1)

        self.expand_all_btn.clicked.connect(self.tree.expandAll)
        self.collapse_all_btn.clicked.connect(self.tree.collapseAll)
        self.expand_top_btn.clicked.connect(self._expand_top_level)
        self.copy_json_btn.clicked.connect(self._copy_json)
        self.copy_message_btn.clicked.connect(self._copy_message)

        self._populate()

    def _status_text(self) -> str:
        source = "No info.json found"
        if self._analysis.path:
            source = f"{self._mod_path} :: {self._analysis.path}"
        base = f"Source: {source} | Status: {self._analysis.status}"
        if self._analysis.error_text:
            return f"{base} | Error: {self._analysis.error_text}"
        return base

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
