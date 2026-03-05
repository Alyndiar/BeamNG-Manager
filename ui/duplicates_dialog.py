from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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
    def __init__(self, index: ScanIndex, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Duplicate Mods")
        self.resize(900, 500)
        self._index = index

        self.active_only = QCheckBox("Show only duplicates among ACTIVE packs", self)
        self.include_misc = QCheckBox("Include loose/repo/orphans in duplicate scan", self)

        refresh_btn = QPushButton("Refresh", self)
        close_btn = QPushButton("Close", self)
        refresh_btn.clicked.connect(self.refresh)
        close_btn.clicked.connect(self.accept)

        self.summary = QLabel("", self)

        self.table = QTableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Signature", "Source", "Pack", "Path"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)

        options = QVBoxLayout()
        options.addWidget(self.active_only)
        options.addWidget(self.include_misc)

        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(refresh_btn)
        actions.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(options)
        layout.addWidget(self.summary)
        layout.addWidget(self.table)
        layout.addLayout(actions)

        self.refresh()

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
