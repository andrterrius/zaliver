"""Диалог предпросмотра обрезанных аватарок."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QResizeEvent
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

_PREVIEW_VIEWPORT_MARGIN = 12
_SOURCE_PREVIEW_MIN_H = 360
_PREVIEW_MAX_W = 680
_PREVIEW_MAX_H = 520


def _fit_dialog_preview(pix: QPixmap, max_w: int, max_h: int) -> QPixmap:
    if pix.isNull() or max_w < 1 or max_h < 1:
        return pix
    w, h = pix.width(), pix.height()
    if w < 1 or h < 1:
        return pix
    scale = min(max_w / w, max_h / h, 1.0)
    target_w = max(1, int(w * scale))
    target_h = max(1, int(h * scale))
    if target_w == w and target_h == h:
        return pix
    return pix.scaled(
        target_w,
        target_h,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class _SourcePreviewBlock(QWidget):
    """Превью исходного файла с рамками — как в ProfileChannelSetupDialog."""

    def __init__(self, name: str, preview_png: bytes, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._preview_png = preview_png or b""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        title = QLabel(name)
        title.setObjectName("hint")
        lay.addWidget(title)

        self._image = QLabel("Превью недоступно")
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setObjectName("profilePreviewImage")
        self._image.setStyleSheet("background: #111; color: #888;")
        self._image.setScaledContents(False)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll.setMinimumHeight(_SOURCE_PREVIEW_MIN_H)
        self._scroll.setWidget(self._image)
        lay.addWidget(self._scroll)

        self._apply_pixmap()

    def _preview_bounds(self) -> tuple[int, int]:
        vp = self._scroll.viewport()
        margin = _PREVIEW_VIEWPORT_MARGIN
        max_w = max(_PREVIEW_MAX_W, vp.width() - margin * 2)
        max_h = max(_PREVIEW_MAX_H, vp.height() - margin * 2)
        return max_w, max_h

    def _apply_pixmap(self) -> None:
        preview_pix = QPixmap()
        if self._preview_png:
            preview_pix.loadFromData(self._preview_png, "PNG")
        max_w, max_h = self._preview_bounds()
        if not preview_pix.isNull():
            preview_pix = _fit_dialog_preview(preview_pix, max_w, max_h)
        if preview_pix.isNull():
            self._image.setText("Превью недоступно")
            self._image.setPixmap(QPixmap())
            self._image.setMinimumSize(0, 0)
            self._image.resize(1, 1)
            return

        self._image.setText("")
        self._image.setPixmap(preview_pix)
        self._image.setFixedSize(preview_pix.size())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_pixmap()


class AvatarCropPreviewDialog(QDialog):
    def __init__(
        self,
        *,
        file_previews: list[tuple[str, bytes]],
        crop_mode: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Предпросмотр аватарок")
        self.setModal(True)
        self.resize(760, 640)
        self.setMinimumSize(520, 480)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        hint = QLabel(
            "Как программа нашла и вырезала иконки на исходных файлах."
            if crop_mode
            else "Файлы загружены целиком, без автоматической обрезки."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        content_l = QVBoxLayout(content)
        content_l.setSpacing(12)
        content_l.setContentsMargins(0, 0, 0, 0)

        if file_previews:
            for name, preview_png in file_previews:
                content_l.addWidget(_SourcePreviewBlock(name, preview_png, content))
        else:
            empty = QLabel("Превью недоступно")
            empty.setObjectName("hint")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            content_l.addWidget(empty)

        content_l.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        btns = QHBoxLayout()
        btns.addStretch()
        close_btn = QPushButton("Закрыть")
        close_btn.setObjectName("secondary")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(close_btn)
        root.addLayout(btns)
