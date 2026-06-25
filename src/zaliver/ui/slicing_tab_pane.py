"""UI for the «Нарезки» tab (audio-peak slicing)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import Qt, QSettings, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from zaliver.processing.slicing import (
    DEFAULT_MAX_SCENE_DURATION,
    DEFAULT_MAX_SCENES,
    DEFAULT_MIN_SCENE_DURATION,
    DEFAULT_MIN_SCENES,
)
from zaliver.processing.text_overlay import (
    NEON_WAVE_CHAR_PHASE,
    TextOverlaySettings,
    list_bundled_overlay_fonts,
)
from zaliver.ui.text_overlay_preview import TextOverlayPreviewWidget
from zaliver.ui.widgets import (
    AnimatedProgressBar,
    CollapsibleSection,
    SmoothSlider,
    ToggleSwitch,
    configure_log_splitter,
    make_log_export_button,
)

_INT_MAX = 2_147_483_647

DEFAULT_SLICE_TEXT_OVERLAY_TEXT = "5.000.000$ GIVEAWAY IN BIO"
DEFAULT_SLICE_TEXT_OVERLAY_FONT_SIZE = 58
DEFAULT_SLICE_WAVE_AMP_FRAC = 0.15
DEFAULT_SLICE_WAVE_FRAME_SPEED = 0.05
DEFAULT_SLICE_TEXT_OVERLAY_ANCHOR_X = 0.5
DEFAULT_SLICE_TEXT_OVERLAY_ANCHOR_Y = 0.5


class SlicingTabPane(QWidget):
    start_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    install_ffmpeg_requested = pyqtSignal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        settings: QSettings,
        max_workers_fn: Callable[[], int],
        default_workers_fn: Callable[[], int],
        apply_thread_cap_fn: Callable[[SmoothSlider], None],
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._max_workers_fn = max_workers_fn
        self._default_workers_fn = default_workers_fn
        self._apply_thread_cap_fn = apply_thread_cap_fn
        self._clip_files: list[str] = []
        self._music_files: list[str] = []
        self._text_glow_color = "#00FFFF"
        self._text_text_color = "#FFFFFF"
        self._text_font_path = ""
        self._build_ui()
        self.load_settings()

    def build_options(self) -> dict[str, Any]:
        return {
            "output_dir": self.output_dir_edit.text().strip(),
            "clip_files": list(self._clip_files),
            "music_files": list(self._music_files),
            "num_workers": int(self.thread_slider.value()),
            "copies_per_track": int(self.copies_per_track.value()),
            "text_overlay": self.text_overlay_settings().to_dict(),
            "use_suggested_durations": bool(self.auto_scene_durations.isChecked()),
            "min_scene_duration": float(self.min_scene_duration.value()),
            "max_scene_duration": float(self.max_scene_duration.value()),
            "min_scenes": int(self.min_scenes.value()),
            "max_scenes": int(self.max_scenes.value()),
            "use_gpu": bool(self.use_gpu.isChecked()),
            "use_gpu_finalize": bool(self.use_gpu_finalize.isChecked()),
        }

    def validate_scene_options(self) -> str | None:
        if int(self.min_scenes.value()) > int(self.max_scenes.value()):
            return "Мин. количество сцен не может быть больше максимального."
        if not self.auto_scene_durations.isChecked():
            if float(self.min_scene_duration.value()) > float(self.max_scene_duration.value()):
                return "Мин. длительность сцены не может быть больше максимальной."
        return None

    def text_overlay_settings(self) -> TextOverlaySettings:
        orient = self.text_overlay_orientation.currentData()
        ax, ay = self.text_overlay_preview.anchor()
        waf = self.text_overlay_wave_amp.value() / 100.0
        wfs = self.text_overlay_wave_speed.value() / 100.0
        return TextOverlaySettings(
            enabled=bool(self.text_overlay_enabled.isChecked()),
            text=self.text_overlay_edit.toPlainText(),
            font_size=int(self.text_overlay_font_size.value()),
            glow_enabled=bool(self.text_overlay_glow_enabled.isChecked()),
            glow_color=self._text_glow_color,
            text_color=self._text_text_color,
            letter_spacing=int(self.text_overlay_letter_spacing.value()),
            custom_font_path=self._text_font_path,
            font_bold=bool(self.text_overlay_font_bold.isChecked()),
            preview_orientation=orient if isinstance(orient, str) else "vertical",
            anchor_x=float(ax),
            anchor_y=float(ay),
            wave_amp_frac=float(waf),
            wave_char_phase=float(NEON_WAVE_CHAR_PHASE),
            wave_frame_speed=float(wfs),
            from_middle=bool(self.text_overlay_from_middle.isChecked()),
        )

    def _apply_fixed_text_overlay_defaults(self) -> None:
        self.text_overlay_edit.setPlainText(DEFAULT_SLICE_TEXT_OVERLAY_TEXT)
        self.text_overlay_font_size.setValue(DEFAULT_SLICE_TEXT_OVERLAY_FONT_SIZE)
        self.text_overlay_glow_enabled.setChecked(True)
        self.text_overlay_letter_spacing.setValue(0)
        self.text_overlay_wave_amp.setValue(int(round(DEFAULT_SLICE_WAVE_AMP_FRAC * 100)))
        self.text_overlay_wave_speed.setValue(int(round(DEFAULT_SLICE_WAVE_FRAME_SPEED * 100)))
        self.text_overlay_preview.set_anchor(
            DEFAULT_SLICE_TEXT_OVERLAY_ANCHOR_X,
            DEFAULT_SLICE_TEXT_OVERLAY_ANCHOR_Y,
        )

    def load_settings(self) -> None:
        s = self._settings
        self.output_dir_edit.setText(s.value("slice/output_folder", "", type=str) or "")
        try:
            files = s.value("slice/clip_files", [], type=list) or []
        except Exception:
            files = []
        self._clip_files = [str(x) for x in files if str(x).strip()]
        try:
            mf = s.value("slice/music_files", [], type=list) or []
        except Exception:
            mf = []
        self._music_files = []
        for p in mf:
            try:
                if Path(str(p)).is_file():
                    self._music_files.append(str(Path(p).resolve()))
            except OSError:
                continue
        self._sync_clip_hint()
        self._sync_music_hint()
        self.text_overlay_enabled.setChecked(
            bool(s.value("slice/text_overlay_enabled", True, type=bool))
        )
        self._apply_fixed_text_overlay_defaults()
        self.text_overlay_from_middle.setChecked(
            bool(s.value("slice/text_overlay_from_middle", True, type=bool))
        )
        orient = s.value("slice/text_overlay_orientation", "vertical", type=str) or "vertical"
        idx = self.text_overlay_orientation.findData(
            "horizontal" if orient == "horizontal" else "vertical"
        )
        if idx >= 0:
            self.text_overlay_orientation.setCurrentIndex(idx)
        self._text_glow_color = (
            s.value("slice/text_overlay_glow_color", "#00FFFF", type=str) or "#00FFFF"
        )
        self._text_text_color = (
            s.value("slice/text_overlay_text_color", "#FFFFFF", type=str) or "#FFFFFF"
        )
        self._text_font_path = (
            s.value("slice/text_overlay_font_path", "", type=str) or ""
        ).strip()
        self._populate_text_font_combo()
        self.text_overlay_font_bold.setChecked(
            bool(s.value("slice/text_overlay_font_bold", True, type=bool))
        )
        self._sync_color_btn(self.text_overlay_glow_btn, self._text_glow_color)
        self._sync_color_btn(self.text_overlay_text_btn, self._text_text_color)
        self._sync_wave_labels()
        self._update_text_overlay_controls()
        self._sync_text_overlay_preview(
            DEFAULT_SLICE_TEXT_OVERLAY_ANCHOR_X,
            DEFAULT_SLICE_TEXT_OVERLAY_ANCHOR_Y,
        )
        try:
            cp = int(s.value("slice/copies_per_track", 1, type=int))
        except Exception:
            cp = 1
        self.copies_per_track.setValue(max(1, cp))
        if hasattr(self, "delete_after_upload"):
            self.delete_after_upload.setChecked(
                bool(s.value("slice/delete_after_upload", False, type=bool))
            )
        self.auto_scene_durations.setChecked(
            bool(s.value("slice/auto_scene_durations", True, type=bool))
        )
        try:
            min_dur = float(
                s.value("slice/min_scene_duration", DEFAULT_MIN_SCENE_DURATION, type=float)
            )
        except Exception:
            min_dur = DEFAULT_MIN_SCENE_DURATION
        try:
            max_dur = float(
                s.value("slice/max_scene_duration", DEFAULT_MAX_SCENE_DURATION, type=float)
            )
        except Exception:
            max_dur = DEFAULT_MAX_SCENE_DURATION
        self.min_scene_duration.setValue(max(0.1, min(60.0, min_dur)))
        self.max_scene_duration.setValue(max(0.1, min(60.0, max_dur)))
        try:
            min_sc = int(s.value("slice/min_scenes", DEFAULT_MIN_SCENES, type=int))
        except Exception:
            min_sc = DEFAULT_MIN_SCENES
        try:
            max_sc = int(s.value("slice/max_scenes", DEFAULT_MAX_SCENES, type=int))
        except Exception:
            max_sc = DEFAULT_MAX_SCENES
        self.min_scenes.setValue(max(1, min_sc))
        self.max_scenes.setValue(max(1, max_sc))
        self._update_scene_duration_controls()
        if hasattr(self, "use_gpu"):
            self.use_gpu.setChecked(
                bool(s.value("use_gpu_enabled", False, type=bool))
            )
            self.use_gpu_finalize.setChecked(
                bool(s.value("use_gpu_finalize_enabled", False, type=bool))
            )

    def save_settings(self) -> None:
        s = self._settings
        s.setValue("slice/output_folder", self.output_dir_edit.text().strip())
        s.setValue("slice/clip_files", list(self._clip_files))
        s.setValue("slice/music_files", list(self._music_files))
        s.setValue("slice/copies_per_track", int(self.copies_per_track.value()))
        if hasattr(self, "delete_after_upload"):
            s.setValue(
                "slice/delete_after_upload", bool(self.delete_after_upload.isChecked())
            )
        s.setValue("slice/auto_scene_durations", bool(self.auto_scene_durations.isChecked()))
        s.setValue("slice/min_scene_duration", float(self.min_scene_duration.value()))
        s.setValue("slice/max_scene_duration", float(self.max_scene_duration.value()))
        s.setValue("slice/min_scenes", int(self.min_scenes.value()))
        s.setValue("slice/max_scenes", int(self.max_scenes.value()))
        if hasattr(self, "use_gpu"):
            s.setValue("use_gpu_enabled", bool(self.use_gpu.isChecked()))
            s.setValue("use_gpu_finalize_enabled", bool(self.use_gpu_finalize.isChecked()))
        s.setValue("slice/text_overlay_enabled", bool(self.text_overlay_enabled.isChecked()))
        s.setValue("slice/text_overlay_from_middle", bool(self.text_overlay_from_middle.isChecked()))
        orient = self.text_overlay_orientation.currentData()
        s.setValue(
            "slice/text_overlay_orientation",
            orient if isinstance(orient, str) else "vertical",
        )
        s.setValue("slice/text_overlay_glow_color", self._text_glow_color)
        s.setValue("slice/text_overlay_text_color", self._text_text_color)
        s.setValue("slice/text_overlay_font_path", self._text_font_path)
        s.setValue(
            "slice/text_overlay_font_bold", bool(self.text_overlay_font_bold.isChecked())
        )

    def set_running(self, *, running: bool) -> None:
        if running:
            self.set_busy()
        else:
            self.set_idle()

    def set_busy(self) -> None:
        """Нарезка или залив: Старт выключен, Отмена включена."""
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)

    def set_idle(self) -> None:
        """Готов к новому запуску."""
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(12, 8, 12, 12)

        title = QLabel("Zaliver")
        title.setObjectName("title")
        sub = QLabel("Клипы + трек → нарезка по пикам аудио · текст поверх видео")
        sub.setObjectName("hint")

        self.btn_start = QPushButton("Старт")
        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setEnabled(False)
        self.btn_start.clicked.connect(self.start_requested.emit)
        self.btn_cancel.clicked.connect(self.cancel_requested.emit)

        self.progress = AnimatedProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValueImmediate(0)
        self.progress.setMinimumWidth(160)
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("hint")

        header = QHBoxLayout()
        header.addWidget(title)
        header.addWidget(self.progress, 1)
        header.addWidget(self.btn_start)
        header.addWidget(self.btn_cancel)
        root.addLayout(header)
        root.addWidget(self.progress_label)
        root.addWidget(sub)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        io = QGroupBox("Файлы и папка результата")
        io_grid = QGridLayout(io)
        btn_clips = QPushButton("Выбрать клипы…")
        btn_clips.setObjectName("secondary")
        btn_clips.clicked.connect(self._browse_clips)
        self._clip_hint = QLabel("")
        self._clip_hint.setObjectName("hint")
        self._clip_hint.setWordWrap(True)
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText("Папка для нарезанных роликов…")
        btn_out = QPushButton("Обзор…")
        btn_out.setObjectName("secondary")
        btn_out.clicked.connect(self._browse_output_dir)
        io_grid.addWidget(QLabel("Исходные клипы:"), 0, 0)
        io_grid.addWidget(self._clip_hint, 0, 1)
        io_grid.addWidget(btn_clips, 0, 2)
        io_grid.addWidget(QLabel("Выходная папка:"), 1, 0)
        io_grid.addWidget(self.output_dir_edit, 1, 1)
        io_grid.addWidget(btn_out, 1, 2)
        self.copies_per_track = QSpinBox()
        self.copies_per_track.setRange(1, _INT_MAX)
        self.copies_per_track.setValue(1)
        self.copies_per_track.valueChanged.connect(lambda *_: self.save_settings())
        io_grid.addWidget(QLabel("Количество роликов:"), 2, 0)
        io_grid.addWidget(self.copies_per_track, 2, 1)
        self.delete_after_upload = QCheckBox("Удалять после залива")
        self.delete_after_upload.setChecked(False)
        self.delete_after_upload.setToolTip(
            "После успешной загрузки на YouTube файл удаляется из выходной папки."
        )
        self.delete_after_upload.toggled.connect(self.save_settings)
        io_grid.addWidget(self.delete_after_upload, 3, 0, 1, 3)

        music_gb = QGroupBox("Треки для нарезки")
        music_grid = QGridLayout(music_gb)
        btn_music = QPushButton("Выбрать треки…")
        btn_music.setObjectName("secondary")
        btn_music.clicked.connect(self._browse_music)
        self._music_hint = QLabel("")
        self._music_hint.setObjectName("hint")
        self._music_hint.setWordWrap(True)
        music_grid.addWidget(QLabel("Аудиотреки:"), 0, 0)
        music_grid.addWidget(self._music_hint, 0, 1)
        music_grid.addWidget(btn_music, 0, 2)
        music_desc = QLabel(
            "Аудио задаёт длительность и моменты смены кадра. "
            "Треки берутся по очереди и при необходимости повторяются."
        )
        music_desc.setObjectName("hint")
        music_desc.setWordWrap(True)
        music_grid.addWidget(music_desc, 1, 0, 1, 3)

        duration_gb = QGroupBox("Длительность сцены")
        dg = QGridLayout(duration_gb)
        dg.setHorizontalSpacing(8)
        self.auto_scene_durations = QCheckBox(
            "Автоматически подобрать оптимальную длительность"
        )
        self.auto_scene_durations.setChecked(True)
        self.auto_scene_durations.setToolTip(
            "Анализ пиков выбранного трека и рекомендация MIN/MAX длительности сцены. "
            "При включении ручные значения ниже не используются."
        )
        self.auto_scene_durations.toggled.connect(self._update_scene_duration_controls)
        self.auto_scene_durations.toggled.connect(self.save_settings)
        self.min_scene_duration = QDoubleSpinBox()
        self.min_scene_duration.setRange(0.1, 60.0)
        self.min_scene_duration.setSingleStep(0.05)
        self.min_scene_duration.setDecimals(2)
        self.min_scene_duration.setValue(DEFAULT_MIN_SCENE_DURATION)
        self.min_scene_duration.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.min_scene_duration.valueChanged.connect(lambda *_: self.save_settings())
        self.max_scene_duration = QDoubleSpinBox()
        self.max_scene_duration.setRange(0.1, 60.0)
        self.max_scene_duration.setSingleStep(0.05)
        self.max_scene_duration.setDecimals(2)
        self.max_scene_duration.setValue(DEFAULT_MAX_SCENE_DURATION)
        self.max_scene_duration.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.max_scene_duration.valueChanged.connect(lambda *_: self.save_settings())
        dg.addWidget(self.auto_scene_durations, 0, 0, 1, 2)
        dg.addWidget(QLabel("Мин. (с):"), 1, 0)
        dg.addWidget(self.min_scene_duration, 1, 1)
        dg.addWidget(QLabel("Макс. (с):"), 2, 0)
        dg.addWidget(self.max_scene_duration, 2, 1)
        duration_hint = QLabel(
            "Интервал между сменами кадра на пиках аудио."
        )
        duration_hint.setObjectName("hint")
        duration_hint.setWordWrap(True)
        dg.addWidget(duration_hint, 3, 0, 1, 2)
        self._update_scene_duration_controls()

        scenes_gb = QGroupBox("Количество сцен")
        sg = QGridLayout(scenes_gb)
        sg.setHorizontalSpacing(8)
        self.min_scenes = QSpinBox()
        self.min_scenes.setRange(1, 999)
        self.min_scenes.setValue(DEFAULT_MIN_SCENES)
        self.min_scenes.valueChanged.connect(lambda *_: self.save_settings())
        self.max_scenes = QSpinBox()
        self.max_scenes.setRange(1, 999)
        self.max_scenes.setValue(DEFAULT_MAX_SCENES)
        self.max_scenes.valueChanged.connect(lambda *_: self.save_settings())
        sg.addWidget(QLabel("Мин.:"), 0, 0)
        sg.addWidget(self.min_scenes, 0, 1)
        sg.addWidget(QLabel("Макс.:"), 1, 0)
        sg.addWidget(self.max_scenes, 1, 1)
        scene_hint = QLabel(
            "Число сцен выбирается случайно в заданном диапазоне."
        )
        scene_hint.setObjectName("hint")
        scene_hint.setWordWrap(True)
        sg.addWidget(scene_hint, 2, 0, 1, 2)

        proc = QGroupBox("Обработка")
        pg = QGridLayout(proc)
        self.thread_slider = SmoothSlider(Qt.Orientation.Horizontal)
        self.thread_slider.setMinimum(1)
        self.thread_slider.setMaximum(self._max_workers_fn())
        self.thread_slider.setValue(self._default_workers_fn())
        self.thread_label = QLabel()
        self._update_thread_label(self.thread_slider.value())
        self.thread_slider.valueChanged.connect(self._update_thread_label)
        proc_hint = QLabel(
            "Несколько треков обрабатываются параллельно. Нужны ffmpeg и ffprobe в PATH."
        )
        proc_hint.setObjectName("hint")
        proc_hint.setWordWrap(True)
        pg.addWidget(proc_hint, 0, 0, 1, 2)
        ff_row_w = QWidget()
        self._ffmpeg_row = ff_row_w
        ff_row = QHBoxLayout(ff_row_w)
        ff_row.setContentsMargins(0, 0, 0, 0)
        self.ffmpeg_hint = QLabel()
        self.ffmpeg_hint.setObjectName("hint")
        self.ffmpeg_hint.setWordWrap(True)
        self.btn_install_ffmpeg = QPushButton("Установить ffmpeg")
        self.btn_install_ffmpeg.setObjectName("secondary")
        self.btn_install_ffmpeg.clicked.connect(self.install_ffmpeg_requested.emit)
        ff_row.addWidget(self.ffmpeg_hint, 1)
        ff_row.addWidget(self.btn_install_ffmpeg, 0, Qt.AlignmentFlag.AlignRight)
        pg.addWidget(ff_row_w, 1, 0, 1, 2)
        self._ffmpeg_row.setVisible(False)

        self.use_gpu = ToggleSwitch(
            "GPU при обработке кадров (декод, фильтры, кодирование)"
        )
        self.use_gpu.setChecked(
            bool(self._settings.value("use_gpu_enabled", False, type=bool))
        )
        self.use_gpu.toggled.connect(self.save_settings)
        self.use_gpu_finalize = ToggleSwitch(
            "GPU при склейке и mux звука (concat, ускорение, текст)"
        )
        self.use_gpu_finalize.setChecked(
            bool(self._settings.value("use_gpu_finalize_enabled", False, type=bool))
        )
        self.use_gpu_finalize.toggled.connect(self.save_settings)
        gpu_hint = QLabel(
            "Независимо друг от друга. Можно сцены на CPU, а финальный проход на GPU (NVENC/QSV/AMF)."
        )
        gpu_hint.setObjectName("hint")
        gpu_hint.setWordWrap(True)
        pg.addWidget(self.use_gpu, 2, 0, 1, 2)
        pg.addWidget(self.use_gpu_finalize, 3, 0, 1, 2)
        pg.addWidget(gpu_hint, 4, 0, 1, 2)

        pg.addWidget(QLabel("Потоков:"), 5, 0)
        thr = QHBoxLayout()
        thr.addWidget(self.thread_slider, 1)
        thr.addWidget(self.thread_label)
        tw = QWidget()
        tw.setLayout(thr)
        pg.addWidget(tw, 5, 1)

        text_gb = QGroupBox("Текст на видео")
        text_outer = QVBoxLayout(text_gb)
        self._text_section = CollapsibleSection("Текст на видео (неон)")
        text_inner = QWidget()
        text_l = QVBoxLayout(text_inner)
        self.text_overlay_enabled = ToggleSwitch("Накладывать текст на каждый ролик")
        self.text_overlay_enabled.setChecked(True)
        self.text_overlay_enabled.toggled.connect(self._update_text_overlay_controls)
        self.text_overlay_enabled.toggled.connect(self.save_settings)
        text_l.addWidget(self.text_overlay_enabled)
        self._text_panel = QWidget()
        tp = QVBoxLayout(self._text_panel)
        tp.setContentsMargins(0, 0, 0, 0)
        self.text_overlay_edit = QPlainTextEdit()
        self.text_overlay_edit.setPlaceholderText("Текст для наложения…")
        self.text_overlay_edit.setPlainText(DEFAULT_SLICE_TEXT_OVERLAY_TEXT)
        self.text_overlay_edit.setMaximumHeight(72)
        self.text_overlay_edit.textChanged.connect(self._schedule_preview)
        tp.addWidget(self.text_overlay_edit)
        self.text_overlay_from_middle = QCheckBox("Текст с середины видео до конца")
        self.text_overlay_from_middle.setChecked(True)
        self.text_overlay_from_middle.toggled.connect(self.save_settings)
        tp.addWidget(self.text_overlay_from_middle)
        opts = QGridLayout()
        self.text_overlay_font_size = QSpinBox()
        self.text_overlay_font_size.setRange(12, 240)
        self.text_overlay_font_size.setValue(DEFAULT_SLICE_TEXT_OVERLAY_FONT_SIZE)
        self.text_overlay_font_size.valueChanged.connect(self._schedule_preview)
        self.text_overlay_orientation = QComboBox()
        self.text_overlay_orientation.addItem("Вертикальное 9:16", "vertical")
        self.text_overlay_orientation.addItem("Горизонтальное 16:9", "horizontal")
        self.text_overlay_orientation.currentIndexChanged.connect(self._on_orient_changed)
        self.text_overlay_glow_btn = QPushButton("Цвет неона…")
        self.text_overlay_glow_btn.setObjectName("secondary")
        self.text_overlay_glow_btn.clicked.connect(self._pick_glow)
        self.text_overlay_glow_enabled = QCheckBox("Включено")
        self.text_overlay_glow_enabled.setChecked(True)
        self.text_overlay_glow_enabled.toggled.connect(self._on_glow_enabled_changed)
        glow_row = QHBoxLayout()
        glow_row.addWidget(self.text_overlay_glow_enabled)
        glow_row.addWidget(self.text_overlay_glow_btn)
        glow_row.addStretch()
        glow_row_w = QWidget()
        glow_row_w.setLayout(glow_row)
        self.text_overlay_text_btn = QPushButton("Цвет текста…")
        self.text_overlay_text_btn.setObjectName("secondary")
        self.text_overlay_text_btn.clicked.connect(self._pick_text)
        self.text_overlay_letter_spacing = QSpinBox()
        self.text_overlay_letter_spacing.setRange(-20, 80)
        self.text_overlay_letter_spacing.setValue(0)
        self.text_overlay_letter_spacing.setSuffix(" px")
        self.text_overlay_letter_spacing.valueChanged.connect(self._schedule_preview)
        self.text_overlay_font_combo = QComboBox()
        self.text_overlay_font_combo.currentIndexChanged.connect(self._on_font_changed)
        self.text_overlay_font_browse_btn = QPushButton("Файл…")
        self.text_overlay_font_browse_btn.setObjectName("secondary")
        self.text_overlay_font_browse_btn.clicked.connect(self._pick_font_file)
        self.text_overlay_font_bold = QCheckBox("Жирный")
        self.text_overlay_font_bold.setChecked(True)
        self.text_overlay_font_bold.toggled.connect(self._on_font_bold_changed)
        font_row = QHBoxLayout()
        font_row.addWidget(self.text_overlay_font_combo, 1)
        font_row.addWidget(self.text_overlay_font_bold)
        font_row.addWidget(self.text_overlay_font_browse_btn)
        font_row_w = QWidget()
        font_row_w.setLayout(font_row)
        self._populate_text_font_combo()
        opts.addWidget(QLabel("Размер шрифта:"), 0, 0)
        opts.addWidget(self.text_overlay_font_size, 0, 1)
        opts.addWidget(QLabel("Пример кадра:"), 1, 0)
        opts.addWidget(self.text_overlay_orientation, 1, 1)
        opts.addWidget(QLabel("Свечение:"), 2, 0)
        opts.addWidget(glow_row_w, 2, 1)
        opts.addWidget(QLabel("Текст:"), 3, 0)
        opts.addWidget(self.text_overlay_text_btn, 3, 1)
        opts.addWidget(QLabel("Межбуквенный интервал:"), 4, 0)
        opts.addWidget(self.text_overlay_letter_spacing, 4, 1)
        opts.addWidget(QLabel("Шрифт:"), 5, 0)
        opts.addWidget(font_row_w, 5, 1)
        self.text_overlay_wave_amp = SmoothSlider(Qt.Orientation.Horizontal)
        self.text_overlay_wave_amp.setRange(0, 35)
        self.text_overlay_wave_amp.setValue(int(round(DEFAULT_SLICE_WAVE_AMP_FRAC * 100)))
        self.text_overlay_wave_amp.valueChanged.connect(self._on_wave_changed)
        self.text_overlay_wave_amp_label = QLabel()
        self.text_overlay_wave_speed = SmoothSlider(Qt.Orientation.Horizontal)
        self.text_overlay_wave_speed.setRange(0, 25)
        self.text_overlay_wave_speed.setValue(int(round(DEFAULT_SLICE_WAVE_FRAME_SPEED * 100)))
        self.text_overlay_wave_speed.valueChanged.connect(self._on_wave_changed)
        self.text_overlay_wave_speed_label = QLabel()
        self._sync_wave_labels()
        wa = QHBoxLayout()
        wa.addWidget(self.text_overlay_wave_amp, 1)
        wa.addWidget(self.text_overlay_wave_amp_label)
        ws = QHBoxLayout()
        ws.addWidget(self.text_overlay_wave_speed, 1)
        ws.addWidget(self.text_overlay_wave_speed_label)
        opts.addWidget(QLabel("Волна — амплитуда:"), 6, 0)
        waw = QWidget()
        waw.setLayout(wa)
        opts.addWidget(waw, 6, 1)
        opts.addWidget(QLabel("Скорость:"), 7, 0)
        wsw = QWidget()
        wsw.setLayout(ws)
        opts.addWidget(wsw, 7, 1)
        ow = QWidget()
        ow.setLayout(opts)
        tp.addWidget(ow)
        center_btn = QPushButton("По центру (горизонт.)")
        center_btn.setObjectName("secondary")
        center_btn.clicked.connect(self._center_text)
        center_v_btn = QPushButton("По центру (вертик.)")
        center_v_btn.setObjectName("secondary")
        center_v_btn.clicked.connect(self._center_text_vertically)
        cr = QHBoxLayout()
        cr.addWidget(center_btn)
        cr.addWidget(center_v_btn)
        cr.addStretch()
        cw = QWidget()
        cw.setLayout(cr)
        tp.addWidget(cw)
        self.text_overlay_preview = TextOverlayPreviewWidget()
        self.text_overlay_preview.setMinimumHeight(240)
        self.text_overlay_preview.setMaximumHeight(340)
        self.text_overlay_preview.positionChanged.connect(lambda *_: self.save_settings())
        tp.addWidget(self.text_overlay_preview)
        text_l.addWidget(self._text_panel)
        self._text_section.content_layout().addWidget(text_inner)
        self._text_section.set_expanded(True)
        text_outer.addWidget(self._text_section)
        self._update_text_overlay_controls()
        self._sync_color_btn(self.text_overlay_glow_btn, self._text_glow_color)
        self._sync_color_btn(self.text_overlay_text_btn, self._text_text_color)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.addWidget(io)
        il.addWidget(music_gb)
        il.addWidget(duration_gb)
        il.addWidget(scenes_gb)
        il.addWidget(proc)
        il.addWidget(text_gb)
        il.addStretch()
        scroll.setWidget(inner)

        right = QWidget()
        rl = QVBoxLayout(right)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(220)
        self.log.setPlaceholderText("Лог…")
        log_header = QHBoxLayout()
        log_header.addStretch()
        log_header.addWidget(
            make_log_export_button(
                self.log,
                self,
                default_filename="zaliver_slicing_log.txt",
            )
        )
        rl.addLayout(log_header)
        rl.addWidget(self.log, 1)

        splitter.addWidget(scroll)
        splitter.addWidget(right)
        configure_log_splitter(splitter, form_panel=scroll, log_panel=right)
        root.addWidget(splitter, 1)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._sync_text_overlay_preview)
        self._apply_thread_cap_fn(self.thread_slider)

    def _update_scene_duration_controls(self, _checked: bool = False) -> None:
        auto = bool(self.auto_scene_durations.isChecked())
        self.min_scene_duration.setEnabled(not auto)
        self.max_scene_duration.setEnabled(not auto)

    def sync_ffmpeg_install_row(
        self, *, visible: bool, hint: str = "", button_text: str = "Установить ffmpeg"
    ) -> None:
        self._ffmpeg_row.setVisible(bool(visible))
        if visible:
            self.ffmpeg_hint.setText(hint)
            self.btn_install_ffmpeg.setText(button_text)
        else:
            self.ffmpeg_hint.clear()

    def sync_ffmpeg_hint(self, text: str) -> None:
        """Обратная совместимость: только текст, видимость не меняет."""
        self.ffmpeg_hint.setText(text)

    def _update_thread_label(self, v: int) -> None:
        mx = self._max_workers_fn()
        self.thread_label.setText(f"{int(v)} / {mx}")

    def _browse_clips(self) -> None:
        start = str(Path(self._clip_files[0]).parent) if self._clip_files else str(Path.home())
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите видеоклипы для сцен",
            start,
            "Видео (*.mp4 *.mkv *.mov *.avi *.webm *.m4v);;Все файлы (*)",
        )
        if files:
            self._clip_files = [f for f in files if str(f).strip()]
            self._sync_clip_hint()
            self.save_settings()

    def _browse_output_dir(self) -> None:
        start = self.output_dir_edit.text().strip() or (
            str(Path(self._clip_files[0]).parent) if self._clip_files else str(Path.home())
        )
        path = QFileDialog.getExistingDirectory(self, "Папка для нарезанных роликов", start)
        if path:
            self.output_dir_edit.setText(path)
            self.save_settings()

    def _browse_music(self) -> None:
        start = str(Path(self._music_files[0]).parent) if self._music_files else str(Path.home())
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите аудиотреки для нарезки (можно несколько)",
            start,
            "Аудио (*.mp3 *.wav *.m4a *.aac *.flac *.ogg);;Все файлы (*)",
        )
        if files:
            self._music_files = [f for f in files if str(f).strip()]
            self._sync_music_hint()
            self.save_settings()

    def _sync_clip_hint(self) -> None:
        n = len(self._clip_files)
        if n <= 0:
            self._clip_hint.setText("Не выбрано — нажмите «Выбрать клипы…»")
            return
        names = [Path(p).name for p in self._clip_files]
        preview = ", ".join(names[:4])
        if n > 4:
            preview = f"{preview} и ещё {n - 4}"
        self._clip_hint.setText(f"Выбрано: {n} ({preview})")

    def _sync_music_hint(self) -> None:
        n = len(self._music_files)
        if n <= 0:
            self._music_hint.setText("Не выбрано — нажмите «Выбрать треки…»")
            self._music_hint.setToolTip("")
            return
        names = [Path(p).name for p in self._music_files]
        preview = ", ".join(names[:4])
        if n > 4:
            preview = f"{preview} и ещё {n - 4}"
        self._music_hint.setText(f"Выбрано: {n} ({preview})")
        self._music_hint.setToolTip("\n".join(names))

    def _sync_color_btn(self, btn: QPushButton, hex_color: str) -> None:
        c = QColor(hex_color)
        fg = "#0f1117" if c.lightness() > 140 else "#f8fafc"
        btn.setStyleSheet(f"background-color: {c.name()}; color: {fg}; font-weight: 700;")

    def _sync_wave_labels(self) -> None:
        self.text_overlay_wave_amp_label.setText(f"{self.text_overlay_wave_amp.value()} %")
        self.text_overlay_wave_speed_label.setText(
            f"{self.text_overlay_wave_speed.value() / 100.0:.2f}"
        )

    def _update_text_overlay_controls(self, _checked: bool = False) -> None:
        on = bool(self.text_overlay_enabled.isChecked())
        self._text_panel.setEnabled(on)
        glow_on = bool(self.text_overlay_glow_enabled.isChecked())
        self.text_overlay_glow_btn.setEnabled(glow_on)
        if on:
            self._sync_text_overlay_preview()

    def _populate_text_font_combo(self) -> None:
        if not hasattr(self, "text_overlay_font_combo"):
            return
        combo = self.text_overlay_font_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("По умолчанию (Montserrat Bold)", "")
        for label, path in list_bundled_overlay_fonts():
            combo.addItem(label, path)
        custom = (self._text_font_path or "").strip()
        if custom:
            try:
                resolved = str(Path(custom).resolve())
            except OSError:
                resolved = custom
            if combo.findData(resolved) < 0 and combo.findData(custom) < 0:
                combo.addItem(f"Свой: {Path(custom).name}", resolved)
                custom = resolved
        idx = combo.findData(custom)
        if idx < 0 and custom:
            idx = combo.findData(self._text_font_path)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        if idx >= 0:
            data = combo.itemData(idx)
            self._text_font_path = str(data) if data else ""
        combo.blockSignals(False)

    def _on_glow_enabled_changed(self, _checked: bool) -> None:
        self._update_text_overlay_controls()
        self._sync_text_overlay_preview()
        self.save_settings()

    def _on_font_bold_changed(self, _checked: bool) -> None:
        self._sync_text_overlay_preview()
        self.save_settings()

    def _on_font_changed(self, _index: int) -> None:
        data = self.text_overlay_font_combo.currentData()
        self._text_font_path = str(data) if data else ""
        self._sync_text_overlay_preview()
        self.save_settings()

    def _pick_font_file(self) -> None:
        start = (
            str(Path(self._text_font_path).parent)
            if self._text_font_path
            else str(Path.home())
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите файл шрифта",
            start,
            "Шрифты (*.ttf *.otf *.ttc);;Все файлы (*)",
        )
        if not path:
            return
        self._text_font_path = path
        self._populate_text_font_combo()
        self._sync_text_overlay_preview()
        self.save_settings()

    def _schedule_preview(self) -> None:
        self._preview_timer.start(40)
        self.save_settings()

    def _on_orient_changed(self, _index: int) -> None:
        self._sync_text_overlay_preview()
        self.save_settings()

    def _on_wave_changed(self, _v: int) -> None:
        self._sync_wave_labels()
        self._sync_text_overlay_preview()
        self.save_settings()

    def _center_text(self) -> None:
        _ax, ay = self.text_overlay_preview.anchor()
        self.text_overlay_preview.set_anchor(0.5, ay)
        self.save_settings()

    def _center_text_vertically(self) -> None:
        ax, _ay = self.text_overlay_preview.anchor()
        self.text_overlay_preview.set_anchor(ax, 0.5)
        self.save_settings()

    def _pick_glow(self) -> None:
        picked = QColorDialog.getColor(QColor(self._text_glow_color), self, "Цвет неона")
        if not picked.isValid():
            return
        self._text_glow_color = picked.name().upper()
        self._sync_color_btn(self.text_overlay_glow_btn, self._text_glow_color)
        self._sync_text_overlay_preview()
        self.save_settings()

    def _pick_text(self) -> None:
        picked = QColorDialog.getColor(QColor(self._text_text_color), self, "Цвет текста")
        if not picked.isValid():
            return
        self._text_text_color = picked.name().upper()
        self._sync_color_btn(self.text_overlay_text_btn, self._text_text_color)
        self._sync_text_overlay_preview()
        self.save_settings()

    def _sync_text_overlay_preview(
        self, anchor_x: float | None = None, anchor_y: float | None = None
    ) -> None:
        if not bool(self.text_overlay_enabled.isChecked()):
            return
        orient = self.text_overlay_orientation.currentData()
        preview = self.text_overlay_preview
        preview.blockSignals(True)
        preview.set_orientation(orient if isinstance(orient, str) else "vertical")
        preview.set_font_size(int(self.text_overlay_font_size.value()))
        preview.set_glow_enabled(bool(self.text_overlay_glow_enabled.isChecked()))
        preview.set_glow_color(self._text_glow_color)
        preview.set_text_color(self._text_text_color)
        preview.set_letter_spacing(int(self.text_overlay_letter_spacing.value()))
        preview.set_font_path(self._text_font_path)
        preview.set_font_bold(bool(self.text_overlay_font_bold.isChecked()))
        preview.set_wave_settings(
            self.text_overlay_wave_amp.value() / 100.0,
            self.text_overlay_wave_speed.value() / 100.0,
        )
        preview.set_text(self.text_overlay_edit.toPlainText())
        if anchor_x is not None and anchor_y is not None:
            preview.set_anchor(anchor_x, anchor_y)
        preview.blockSignals(False)
