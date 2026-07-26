"""Диалог подсказок по переменным для названий и описаний."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCursor, QMouseEvent
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from zaliver.title_variables import MAX_YOUTUBE_TITLE_LENGTH, TITLE_VARIABLES, TITLE_VARIABLES_EXAMPLE


class _ClickableVariableLabel(QLabel):
    def __init__(self, token: str, on_click: Callable[[str], None], parent=None) -> None:
        super().__init__(token, parent)
        self._token = token
        self._on_click = on_click
        self.setObjectName("titleVariableToken")
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setToolTip("Нажмите, чтобы вставить в позицию курсора")

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        if event is not None and event.button() == Qt.MouseButton.LeftButton:
            self._on_click(self._token)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class TitleVariablesDialog(QDialog):
    def __init__(
        self,
        *,
        on_insert: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_insert = on_insert
        self.setWindowTitle("Переменные для названий и описаний")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.resize(760, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        title = QLabel("Переменные для названий и описаний")
        title.setObjectName("title")
        root.addWidget(title)

        hint = QLabel(
            "Вставляйте в любое место заголовка или описания. "
            f"Лимит YouTube для названия — {MAX_YOUTUBE_TITLE_LENGTH} символов."
        )
        hint.setObjectName("hint")
        root.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_body = QWidget()
        scroll_body_l = QVBoxLayout(scroll_body)
        scroll_body_l.setContentsMargins(0, 0, 0, 0)
        scroll_body_l.setSpacing(0)

        for idx, item in enumerate(TITLE_VARIABLES):
            row = QFrame()
            row.setObjectName("titleVariableRow")
            row_l = QHBoxLayout(row)
            row_l.setContentsMargins(0, 10, 0, 10)
            row_l.setSpacing(16)

            token_lbl = _ClickableVariableLabel(item.token, self._insert_token, row)
            token_lbl.setMinimumWidth(150)
            row_l.addWidget(token_lbl, 0)

            example_lbl = QLabel(item.example)
            example_lbl.setObjectName("titleVariableExample")
            example_lbl.setMinimumWidth(170)
            row_l.addWidget(example_lbl, 0)

            desc_lbl = QLabel(item.description)
            desc_lbl.setWordWrap(True)
            desc_lbl.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
            )
            row_l.addWidget(desc_lbl, 1)

            scroll_body_l.addWidget(row)
            if idx < len(TITLE_VARIABLES) - 1:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.HLine)
                sep.setObjectName("titleVariableSeparator")
                scroll_body_l.addWidget(sep)

        scroll_body_l.addStretch()
        scroll.setWidget(scroll_body)
        root.addWidget(scroll, 1)

        example_title = QLabel("Пример:")
        example_title.setObjectName("hint")
        root.addWidget(example_title)

        example_box = QFrame()
        example_box.setObjectName("titleVariablesExample")
        example_box_l = QVBoxLayout(example_box)
        example_box_l.setContentsMargins(12, 10, 12, 10)
        example_text = QLabel(TITLE_VARIABLES_EXAMPLE)
        example_text.setWordWrap(True)
        example_text.setObjectName("titleVariableExampleText")
        example_box_l.addWidget(example_text)
        root.addWidget(example_box)

        footer = QHBoxLayout()
        footer.addStretch()
        btn_close = QPushButton("Закрыть")
        btn_close.clicked.connect(self.accept)
        footer.addWidget(btn_close)
        root.addLayout(footer)

    def _insert_token(self, token: str) -> None:
        self._on_insert(token)
