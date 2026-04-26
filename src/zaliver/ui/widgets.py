"""Custom controls: accent slider groove/handle, switch, animated progress."""

from __future__ import annotations

from PyQt6.QtCore import (
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    Qt,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QProgressBar,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


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
        self.setFixedHeight(28)
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

        margin = 4
        gy = groove.center().y()
        track_h = 6
        rx = 3
        x0 = groove.left() + margin
        x1 = groove.right() - margin
        y0 = gy - track_h // 2

        bg = QColor("#1e2230")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(x0, y0, x1 - x0, track_h, rx, rx)

        span = handle.center().x() - x0
        if span > 2:
            g = QLinearGradient(float(x0), 0, float(x0 + span), 0)
            g.setColorAt(0, self._accent)
            g.setColorAt(1, QColor("#a855f7"))
            painter.setBrush(g)
            painter.drawRoundedRect(x0, y0, span, track_h, rx, rx)

        hx, hy = handle.center().x(), handle.center().y()
        r = 9
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
