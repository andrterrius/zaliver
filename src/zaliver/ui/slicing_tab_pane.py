"""UI for the «Нарезки» tab (audio-peak slicing)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QSettings, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
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
    QStackedWidget,
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
    ToggleSwitch,
    ValueRangeSlider,
    configure_log_splitter,
    make_log_export_button,
    make_work_section_nav,
    wrap_work_section_page,
    FlowLayout,
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

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        settings: QSettings,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
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
            "copies_per_track": int(self.copies_per_track.value()),
            "text_overlay": self.text_overlay_options_dict(),
            "use_suggested_durations": bool(self.auto_scene_durations.isChecked()),
            "min_scene_duration": float(self.scene_duration.lowValue()),
            "max_scene_duration": float(self.scene_duration.highValue()),
            "min_scenes": int(self.scenes_count.lowValue()),
            "max_scenes": int(self.scenes_count.highValue()),
        }

    def validate_scene_options(self) -> str | None:
        if int(self.scenes_count.lowValue()) > int(self.scenes_count.highValue()):
            return "Мин. количество сцен не может быть больше максимального."
        if not self.auto_scene_durations.isChecked():
            if float(self.scene_duration.lowValue()) > float(self.scene_duration.highValue()):
                return "Мин. длительность сцены не может быть больше максимальной."
        return None

    def text_overlay_settings(self) -> TextOverlaySettings:
        orient = self.text_overlay_orientation.currentData()
        ax, ay = self.text_overlay_preview.anchor()
        waf_lo = self.text_overlay_wave_amp.lowValue() / 100.0
        waf_hi = self.text_overlay_wave_amp.highValue() / 100.0
        wfs_lo = self.text_overlay_wave_speed.lowValue() / 100.0
        wfs_hi = self.text_overlay_wave_speed.highValue() / 100.0
        waf = (waf_lo + waf_hi) * 0.5
        wfs = (wfs_lo + wfs_hi) * 0.5
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

    def text_overlay_options_dict(self) -> dict[str, Any]:
        d = self.text_overlay_settings().to_dict()
        d["wave_amp_frac_min"] = float(self.text_overlay_wave_amp.lowValue() / 100.0)
        d["wave_amp_frac_max"] = float(self.text_overlay_wave_amp.highValue() / 100.0)
        d["wave_frame_speed_min"] = float(self.text_overlay_wave_speed.lowValue() / 100.0)
        d["wave_frame_speed_max"] = float(self.text_overlay_wave_speed.highValue() / 100.0)
        return d

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
            bool(s.value("slice/auto_scene_durations", False, type=bool))
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
        self.scene_duration.setValues(
            max(0.1, min(60.0, min_dur)),
            max(0.1, min(60.0, max_dur)),
        )
        try:
            min_sc = int(s.value("slice/min_scenes", DEFAULT_MIN_SCENES, type=int))
        except Exception:
            min_sc = DEFAULT_MIN_SCENES
        try:
            max_sc = int(s.value("slice/max_scenes", DEFAULT_MAX_SCENES, type=int))
        except Exception:
            max_sc = DEFAULT_MAX_SCENES
        self.scenes_count.setValues(max(1, min_sc), max(1, max_sc))
        self._update_scene_duration_controls()

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
        s.setValue("slice/min_scene_duration", float(self.scene_duration.lowValue()))
        s.setValue("slice/max_scene_duration", float(self.scene_duration.highValue()))
        s.setValue("slice/min_scenes", int(self.scenes_count.lowValue()))
        s.setValue("slice/max_scenes", int(self.scenes_count.highValue()))
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
        root.setSpacing(4)
        root.setContentsMargins(12, 8, 12, 12)

        title = QLabel("Zaliver")
        title.setObjectName("title")

        self.btn_start = QPushButton("Старт")
        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setEnabled(False)
        self.btn_start.clicked.connect(self.start_requested.emit)
        self.btn_cancel.clicked.connect(self.cancel_requested.emit)

        self.progress = AnimatedProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValueImmediate(0)
        self.progress.setMinimumWidth(80)
        self.progress.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("hint")
        self.progress_label.setMinimumHeight(0)
        self.progress_label.setStyleSheet("min-height: 0; padding: 0; margin: 0;")

        header = QHBoxLayout()
        header.addWidget(title)
        header.addWidget(self.progress, 1)
        header.addWidget(self.btn_start)
        header.addWidget(self.btn_cancel)

        header_block = QVBoxLayout()
        header_block.setContentsMargins(0, 0, 0, 0)
        header_block.setSpacing(0)
        header_block.addLayout(header)
        header_block.addWidget(self.progress_label)

        section_nav, section_nav_group, _section_btns = make_work_section_nav(
            ["Исходники", "Сцены", "Текст", "Музыка"],
            parent=self,
        )
        header_block.addWidget(section_nav)
        root.addLayout(header_block)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        io = QGroupBox("Файлы и папка результата")
        io_grid = QGridLayout(io)
        io_grid.setHorizontalSpacing(8)
        io_grid.setVerticalSpacing(8)
        self._btn_pick_clips = QPushButton("Выбрать клипы…")
        self._btn_pick_clips.setObjectName("secondary")
        self._btn_pick_clips.clicked.connect(self._browse_clips)
        self._btn_add_clips = QPushButton("Добавить еще файлы…")
        self._btn_add_clips.setObjectName("secondary")
        self._btn_add_clips.clicked.connect(self._add_clips)
        self._btn_clear_clips = QPushButton("Очистить")
        self._btn_clear_clips.setObjectName("secondary")
        self._btn_clear_clips.clicked.connect(self._clear_clips)
        clip_btns = FlowLayout(hspacing=6, vspacing=6)
        clip_btns.addWidget(self._btn_pick_clips)
        clip_btns.addWidget(self._btn_add_clips)
        clip_btns.addWidget(self._btn_clear_clips)
        clip_btns_w = QWidget()
        clip_btns_w.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        clip_btns_w.setLayout(clip_btns)
        self._clip_hint = QLabel("")
        self._clip_hint.setObjectName("hint")
        self._clip_hint.setWordWrap(True)
        self._clip_hint.setMinimumWidth(0)
        self._clip_hint.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setObjectName("ioPathEdit")
        self.output_dir_edit.setPlaceholderText("Папка для нарезанных роликов…")
        self.output_dir_edit.setMinimumWidth(0)
        self.output_dir_edit.setMaximumWidth(480)
        self.output_dir_edit.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        btn_out = QPushButton("Выходная папка…")
        btn_out.setObjectName("secondary")
        btn_out.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
        )
        btn_out.clicked.connect(self._browse_output_dir)
        out_row = QHBoxLayout()
        out_row.setContentsMargins(0, 0, 0, 0)
        out_row.setSpacing(8)
        out_row.addWidget(self.output_dir_edit, 1)
        out_row.addWidget(btn_out, 0)
        out_row.addStretch(1)

        io_grid.addWidget(QLabel("Исходные клипы:"), 0, 0, Qt.AlignmentFlag.AlignTop)
        io_grid.addWidget(self._clip_hint, 0, 1)
        io_grid.addWidget(clip_btns_w, 1, 1)
        io_grid.addWidget(QLabel("Выходная папка:"), 2, 0)
        io_grid.addLayout(out_row, 2, 1)
        io_grid.setColumnStretch(1, 1)
        io_grid.setColumnMinimumWidth(0, 0)
        io_grid.setColumnMinimumWidth(1, 0)
        self.copies_per_track = QSpinBox()
        self.copies_per_track.setRange(1, _INT_MAX)
        self.copies_per_track.setValue(1)
        self.copies_per_track.setMaximumWidth(120)
        self.copies_per_track.valueChanged.connect(lambda *_: self.save_settings())
        io_grid.addWidget(QLabel("Количество роликов:"), 3, 0)
        io_grid.addWidget(self.copies_per_track, 3, 1)
        self.delete_after_upload = QCheckBox("Удалять после залива")
        self.delete_after_upload.setChecked(False)
        self.delete_after_upload.setToolTip(
            "После полного завершения очереди залива успешно загруженные файлы "
            "удаляются из выходной папки."
        )
        self.delete_after_upload.toggled.connect(self.save_settings)
        io_grid.addWidget(self.delete_after_upload, 4, 0, 1, 2)

        music_gb = QGroupBox("Треки для нарезки")
        music_grid = QGridLayout(music_gb)
        self._btn_pick_music = QPushButton("Выбрать треки…")
        self._btn_pick_music.setObjectName("secondary")
        self._btn_pick_music.clicked.connect(self._browse_music)
        self._btn_add_music = QPushButton("Добавить еще файлы…")
        self._btn_add_music.setObjectName("secondary")
        self._btn_add_music.clicked.connect(self._add_music)
        self._btn_clear_music = QPushButton("Очистить")
        self._btn_clear_music.setObjectName("secondary")
        self._btn_clear_music.clicked.connect(self._clear_music)
        music_btns = FlowLayout(hspacing=6, vspacing=6)
        music_btns.addWidget(self._btn_pick_music)
        music_btns.addWidget(self._btn_add_music)
        music_btns.addWidget(self._btn_clear_music)
        music_btns_w = QWidget()
        music_btns_w.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        music_btns_w.setLayout(music_btns)
        self._music_hint = QLabel("")
        self._music_hint.setObjectName("hint")
        self._music_hint.setWordWrap(True)
        music_grid.addWidget(QLabel("Аудиотреки:"), 0, 0)
        music_grid.addWidget(self._music_hint, 0, 1)
        music_grid.addWidget(music_btns_w, 0, 2)
        music_desc = QLabel(
            "Аудио задаёт длительность и моменты смены кадра. "
            "Для каждого ролика трек выбирается случайно; "
            "повторы — только если роликов больше, чем треков."
        )
        music_desc.setObjectName("hint")
        music_desc.setWordWrap(True)
        music_grid.addWidget(music_desc, 1, 0, 1, 3)

        duration_gb = QGroupBox("Длительность сцены")
        dg = QGridLayout(duration_gb)
        dg.setHorizontalSpacing(8)
        dg.setVerticalSpacing(6)
        self.auto_scene_durations = QCheckBox(
            "Автоматически подобрать оптимальную длительность"
        )
        self.auto_scene_durations.setChecked(False)
        self.auto_scene_durations.setToolTip(
            "Анализ пиков выбранного трека и рекомендация MIN/MAX длительности сцены. "
            "При включении ручные значения ниже не используются."
        )
        self.auto_scene_durations.toggled.connect(self._update_scene_duration_controls)
        self.auto_scene_durations.toggled.connect(self.save_settings)
        self.scene_duration = ValueRangeSlider(
            minimum=0.1,
            maximum=60.0,
            low=DEFAULT_MIN_SCENE_DURATION,
            high=DEFAULT_MAX_SCENE_DURATION,
            step=0.05,
            decimals=2,
            suffix=" с",
        )
        self.scene_duration.rangeChangeFinished.connect(lambda *_: self.save_settings())
        dg.addWidget(self.auto_scene_durations, 0, 0, 1, 2)
        dg.addWidget(QLabel("Длительность:"), 1, 0, Qt.AlignmentFlag.AlignVCenter)
        dg.addWidget(self.scene_duration, 1, 1)
        duration_hint = QLabel(
            "Интервал между сменами кадра на пиках аудио. "
            "Разведите точки — случайный диапазон."
        )
        duration_hint.setObjectName("hint")
        duration_hint.setWordWrap(True)
        dg.addWidget(duration_hint, 2, 0, 1, 2)
        self._update_scene_duration_controls()

        scenes_gb = QGroupBox("Количество сцен")
        sg = QGridLayout(scenes_gb)
        sg.setHorizontalSpacing(8)
        sg.setVerticalSpacing(6)
        self.scenes_count = ValueRangeSlider(
            minimum=1,
            maximum=999,
            low=DEFAULT_MIN_SCENES,
            high=DEFAULT_MAX_SCENES,
            step=1,
            decimals=0,
        )
        self.scenes_count.rangeChangeFinished.connect(lambda *_: self.save_settings())
        sg.addWidget(QLabel("Сцены:"), 0, 0, Qt.AlignmentFlag.AlignVCenter)
        sg.addWidget(self.scenes_count, 0, 1)
        scene_hint = QLabel(
            "Число сцен выбирается случайно в заданном диапазоне."
        )
        scene_hint.setObjectName("hint")
        scene_hint.setWordWrap(True)
        sg.addWidget(scene_hint, 1, 0, 1, 2)

        text_gb = QGroupBox("Текст на видео")
        text_outer = QVBoxLayout(text_gb)
        text_outer.setSpacing(8)
        self.text_overlay_enabled = ToggleSwitch("Добавить текст")
        self.text_overlay_enabled.setChecked(True)
        self.text_overlay_enabled.toggled.connect(self._update_text_overlay_controls)
        self.text_overlay_enabled.toggled.connect(self.save_settings)
        text_outer.addWidget(self.text_overlay_enabled)
        self._text_panel = QWidget()
        tp = QVBoxLayout(self._text_panel)
        tp.setContentsMargins(0, 0, 0, 0)
        tp.setSpacing(8)
        self.text_overlay_from_middle = QCheckBox("Текст с середины видео до конца")
        self.text_overlay_from_middle.setChecked(True)
        self.text_overlay_from_middle.toggled.connect(self.save_settings)
        tp.addWidget(self.text_overlay_from_middle)
        self.text_overlay_edit = QPlainTextEdit()
        self.text_overlay_edit.setPlaceholderText("Текст для наложения…")
        self.text_overlay_edit.setPlainText(DEFAULT_SLICE_TEXT_OVERLAY_TEXT)
        self.text_overlay_edit.setMaximumHeight(72)
        self.text_overlay_edit.textChanged.connect(self._schedule_preview)
        tp.addWidget(self.text_overlay_edit)
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
        self.text_overlay_orientation.hide()
        opts.addWidget(QLabel("Размер"), 0, 0)
        opts.addWidget(self.text_overlay_font_size, 0, 1)
        opts.addWidget(QLabel("Свечение"), 1, 0)
        opts.addWidget(glow_row_w, 1, 1)
        opts.addWidget(QLabel("Цвет"), 2, 0)
        opts.addWidget(self.text_overlay_text_btn, 2, 1)
        opts.addWidget(QLabel("Межбуквенный интервал"), 3, 0)
        opts.addWidget(self.text_overlay_letter_spacing, 3, 1)
        opts.addWidget(QLabel("Шрифт"), 4, 0)
        opts.addWidget(font_row_w, 4, 1)
        self.text_overlay_wave_amp = ValueRangeSlider(
            minimum=0,
            maximum=35,
            value=int(round(DEFAULT_SLICE_WAVE_AMP_FRAC * 100)),
            step=1,
            decimals=0,
            suffix=" %",
        )
        self.text_overlay_wave_amp.rangeChanged.connect(
            lambda *_: self._schedule_preview_light()
        )
        self.text_overlay_wave_amp.rangeChangeFinished.connect(self._on_wave_changed)
        self.text_overlay_wave_speed = ValueRangeSlider(
            minimum=0,
            maximum=25,
            value=int(round(DEFAULT_SLICE_WAVE_FRAME_SPEED * 100)),
            step=1,
            decimals=0,
        )
        self.text_overlay_wave_speed.rangeChanged.connect(
            lambda *_: self._schedule_preview_light()
        )
        self.text_overlay_wave_speed.rangeChangeFinished.connect(self._on_wave_changed)
        opts.addWidget(QLabel("Волна - амплитуда"), 5, 0)
        opts.addWidget(self.text_overlay_wave_amp, 5, 1)
        opts.addWidget(QLabel("Волна - скорость"), 6, 0)
        opts.addWidget(self.text_overlay_wave_speed, 6, 1)
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
        text_outer.addWidget(self._text_panel)
        self._update_text_overlay_controls()
        self._sync_color_btn(self.text_overlay_glow_btn, self._text_glow_color)
        self._sync_color_btn(self.text_overlay_text_btn, self._text_text_color)

        self._slice_section_stack = QStackedWidget()
        self._slice_section_stack.addWidget(wrap_work_section_page(io))
        self._slice_section_stack.addWidget(
            wrap_work_section_page(duration_gb, scenes_gb)
        )
        self._slice_section_stack.addWidget(wrap_work_section_page(text_gb))
        self._slice_section_stack.addWidget(wrap_work_section_page(music_gb))
        section_nav_group.idClicked.connect(self._slice_section_stack.setCurrentIndex)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setMinimumWidth(0)
        inner = QWidget()
        inner.setMinimumWidth(0)
        inner.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 0, 0)
        il.addWidget(self._slice_section_stack)
        il.addStretch()
        scroll.setWidget(inner)
        scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

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

    def _update_scene_duration_controls(self, _checked: bool = False) -> None:
        auto = bool(self.auto_scene_durations.isChecked())
        self.scene_duration.setEnabled(not auto)

    def _normalize_path_key(self, p: str) -> str:
        try:
            return os.path.normcase(str(Path(p).resolve()))
        except OSError:
            return os.path.normcase(os.path.normpath(str(p)))

    def _merge_unique_paths(self, existing: list[str], new_files: list[str]) -> list[str]:
        seen = {self._normalize_path_key(p) for p in existing}
        merged = list(existing)
        for f in new_files:
            raw = str(f).strip()
            if not raw:
                continue
            try:
                p = str(Path(raw).resolve())
            except OSError:
                p = raw
            key = self._normalize_path_key(p)
            if key in seen:
                continue
            seen.add(key)
            merged.append(p)
        return merged

    def _clips_dialog_filter(self) -> str:
        return "Видео (*.mp4 *.mkv *.mov *.avi *.webm *.m4v);;Все файлы (*)"

    def _music_dialog_filter(self) -> str:
        return "Аудио (*.mp3 *.wav *.m4a *.aac *.flac *.ogg);;Все файлы (*)"

    def _clips_start_dir(self) -> str:
        if self._clip_files:
            return str(Path(self._clip_files[0]).parent)
        return str(Path.home())

    def _music_start_dir(self) -> str:
        if self._music_files:
            return str(Path(self._music_files[0]).parent)
        return str(Path.home())

    def _browse_clips(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите видеоклипы для сцен",
            self._clips_start_dir(),
            self._clips_dialog_filter(),
        )
        if files:
            self._clip_files = self._merge_unique_paths([], files)
            self._sync_clip_hint()
            self.save_settings()

    def _add_clips(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Добавить видеоклипы к списку",
            self._clips_start_dir(),
            self._clips_dialog_filter(),
        )
        if files:
            self._clip_files = self._merge_unique_paths(self._clip_files, files)
            self._sync_clip_hint()
            self.save_settings()

    def _clear_clips(self) -> None:
        if not self._clip_files:
            return
        self._clip_files = []
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
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите аудиотреки для нарезки (можно несколько)",
            self._music_start_dir(),
            self._music_dialog_filter(),
        )
        if files:
            self._music_files = self._merge_unique_paths([], files)
            self._sync_music_hint()
            self.save_settings()

    def _add_music(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Добавить аудиотреки к списку",
            self._music_start_dir(),
            self._music_dialog_filter(),
        )
        if files:
            self._music_files = self._merge_unique_paths(self._music_files, files)
            self._sync_music_hint()
            self.save_settings()

    def _clear_music(self) -> None:
        if not self._music_files:
            return
        self._music_files = []
        self._sync_music_hint()
        self.save_settings()

    def _sync_clip_hint(self) -> None:
        n = len(self._clip_files)
        has_files = n > 0
        self._btn_add_clips.setVisible(has_files)
        self._btn_clear_clips.setVisible(has_files)
        if n <= 0:
            self._clip_hint.setText("Не выбрано — нажмите «Выбрать клипы…»")
            self._schedule_preview()
            return
        names = [Path(p).name for p in self._clip_files]
        preview = ", ".join(names[:4])
        if n > 4:
            preview = f"{preview} и ещё {n - 4}"
        self._clip_hint.setText(f"Выбрано: {n} ({preview})")
        self._schedule_preview()

    def _sync_music_hint(self) -> None:
        n = len(self._music_files)
        has_files = n > 0
        self._btn_add_music.setVisible(has_files)
        self._btn_clear_music.setVisible(has_files)
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
        return

    def _update_text_overlay_controls(self, _checked: bool = False) -> None:
        on = bool(self.text_overlay_enabled.isChecked())
        self._text_panel.setVisible(on)
        self._text_panel.setEnabled(on)
        glow_on = bool(self.text_overlay_glow_enabled.isChecked())
        self.text_overlay_glow_btn.setEnabled(on and glow_on)
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

    def _schedule_preview_light(self) -> None:
        """Только превью с debounce — без записи настроек (для drag ползунка)."""
        self._preview_timer.start(40)

    def _on_orient_changed(self, _index: int) -> None:
        self._sync_text_overlay_preview()
        self.save_settings()

    def _on_wave_changed(self, *_args) -> None:
        self._schedule_preview_light()
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
        preview = self.text_overlay_preview
        first_clip = next(
            (
                p
                for p in self._clip_files
                if str(p).strip() and Path(p).is_file()
            ),
            None,
        )
        preview.set_background_video(first_clip)
        overlay_on = bool(self.text_overlay_enabled.isChecked())
        preview.set_text_visible(overlay_on)
        if not overlay_on:
            return
        orient = self.text_overlay_orientation.currentData()
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
            (
                self.text_overlay_wave_amp.lowValue()
                + self.text_overlay_wave_amp.highValue()
            )
            * 0.005,
            (
                self.text_overlay_wave_speed.lowValue()
                + self.text_overlay_wave_speed.highValue()
            )
            * 0.005,
        )
        preview.set_text(self.text_overlay_edit.toPlainText())
        if anchor_x is not None and anchor_y is not None:
            preview.set_anchor(anchor_x, anchor_y)
        preview.blockSignals(False)
