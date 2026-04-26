"""Main application window."""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
from datetime import datetime
from functools import partial
from pathlib import Path

from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPointF,
    QSettings,
    QSize,
    QThread,
    QTimer,
    Qt,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QDesktopServices, QMouseEvent, QPixmap, QShowEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from zaliver.db.video_store import VideoStore
from zaliver.antydetect.api import DolphinAntyError, DolphinAntyPublicAPI
from zaliver.processing.ffmpeg_merge import check_ffmpeg_tools
from zaliver.processing.pipeline import RandomUniquifyBounds, UniquifySettings
from zaliver.processing.thread_worker import ProcessingController
from zaliver.ui.dolphin_profile_row import DolphinProfileRow
from zaliver.ui.ffmpeg_install_worker import FfmpegInstallWorker
from zaliver.ui.widgets import (
    AnimatedProgressBar,
    CollapsibleSection,
    SmoothSlider,
    ToggleSwitch,
)

# Qt SpinBox/DoubleSpinBox всегда имеют min/max.
# Чтобы в UI не было "лимитов", используем максимально широкие диапазоны,
# но оставляем минимальные логические ограничения там, где отрицательные значения
# ломают смысл (например, количество копий).
_INT_MIN = -2_147_483_648
_INT_MAX = 2_147_483_647
_BIG_FLOAT = 1.0e12

_READY_THUMB_W = 176
_READY_THUMB_H = 99


def _format_stored_datetime(iso_s: str) -> str:
    """Человекочитаемая дата/время из ISO-строки БД (в локальном поясе)."""
    if not (iso_s or "").strip():
        return "—"
    s = iso_s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%d.%m.%Y  %H:%M")
    except ValueError:
        return s.replace("T", " ")[:19]


class _ReadyVideoRow(QWidget):
    """Строка готового видео: открыть файл — только клик по превью; Ctrl/Shift — выделение; «Убрать» — из списка."""

    activated = pyqtSignal(str)
    remove_requested = pyqtSignal(int)

    def __init__(
        self,
        video_id: int,
        index: int,
        path: str,
        filename: str,
        when_text: str,
        thumb_path: str | None,
        tooltip: str,
        list_widget: QListWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # После setItemWidget родитель строки — viewport списка, не QListWidget.
        self._list = list_widget
        self._path = path
        self._video_id = video_id
        self._suppress_activate = False
        self._press_on_thumb_plain = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip(tooltip)

        row = QHBoxLayout(self)
        row.setSpacing(14)
        row.setContentsMargins(6, 6, 10, 6)

        num = QLabel(str(index))
        num.setObjectName("readyRowNumber")
        num.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )
        num.setFixedWidth(68)
        num.setMinimumHeight(_READY_THUMB_H)

        thumb = QLabel()
        thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        thumb.setToolTip("Клик — открыть видео в системе")
        thumb.setFixedSize(_READY_THUMB_W, _READY_THUMB_H)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setObjectName("readyThumb")
        pm: QPixmap | None = None
        if thumb_path:
            tp = Path(thumb_path)
            if tp.is_file():
                loaded = QPixmap(str(tp))
                if not loaded.isNull():
                    pm = loaded.scaled(
                        _READY_THUMB_W,
                        _READY_THUMB_H,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
        if pm is not None and not pm.isNull():
            thumb.setPixmap(pm)
        else:
            thumb.setText("нет превью")
            thumb.setObjectName("readyThumbEmpty")

        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        title = QLabel(filename)
        title.setObjectName("readyRowTitle")
        title.setWordWrap(True)
        sub = QLabel(f"Создан: {when_text}")
        sub.setObjectName("readyRowDate")
        sub.setWordWrap(True)
        text_col.addWidget(title)
        text_col.addWidget(sub)
        text_col.addStretch()

        row.addWidget(num)
        row.addWidget(thumb)
        row.addLayout(text_col, 1)

        self._btn_remove = QPushButton("Убрать")
        self._btn_remove.setObjectName("secondary")
        self._btn_remove.setCursor(Qt.CursorShape.ArrowCursor)
        self._btn_remove.setToolTip(
            "Убрать из списка приложения (файл на диске не удаляется)"
        )
        self._btn_remove.clicked.connect(
            lambda: self.remove_requested.emit(self._video_id)
        )
        row.addWidget(self._btn_remove, 0, Qt.AlignmentFlag.AlignTop)

        self._thumb = thumb
        for w in (num, thumb, title, sub):
            w.installEventFilter(self)

    def _own_item(self) -> QListWidgetItem | None:
        lw = self._list
        for i in range(lw.count()):
            it = lw.item(i)
            if lw.itemWidget(it) is self:
                return it
        return None

    def _body_mouse_press(self, event: QMouseEvent, *, on_thumb: bool) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        it = self._own_item()
        if it is None:
            return
        lw = self._list
        mods = event.modifiers()
        ctrl = mods & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
        )
        shift = mods & Qt.KeyboardModifier.ShiftModifier
        if ctrl:
            it.setSelected(not it.isSelected())
            lw.setCurrentItem(it)
            self._suppress_activate = True
            self._press_on_thumb_plain = False
            return
        if shift:
            anchor = lw.currentItem()
            if anchor is None:
                it.setSelected(True)
                lw.setCurrentItem(it)
            else:
                i_a = lw.row(anchor)
                i_b = lw.row(it)
                top, bottom = sorted((i_a, i_b))
                lw.clearSelection()
                for r in range(top, bottom + 1):
                    ri = lw.item(r)
                    if ri is not None:
                        ri.setSelected(True)
                lw.setCurrentItem(it)
            self._suppress_activate = True
            self._press_on_thumb_plain = False
            return
        lw.clearSelection()
        it.setSelected(True)
        lw.setCurrentItem(it)
        self._press_on_thumb_plain = on_thumb

    def _body_mouse_release(self, event: QMouseEvent, *, on_thumb: bool) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._suppress_activate:
            self._suppress_activate = False
            self._press_on_thumb_plain = False
            return
        if on_thumb and self._press_on_thumb_plain:
            self.activated.emit(self._path)
        self._press_on_thumb_plain = False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if isinstance(watched, QWidget) and isinstance(event, QMouseEvent):
            if event.type() == QEvent.Type.MouseButtonPress:
                gp = watched.mapToGlobal(event.position().toPoint())
                local = QPointF(self.mapFromGlobal(gp))
                synth = QMouseEvent(
                    QEvent.Type.MouseButtonPress,
                    local,
                    event.globalPosition(),
                    event.button(),
                    event.buttons(),
                    event.modifiers(),
                )
                on_thumb = watched is self._thumb
                self._body_mouse_press(synth, on_thumb=on_thumb)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                gp = watched.mapToGlobal(event.position().toPoint())
                local = QPointF(self.mapFromGlobal(gp))
                synth = QMouseEvent(
                    QEvent.Type.MouseButtonRelease,
                    local,
                    event.globalPosition(),
                    event.button(),
                    event.buttons(),
                    event.modifiers(),
                )
                on_thumb = watched is self._thumb
                self._body_mouse_release(synth, on_thumb=on_thumb)
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._btn_remove.geometry().contains(event.position().toPoint()):
            return super().mousePressEvent(event)
        on_thumb = self._thumb.geometry().contains(event.position().toPoint())
        self._body_mouse_press(event, on_thumb=on_thumb)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._btn_remove.geometry().contains(event.position().toPoint()):
            return super().mouseReleaseEvent(event)
        on_thumb = self._thumb.geometry().contains(event.position().toPoint())
        self._body_mouse_release(event, on_thumb=on_thumb)


