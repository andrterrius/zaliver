"""Independent, persistent source lists for combined uploads."""

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QPushButton,
    QListWidget,
    QFileDialog,
    QAbstractItemView,
    QGroupBox,
)
from zaliver.ui.widgets import FlowLayout


PLATFORM_NAMES = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok"}


class PlatformSourcesPanel(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.lists = {}
        self.groups = {}
        for platform, name in PLATFORM_NAMES.items():
            group = QGroupBox(name)
            box = QVBoxLayout(group)
            files = QListWidget()
            files.setObjectName("musicTracksList")
            files.setMinimumWidth(0)
            files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            files.setMaximumHeight(100)
            files.setMinimumHeight(60)
            self.lists[platform] = files
            self.groups[platform] = group
            row = FlowLayout(hspacing=6, vspacing=6)
            for text, action in (
                ("Выбрать файлы…", "replace"),
                ("Добавить…", "add"),
                ("Удалить выбранные", "remove"),
                ("Очистить", "clear"),
            ):
                button = QPushButton(text)
                button.setObjectName("secondary")
                button.clicked.connect(
                    lambda checked=False, p=platform, a=action: self.edit(p, a)
                )
                row.addWidget(button)
            box.addWidget(files)
            box.addLayout(row)
            layout.addWidget(group)

    def paths(self, platform):
        files = self.lists[platform]
        return [files.item(i).text() for i in range(files.count())]

    def set_paths(self, platform, paths):
        files = self.lists[platform]
        files.clear()
        files.addItems(list(dict.fromkeys(str(p) for p in paths if str(p).strip())))

    def edit(self, platform, action):
        files = self.lists[platform]
        if action in ("replace", "add"):
            current = self.paths(platform)
            paths, _ = QFileDialog.getOpenFileNames(
                self,
                PLATFORM_NAMES[platform],
                str(Path(current[0]).parent) if current else "",
                "Видео (*.mp4 *.mkv *.mov *.avi *.webm *.m4v);;Все файлы (*)",
            )
            if not paths:
                return
            self.set_paths(platform, (current if action == "add" else []) + paths)
        elif action == "remove":
            for item in files.selectedItems():
                files.takeItem(files.row(item))
        else:
            files.clear()
        self.changed.emit()
