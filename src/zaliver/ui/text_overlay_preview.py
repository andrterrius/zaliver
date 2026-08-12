"""Interactive preview for neon text overlay placement."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PyQt6.QtWidgets import QPushButton, QSizePolicy, QWidget

from zaliver.processing.text_overlay import (
    NEON_WAVE_AMP_FRAC,
    NEON_WAVE_CHAR_PHASE,
    NEON_WAVE_FRAME_SPEED,
    REF_HORIZONTAL,
    REF_VERTICAL,
    effective_font_path,
    font_path_for_unit,
    is_emoji_unit,
    layout_line_chars,
    measure_text_block,
    neon_glow_layers,
    resolve_color_emoji_font_path,
    wave_offset_y,
    wrap_text_lines,
    _make_qfont,
)

_PLAY_MAX_WIDTH = 480

_FRAME_CACHE: dict[tuple[str, float, int], QPixmap] = {}
_FRAME_CACHE_MAX = 12


def _popen_flags() -> int:
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def load_video_frame_pixmap(
    video_path: str | Path,
    *,
    max_width: int = 720,
    seek_sec: float = 0.5,
) -> QPixmap | None:
    """Кадр из видео через ffmpeg (кэш по пути + mtime)."""
    try:
        p = Path(video_path)
        if not p.is_file():
            return None
        key_path = str(p.resolve())
        mtime = float(p.stat().st_mtime)
    except OSError:
        return None

    cache_key = (key_path, mtime, int(max_width))
    cached = _FRAME_CACHE.get(cache_key)
    if cached is not None and not cached.isNull():
        return cached

    from zaliver.processing.ffmpeg_merge import resolve_ffmpeg_executable

    exe = resolve_ffmpeg_executable()
    if not exe:
        return None

    def _grab(ss: float) -> QPixmap | None:
        cmd = [
            exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, float(ss)):.3f}",
            "-i",
            key_path,
            "-frames:v",
            "1",
            "-vf",
            f"scale={max(160, int(max_width))}:-2",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-",
        ]
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                timeout=45,
                creationflags=_popen_flags(),
            )
        except Exception:
            return None
        if r.returncode != 0 or not r.stdout:
            return None
        pm = QPixmap()
        if not pm.loadFromData(r.stdout, "JPG") or pm.isNull():
            return None
        return pm

    pix = _grab(seek_sec)
    if pix is None and seek_sec > 0.05:
        pix = _grab(0.0)
    if pix is None:
        return None

    if len(_FRAME_CACHE) >= _FRAME_CACHE_MAX:
        try:
            _FRAME_CACHE.pop(next(iter(_FRAME_CACHE)))
        except StopIteration:
            pass
    _FRAME_CACHE[cache_key] = pix
    return pix


class _PreviewVideoWorker(QThread):
    """Декодирует ролик в raw RGB кадры примерно в реальном времени."""

    frame_ready = pyqtSignal(bytes, int, int)
    failed = pyqtSignal(str)
    finished_ok = pyqtSignal()

    def __init__(self, video_path: str, *, max_width: int = _PLAY_MAX_WIDTH) -> None:
        super().__init__()
        self._video_path = video_path
        self._max_width = max(160, int(max_width))
        self._proc: subprocess.Popen[bytes] | None = None
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True
        proc = self._proc
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            pass

    def run(self) -> None:
        from zaliver.processing.ffmpeg_merge import resolve_ffmpeg_executable
        from zaliver.processing.ffmpeg_probe import probe_video_stream

        exe = resolve_ffmpeg_executable()
        if not exe:
            self.failed.emit("ffmpeg не найден")
            return
        try:
            # Display-размер (после rotate/SAR) — как у статичного кадра превью.
            src_w, src_h, _fps, _fc, _fcc = probe_video_stream(self._video_path)
        except Exception as exc:
            self.failed.emit(str(exc) or "не удалось прочитать видео")
            return
        if src_w <= 0 or src_h <= 0:
            self.failed.emit("некорректный размер кадра")
            return

        # Ограничиваем по ширине превью, сохраняя пропорции display-кадра.
        out_w = min(self._max_width, int(src_w))
        out_h = max(2, int(round(src_h * (out_w / float(src_w)))))
        out_w -= out_w % 2
        out_h -= out_h % 2
        if out_w < 2 or out_h < 2:
            self.failed.emit("слишком маленький кадр")
            return

        frame_bytes = out_w * out_h * 3
        # force_original_aspect_ratio+pad: без растягивания, если autorotate
        # и probe чуть расходятся; setsar=1 — квадратные пиксели как у still JPG.
        vf = (
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1"
        )
        cmd = [
            exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-i",
            self._video_path,
            "-an",
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_popen_flags(),
            )
        except Exception as exc:
            self.failed.emit(str(exc) or "не удалось запустить ffmpeg")
            return

        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while not self._stop:
                data = proc.stdout.read(frame_bytes)
                if not data or len(data) < frame_bytes:
                    break
                if self._stop:
                    break
                self.frame_ready.emit(bytes(data), out_w, out_h)
        finally:
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
            self._proc = None
        if not self._stop:
            self.finished_ok.emit()


class TextOverlayPreviewWidget(QWidget):
    """Drag text on a vertical/horizontal frame; position stored as normalized anchor."""

    positionChanged = pyqtSignal(float, float)
    playbackChanged = pyqtSignal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(220, 240)
        self.setMaximumHeight(340)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self._orientation = "vertical"
        self._text = ""
        self._font_size = 95
        self._max_width_frac = 0.85
        self._glow_color = QColor("#00FFFF")
        self._text_color = QColor("#FFFFFF")
        self._anchor_x = 0.5
        self._anchor_y = 0.15
        self._glow_enabled = True
        self._letter_spacing = 0
        self._font_bold = True
        self._custom_font_path = ""
        self._dragging = False
        self._drag_offset = QPointF(0.0, 0.0)
        self._font_path = effective_font_path("", bold=True)
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
        self._bg_video_path: str | None = None
        self._bg_pixmap: QPixmap | None = None
        self._text_visible = True
        self._playing = False
        self._play_worker: _PreviewVideoWorker | None = None
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(33)
        self._anim_timer.timeout.connect(self._on_anim_tick)

        self._btn_play = QPushButton("▶", self)
        self._btn_play.setObjectName("textPreviewNav")
        self._btn_play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_play.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_play.setFixedSize(36, 36)
        self._btn_play.setToolTip("Смотреть видео с текстом")
        self._btn_play.setAutoDefault(False)
        self._btn_play.setDefault(False)
        self._btn_play.clicked.connect(self.toggle_playback)
        self._btn_play.setEnabled(False)
        self._position_play_button()

    def is_playing(self) -> bool:
        return bool(self._playing)

    def toggle_playback(self) -> None:
        if self._playing:
            self.stop_playback()
        else:
            self.start_playback()

    def start_playback(self) -> None:
        path = (self._bg_video_path or "").strip()
        if not path or not Path(path).is_file():
            return
        if self._playing:
            return
        self.stop_playback(restore_still=False)
        worker = _PreviewVideoWorker(path, max_width=_PLAY_MAX_WIDTH)
        worker.frame_ready.connect(self._on_play_frame)
        worker.failed.connect(self._on_play_failed)
        worker.finished_ok.connect(self._on_play_finished)
        worker.finished.connect(self._on_play_thread_finished)
        self._play_worker = worker
        self._playing = True
        self._anim_frame = 0
        self._stop_animation()
        self._sync_play_button()
        self.playbackChanged.emit(True)
        worker.start()

    def stop_playback(self, *, restore_still: bool = True) -> None:
        was_playing = self._playing or self._play_worker is not None
        worker = self._play_worker
        self._play_worker = None
        self._playing = False
        if worker is not None:
            try:
                worker.frame_ready.disconnect(self._on_play_frame)
            except TypeError:
                pass
            try:
                worker.failed.disconnect(self._on_play_failed)
            except TypeError:
                pass
            try:
                worker.finished_ok.disconnect(self._on_play_finished)
            except TypeError:
                pass
            try:
                worker.finished.disconnect(self._on_play_thread_finished)
            except TypeError:
                pass
            worker.request_stop()
            if worker.isRunning() and not worker.wait(1500):
                worker.terminate()
                worker.wait(500)
        self._sync_play_button()
        if was_playing:
            self.playbackChanged.emit(False)
        if restore_still and self._bg_video_path:
            self._bg_pixmap = load_video_frame_pixmap(self._bg_video_path)
            self.update()
        if self._text_visible:
            self._start_animation()

    def _on_play_frame(self, data: bytes, width: int, height: int) -> None:
        if not self._playing:
            return
        img = QImage(
            data,
            int(width),
            int(height),
            int(width) * 3,
            QImage.Format.Format_RGB888,
        )
        if img.isNull():
            return
        self._bg_pixmap = QPixmap.fromImage(img.copy())
        self._anim_frame += 1
        self.update()

    def _on_play_failed(self, _message: str) -> None:
        self.stop_playback(restore_still=True)

    def _on_play_finished(self) -> None:
        self.stop_playback(restore_still=True)

    def _on_play_thread_finished(self) -> None:
        # Поток уже завершился; stop_playback обычно уже вызван из finished_ok/failed.
        if self._play_worker is not None and not self._play_worker.isRunning():
            self._play_worker = None
        if self._playing:
            self.stop_playback(restore_still=True)
    def _sync_play_button(self) -> None:
        has_video = bool(self._bg_video_path)
        self._btn_play.setEnabled(has_video)
        if self._playing:
            self._btn_play.setText("⏹")
            self._btn_play.setToolTip("Остановить просмотр")
        else:
            self._btn_play.setText("▶")
            self._btn_play.setToolTip(
                "Смотреть видео с текстом"
                if has_video
                else "Нет видео для предпросмотра"
            )

    def _position_play_button(self) -> None:
        margin = 10
        x = max(margin, self.width() - self._btn_play.width() - margin)
        y = max(margin, self.height() - self._btn_play.height() - margin)
        self._btn_play.move(x, y)
        self._btn_play.raise_()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_play_button()

    def set_text_visible(self, visible: bool) -> None:
        """Показывать ли наложенный текст (выкл. — только кадр фона)."""
        visible = bool(visible)
        if visible == self._text_visible:
            return
        self._text_visible = visible
        if visible and not self._playing:
            self._start_animation()
        else:
            self._stop_animation()
        self.update()

    def set_background_video(
        self, path: str | Path | None, *, force: bool = False
    ) -> None:
        """Фон превью — кадр из исходного ролика (или сброс)."""
        raw = str(path or "").strip()
        if not raw:
            if self._playing:
                self.stop_playback(restore_still=False)
            if self._bg_video_path is None and self._bg_pixmap is None and not force:
                self._sync_play_button()
                return
            self._bg_video_path = None
            self._bg_pixmap = None
            self._sync_play_button()
            self.update()
            return
        try:
            resolved = str(Path(raw).resolve())
        except OSError:
            resolved = raw
        if (
            not force
            and resolved == self._bg_video_path
            and self._bg_pixmap is not None
            and not self._bg_pixmap.isNull()
            and not self._playing
        ):
            self._sync_play_button()
            return
        if self._playing:
            self.stop_playback(restore_still=False)
        self._bg_video_path = resolved
        # Сразу сбросить старый кадр, чтобы смена файла была заметна.
        self._bg_pixmap = None
        self._sync_play_button()
        self.update()
        self._bg_pixmap = load_video_frame_pixmap(resolved)
        self.update()

    def _on_anim_tick(self) -> None:
        if self._playing:
            return
        if not self._text_visible or not self._char_lines or not self.isVisible():
            return
        self._anim_frame += 1
        self.update()

    def _start_animation(self) -> None:
        if self._playing:
            return
        if self._text_visible and self._char_lines and self.isVisible():
            if not self._anim_timer.isActive():
                self._anim_timer.start()

    def _stop_animation(self) -> None:
        if self._anim_timer.isActive():
            self._anim_timer.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._position_play_button()
        if self._text_visible and not self._playing:
            self._start_animation()

    def hideEvent(self, event) -> None:
        if self._playing:
            self.stop_playback(restore_still=True)
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

    def set_glow_enabled(self, enabled: bool) -> None:
        self._glow_enabled = bool(enabled)
        self.update()

    def set_letter_spacing(self, spacing_px: int) -> None:
        self._letter_spacing = max(-50, min(120, int(spacing_px)))
        self._relayout()
        self.update()

    def set_font_path(self, custom_path: str = "") -> None:
        self._custom_font_path = (custom_path or "").strip()
        self._relayout()
        self.update()

    def set_font_bold(self, bold: bool) -> None:
        self._font_bold = bool(bold)
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
        self._font_path = effective_font_path(self._custom_font_path, bold=self._font_bold)
        _, _, fw, fh = self._frame_geometry()
        scale = fh / self._ref_size()[1] if fh > 0 else 1.0
        self._lines = wrap_text_lines(
            self._text,
            self._font_size,
            max_w,
            self._font_path,
            self._letter_spacing,
            bold=self._font_bold,
        )
        self._char_lines = [
            layout_line_chars(
                ln,
                self._font_size,
                self._font_path,
                self._letter_spacing,
                bold=self._font_bold,
            )
            for ln in self._lines
        ]
        self._block_w, self._block_h, self._line_h = measure_text_block(
            self._lines,
            self._font_size,
            self._font_path,
            self._letter_spacing,
            bold=self._font_bold,
        )
        painted_size = max(8, int(round(self._font_size * scale)))
        self._wave_amp = max(0.0, painted_size * self._wave_amp_frac)
        self._block_h += int(self._wave_amp * 2)

        if not self._text_visible or not self._char_lines or not any(self._char_lines):
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
            bold=self._font_bold,
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
        *,
        fill_color: QColor | None = None,
        apply_glow: bool = True,
    ) -> None:
        cy = int(y + wave_dy)
        cx = int(x)
        text_y = cy + fm.ascent()
        glow = self._glow_color

        if apply_glow and self._glow_enabled:
            for dx, dy, alpha in glow_layers:
                c = QColor(glow)
                c.setAlpha(max(0, min(255, int(alpha * 255))))
                painter.setPen(c)
                painter.drawText(int(cx + dx), int(text_y + dy), ch)

        core = QColor(fill_color) if fill_color is not None else QColor(self._text_color)
        painter.setPen(core)
        painter.drawText(int(cx), int(text_y), ch)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor("#0f1117"))

        fx, fy, fw, fh = self._frame_geometry()
        ref_h = self._ref_size()[1]
        scale = fh / ref_h if ref_h > 0 else 1.0
        frame_rect = QRectF(fx, fy, fw, fh)
        radius = 8.0

        clip = QPainterPath()
        clip.addRoundedRect(frame_rect, radius, radius)
        painter.setClipPath(clip)

        bg = self._bg_pixmap
        if bg is not None and not bg.isNull():
            scaled = bg.scaled(
                max(1, int(round(fw))),
                max(1, int(round(fh))),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            px = fx + (fw - scaled.width()) / 2.0
            py = fy + (fh - scaled.height()) / 2.0
            painter.drawPixmap(int(round(px)), int(round(py)), scaled)
            painter.fillRect(frame_rect, QColor(0, 0, 0, 40))
        else:
            painter.fillRect(frame_rect, QColor("#1a1f2e"))

        painter.setClipping(False)
        painter.setPen(QPen(QColor("#334155"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(frame_rect, radius, radius)

        if not self._text_visible:
            return

        if not self._char_lines or not any(self._char_lines):
            painter.setPen(QColor("#475569"))
            painter.drawText(
                int(fx + fw / 2 - 70),
                int(fy + fh / 2),
                "Введи текст…",
            )
            return

        block_x, block_y = self._block_top_left()
        main_font = self._font_for_paint(scale)
        painted_size = max(8, int(round(self._font_size * scale)))
        main_fm = QFontMetrics(main_font)
        line_h = max(int(self._line_h * scale), main_fm.height())
        wave_pad = int(self._wave_amp)
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
                emoji = is_emoji_unit(ch)
                color_path = resolve_color_emoji_font_path() if emoji else ""
                unit_path = (
                    color_path
                    if color_path
                    else font_path_for_unit(ch, self._font_path, bold=self._font_bold)
                )
                font = _make_qfont(unit_path, painted_size, bold=self._font_bold)
                painter.setFont(font)
                fm = QFontMetrics(font)
                wave_dy = wave_offset_y(
                    char_global,
                    self._anim_frame,
                    self._wave_amp,
                    char_phase=self._wave_char_phase,
                    frame_speed=self._wave_frame_speed,
                )
                color_emoji = bool(emoji and color_path)
                self._paint_neon_char(
                    painter,
                    fm,
                    ch,
                    block_x + int(x_off * scale),
                    y,
                    wave_dy,
                    glow_layers,
                    # Color glyphs ignore pen on Win/macOS; black avoids tinting.
                    fill_color=(
                        QColor(0, 0, 0)
                        if color_emoji
                        else (QColor("#FFFFFF") if emoji else None)
                    ),
                    apply_glow=not emoji,
                )
                char_global += 1
            y += line_h

    def _hit_test(self, pos: QPointF) -> bool:
        if not self._text_visible:
            return False
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
