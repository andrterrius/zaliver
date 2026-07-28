"""Экран выбора режима: YouTube / Instagram / Yt+Inst."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent, QResizeEvent
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from zaliver.ui.platform import PLATFORM_CHOICES


class _PlatformCard(QFrame):
    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("platformCard")
        self.setMinimumSize(180, 150)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
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
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 32, 24, 32)
        root.setSpacing(24)

        root.addStretch(1)

        title = QLabel("Zaliver")
        title.setObjectName("platformSelectTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(title)

        subtitle = QLabel("Выберите режим")
        subtitle.setObjectName("platformSelectSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(subtitle)

        self._cards_row = QHBoxLayout()
        self._cards_row.setSpacing(24)
        self._cards_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cards: list[_PlatformCard] = []

        for platform_id, name, hint in PLATFORM_CHOICES:
            card = self._make_card(platform_id, name, hint)
            self._cards.append(card)
            self._cards_row.addWidget(card)

        root.addLayout(self._cards_row)
        root.addStretch(2)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Обе карточки всегда в одном ряду; на узком экране чуть сжимаются.
        n = max(1, len(self._cards))
        gap = 24 * (n - 1)
        avail = max(180 * n + gap, self.width() - 64)
        card_w = min(280, max(180, (avail - gap) // n))
        card_h = min(200, max(150, int(card_w * 0.72)))
        for card in self._cards:
            card.setFixedSize(card_w, card_h)

    def _make_card(self, platform_id: str, name: str, hint: str) -> _PlatformCard:
        card = _PlatformCard()
        card.clicked.connect(lambda: self.platform_chosen.emit(platform_id))

        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 24, 20, 20)
        layout.setSpacing(10)

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
