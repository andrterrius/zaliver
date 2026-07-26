"""Custom controls: accent slider groove/handle, switch, animated progress."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QLayout,
    QLayoutItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStyle,
    QStyleOptionSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# Лог справа: мягкие минимумы, чтобы окно не раздувалось на macOS / узких экранах.
LOG_PANEL_MAX_WIDTH = 300
LOG_PANEL_MIN_WIDTH = 120
FORM_PANEL_MIN_WIDTH = 280
LOG_PANEL_PREFERRED_WIDTH = 260


def make_log_export_button(
    log_widget: QWidget,
    parent: QWidget,
    *,
    default_filename: str = "zaliver_log.txt",
) -> QPushButton:
    """Кнопка «Экспорт логов» — сохраняет plain text из QPlainTextEdit в .txt."""

    def _export() -> None:
        path, _ = QFileDialog.getSaveFileName(
            parent,
            "Экспорт логов",
            default_filename,
            "Текстовые файлы (*.txt);;Все файлы (*.*)",
        )
        if not path:
            return
        try:
            text = log_widget.toPlainText()  # type: ignore[attr-defined]
            Path(path).write_text(text, encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(
                parent,
                "Экспорт логов",
                f"Не удалось сохранить файл:\n{e}",
            )

    btn = QPushButton("Экспорт логов")
    btn.setObjectName("secondary")
    btn.clicked.connect(_export)
    return btn


def configure_log_splitter(
    splitter: QSplitter,
    *,
    form_panel: QWidget,
    log_panel: QWidget,
) -> None:
    """Лог справа с ограниченной шириной; слева — форма, которая может сжиматься."""
    splitter.setChildrenCollapsible(True)
    form_panel.setMinimumWidth(FORM_PANEL_MIN_WIDTH)
    form_panel.setSizePolicy(
        QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
    )
    log_panel.setMinimumWidth(LOG_PANEL_MIN_WIDTH)
    log_panel.setMaximumWidth(LOG_PANEL_MAX_WIDTH)
    log_panel.setSizePolicy(
        QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
    )
    splitter.setStretchFactor(0, 1)
    splitter.setStretchFactor(1, 0)
    splitter.setSizes([10_000, LOG_PANEL_PREFERRED_WIDTH])


class FlowLayout(QLayout):
    """Горизонтальный поток с переносом строк — для адаптивных тулбаров."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        margin: int = 0,
        hspacing: int = 8,
        vspacing: int = 8,
    ) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._hspace = hspacing
        self._vspace = vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        for item in self._items:
            wid = item.widget()
            space_x = self._hspace
            space_y = self._vspace
            if wid is not None:
                try:
                    space_x += max(
                        0,
                        wid.style().layoutSpacing(
                            QSizePolicy.ControlType.PushButton,
                            QSizePolicy.ControlType.PushButton,
                            Qt.Orientation.Horizontal,
                        ),
                    )
                    space_y += max(
                        0,
                        wid.style().layoutSpacing(
                            QSizePolicy.ControlType.PushButton,
                            QSizePolicy.ControlType.PushButton,
                            Qt.Orientation.Vertical,
                        ),
                    )
                except Exception:
                    pass
            next_x = x + item.sizeHint().width() + space_x
            if next_x - space_x > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + space_y
                next_x = x + item.sizeHint().width() + space_x
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))
            x = next_x
            line_height = max(line_height, item.sizeHint().height())
        return y + line_height - rect.y() + m.bottom()


class CollapsibleSection(QWidget):
    """Сворачиваемый блок (стрелка + контент)."""

    expansionChanged = pyqtSignal(bool)

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self._title = title
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        self._btn = QToolButton()
        self._btn.setText(title)
        self._btn.setCheckable(True)
        self._btn.setChecked(False)
        self._btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._btn.setArrowType(Qt.ArrowType.RightArrow)
        self._btn.setObjectName("secondary")
        self._btn.toggled.connect(self._on_toggled)
        root.addWidget(self._btn)

        self._content = QWidget()
        self._content.setVisible(False)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(16, 0, 0, 0)
        root.addWidget(self._content)

    def _on_toggled(self, expanded: bool) -> None:
        self._content.setVisible(expanded)
        self._btn.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.expansionChanged.emit(expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._btn.blockSignals(True)
        self._btn.setChecked(expanded)
        self._btn.blockSignals(False)
        self._on_toggled(expanded)

    def content_layout(self) -> QVBoxLayout:
        return self._content_layout


class SmoothSlider(QSlider):
    """Horizontal slider with rounded track and glow handle."""

    def __init__(self, orientation=Qt.Orientation.Horizontal, parent=None) -> None:
        super().__init__(orientation, parent)
        # Высота с запасом под ручку; вертикаль центрируется в paintEvent и в layout (AlignVCenter).
        self.setMinimumHeight(34)
        self.setMaximumHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._accent = QColor("#6366f1")

    def set_accent(self, hex_color: str) -> None:
        self._accent = QColor(hex_color)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            opt,
            QStyle.SubControl.SC_SliderGroove,
            self,
        )
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            opt,
            QStyle.SubControl.SC_SliderHandle,
            self,
        )

        cr = self.contentsRect()
        cy = cr.center().y()

        margin = 4
        x0 = max(cr.left() + margin, groove.left() + margin)
        x1 = min(cr.right() - margin, groove.right() - margin)
        if x1 <= x0 + 4:
            x0, x1 = cr.left() + margin * 2, cr.right() - margin * 2

        track_h = 6
        rx = 3
        y0 = cy - track_h // 2

        bg = QColor("#1e2230")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(x0, y0, x1 - x0, track_h, rx, rx)

        hx = handle.center().x()
        hx = max(x0 + 2, min(x1 - 2, hx))
        span = hx - x0
        if span > 2:
            g = QLinearGradient(float(x0), 0, float(x0 + span), 0)
            g.setColorAt(0, self._accent)
            g.setColorAt(1, QColor("#a855f7"))
            painter.setBrush(g)
            painter.drawRoundedRect(x0, y0, span, track_h, rx, rx)

        r = 9
        hy = cy
        painter.setBrush(QColor("#f8fafc"))
        painter.setPen(QPen(QColor("#c7d2fe"), 1))
        painter.drawEllipse(hx - r, hy - r, 2 * r, 2 * r)


class ToggleSwitch(QCheckBox):
    """Wide pill switch with animated thumb position via stylesheet + check state."""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class _ProgressBarAnimator(QObject):
    def __init__(self, bar: "AnimatedProgressBar") -> None:
        super().__init__(bar)
        self._bar = bar
        self._anim = QPropertyAnimation(bar, b"displayValue", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def animate_to(self, value: int) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._bar.displayValue)
        self._anim.setEndValue(value)
        self._anim.start()


class AnimatedProgressBar(QProgressBar):
    """Smooth value transitions for UI responsiveness."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._display = 0
        self._animator = _ProgressBarAnimator(self)

    def get_display_value(self) -> int:
        return self._display

    def set_display_value(self, v: int) -> None:
        self._display = v
        super().setValue(v)

    displayValue = pyqtProperty(int, get_display_value, set_display_value)

    def setValue(self, value: int) -> None:
        self._animator.animate_to(max(0, min(value, self.maximum())))

    def setValueImmediate(self, value: int) -> None:
        self._animator._anim.stop()
        self._display = max(0, min(value, self.maximum()))
        super().setValue(self._display)
