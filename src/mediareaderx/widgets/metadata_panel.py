"""Dockable tree that lists file, image/video, camera and GPS metadata."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QAbstractItemView, QDockWidget, QTreeWidget, QTreeWidgetItem

from mediareaderx.metadata import MetaItem


class MetadataPanel(QDockWidget):
    def __init__(self, parent=None):
        super().__init__("Metadata", parent)
        self.setObjectName("MetadataPanel")
        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Property", "Value"])
        self.tree.setUniformRowHeights(True)
        # Long paths keep their start and end ("C:\Users\...\Album"), the parts people recognise.
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setAccessibleName("Metadata")
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        # Ctrl+C is handled by the main window, which routes it here when this
        # tree has focus (a second Ctrl+C shortcut would be ambiguous).
        copy = QAction("Copy", self.tree)
        copy.triggered.connect(self.copy_selection)
        copy_all = QAction("Copy all", self.tree)
        copy_all.triggered.connect(self.copy_all)
        self.tree.addActions([copy, copy_all])
        self.setWidget(self.tree)
        self.setMinimumWidth(260)

    def set_items(self, items: list[MetaItem], extend: bool = False) -> None:
        if not extend:
            self.tree.clear()
        sections: dict[str, QTreeWidgetItem] = {
            self.tree.topLevelItem(i).text(0): self.tree.topLevelItem(i)
            for i in range(self.tree.topLevelItemCount())
        }
        for item in items:
            parent = sections.get(item.section)
            if parent is None:
                parent = QTreeWidgetItem(self.tree, [item.section])
                font = parent.font(0)
                font.setBold(True)
                parent.setFont(0, font)
                parent.setFirstColumnSpanned(True)
                # The full EXIF dump is long; keep it collapsed by default.
                parent.setExpanded(item.section != "EXIF (all tags)")
                sections[item.section] = parent
            child = QTreeWidgetItem(parent, [item.key, item.value])
            child.setToolTip(1, item.value)
        self.tree.resizeColumnToContents(0)

    def clear(self) -> None:
        self.tree.clear()

    def _rows(self, only_selected: bool) -> str:
        lines = []
        for i in range(self.tree.topLevelItemCount()):
            sec = self.tree.topLevelItem(i)
            for j in range(sec.childCount()):
                row = sec.child(j)
                if not only_selected or row.isSelected() or sec.isSelected():
                    lines.append(f"{sec.text(0)}\t{row.text(0)}\t{row.text(1)}")
        return "\n".join(lines)

    def copy_selection(self) -> None:
        QGuiApplication.clipboard().setText(self._rows(only_selected=True))

    def copy_all(self) -> None:
        QGuiApplication.clipboard().setText(self._rows(only_selected=False))
