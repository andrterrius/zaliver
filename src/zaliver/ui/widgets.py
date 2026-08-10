"""Custom controls: accent slider groove/handle, switch, animated progress."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPen, QWheelEvent
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
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


def make_work_section_nav(
    labels: list[str],
    *,
    parent: QWidget | None = None,
    initial: int = 0,
) -> tuple[QWidget, QButtonGroup, list[QPushButton]]:
    """Ряд кнопок-разделов (Исходники / Фильтры / …) для Уникализации и Нарезки."""
    row = QWidget(parent)
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    group = QButtonGroup(row)
    group.setExclusive(True)
    buttons: list[QPushButton] = []
    for i, label in enumerate(labels):
        btn = QPushButton(label)
        btn.setObjectName("workSectionNav")
        btn.setCheckable(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        group.addButton(btn, i)
        lay.addWidget(btn, 0)
        buttons.append(btn)
    lay.addStretch(1)
    if buttons:
        idx = max(0, min(initial, len(buttons) - 1))
        buttons[idx].setChecked(True)
    return row, group, buttons


def wrap_work_section_page(*sections: QWidget) -> QWidget:
    """Страница стека: один или несколько блоков + растяжка снизу."""
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    for section in sections:
        lay.addWidget(section)
    lay.addStretch(1)
    return page


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

    def wheelEvent(self, event: QWheelEvent) -> None:  # type: ignore[override]
        # Не менять значение колёсиком — только перетаскивание (скролл страницы проходит выше).
        event.ignore()

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


class ValueSlider(QWidget):
    """Ползунок + подпись значения; колесо мыши игнорируется (только drag)."""

    valueChanged = pyqtSignal(float)

    def __init__(
        self,
        *,
        minimum: float,
        maximum: float,
        value: float | None = None,
        step: float = 1.0,
        decimals: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if maximum < minimum:
            minimum, maximum = maximum, minimum
        step = float(step) if step else 1.0
        if step <= 0:
            step = 1.0
        self._decimals = max(0, int(decimals))
        self._scale = 10 ** self._decimals if self._decimals > 0 else max(
            1, int(round(1.0 / step)) if step < 1.0 else 1
        )
        if self._decimals == 0 and step >= 1.0:
            self._scale = 1
        self._slider = SmoothSlider(Qt.Orientation.Horizontal)
        self._slider.setMinimum(int(round(minimum * self._scale)))
        self._slider.setMaximum(int(round(maximum * self._scale)))
        tick = max(1, int(round(step * self._scale)))
        self._slider.setSingleStep(tick)
        self._slider.setPageStep(tick * 5)
        initial = minimum if value is None else float(value)
        self._slider.setValue(int(round(initial * self._scale)))
        self._label = QLabel()
        self._label.setObjectName("hint")
        self._label.setMinimumWidth(52)
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self._slider, 1, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._label, 0, Qt.AlignmentFlag.AlignVCenter)
        self._slider.valueChanged.connect(self._on_slider)
        self._sync_label()

    def _from_slider(self, raw: int) -> float:
        return float(raw) / float(self._scale)

    def _on_slider(self, raw: int) -> None:
        self._sync_label()
        self.valueChanged.emit(self._from_slider(raw))

    def _sync_label(self) -> None:
        v = self._from_slider(self._slider.value())
        if self._decimals <= 0:
            self._label.setText(str(int(round(v))))
        else:
            self._label.setText(f"{v:.{self._decimals}f}")

    def value(self) -> float:
        return self._from_slider(self._slider.value())

    def setValue(self, value: float) -> None:  # noqa: N802
        lo = self._slider.minimum()
        hi = self._slider.maximum()
        raw = int(round(float(value) * self._scale))
        self._slider.setValue(max(lo, min(hi, raw)))

    def setRange(self, minimum: float, maximum: float) -> None:  # noqa: N802
        if maximum < minimum:
            minimum, maximum = maximum, minimum
        cur = self.value()
        self._slider.blockSignals(True)
        self._slider.setMinimum(int(round(minimum * self._scale)))
        self._slider.setMaximum(int(round(maximum * self._scale)))
        self._slider.blockSignals(False)
        self.setValue(cur)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802
        super().setEnabled(enabled)
        self._slider.setEnabled(enabled)
        self._label.setEnabled(enabled)


class RangeSmoothSlider(QWidget):
    """Двойной ползунок: одна точка, при разведении — две ручки и заливка между ними."""

    rangeChanged = pyqtSignal(int, int)
    sliderPressed = pyqtSignal()
    sliderReleased = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(34)
        self.setMaximumHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self._accent = QColor("#6366f1")
        self._accent_end = QColor("#a855f7")
        self._track_bg = QColor("#1e2230")
        self._handle_fill = QColor("#f8fafc")
        self._handle_pen = QPen(QColor("#c7d2fe"), 1)
        self._minimum = 0
        self._maximum = 100
        self._low = 0
        self._high = 0
        self._single_step = 1
        self._page_step = 5
        self._active: str | None = None  # "low" | "high" | "expand"
        self._press_low = 0
        self._press_high = 0
        self._handle_r = 9
        self._geom_x0 = 0
        self._geom_x1 = 1
        self._geom_cy = 0
        self._geom_valid = False

    def wheelEvent(self, event: QWheelEvent) -> None:  # type: ignore[override]
        event.ignore()

    def set_accent(self, hex_color: str) -> None:
        self._accent = QColor(hex_color)
        self.update()

    def minimum(self) -> int:
        return self._minimum

    def maximum(self) -> int:
        return self._maximum

    def low(self) -> int:
        return self._low

    def high(self) -> int:
        return self._high

    def isSliderDown(self) -> bool:  # noqa: N802
        return self._active is not None

    def value(self) -> int:
        """Совместимость с QSlider: нижняя граница (или единственное значение)."""
        return self._low

    def setMinimum(self, value: int) -> None:  # noqa: N802
        self.setRange(int(value), self._maximum)

    def setMaximum(self, value: int) -> None:  # noqa: N802
        self.setRange(self._minimum, int(value))

    def setRange(self, minimum: int, maximum: int) -> None:  # noqa: N802
        if maximum < minimum:
            minimum, maximum = maximum, minimum
        self._minimum = int(minimum)
        self._maximum = int(maximum)
        self._low = max(self._minimum, min(self._maximum, self._low))
        self._high = max(self._low, min(self._maximum, self._high))
        self._geom_valid = False
        self.update()

    def setSingleStep(self, step: int) -> None:  # noqa: N802
        self._single_step = max(1, int(step))

    def setPageStep(self, step: int) -> None:  # noqa: N802
        self._page_step = max(1, int(step))

    def setLow(self, value: int) -> None:  # noqa: N802
        self.setSpan(int(value), self._high)

    def setHigh(self, value: int) -> None:  # noqa: N802
        self.setSpan(self._low, int(value))

    def setValue(self, value: int) -> None:  # noqa: N802
        """Схлопнуть обе ручки в одно значение."""
        v = max(self._minimum, min(self._maximum, int(value)))
        self.setSpan(v, v)

    def setSpan(self, low: int, high: int) -> None:  # noqa: N802
        lo = max(self._minimum, min(self._maximum, int(low)))
        hi = max(self._minimum, min(self._maximum, int(high)))
        if hi < lo:
            lo, hi = hi, lo
        if lo == self._low and hi == self._high:
            return
        self._low = lo
        self._high = hi
        self.update()
        self.rangeChanged.emit(self._low, self._high)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        self._geom_valid = False
        super().resizeEvent(event)

    def _ensure_geom(self) -> None:
        if self._geom_valid:
            return
        cr = self.contentsRect()
        margin = 4 + self._handle_r
        x0 = cr.left() + margin
        x1 = cr.right() - margin
        if x1 <= x0 + 4:
            x0, x1 = cr.left() + margin, cr.right() - margin
        self._geom_x0 = x0
        self._geom_x1 = x1
        self._geom_cy = cr.center().y()
        self._geom_valid = True

    def _value_to_x(self, value: int) -> float:
        self._ensure_geom()
        span = max(1, self._maximum - self._minimum)
        t = (value - self._minimum) / float(span)
        return self._geom_x0 + t * (self._geom_x1 - self._geom_x0)

    def _x_to_value(self, x: float) -> int:
        self._ensure_geom()
        x0, x1 = self._geom_x0, self._geom_x1
        if x1 <= x0:
            return self._minimum
        t = max(0.0, min(1.0, (x - x0) / float(x1 - x0)))
        raw = self._minimum + t * (self._maximum - self._minimum)
        step = self._single_step
        snapped = int(round(raw / step) * step)
        return max(self._minimum, min(self._maximum, snapped))

    def _handle_at(self, pos: QPoint) -> str | None:
        self._ensure_geom()
        x = float(pos.x())
        y = float(pos.y())
        cy = self._geom_cy
        if abs(y - cy) > self._handle_r + 6:
            return None
        lx = self._value_to_x(self._low)
        hx = self._value_to_x(self._high)
        if self._low == self._high:
            if abs(x - lx) <= self._handle_r + 4:
                return "expand"
            return None
        dl = abs(x - lx)
        dh = abs(x - hx)
        hit = self._handle_r + 4
        if dl <= hit and dl <= dh:
            return "low"
        if dh <= hit:
            return "high"
        return None

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            return
        self._geom_valid = False
        pos = event.position().toPoint()
        hit = self._handle_at(pos)
        if hit is None:
            v = self._x_to_value(float(pos.x()))
            self.setSpan(v, v)
            hit = "expand"
        self._active = hit
        self._press_low = self._low
        self._press_high = self._high
        self.sliderPressed.emit()
        self.update()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._active is None:
            return
        v = self._x_to_value(float(event.position().x()))
        if self._active == "expand":
            if v >= self._press_low:
                self.setSpan(self._press_low, v)
            else:
                self.setSpan(v, self._press_high)
            if self._low != self._high:
                self._active = "high" if v >= self._press_low else "low"
        elif self._active == "low":
            self.setSpan(min(v, self._high), self._high)
        elif self._active == "high":
            self.setSpan(self._low, max(v, self._low))

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            return
        was_down = self._active is not None
        self._active = None
        if was_down:
            self.sliderReleased.emit()
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._ensure_geom()
        x0, x1, cy = self._geom_x0, self._geom_x1, self._geom_cy
        track_h = 6
        rx = 3
        y0 = cy - track_h // 2

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._track_bg)
        painter.drawRoundedRect(x0, y0, x1 - x0, track_h, rx, rx)

        lx = self._value_to_x(self._low)
        hx = self._value_to_x(self._high)
        span = hx - lx
        if span > 1.5:
            g = QLinearGradient(float(lx), 0, float(hx), 0)
            g.setColorAt(0, self._accent)
            g.setColorAt(1, self._accent_end)
            painter.setBrush(g)
            painter.drawRoundedRect(int(lx), y0, max(1, int(span)), track_h, rx, rx)

        r = self._handle_r
        painter.setBrush(self._handle_fill)
        painter.setPen(self._handle_pen)
        painter.drawEllipse(int(lx - r), cy - r, 2 * r, 2 * r)
        if self._low != self._high:
            painter.drawEllipse(int(hx - r), cy - r, 2 * r, 2 * r)


class ValueRangeSlider(QWidget):
    """Ползунок диапазона + подпись; при совпадении границ — точное значение."""

    rangeChanged = pyqtSignal(float, float)
    rangeChangeFinished = pyqtSignal(float, float)

    def __init__(
        self,
        *,
        minimum: float,
        maximum: float,
        low: float | None = None,
        high: float | None = None,
        value: float | None = None,
        step: float = 1.0,
        decimals: int = 0,
        suffix: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if maximum < minimum:
            minimum, maximum = maximum, minimum
        step = float(step) if step else 1.0
        if step <= 0:
            step = 1.0
        self._decimals = max(0, int(decimals))
        self._suffix = str(suffix or "")
        self._scale = 10 ** self._decimals if self._decimals > 0 else max(
            1, int(round(1.0 / step)) if step < 1.0 else 1
        )
        if self._decimals == 0 and step >= 1.0:
            self._scale = 1
        self._last_label = ""
        self._slider = RangeSmoothSlider()
        self._slider.setMinimum(int(round(minimum * self._scale)))
        self._slider.setMaximum(int(round(maximum * self._scale)))
        tick = max(1, int(round(step * self._scale)))
        self._slider.setSingleStep(tick)
        self._slider.setPageStep(tick * 5)
        if value is not None and low is None and high is None:
            lo = hi = float(value)
        else:
            lo = float(minimum if low is None else low)
            hi = float(lo if high is None else high)
        self._slider.blockSignals(True)
        self._slider.setSpan(
            int(round(lo * self._scale)),
            int(round(hi * self._scale)),
        )
        self._slider.blockSignals(False)
        self._label = QLabel()
        self._label.setObjectName("hint")
        # Фиксированная ширина по худшему случаю подписи — без reflow layout при drag.
        worst = f"{self._fmt(maximum)}–{self._fmt(maximum)}"
        if self._suffix and not worst.endswith(self._suffix):
            # _fmt уже добавляет suffix к каждому числу; для пары без двойного суффикса:
            if self._decimals <= 0:
                worst = f"{int(round(maximum))}–{int(round(maximum))}{self._suffix}"
            else:
                worst = (
                    f"{maximum:.{self._decimals}f}–"
                    f"{maximum:.{self._decimals}f}{self._suffix}"
                )
        fm = self._label.fontMetrics()
        label_w = fm.boundingRect(worst).width() + fm.horizontalAdvance("0") * 2 + 8
        self._label.setFixedWidth(max(96, label_w))
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self._slider, 1, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._label, 0, Qt.AlignmentFlag.AlignVCenter)
        self._slider.rangeChanged.connect(self._on_slider)
        self._slider.sliderReleased.connect(self._on_slider_released)
        self._sync_label()

    def _from_slider(self, raw: int) -> float:
        return float(raw) / float(self._scale)

    def _fmt(self, v: float) -> str:
        if self._decimals <= 0:
            text = str(int(round(v)))
        else:
            text = f"{v:.{self._decimals}f}"
        return f"{text}{self._suffix}" if self._suffix else text

    def _fmt_pair(self, lo: float, hi: float) -> str:
        if abs(hi - lo) < 10 ** (-(self._decimals + 1)):
            return self._fmt(lo)
        if self._suffix:
            if self._decimals <= 0:
                a, b = str(int(round(lo))), str(int(round(hi)))
            else:
                a = f"{lo:.{self._decimals}f}"
                b = f"{hi:.{self._decimals}f}"
            return f"{a}–{b}{self._suffix}"
        return f"{self._fmt(lo)}–{self._fmt(hi)}"

    def _on_slider(self, raw_lo: int, raw_hi: int) -> None:
        self._sync_label()
        self.rangeChanged.emit(self._from_slider(raw_lo), self._from_slider(raw_hi))

    def _on_slider_released(self) -> None:
        self.rangeChangeFinished.emit(self.lowValue(), self.highValue())

    def _sync_label(self) -> None:
        text = self._fmt_pair(self.lowValue(), self.highValue())
        if text == self._last_label:
            return
        self._last_label = text
        self._label.setText(text)

    def lowValue(self) -> float:  # noqa: N802
        return self._from_slider(self._slider.low())

    def highValue(self) -> float:  # noqa: N802
        return self._from_slider(self._slider.high())

    def value(self) -> float:
        """Нижняя граница (или единственное значение, если диапазон схлопнут)."""
        return self.lowValue()

    def setValue(self, value: float) -> None:  # noqa: N802
        self.setValues(float(value), float(value))

    def setValues(self, low: float, high: float) -> None:  # noqa: N802
        self._slider.setSpan(
            int(round(float(low) * self._scale)),
            int(round(float(high) * self._scale)),
        )

    def setRange(self, minimum: float, maximum: float) -> None:  # noqa: N802
        if maximum < minimum:
            minimum, maximum = maximum, minimum
        lo, hi = self.lowValue(), self.highValue()
        self._slider.blockSignals(True)
        self._slider.setMinimum(int(round(minimum * self._scale)))
        self._slider.setMaximum(int(round(maximum * self._scale)))
        self._slider.blockSignals(False)
        self.setValues(lo, hi)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802
        super().setEnabled(enabled)
        self._slider.setEnabled(enabled)
        self._label.setEnabled(enabled)

    def set_accent(self, hex_color: str) -> None:
        self._slider.set_accent(hex_color)


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


class NoWheelSpinBox(QSpinBox):
    """Числовое поле со стрелками; значение не меняется колёсиком."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)

    def wheelEvent(self, event: QWheelEvent) -> None:  # type: ignore[override]
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    """Дробное поле со стрелками; значение не меняется колёсиком."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)

    def wheelEvent(self, event: QWheelEvent) -> None:  # type: ignore[override]
        event.ignore()


class _SpinBoxInputPolicy(QObject):
    """Глобально: стрелки up/down видны; колесо не меняет значение."""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if isinstance(obj, QAbstractSpinBox):
            et = event.type()
            if et == QEvent.Type.Wheel:
                event.ignore()
                return True
            if et in (
                QEvent.Type.Show,
                QEvent.Type.Polish,
                QEvent.Type.StyleChange,
            ):
                if obj.buttonSymbols() != QAbstractSpinBox.ButtonSymbols.UpDownArrows:
                    obj.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        return False


def install_spinbox_input_policy(app: QApplication) -> _SpinBoxInputPolicy:
    """Установить политику ввода для числовых полей (один раз на приложение)."""
    policy = _SpinBoxInputPolicy(app)
    app.installEventFilter(policy)
    return policy
