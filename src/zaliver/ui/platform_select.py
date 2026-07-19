"""Экран выбора режима: YouTube / Instagram."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from zaliver.ui.platform import PLATFORM_CHOICES


class _PlatformCard(QFrame):
    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("platformCard")
        self.setFixedSize(280, 200)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class PlatformSelectPane(QWidget):
    """Стартовый экран: выбрать платформу залива."""

    platform_chosen = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("platformSelectRoot")

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 48, 32, 48)
        root.setSpacing(28)

        root.addStretch(1)

        title = QLabel("Zaliver")
        title.setObjectName("platformSelectTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(title)

        subtitle = QLabel("Выберите режим")
        subtitle.setObjectName("platformSelectSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(subtitle)

        cards = QHBoxLayout()
        cards.setSpacing(24)
        cards.setAlignment(Qt.AlignmentFlag.AlignCenter)

        for platform_id, name, hint in PLATFORM_CHOICES:
            cards.addWidget(self._make_card(platform_id, name, hint))

        root.addLayout(cards)
        root.addStretch(2)

    def _make_card(self, platform_id: str, name: str, hint: str) -> QFrame:
        card = _PlatformCard()
        card.clicked.connect(lambda: self.platform_chosen.emit(platform_id))

        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 28, 24, 24)
        layout.setSpacing(12)

        name_lbl = QLabel(name)
        name_lbl.setObjectName("platformCardName")
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(name_lbl)

        hint_lbl = QLabel(hint)
        hint_lbl.setObjectName("platformCardHint")
        hint_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_lbl.setWordWrap(True)
        layout.addWidget(hint_lbl)

        layout.addStretch(1)

        btn = QPushButton("Открыть")
        btn.setObjectName("platformCardBtn")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda: self.platform_chosen.emit(platform_id))
        layout.addWidget(btn)

        return card