def _default_workers() -> int:
    # Для одиночного длинного ролика приложение умеет нарезать на части (если есть ffmpeg)
    # и тем самым эффективно загрузить все CPU. Поэтому по умолчанию используем все
    # логические ядра, а не (CPU-1).
    return max(1, os.cpu_count() or 2)


def _max_worker_slider() -> int:
    # До всех логических CPU: при разбиении ролика на части полезнее занять последнее ядро.
    return max(1, os.cpu_count() or 2)


class MainWindow(QWidget):
    _after_video_saved = pyqtSignal()
    _profiles_loaded = pyqtSignal(object)
    _profiles_load_failed = pyqtSignal(str)
    _dolphin_google_ready = pyqtSignal(str)
    _dolphin_google_failed = pyqtSignal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Zaliver — уникализация видео")
        self.setObjectName("zaliverRoot")
        self._work_thread: QThread | None = None
        self._processor: ProcessingController | None = None
        self._ff_thread: QThread | None = None
        self._ff_worker: FfmpegInstallWorker | None = None
        self._ffmpeg_progress_dlg: QProgressDialog | None = None
        self._selected_input_files: list[str] = []
        self._video_store = VideoStore()

        self._settings = QSettings("Zaliver", "Zaliver")
        self._profiles_raw: list[dict[str, object]] | None = None
        self._profiles_filter_timer = QTimer(self)
        self._profiles_filter_timer.setSingleShot(True)
        self._profiles_filter_timer.timeout.connect(self._apply_profiles_filter)
        self._build_ui()
        self._profiles_loaded.connect(self._on_profiles_loaded)
        self._profiles_load_failed.connect(self._on_profiles_load_failed)
        self._dolphin_google_ready.connect(self._on_dolphin_google_ready)
        self._dolphin_google_failed.connect(self._on_dolphin_google_failed)
        self._after_video_saved.connect(self._refresh_ready_list)
        self._apply_theme()
        self.showMaximized()
        self._load_folder_settings()
        self._load_antydetect_settings()
        self._sync_ffmpeg_install_row()

    def _theme_path(self) -> Path:
        return Path(__file__).with_name("theme.qss")

    def _apply_theme(self) -> None:
        p = self._theme_path()
        if p.is_file():
            self.setStyleSheet(p.read_text(encoding="utf-8"))

    def _build_ui(self) -> None:
        home = QWidget()
        home_l = QVBoxLayout(home)
        home_l.setSpacing(12)
        home_l.setContentsMargins(12, 8, 12, 12)

        title = QLabel("Zaliver")
        title.setObjectName("title")
        sub = QLabel("Выбор видео → папка результатов · случайная уникализация ")
        sub.setObjectName("hint")

        self.btn_start = QPushButton("Старт")
        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setEnabled(False)
        self.btn_start.clicked.connect(self._start)
        self.btn_cancel.clicked.connect(self._cancel)

        self.progress = AnimatedProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValueImmediate(0)
        self.progress.setMinimumWidth(160)
        self.progress.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("hint")

        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        header_row.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(self.progress, 1, Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(self.btn_start, 0, Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(self.btn_cancel, 0, Qt.AlignmentFlag.AlignVCenter)
        home_l.addLayout(header_row)
        home_l.addWidget(self.progress_label)
        home_l.addWidget(sub)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        io = QGroupBox("Файлы и папка результата")
        io_grid = QGridLayout(io)
        btn_pick_files = QPushButton("Выбрать файлы…")
        btn_pick_files.setObjectName("secondary")
        btn_pick_files.clicked.connect(self._browse_input_files)
        self._input_files_hint = QLabel("")
        self._input_files_hint.setObjectName("hint")
        self._input_files_hint.setWordWrap(True)
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText("Папка для уникализированных файлов…")
        btn_out = QPushButton("Обзор…")
        btn_out.setObjectName("secondary")
        btn_out.clicked.connect(self._browse_output_dir)
        io_grid.addWidget(QLabel("Исходные видео:"), 0, 0)
        io_grid.addWidget(self._input_files_hint, 0, 1)
        io_grid.addWidget(btn_pick_files, 0, 2)
        io_grid.addWidget(QLabel("Выходная папка:"), 1, 0)
        io_grid.addWidget(self.output_dir_edit, 1, 1)
        io_grid.addWidget(btn_out, 1, 2)
        self.copies_per_file = QSpinBox()
        self.copies_per_file.setRange(1, _INT_MAX)
        self.copies_per_file.setValue(1)
        io_grid.addWidget(QLabel("Копий на исходник:"), 2, 0)
        io_grid.addWidget(self.copies_per_file, 2, 1)
        copies_hint = QLabel(
            "Каждая копия — отдельный прогон со своими случайными параметрами "
            "(при включённой случайной уникализации). Например: 10 видео × 5 = 50 файлов."
        )
        copies_hint.setObjectName("hint")
        copies_hint.setWordWrap(True)
        io_grid.addWidget(copies_hint, 3, 0, 1, 3)
        io_hint = QLabel(
            "Имена: имя_u_<случайные hex>.mp4 — у каждого выхода свой суффикс (не счётчик)."
        )
        io_hint.setObjectName("hint")
        io_hint.setWordWrap(True)
        io_grid.addWidget(io_hint, 4, 0, 1, 3)

        proc = QGroupBox("Обработка")
        pg = QGridLayout(proc)
        self.thread_slider = SmoothSlider(Qt.Orientation.Horizontal)
        self.thread_slider.setMinimum(1)
        # Единственный лимит в UI: количество потоков (до числа логических CPU).
        self.thread_slider.setMaximum(_max_worker_slider())
        self.thread_slider.setValue(_default_workers())
        self.thread_label = QLabel()
        self._update_thread_label(self.thread_slider.value())
        self.thread_slider.valueChanged.connect(self._update_thread_label)

        proc_hint = QLabel(
            "Обработка целиком через ffmpeg (фильтры + кодирование). Несколько роликов — "
            "параллельно по файлам; длинный ролик режется на части для загрузки CPU. "
            "Нужны ffmpeg и ffprobe в PATH. Результат — MP4 (H.264 + AAC из исходника, если есть звук)."
        )
        proc_hint.setObjectName("hint")
        proc_hint.setWordWrap(True)
        pg.addWidget(proc_hint, 0, 0, 1, 2)

        self._ffmpeg_row = QWidget()
        ff_row = QHBoxLayout(self._ffmpeg_row)
        ff_row.setContentsMargins(0, 0, 0, 0)
        self.ffmpeg_hint = QLabel()
        self.ffmpeg_hint.setObjectName("hint")
        self.ffmpeg_hint.setWordWrap(True)
        self.btn_install_ffmpeg = QPushButton("Установить ffmpeg")
        self.btn_install_ffmpeg.setObjectName("secondary")
        self.btn_install_ffmpeg.clicked.connect(self._on_install_ffmpeg)
        ff_row.addWidget(self.ffmpeg_hint, 1)
        ff_row.addWidget(self.btn_install_ffmpeg, 0, Qt.AlignmentFlag.AlignRight)
        pg.addWidget(self._ffmpeg_row, 1, 0, 1, 2)

        self.use_gpu = ToggleSwitch("Использовать GPU для кодирования (если доступно)")
        self.use_gpu.setChecked(True)
        gpu_hint = QLabel(
            "Если ffmpeg поддерживает NVENC/QSV/AMF, сегменты будут кодироваться быстрее. "
            "Эффекты считаются в CPU, ускоряется именно энкод."
        )
        gpu_hint.setObjectName("hint")
        gpu_hint.setWordWrap(True)
        pg.addWidget(self.use_gpu, 2, 0, 1, 2)
        pg.addWidget(gpu_hint, 3, 0, 1, 2)

        pg.addWidget(QLabel("Потоков процессов:"), 4, 0)
        thr_row = QHBoxLayout()
        thr_row.addWidget(self.thread_slider, 1)
        thr_row.addWidget(self.thread_label)
        w_thr = QWidget()
        w_thr.setLayout(thr_row)
        pg.addWidget(w_thr, 4, 1)

        fx = QGroupBox("Уникализация (лёгкие эффекты)")
        fx_layout = QVBoxLayout(fx)
        fx_layout.setSpacing(8)

        self.random_uniquify = ToggleSwitch(
            "Случайные параметры для каждого файла (каждый запуск — новый набор)"
        )
        self.random_uniquify.setChecked(True)
        self.random_uniquify.toggled.connect(self._on_random_uniquify_toggled)
        fx_layout.addWidget(self.random_uniquify)

        self._random_bounds_section = CollapsibleSection(
            "Границы случайной уникализации (от / до)"
        )
        bounds_inner = QWidget()
        rg = QGridLayout(bounds_inner)
        rg.setHorizontalSpacing(8)

        def _dspin(lo: float, hi: float, step: float, dec: int) -> tuple[QDoubleSpinBox, QDoubleSpinBox]:
            a, b = QDoubleSpinBox(), QDoubleSpinBox()
            for w in (a, b):
                w.setRange(-_BIG_FLOAT, _BIG_FLOAT)
                w.setSingleStep(step)
                w.setDecimals(dec)
                w.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            a.setValue(lo)
            b.setValue(hi)
            return a, b

        def _ispin(lo: int, hi: int) -> tuple[QSpinBox, QSpinBox]:
            a, b = QSpinBox(), QSpinBox()
            for w in (a, b):
                w.setRange(_INT_MIN, _INT_MAX)
            a.setValue(lo)
            b.setValue(hi)
            return a, b

        def _bounds_row(row: int, title: str, w_lo: QWidget, w_hi: QWidget) -> None:
            rg.addWidget(QLabel(title), row, 0)
            rg.addWidget(QLabel("от"), row, 1)
            rg.addWidget(w_lo, row, 2)
            rg.addWidget(QLabel("до"), row, 3)
            rg.addWidget(w_hi, row, 4)

        br = 0
        self.rb_brightness_min, self.rb_brightness_max = _dspin(-22.0, 22.0, 1.0, 1)
        _bounds_row(br, "Яркость (±)", self.rb_brightness_min, self.rb_brightness_max)
        br += 1
        self.rb_contrast_min, self.rb_contrast_max = _dspin(0.88, 1.14, 0.01, 3)
        _bounds_row(br, "Контраст", self.rb_contrast_min, self.rb_contrast_max)
        br += 1
        self.rb_saturation_min, self.rb_saturation_max = _dspin(0.88, 1.12, 0.01, 3)
        _bounds_row(br, "Насыщенность", self.rb_saturation_min, self.rb_saturation_max)
        br += 1
        self.rb_crop_jitter_min, self.rb_crop_jitter_max = _ispin(0, 3)
        _bounds_row(br, "Кроп-джиттер (px)", self.rb_crop_jitter_min, self.rb_crop_jitter_max)
        br += 1
        self.rb_scale_pct_min, self.rb_scale_pct_max = _dspin(95, 100.6, 0.1, 2)
        _bounds_row(br, "Масштаб %", self.rb_scale_pct_min, self.rb_scale_pct_max)
        br += 1
        self.rb_noise_min, self.rb_noise_max = _dspin(0.5, 4.0, 0.05, 2)
        _bounds_row(br, "Шум σ", self.rb_noise_min, self.rb_noise_max)
        br += 1
        self.rb_seed_min, self.rb_seed_max = _ispin(0, 99_999_999)
        _bounds_row(br, "Seed", self.rb_seed_min, self.rb_seed_max)
        br += 1
        self.audio_speed_min, self.audio_speed_max = _dspin(1.0, 1.1, 0.01, 2)
        _bounds_row(br, "Скорость видео+аудио (x)", self.audio_speed_min, self.audio_speed_max)
        br += 1
        self.audio_chorus_prob = QDoubleSpinBox()
        self.audio_chorus_prob.setRange(-_BIG_FLOAT, _BIG_FLOAT)
        self.audio_chorus_prob.setSingleStep(0.05)
        self.audio_chorus_prob.setDecimals(2)
        self.audio_chorus_prob.setValue(0.45)
        self.audio_chorus_prob.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        rg.addWidget(QLabel("Вероятность хора (0…1):"), br, 0, 1, 2)
        rg.addWidget(self.audio_chorus_prob, br, 2, 1, 3)
        self._random_bounds_section.content_layout().addWidget(bounds_inner)
        self._random_bounds_section.set_expanded(True)
        fx_layout.addWidget(self._random_bounds_section)
        self._random_bounds_panel = bounds_inner

        self._manual_section = CollapsibleSection("Ручные параметры и аудио")
        manual_inner = QWidget()
        mg = QGridLayout(manual_inner)

        self.brightness = QSpinBox()
        self.brightness.setRange(_INT_MIN, _INT_MAX)
        self.brightness.setValue(0)
        self.contrast = QDoubleSpinBox()
        self.contrast.setRange(-_BIG_FLOAT, _BIG_FLOAT)
        self.contrast.setSingleStep(0.01)
        self.contrast.setValue(1.0)
        self.contrast.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.saturation = QDoubleSpinBox()
        self.saturation.setRange(-_BIG_FLOAT, _BIG_FLOAT)
        self.saturation.setSingleStep(0.01)
        self.saturation.setValue(1.0)
        self.saturation.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.crop_jitter = QSpinBox()
        self.crop_jitter.setRange(_INT_MIN, _INT_MAX)
        self.crop_jitter.setValue(1)
        self.scale_pct = QDoubleSpinBox()
        self.scale_pct.setRange(-_BIG_FLOAT, _BIG_FLOAT)
        self.scale_pct.setDecimals(2)
        self.scale_pct.setValue(100.0)
        self.scale_pct.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.noise = QDoubleSpinBox()
        self.noise.setRange(-_BIG_FLOAT, _BIG_FLOAT)
        self.noise.setSingleStep(0.5)
        self.noise.setValue(1.0)
        self.noise.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.seed = QSpinBox()
        self.seed.setRange(_INT_MIN, _INT_MAX)
        self.seed.setValue(42)

        self._manual_video_widgets = [
            self.brightness,
            self.contrast,
            self.saturation,
            self.crop_jitter,
            self.scale_pct,
            self.noise,
            self.seed,
        ]

        r = 0
        for label, w in [
            ("Яркость (±):", self.brightness),
            ("Контраст:", self.contrast),
            ("Насыщенность:", self.saturation),
            ("Кроп-джиттер (px):", self.crop_jitter),
            ("Масштаб %:", self.scale_pct),
            ("Шум σ:", self.noise),
            ("Seed:", self.seed),
        ]:
            mg.addWidget(QLabel(label), r, 0)
            mg.addWidget(w, r, 1)
            r += 1

        mg.addWidget(QLabel("— Случайные: включение —"), r, 0, 1, 2)
        r += 1
        self.audio_speed = ToggleSwitch(
            "Ускорение видео и аудио (случайно, один коэффициент)"
        )
        self.audio_speed.setChecked(True)
        self.audio_chorus = ToggleSwitch("Лёгкий хорус (случайно)")
        self.audio_chorus.setChecked(True)

        self._random_audio_widgets = [
            self.audio_speed,
            self.audio_chorus,
        ]

        mg.addWidget(self.audio_speed, r, 0, 1, 2)
        r += 1
        mg.addWidget(self.audio_chorus, r, 0, 1, 2)
        r += 1

        mg.addWidget(QLabel("— Скорость и аудио (ручные) —"), r, 0, 1, 2)
        r += 1
        self.playback_speed_manual = QDoubleSpinBox()
        self.playback_speed_manual.setRange(-_BIG_FLOAT, _BIG_FLOAT)
        self.playback_speed_manual.setSingleStep(0.01)
        self.playback_speed_manual.setDecimals(2)
        self.playback_speed_manual.setValue(1.05)
        self.playback_speed_manual.setButtonSymbols(
            QAbstractSpinBox.ButtonSymbols.NoButtons
        )
        self.audio_chorus_manual = ToggleSwitch("Хорус (включить)")
        self.audio_chorus_manual.setChecked(False)
        self._manual_audio_widgets = [
            self.playback_speed_manual,
            self.audio_chorus_manual,
        ]
        mg.addWidget(QLabel("Скорость видео+аудио (x):"), r, 0)
        mg.addWidget(self.playback_speed_manual, r, 1)
        r += 1
        mg.addWidget(self.audio_chorus_manual, r, 0, 1, 2)
        r += 1

        self._manual_section.content_layout().addWidget(manual_inner)
        fx_layout.addWidget(self._manual_section)
        self._manual_panel = manual_inner
        self._manual_section.set_expanded(True)
        self._on_random_uniquify_toggled(self.random_uniquify.isChecked())

        scroll_left = QScrollArea()
        scroll_left.setWidgetResizable(True)
        scroll_left.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_left.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner_left = QWidget()
        inner_left_l = QVBoxLayout(inner_left)
        inner_left_l.addWidget(io)
        inner_left_l.addWidget(proc)
        inner_left_l.addWidget(fx)
        inner_left_l.addStretch()
        scroll_left.setWidget(inner_left)
        scroll_left.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        right = QWidget()
        rl = QVBoxLayout(right)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(220)
        self.log.setPlaceholderText("Лог…")
        rl.addWidget(self.log, 1)

        splitter.addWidget(scroll_left)
        splitter.addWidget(right)
        splitter.setSizes([420, 580])
        home_l.addWidget(splitter, 1)

        ready = QWidget()
        ready_l = QVBoxLayout(ready)
        ready_l.setSpacing(10)
        ready_l.setContentsMargins(12, 12, 12, 12)
        ready_title = QLabel("Готовые видео")
        ready_title.setObjectName("title")
        ready_hint = QLabel(
            "Список сохранённых результатов (SQLite). Клик по превью — открыть файл. "
            "Ctrl+клик или Shift+клик по строке — выделить несколько; затем «Удалить выбранные…». "
            "Файлы на диске при удалении из списка не удаляются."
        )
        ready_hint.setObjectName("hint")
        ready_hint.setWordWrap(True)
        ready_top = QHBoxLayout()
        btn_refresh_ready = QPushButton("Обновить список")
        btn_refresh_ready.setObjectName("secondary")
        btn_refresh_ready.clicked.connect(self._refresh_ready_list)
        btn_remove_selected = QPushButton("Удалить выбранные…")
        btn_remove_selected.setObjectName("danger")
        btn_remove_selected.clicked.connect(self._on_ready_remove_selected)
        ready_top.addWidget(ready_title)
        ready_top.addStretch()
        ready_top.addWidget(btn_remove_selected)
        ready_top.addWidget(btn_refresh_ready)
        ready_l.addLayout(ready_top)
        ready_l.addWidget(ready_hint)
        self._ready_list = QListWidget()
        self._ready_list.setObjectName("readyList")
        self._ready_list.setSpacing(6)
        self._ready_list.setAlternatingRowColors(False)
        self._ready_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._ready_list.setUniformItemSizes(True)
        ready_l.addWidget(self._ready_list, 1)

        profiles = QWidget()
        profiles_l = QVBoxLayout(profiles)
        profiles_l.setSpacing(10)
        profiles_l.setContentsMargins(12, 12, 12, 12)
        profiles_title = QLabel("Профили Dolphin Anty")
        profiles_title.setObjectName("title")
        profiles_hint = QLabel(
            "Подгрузка профилей через глобальный Public API Dolphin{anty} "
            "(нужен JWT токен из личного кабинета; отправляется как Authorization: Bearer …). "
            "Поле поиска фильтрует уже загруженный список; кнопка «Обновить» (или Enter) — повторный запрос к API. "
            "Клик по профилю запускает его через Local API в headless, открывает YouTube Studio "
            "и загрузку последнего готового видео из каталога (сессия Studio должна быть в профиле). "
            "Клики не блокируют друг друга (параллельно в фоне)."
        )
        profiles_hint.setObjectName("hint")
        profiles_hint.setWordWrap(True)

        profiles_top = QHBoxLayout()
        self._dolphin_query = QLineEdit()
        self._dolphin_query.setPlaceholderText("Поиск по загруженным профилям…")
        self._btn_profiles_refresh = QPushButton("Обновить")
        self._btn_profiles_refresh.setObjectName("secondary")
        self._btn_profiles_refresh.clicked.connect(self._refresh_antydetect_profiles)
        profiles_top.addWidget(profiles_title)
        profiles_top.addStretch()
        profiles_top.addWidget(self._dolphin_query, 1)
        profiles_top.addWidget(self._btn_profiles_refresh)

        self._profiles_status = QLabel("")
        self._profiles_status.setObjectName("hint")
        self._profiles_status.setWordWrap(True)

        self._profiles_list = QListWidget()
        self._profiles_list.setObjectName("profilesList")
        self._profiles_list.setSpacing(4)
        self._profiles_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._dolphin_query.textChanged.connect(self._schedule_profiles_filter)
        self._dolphin_query.returnPressed.connect(self._refresh_antydetect_profiles)
        self._profiles_list.itemClicked.connect(self._on_profiles_list_clicked)

        profiles_l.addLayout(profiles_top)
        profiles_l.addWidget(profiles_hint)
        profiles_l.addWidget(self._profiles_status)
        profiles_l.addWidget(self._profiles_list, 1)

        settings = QWidget()
        settings_l = QVBoxLayout(settings)
        settings_l.setSpacing(12)
        settings_l.setContentsMargins(12, 12, 12, 12)
        settings_title = QLabel("Настройки")
        settings_title.setObjectName("title")
        settings_hint = QLabel(
            "Настройки интеграции с антидетект-браузером Dolphin{anty}. "
            "Токен хранится локально в настройках приложения (QSettings). "
            "Для загрузки списка профилей используется Public API (Authorization: Bearer …). "
            "Local API нужен для запуска/остановки профиля и подключения автоматизации (CDP)."
        )
        settings_hint.setObjectName("hint")
        settings_hint.setWordWrap(True)

        gb = QGroupBox("Dolphin Anty")
        gg = QGridLayout(gb)
        public_host = QLabel("Public API: https://dolphin-anty-api.com")
        public_host.setObjectName("hint")
        public_host.setWordWrap(True)
        self._dolphin_token = QLineEdit()
        self._dolphin_token.setPlaceholderText("JWT токен (Public API)…")
        self._dolphin_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._dolphin_token.setToolTip(
            "Токен из личного кабинета Dolphin. "
            "Используется для Public API как заголовок Authorization: Bearer <token>."
        )

        self._btn_save_antydetect = QPushButton("Сохранить")
        self._btn_save_antydetect.setObjectName("secondary")
        self._btn_save_antydetect.clicked.connect(self._save_antydetect_settings)

        self._btn_test_profiles = QPushButton("Проверить и загрузить профили")
        self._btn_test_profiles.clicked.connect(self._refresh_antydetect_profiles)

        self._settings_status = QLabel("")
        self._settings_status.setObjectName("hint")
        self._settings_status.setWordWrap(True)

        gg.addWidget(public_host, 0, 0, 1, 2)
        gg.addWidget(QLabel("JWT:"), 1, 0)
        gg.addWidget(self._dolphin_token, 1, 1)
        btns = QHBoxLayout()
        btns.addWidget(self._btn_test_profiles)
        btns.addStretch()
        btns.addWidget(self._btn_save_antydetect)
        w_btns = QWidget()
        w_btns.setLayout(btns)
        gg.addWidget(w_btns, 2, 0, 1, 2)
        gg.addWidget(self._settings_status, 3, 0, 1, 2)

        settings_l.addWidget(settings_title)
        settings_l.addWidget(settings_hint)
        settings_l.addWidget(gb)
        settings_l.addStretch()

        self._stack = QStackedWidget()
        self._stack.addWidget(home)
        self._stack.addWidget(ready)
        self._stack.addWidget(profiles)
        self._stack.addWidget(settings)

        self._nav = QListWidget()
        self._nav.setObjectName("sideNav")
        self._nav.setFixedWidth(210)
        self._nav.addItems(["Главная", "Готовые видео", "Профили", "Настройки"])
        self._nav.setCurrentRow(0)
        self._nav.currentRowChanged.connect(self._on_nav_row_changed)

        outer = QHBoxLayout(self)
        outer.setSpacing(12)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.addWidget(self._nav)
        outer.addWidget(self._stack, 1)

    def _on_nav_row_changed(self, row: int) -> None:
        self._stack.setCurrentIndex(max(0, min(row, self._stack.count() - 1)))
        if row == 1:
            self._refresh_ready_list()
        if row == 2:
            self._refresh_antydetect_profiles()

    def _open_video_path(self, raw: str) -> None:
        if not raw:
            return
        p = Path(str(raw))
        try:
            if not p.is_file():
                QMessageBox.warning(self, "Zaliver", "Файл не найден (возможно, удалён).")
                self._refresh_ready_list()
                return
        except OSError:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.resolve())))

    def _on_ready_remove_requested(self, video_id: int) -> None:
        r = QMessageBox.question(
            self,
            "Zaliver",
            "Убрать эту запись из списка? Файл видео на диске не будет удалён.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        try:
            ok = self._video_store.remove_video_record(int(video_id))
        except (OSError, sqlite3.Error):
            QMessageBox.warning(self, "Zaliver", "Не удалось удалить запись.")
            return
        if not ok:
            QMessageBox.warning(
                self, "Zaliver", "Запись не найдена (список будет обновлён)."
            )
        self._refresh_ready_list()

    def _on_ready_remove_selected(self) -> None:
        items = self._ready_list.selectedItems()
        if not items:
            QMessageBox.information(
                self,
                "Zaliver",
                "Выделите строки (Ctrl+клик или Shift+клик по строкам), "
                "затем снова нажмите «Удалить выбранные…».",
            )
            return
        ids: list[int] = []
        for it in items:
            raw = it.data(Qt.ItemDataRole.UserRole + 1)
            if raw is not None:
                ids.append(int(raw))
        if not ids:
            return
        n = len(ids)
        r = QMessageBox.question(
            self,
            "Zaliver",
            f"Убрать из списка выбранные записи ({n} шт.)? "
            "Файлы видео на диске не будут удалены.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = self._video_store.remove_video_records(ids)
        except (OSError, sqlite3.Error):
            QMessageBox.warning(self, "Zaliver", "Не удалось удалить записи.")
            return
        if removed < n:
            QMessageBox.warning(
                self,
                "Zaliver",
                f"Удалено записей: {removed} из {n} (остальные уже отсутствовали в базе).",
            )
        self._refresh_ready_list()

    def _refresh_ready_list(self) -> None:
        if not hasattr(self, "_ready_list"):
            return
        try:
            self._video_store.prune_missing_files()
        except OSError:
            pass
        self._ready_list.clear()
        try:
            rows = self._video_store.list_videos(500)
        except OSError:
            rows = []
        row_h = _READY_THUMB_H + 28
        vw = self._ready_list.viewport().width()
        w_hint = max(520, vw - 8) if vw > 80 else 560
        for i, v in enumerate(rows, start=1):
            name = Path(v.path).name
            tip = f"{v.path}\nСоздан файл: {v.created_at}\nДобавлено в список: {v.added_at}"
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, v.path)
            it.setData(Qt.ItemDataRole.UserRole + 1, int(v.id))
            it.setToolTip(tip)
            it.setSizeHint(QSize(w_hint, row_h))
            self._ready_list.addItem(it)
            row_w = _ReadyVideoRow(
                v.id,
                i,
                v.path,
                name,
                _format_stored_datetime(v.created_at),
                v.thumb_path,
                tip,
                self._ready_list,
                parent=self._ready_list,
            )
            row_w.activated.connect(self._open_video_path)
            row_w.remove_requested.connect(self._on_ready_remove_requested)
            self._ready_list.setItemWidget(it, row_w)

    def _on_output_saved(self, path: str) -> None:
        def work() -> None:
            try:
                self._video_store.upsert_video(path)
            finally:
                self._after_video_saved.emit()

        threading.Thread(target=work, daemon=True).start()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._sync_ffmpeg_install_row()

    def _sync_ffmpeg_install_row(self) -> None:
        if check_ffmpeg_tools():
            self._ffmpeg_row.setVisible(False)
            return
        self._ffmpeg_row.setVisible(True)
        if sys.platform == "darwin":
            hint = (
                "ffmpeg/ffprobe не найдены — без них обработка недоступна. "
                "Кнопка справа: сначала Homebrew (brew install ffmpeg), иначе "
                "скачивание статической сборки (нужен интернет). На Apple Silicon "
                "лучше поставить brew."
            )
        else:
            hint = (
                "ffmpeg/ffprobe не найдены — без них обработка недоступна. "
                "Нажмите кнопку справа (winget или pip, нужен интернет)."
            )
        self.ffmpeg_hint.setText(hint)

    def _on_ff_install_progress(self, value: int, text: str) -> None:
        dlg = self._ffmpeg_progress_dlg
        if dlg is None:
            return
        dlg.setValue(max(0, min(100, int(value))))
        dlg.setLabelText(text or "…")

    def _on_ff_worker_finished(self, ok: bool, msg: str) -> None:
        dlg = self._ffmpeg_progress_dlg
        if dlg is not None:
            dlg.setValue(100)
            dlg.close()
        self._ffmpeg_progress_dlg = None
        self.btn_install_ffmpeg.setEnabled(True)
        self._sync_ffmpeg_install_row()
        if ok:
            QMessageBox.information(
                self,
                "Zaliver",
                f"ffmpeg установлен и будет использован приложением:\n{msg}",
            )
        else:
            QMessageBox.critical(
                self,
                "Zaliver",
                f"Не удалось установить ffmpeg:\n{msg}",
            )

    def _on_ff_thread_finished(self) -> None:
        self._ff_thread = None
        if self._ff_worker is not None:
            self._ff_worker.deleteLater()
            self._ff_worker = None

    def _on_install_ffmpeg(self) -> None:
        if self._ff_thread is not None and self._ff_thread.isRunning():
            return
        if self._work_thread is not None and self._work_thread.isRunning():
            QMessageBox.warning(
                self,
                "Zaliver",
                "Дождитесь окончания обработки видео или нажмите «Отмена».",
            )
            return
        if check_ffmpeg_tools():
            self._sync_ffmpeg_install_row()
            return

        dlg = QProgressDialog(self)
        dlg.setWindowTitle("Установка ffmpeg")
        dlg.setLabelText("Подготовка…")
        dlg.setRange(0, 100)
        dlg.setValue(0)
        dlg.setMinimumDuration(0)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        try:
            dlg.setCancelButton(None)
        except (TypeError, AttributeError):
            pass
        self._ffmpeg_progress_dlg = dlg
        dlg.show()

        self.btn_install_ffmpeg.setEnabled(False)
        self._append_log("— Установка ffmpeg —")

        self._ff_thread = QThread()
        self._ff_worker = FfmpegInstallWorker()
        self._ff_worker.moveToThread(self._ff_thread)
        self._ff_thread.started.connect(self._ff_worker.run)
        self._ff_worker.log_line.connect(self._append_log)
        self._ff_worker.progress.connect(self._on_ff_install_progress)
        self._ff_worker.finished.connect(self._on_ff_worker_finished)
        self._ff_worker.finished.connect(self._ff_thread.quit)
        self._ff_thread.finished.connect(self._on_ff_thread_finished)
        self._ff_thread.start()

    def _update_thread_label(self, v: int) -> None:
        mx = _max_worker_slider()
        self.thread_label.setText(f"{int(v)} / {mx}")

    def _load_folder_settings(self) -> None:
        out = self._settings.value("output_folder", "", type=str) or ""
        self.output_dir_edit.setText(out)
        try:
            files = self._settings.value("input_files", [], type=list) or []
        except Exception:
            files = []
        self._selected_input_files = [str(x) for x in files if str(x).strip()]
        self._sync_input_files_hint()

    def _save_folder_settings(self) -> None:
        self._settings.setValue("output_folder", self.output_dir_edit.text().strip())
        self._settings.setValue("input_files", list(self._selected_input_files))

    def _load_antydetect_settings(self) -> None:
        if not hasattr(self, "_dolphin_token"):
            return
        token = self._settings.value("antydetect/dolphin_token", "", type=str) or ""
        self._dolphin_token.setText((token or "").strip())

    def _save_antydetect_settings(self) -> None:
        token = (self._dolphin_token.text() or "").strip()
        # Не затираем сохранённый JWT пустым значением: вкладка «Профили» тоже вызывает сохранение,
        # и пустое поле токена (до первого открытия «Настройки») иначе стирает корректный токен из QSettings.
        if token:
            self._settings.setValue("antydetect/dolphin_token", token)
        try:
            self._settings.sync()
        except Exception:
            pass
        if hasattr(self, "_settings_status"):
            self._settings_status.setText("Сохранено.")

    @staticmethod
    def _profile_search_blob(profile: dict[str, object]) -> str:
        parts: list[str] = []

        def add(v: object) -> None:
            if v is None:
                return
            if isinstance(v, str):
                s = v.strip()
                if s:
                    parts.append(s)
                return
            if isinstance(v, (int, float, bool)):
                parts.append(str(v))
                return
            if isinstance(v, dict):
                for vv in v.values():
                    add(vv)
                return
            if isinstance(v, list):
                for vv in v:
                    add(vv)

        add(profile.get("id"))
        add(profile.get("browserProfileId"))
        add(profile.get("name"))
        add(profile.get("mainWebsite"))
        add(profile.get("tags"))
        add(profile.get("proxy"))
        add(profile.get("notes"))
        add(profile.get("note"))
        add(profile.get("status"))
        add(profile.get("statusId"))
        return " ".join(parts).lower()

    @staticmethod
    def _profile_matches(profile: dict[str, object], needle: str) -> bool:
        q = (needle or "").strip().lower()
        if not q:
            return True
        return q in MainWindow._profile_search_blob(profile)

    def _render_profiles_items(self, profiles: list[dict[str, object]]) -> int:
        self._profiles_list.clear()
        n = 0
        for it in profiles:
            pid = str(it.get("id") or it.get("browserProfileId") or "").strip()
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, it)
            item.setData(Qt.ItemDataRole.UserRole + 1, pid)

            row = DolphinProfileRow(it, self._profiles_list)
            item.setSizeHint(row.sizeHint())
            self._profiles_list.addItem(item)
            self._profiles_list.setItemWidget(item, row)
            n += 1
        return n

    def _schedule_profiles_filter(self) -> None:
        if not hasattr(self, "_profiles_list"):
            return
        if self._profiles_raw is None:
            return
        self._profiles_filter_timer.start(150)

    def _apply_profiles_filter(self) -> None:
        if not hasattr(self, "_profiles_list"):
            return
        raw = self._profiles_raw
        if raw is None:
            return

        q = (self._dolphin_query.text() if hasattr(self, "_dolphin_query") else "") or ""
        q = q.strip()
        filtered = [p for p in raw if isinstance(p, dict) and self._profile_matches(p, q)]
        shown = self._render_profiles_items(filtered)
        total = len(raw)
        if q:
            self._profiles_status.setText(f"Фильтр: показано {shown} из {total}")
        else:
            self._profiles_status.setText(f"Загружено профилей: {total}")

    def _refresh_antydetect_profiles(self) -> None:
        if not hasattr(self, "_profiles_list"):
            return
        self._profiles_filter_timer.stop()
        self._save_antydetect_settings()

        token = (self._dolphin_token.text() or "").strip()
        if not token:
            token = (self._settings.value("antydetect/dolphin_token", "", type=str) or "").strip()

        self._btn_profiles_refresh.setEnabled(False)
        self._profiles_status.setText("Загрузка профилей…")

        t = threading.Thread(
            target=self._profiles_worker,
            kwargs={"token": token},
            daemon=True,
        )
        t.start()

    def _profiles_worker(self, *, token: str) -> None:
        try:
            api = DolphinAntyPublicAPI(token=token)
            try:
                # Public API: limit max 100 (OpenAPI). Поиск в UI — локально по загруженному списку.
                profiles = api.list_profiles(limit=100, query=None)
            finally:
                api.close()
            self._profiles_loaded.emit(profiles)
        except DolphinAntyError as e:
            self._profiles_load_failed.emit(str(e))
        except Exception as e:
            self._profiles_load_failed.emit(repr(e))

    def _on_profiles_loaded(self, profiles_obj: object) -> None:
        self._btn_profiles_refresh.setEnabled(True)
        profiles = profiles_obj if isinstance(profiles_obj, list) else []
        cleaned: list[dict[str, object]] = [p for p in profiles if isinstance(p, dict)]
        self._profiles_raw = cleaned
        self._apply_profiles_filter()

    def _on_profiles_load_failed(self, message: str) -> None:
        self._btn_profiles_refresh.setEnabled(True)
        self._profiles_raw = None
        if hasattr(self, "_profiles_list"):
            self._profiles_list.clear()
        self._profiles_status.setText(
            "Не удалось загрузить профили. Проверьте JWT токен (Public API: https://dolphin-anty-api.com).\n"
            f"{message}"
        )

    def _on_profiles_list_clicked(self, item: QListWidgetItem) -> None:
        pid = (item.data(Qt.ItemDataRole.UserRole + 1) or "").strip()
        if not pid:
            self._profiles_status.setText("У профиля нет ID — запуск через Local API невозможен.")
            return
        token = (self._dolphin_token.text() or "").strip()
        if not token:
            token = (self._settings.value("antydetect/dolphin_token", "", type=str) or "").strip()
        threading.Thread(
            target=self._dolphin_google_worker,
            kwargs={"profile_id": pid, "token": token},
            daemon=True,
        ).start()

    def _dolphin_google_worker(self, *, profile_id: str, token: str) -> None:
        try:
            from zaliver.antydetect.dolphin_open import open_google_in_profile

            open_google_in_profile(
                profile_id,
                local_token=token or None,
                headless=True,
            )
            self._dolphin_google_ready.emit(profile_id)
        except Exception as e:
            self._dolphin_google_failed.emit(profile_id, str(e))

    def _on_dolphin_google_ready(self, _profile_id: str) -> None:
        if self._profiles_raw is not None:
            self._apply_profiles_filter()

    def _on_dolphin_google_failed(self, profile_id: str, message: str) -> None:
        if self._profiles_raw is not None:
            self._apply_profiles_filter()
        self._profiles_status.setText(
            f"Профиль {profile_id}: не удалось открыть YouTube Studio / загрузку. "
            f"Нужны Dolphin, Local API, playwright и сессия Studio в профиле. {message}"
        )

    def _browse_input_files(self) -> None:
        if self._selected_input_files:
            start_dir = str(Path(self._selected_input_files[0]).parent)
        else:
            start_dir = str(Path.home())
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите видеофайлы для обработки (можно несколько)",
            start_dir,
            "Видео (*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts);;Все файлы (*)",
        )
        if files:
            self._selected_input_files = [f for f in files if str(f).strip()]
            self._sync_input_files_hint()
            self._save_folder_settings()

    def _sync_input_files_hint(self) -> None:
        if not hasattr(self, "_input_files_hint"):
            return
        n = len(self._selected_input_files or [])
        if n <= 0:
            self._input_files_hint.setText("Не выбрано — нажмите «Выбрать файлы…»")
            self._input_files_hint.setToolTip("")
            return
        names = [Path(p).name for p in self._selected_input_files]
        preview = ", ".join(names[:4])
        if n > 4:
            preview = f"{preview} и ещё {n - 4}"
        self._input_files_hint.setText(f"Выбрано: {n} ({preview})")
        self._input_files_hint.setToolTip("\n".join(names))

    def _browse_output_dir(self) -> None:
        start = self.output_dir_edit.text().strip()
        if not start and self._selected_input_files:
            start = str(Path(self._selected_input_files[0]).parent)
        if not start:
            start = str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Папка для результатов", start)
        if path:
            self.output_dir_edit.setText(path)
            self._save_folder_settings()

    def _on_random_uniquify_toggled(self, random_on: bool) -> None:
        # Keep section visible, but toggle relevant controls.
        if hasattr(self, "_manual_panel"):
            self._manual_panel.setEnabled(True)
        for w in getattr(self, "_manual_video_widgets", []):
            w.setEnabled(not random_on)
        for w in getattr(self, "_manual_audio_widgets", []):
            w.setEnabled(not random_on)
        for w in getattr(self, "_random_audio_widgets", []):
            w.setEnabled(bool(random_on))
        if hasattr(self, "_random_bounds_panel"):
            self._random_bounds_panel.setEnabled(bool(random_on))
        self._manual_section.setEnabled(True)

    def _build_options(self) -> dict:
        st = UniquifySettings(
            brightness_delta=float(self.brightness.value()),
            contrast=float(self.contrast.value()),
            saturation_scale=float(self.saturation.value()),
            crop_jitter_px=int(self.crop_jitter.value()),
            scale_pct=float(self.scale_pct.value()),
            noise_sigma=float(self.noise.value()),
            seed_base=int(self.seed.value()),
            playback_speed_factor=float(self.playback_speed_manual.value()),
            audio_chorus=bool(self.audio_chorus_manual.isChecked()),
        )
        return {
            "input_dir": "",
            "output_dir": self.output_dir_edit.text().strip(),
            "input_files": list(self._selected_input_files),
            "num_workers": int(self.thread_slider.value()),
            "use_gpu": bool(self.use_gpu.isChecked()),
            "settings": st.to_dict(),
            "randomize_uniquify": self.random_uniquify.isChecked(),
            "copies_per_file": int(self.copies_per_file.value()),
            "playback_speed_enabled": bool(self.audio_speed.isChecked()),
            "audio_chorus_enabled": bool(self.audio_chorus.isChecked()),
            "random_bounds": RandomUniquifyBounds(
                brightness_min=float(self.rb_brightness_min.value()),
                brightness_max=float(self.rb_brightness_max.value()),
                contrast_min=float(self.rb_contrast_min.value()),
                contrast_max=float(self.rb_contrast_max.value()),
                saturation_min=float(self.rb_saturation_min.value()),
                saturation_max=float(self.rb_saturation_max.value()),
                crop_jitter_min=int(self.rb_crop_jitter_min.value()),
                crop_jitter_max=int(self.rb_crop_jitter_max.value()),
                scale_pct_min=float(self.rb_scale_pct_min.value()),
                scale_pct_max=float(self.rb_scale_pct_max.value()),
                noise_sigma_min=float(self.rb_noise_min.value()),
                noise_sigma_max=float(self.rb_noise_max.value()),
                seed_min=int(self.rb_seed_min.value()),
                seed_max=int(self.rb_seed_max.value()),
                playback_speed_min=float(self.audio_speed_min.value()),
                playback_speed_max=float(self.audio_speed_max.value()),
                audio_chorus_prob=float(self.audio_chorus_prob.value()),
            ).to_dict(),
        }

    def _start(self) -> None:
        self._save_folder_settings()
        opts = self._build_options()
        if not opts["output_dir"]:
            QMessageBox.warning(self, "Zaliver", "Укажите выходную папку.")
            return
        if not opts.get("input_files"):
            QMessageBox.warning(
                self,
                "Zaliver",
                "Выберите хотя бы один видеофайл (кнопка «Выбрать файлы…»).",
            )
            return
        out_res = Path(opts["output_dir"]).resolve()
        parents = {Path(f).resolve().parent for f in opts["input_files"]}
        if len(parents) == 1 and next(iter(parents)) == out_res:
            QMessageBox.warning(
                self,
                "Zaliver",
                "Папка результатов совпадает с папкой всех исходных файлов — выберите другую.",
            )
            return
        if self._work_thread and self._work_thread.isRunning():
            return

        self.log.clear()
        self.progress.setRange(0, 1)
        self.progress.setValueImmediate(0)
        self.progress_label.setText("Подготовка…")
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)

        self._work_thread = QThread()
        self._processor = ProcessingController()
        self._processor.moveToThread(self._work_thread)
        self._work_thread.started.connect(partial(self._processor.run, opts))
        self._processor.progress.connect(self._on_progress)
        self._processor.finished.connect(self._on_finished)
        self._processor.output_saved.connect(self._on_output_saved)
        self._processor.log_line.connect(self._append_log)
        self._processor.finished.connect(self._work_thread.quit)
        self._processor.finished.connect(self._processor.deleteLater)
        self._work_thread.finished.connect(self._thread_cleanup)
        self._work_thread.start()

    def _thread_cleanup(self) -> None:
        self._work_thread = None
        self._processor = None

    def _cancel(self) -> None:
        if self._processor is not None:
            self._processor.cancel()

    def _on_progress(self, cur: int, total: int, msg: str) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(cur)
        self.progress_label.setText(msg or f"{cur} / {total} кадров")

    def _on_finished(self, ok: bool, msg: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self._append_log("Готово." if ok else f"Ошибка: {msg}")
        if ok:
            QMessageBox.information(self, "Zaliver", f"Сохранено:\n{msg}")
        elif msg and msg != "Отменено.":
            QMessageBox.critical(self, "Zaliver", msg)
        elif msg == "Отменено.":
            QMessageBox.information(self, "Zaliver", "Обработка отменена.")

    def _append_log(self, line: str) -> None:
        self.log.appendPlainText(line)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
