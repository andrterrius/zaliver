"""Interactive preview for neon text overlay placement."""

from __future__ import annotations

from PyQt6.QtCore import QPointF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QMouseEvent, QPainter, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from zaliver.processing.text_overlay import (
    NEON_WAVE_AMP_FRAC,
    NEON_WAVE_CHAR_PHASE,
    NEON_WAVE_FRAME_SPEED,
    REF_HORIZONTAL,
    REF_VERTICAL,
    layout_line_chars,
    measure_text_block,
    neon_glow_layers,
    resolve_font_path,
    wave_offset_y,
    wrap_text_lines,
    _make_qfont,
)


class TextOverlayPreviewWidget(QWidget):
    """Drag text on a vertical/horizontal frame; position stored as normalized anchor."""

    positionChanged = pyqtSignal(float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(220, 240)
        self.setMaximumHeight(340)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self._orientation = "vertical"
        self._text = "GAME IN BIO"
        self._font_size = 95
        self._max_width_frac = 0.85
        self._glow_color = QColor("#00FFFF")
        self._text_color = QColor("#FFFFFF")
        self._anchor_x = 0.5
        self._anchor_y = 0.15
        self._dragging = False
        self._drag_offset = QPointF(0.0, 0.0)
        self._font_path = resolve_font_path()
        self._lines: list[str] = []
        self._char_lines: list[list[tuple[str, int]]] = []
        self._block_w = 0
        self._block_h = 0
        self._line_h = 0
        self._wave_amp_frac = NEON_WAVE_AMP_FRAC
        self._wave_char_phase = NEON_WAVE_CHAR_PHASE
        self._wave_frame_speed = NEON_WAVE_FRAME_SPEED
        self._wave_amp = 4.0
        self._anim_frame = 0
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(33)
        self._anim_timer.timeout.connect(self._on_anim_tick)

    def _on_anim_tick(self) -> None:
        if not self._char_lines or not self.isVisible():
            return
        self._anim_frame += 1
        self.update()

    def _start_animation(self) -> None:
        if self._char_lines and self.isVisible():
            if not self._anim_timer.isActive():
                self._anim_timer.start()

    def _stop_animation(self) -> None:
        if self._anim_timer.isActive():
            self._anim_timer.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._start_animation()

    def hideEvent(self, event) -> None:
        self._stop_animation()
        super().hideEvent(event)

    def set_orientation(self, orientation: str) -> None:
        self._orientation = (
            "horizontal" if str(orientation).lower() == "horizontal" else "vertical"
        )
        self._relayout()
        self.update()
        self._start_animation()

    def set_text(self, text: str) -> None:
        self._text = text or ""
        self._relayout()
        self.update()
        self._start_animation()

    def set_font_size(self, size: int) -> None:
        self._font_size = max(8, min(400, int(size)))
        self._relayout()
        self.update()

    def set_max_width_frac(self, frac: float) -> None:
        self._max_width_frac = max(0.2, min(1.0, float(frac)))
        self._relayout()
        self.update()

    def set_glow_color(self, color: QColor | str) -> None:
        if isinstance(color, str):
            self._glow_color = QColor(color)
        else:
            self._glow_color = color
        self.update()

    def set_text_color(self, color: QColor | str) -> None:
        if isinstance(color, str):
            self._text_color = QColor(color)
        else:
            self._text_color = color
        self.update()

    def set_wave_settings(self, amp_frac: float, frame_speed: float) -> None:
        self._wave_amp_frac = max(0.0, min(0.35, float(amp_frac)))
        self._wave_char_phase = NEON_WAVE_CHAR_PHASE
        self._wave_frame_speed = max(0.0, min(0.25, float(frame_speed)))
        self._relayout()
        self.update()

    def set_anchor(self, x: float, y: float) -> None:
        self._anchor_x = max(0.0, min(1.0, float(x)))
        self._anchor_y = max(0.0, min(1.0, float(y)))
        self.update()

    def anchor(self) -> tuple[float, float]:
        return self._anchor_x, self._anchor_y

    def _ref_size(self) -> tuple[int, int]:
        if self._orientation == "horizontal":
            return REF_HORIZONTAL
        return REF_VERTICAL

    def _frame_geometry(self) -> tuple[float, float, float, float]:
        w = max(40.0, float(self.width()) - 16.0)
        h = max(40.0, float(self.height()) - 16.0)
        ref_w, ref_h = self._ref_size()
        aspect = ref_w / ref_h
        if w / h > aspect:
            fh = h
            fw = fh * aspect
        else:
            fw = w
            fh = fw / aspect
        fx = (self.width() - fw) / 2.0
        fy = (self.height() - fh) / 2.0
        return fx, fy, fw, fh

    def _relayout(self) -> None:
        ref_w, _ref_h = self._ref_size()
        max_w = max(20, int(round(self._max_width_frac * ref_w)))
        self._lines = wrap_text_lines(
            self._text, self._font_size, max_w, self._font_path
        )
        self._char_lines = [
            layout_line_chars(ln, self._font_size, self._font_path) for ln in self._lines
        ]
        self._block_w, self._block_h, self._line_h = measure_text_block(
            self._lines, self._font_size, self._font_path
        )
        _, _, fw, fh = self._frame_geometry()
        scale = fh / self._ref_size()[1] if fh > 0 else 1.0
        painted_size = max(8, int(round(self._font_size * scale)))
        self._wave_amp = max(0.0, painted_size * self._wave_amp_frac)
        self._block_h += int(self._wave_amp * 2)

        if not self._char_lines or not any(self._char_lines):
            self._stop_animation()
        elif self.isVisible():
            self._start_animation()

    def _block_top_left(self) -> tuple[int, int]:
        fx, fy, fw, fh = self._frame_geometry()
        scale = fh / self._ref_size()[1]
        bw = self._block_w * scale
        bh = self._block_h * scale
        cx = fx + self._anchor_x * fw
        cy = fy + self._anchor_y * fh
        return int(round(cx - bw / 2.0)), int(round(cy - bh / 2.0))

    def _font_for_paint(self, scale: float) -> QFont:
        return _make_qfont(
            self._font_path,
            max(8, int(round(self._font_size * scale))),
        )

    def _paint_neon_char(
        self,
        painter: QPainter,
        fm: QFontMetrics,
        ch: str,
        x: int,
        y: int,
        wave_dy: float,
        glow_layers: list[tuple[int, int, float]],
    ) -> None:
        cy = int(y + wave_dy)
        cx = int(x)
        text_y = cy + fm.ascent()
        glow = self._glow_color

        for dx, dy, alpha in glow_layers:
            c = QColor(glow)
            c.setAlpha(max(0, min(255, int(alpha * 255))))
            painter.setPen(c)
            painter.drawText(int(cx + dx), int(text_y + dy), ch)

        core = QColor(self._text_color)
        painter.setPen(core)
        painter.drawText(int(cx), int(text_y), ch)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.fillRect(self.rect(), QColor("#0f1117"))

        fx, fy, fw, fh = self._frame_geometry()
        ref_h = self._ref_size()[1]
        scale = fh / ref_h if ref_h > 0 else 1.0

        painter.setPen(QPen(QColor("#334155"), 1))
        painter.setBrush(QColor("#1a1f2e"))
        painter.drawRoundedRect(int(fx), int(fy), int(fw), int(fh), 8, 8)

        orient_lbl = "9:16" if self._orientation == "vertical" else "16:9"
        painter.setPen(QColor("#64748b"))
        painter.drawText(
            int(fx) + 8,
            int(fy) + 18,
            f"Пример {orient_lbl} - перетащи текст",
        )

        if not self._char_lines or not any(self._char_lines):
            painter.setPen(QColor("#475569"))
            painter.drawText(
                int(fx + fw / 2 - 70),
                int(fy + fh / 2),
                "Введи текст…",
            )
            return

        block_x, block_y = self._block_top_left()
        font = self._font_for_paint(scale)
        painter.setFont(font)
        fm = QFontMetrics(font)
        line_h = max(int(self._line_h * scale), fm.height())
        wave_pad = int(self._wave_amp)
        painted_size = max(8, int(round(self._font_size * scale)))
        glow_layers = neon_glow_layers(
            painted_size, max_layers=10, alpha_scale=0.48
        )

        char_global = 0
        y = block_y + wave_pad
        for li, chars in enumerate(self._char_lines):
            if not chars:
                y += line_h
                continue
            for ch, x_off in chars:
                wave_dy = wave_offset_y(
                    char_global,
                    self._anim_frame,
                    self._wave_amp,
                    char_phase=self._wave_char_phase,
                    frame_speed=self._wave_frame_speed,
                )
                self._paint_neon_char(
                    painter,
                    fm,
                    ch,
                    block_x + int(x_off * scale),
                    y,
                    wave_dy,
                    glow_layers,
                )
                char_global += 1
            y += line_h

    def _hit_test(self, pos: QPointF) -> bool:
        if not self._char_lines or not any(self._char_lines):
            return False
        block_x, block_y = self._block_top_left()
        _, _, fw, fh = self._frame_geometry()
        scale = fh / self._ref_size()[1]
        bw = self._block_w * scale
        bh = self._block_h * scale
        return (
            block_x <= pos.x() <= block_x + bw
            and block_y <= pos.y() <= block_y + bh
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        if self._hit_test(pos):
            self._dragging = True
            block_x, block_y = self._block_top_left()
            _, _, fw, fh = self._frame_geometry()
            scale = fh / self._ref_size()[1]
            bw = self._block_w * scale
            bh = self._block_h * scale
            cx = block_x + bw / 2.0
            cy = block_y + bh / 2.0
            self._drag_offset = QPointF(pos.x() - cx, pos.y() - cy)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        if self._dragging:
            fx, fy, fw, fh = self._frame_geometry()
            cx = pos.x() - self._drag_offset.x()
            cy = pos.y() - self._drag_offset.y()
            ax = (cx - fx) / fw if fw > 0 else self._anchor_x
            ay = (cy - fy) / fh if fh > 0 else self._anchor_y
            self._anchor_x = max(0.0, min(1.0, ax))
            self._anchor_y = max(0.0, min(1.0, ay))
            self.update()
            self.positionChanged.emit(self._anchor_x, self._anchor_y)
            return
        if self._hit_test(pos):
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.setCursor(
                Qt.CursorShape.OpenHandCursor
                if self._hit_test(event.position())
                else Qt.CursorShape.ArrowCursor
            )
