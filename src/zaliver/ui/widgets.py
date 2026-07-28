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
    """Числовое поле без стрелок и без изменения значения колёсиком."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

    def wheelEvent(self, event: QWheelEvent) -> None:  # type: ignore[override]
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    """Дробное поле без стрелок и без изменения значения колёсиком."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

    def wheelEvent(self, event: QWheelEvent) -> None:  # type: ignore[override]
        event.ignore()


class _SpinBoxInputPolicy(QObject):
    """Глобально: без колеса и без up/down на всех QAbstractSpinBox."""

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
                if obj.buttonSymbols() != QAbstractSpinBox.ButtonSymbols.NoButtons:
                    obj.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        return False


def install_spinbox_input_policy(app: QApplication) -> _SpinBoxInputPolicy:
    """Установить политику ввода для числовых полей (один раз на приложение)."""
    policy = _SpinBoxInputPolicy(app)
    app.installEventFilter(policy)
    return policy
