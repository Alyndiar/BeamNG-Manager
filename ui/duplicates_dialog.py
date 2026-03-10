from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.cache import ScanIndex
from core.duplicates import find_duplicates


class DuplicatesDialog(QDialog):
    def __init__(
        self,
        index: ScanIndex,
        parent=None,
        delete_selected_cb: Callable[[list[Path]], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Duplicate Mods")
        self.resize(900, 500)
        self._index = index
        self._delete_selected_cb = delete_selected_cb

        self.active_only = QCheckBox("Show only duplicates among ACTIVE packs", self)
        self.include_misc = QCheckBox("Include loose/repo/orphans in duplicate scan", self)

        refresh_btn = QPushButton("Refresh", self)
        delete_btn = QPushButton("Delete selected mods", self)
        close_btn = QPushButton("Close", self)
        refresh_btn.clicked.connect(self.refresh)
        delete_btn.clicked.connect(self._delete_selected)
        close_btn.clicked.connect(self.accept)

        self.summary = QLabel("", self)

        self.table = QTableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Signature", "Source", "Pack", "Path"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)

        options = QVBoxLayout()
        options.addWidget(self.active_only)
        options.addWidget(self.include_misc)

        actions = QHBoxLayout()
        actions.addWidget(delete_btn)
        actions.addStretch(1)
        actions.addWidget(refresh_btn)
        actions.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(options)
        layout.addWidget(self.summary)
        layout.addWidget(self.table)
        layout.addLayout(actions)

        self.refresh()

    def _selected_paths(self) -> list[Path]:
        rows = self.table.selectionModel().selectedRows()
        paths: list[Path] = []
        seen: set[str] = set()
        for idx in rows:
            item = self.table.item(idx.row(), 3)
            if item is None:
                continue
            path_raw = str(item.data(Qt.UserRole) or "").strip()
            if not path_raw or path_raw in seen:
                continue
            seen.add(path_raw)
            paths.append(Path(path_raw))
        return paths

    def _delete_selected(self) -> None:
        if self._delete_selected_cb is None:
            return
        selected = self._selected_paths()
        if not selected:
            self.summary.setText("No selected rows to delete.")
            return
        deleted = self._delete_selected_cb(selected)
        if deleted:
            self.accept()

    def refresh(self) -> None:
        groups = find_duplicates(
            self._index,
            active_packs_only=self.active_only.isChecked(),
            include_misc_sources=self.include_misc.isChecked(),
        )

        rows = sum(len(g.hits) for g in groups)
        self.table.setRowCount(rows)

        row = 0
        for group in groups:
            for hit in group.hits:
                source = hit.source
                if hit.source == "pack":
                    source = "pack (active)" if hit.active else "pack (inactive)"
                items = [
                    QTableWidgetItem(group.signature),
                    QTableWidgetItem(source),
                    QTableWidgetItem(hit.pack_name or ""),
                    QTableWidgetItem(str(hit.path)),
                ]
                for col, item in enumerate(items):
                    item.setData(Qt.UserRole, hit.path)
                    self.table.setItem(row, col, item)
                row += 1

        self.summary.setText(f"Duplicate groups: {len(groups)} | Duplicate rows: {rows}")
        self.table.resizeColumnsToContents()
