"""Экран выбора режима: Instagram / YouTube / TikTok и «Все вместе»."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QResizeEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from zaliver.config.platform_settings import (
    PLATFORM_INSTAGRAM,
    PLATFORM_TIKTOK,
    PLATFORM_YOUTUBE,
    PLATFORM_YT_INST_TT,
)
from zaliver.ui.platform import PLATFORM_CHOICES


class _PlatformCard(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("platformCard")
        self.setMinimumSize(180, 150)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )


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
        self._cards_row_2 = QHBoxLayout()
        self._cards_row_2.setSpacing(24)
        self._cards_row_2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cards: list[_PlatformCard] = []
        self._combined_card: _PlatformCard | None = None
        self._target_checks: dict[str, QCheckBox] = {}

        for platform_id, name, hint in PLATFORM_CHOICES:
            if platform_id == PLATFORM_YT_INST_TT:
                card = self._make_combined_card(name)
                self._combined_card = card
                self._cards_row_2.addWidget(card)
            else:
                card = self._make_card(platform_id, name)
                self._cards.append(card)
                self._cards_row.addWidget(card)

        root.addLayout(self._cards_row)
        root.addLayout(self._cards_row_2)
        root.addStretch(2)

    def combined_targets(self) -> frozenset[str]:
        """Площадки, отмеченные на карточке «Все вместе»."""
        enabled = {
            key
            for key, box in self._target_checks.items()
            if box.isChecked()
        }
        return frozenset(enabled)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        n = max(1, len(self._cards))
        gap = 24 * (n - 1)
        avail = max(180 * n + gap, self.width() - 64)
        card_w = min(280, max(180, (avail - gap) // n))
        card_h = min(180, max(130, int(card_w * 0.62)))
        for card in self._cards:
            card.setFixedSize(card_w, card_h)
        if self._combined_card is not None:
            self._combined_card.setFixedSize(card_w, card_h + 88)

    def _make_card(self, platform_id: str, name: str) -> _PlatformCard:
        card = _PlatformCard()

        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 24, 20, 20)
        layout.setSpacing(10)

        name_lbl = QLabel(name)
        name_lbl.setObjectName("platformCardName")
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(name_lbl)
        layout.addStretch(1)

        btn = QPushButton("Открыть")
        btn.setObjectName("platformCardBtn")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(
            lambda _checked=False, pid=platform_id: self.platform_chosen.emit(pid)
        )
        layout.addWidget(btn)

        return card

    def _make_combined_card(self, name: str) -> _PlatformCard:
        card = _PlatformCard()

        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(6)

        name_lbl = QLabel(name)
        name_lbl.setObjectName("platformCardName")
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(name_lbl)

        for key, label in (
            (PLATFORM_INSTAGRAM, "Instagram"),
            (PLATFORM_YOUTUBE, "YouTube"),
            (PLATFORM_TIKTOK, "TikTok"),
        ):
            box = QCheckBox(label)
            box.setObjectName("platformCardCheck")
            box.setChecked(True)
            box.setCursor(Qt.CursorShape.PointingHandCursor)
            self._target_checks[key] = box
            layout.addWidget(box, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addStretch(1)

        btn = QPushButton("Открыть")
        btn.setObjectName("platformCardBtn")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(self._emit_combined)
        layout.addWidget(btn)
        return card

    def _emit_combined(self) -> None:
        if not self.combined_targets():
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.information(
                self,
                "Zaliver",
                "Отметьте хотя бы одну площадку.",
            )
            return
        self.platform_chosen.emit(PLATFORM_YT_INST_TT)
