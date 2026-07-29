"""UI for the «Склейка» tab (two-part beat-synced stitch)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QSettings, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QButtonGroup,
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
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from zaliver.processing.stitching import (
    DEFAULT_STITCH_TRANSITION,
    DEFAULT_STITCH_TRANSITION_DURATION,
    STITCH_TRANSITION_CIRCLE,
    STITCH_TRANSITION_CUT,
    STITCH_TRANSITION_FADE,
    STITCH_TRANSITION_FLASH,
    STITCH_TRANSITION_WHIP,
    STITCH_TRANSITION_ZOOM,
    STITCH_TRANSITION_LABELS,
    STITCH_TRANSITIONS,
    normalize_stitch_transition,
)
from zaliver.processing.text_overlay import (
    NEON_WAVE_CHAR_PHASE,
    TextOverlaySettings,
    list_bundled_overlay_fonts,
)
from zaliver.ui.text_overlay_preview import TextOverlayPreviewWidget
from zaliver.ui.widgets import (
    AnimatedProgressBar,
    SmoothSlider,
    ToggleSwitch,
    configure_log_splitter,
    make_log_export_button,
    make_work_section_nav,
    wrap_work_section_page,
    FlowLayout,
)

_INT_MAX = 2_147_483_647

DEFAULT_STITCH_TEXT_OVERLAY_TEXT = "5.000.000$ GIVEAWAY IN BIO"
DEFAULT_STITCH_TEXT_OVERLAY_FONT_SIZE = 58
DEFAULT_STITCH_WAVE_AMP_FRAC = 0.15
DEFAULT_STITCH_WAVE_FRAME_SPEED = 0.05
DEFAULT_STITCH_TEXT_OVERLAY_ANCHOR_X = 0.5
DEFAULT_STITCH_TEXT_OVERLAY_ANCHOR_Y = 0.5


class StitchingTabPane(QWidget):
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
        self._part1_files: list[str] = []
        self._part2_files: list[str] = []
        self._music_files: list[str] = []
        self._text_glow_color = "#00FFFF"
        self._text_text_color = "#FFFFFF"
        self._text_font_path = ""
        self._text_overlay_preview_index_part1 = 0
        self._text_overlay_preview_index_part2 = 0
        self._loading_settings = False
        self._last_transition = DEFAULT_STITCH_TRANSITION
        self._build_ui()
        self.load_settings()

    def build_options(self) -> dict[str, Any]:
        return {
            "output_dir": self.output_dir_edit.text().strip(),
            "part1_files": list(self._part1_files),
            "part2_files": list(self._part2_files),
            "music_files": list(self._music_files),
            "copies_per_track": int(self.copies_per_track.value()),
            "text_overlay": self.text_overlay_settings().to_dict(),
            "transition": self._selected_transition(),
            "transition_duration": float(DEFAULT_STITCH_TRANSITION_DURATION),
            "transition_random": bool(self.transition_random.isChecked()),
        }

    def _selected_transition(self) -> str:
        btn = self._transition_group.checkedButton()
        if btn is None:
            return normalize_stitch_transition(self._last_transition)
        data = btn.property("transition_id")
        return normalize_stitch_transition(data)

    def _clear_transition_selection(self) -> None:
        self._transition_group.setExclusive(False)
        for btn in self._transition_group.buttons():
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)

    def _sync_transition_controls(self) -> None:
        random_on = bool(self.transition_random.isChecked())
        for rb in self._transition_group.buttons():
            rb.setEnabled(not random_on)
        if random_on:
            # Запомнить текущий выбор, затем снять выделение.
            checked = self._transition_group.checkedButton()
            if checked is not None:
                self._last_transition = normalize_stitch_transition(
                    checked.property("transition_id")
                )
            self._clear_transition_selection()
        else:
            self._transition_group.setExclusive(True)
            if self._transition_group.checkedButton() is None:
                self._set_transition(self._last_transition)

    def validate_part_options(self) -> str | None:
        return None

    def text_overlay_settings(self) -> TextOverlaySettings:
        orient = self.text_overlay_orientation.currentData()
        ax, ay = self.text_overlay_preview_part1.anchor()
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
            after_frame_change=bool(self.text_overlay_after_frame_change.isChecked()),
        )

    def _apply_fixed_text_overlay_defaults(self) -> None:
        self.text_overlay_edit.setPlainText(DEFAULT_STITCH_TEXT_OVERLAY_TEXT)
        self.text_overlay_font_size.setValue(DEFAULT_STITCH_TEXT_OVERLAY_FONT_SIZE)
        self.text_overlay_glow_enabled.setChecked(True)
        self.text_overlay_letter_spacing.setValue(0)
        self.text_overlay_wave_amp.setValue(int(round(DEFAULT_STITCH_WAVE_AMP_FRAC * 100)))
        self.text_overlay_wave_speed.setValue(
            int(round(DEFAULT_STITCH_WAVE_FRAME_SPEED * 100))
        )
        self._set_both_preview_anchors(
            DEFAULT_STITCH_TEXT_OVERLAY_ANCHOR_X,
            DEFAULT_STITCH_TEXT_OVERLAY_ANCHOR_Y,
        )

    def _load_file_list(self, key: str) -> list[str]:
        try:
            files = self._settings.value(key, [], type=list) or []
        except Exception:
            files = []
        out: list[str] = []
        for p in files:
            try:
                path = Path(str(p))
                if path.is_file():
                    out.append(str(path.resolve()))
            except OSError:
                continue
        return out

    def load_settings(self) -> None:
        self._loading_settings = True
        try:
            self._load_settings_impl()
        finally:
            self._loading_settings = False

    def _load_settings_impl(self) -> None:
        s = self._settings
        self.output_dir_edit.setText(s.value("stitch/output_folder", "", type=str) or "")
        self._part1_files = self._load_file_list("stitch/part1_files")
        self._part2_files = self._load_file_list("stitch/part2_files")
        self._music_files = self._load_file_list("stitch/music_files")
        self._sync_part1_hint()
        self._sync_part2_hint()
        self._sync_music_hint()
        saved_transition = normalize_stitch_transition(
            s.value("stitch/transition", DEFAULT_STITCH_TRANSITION, type=str)
        )
        self._last_transition = saved_transition
        self.transition_random.blockSignals(True)
        self.transition_random.setChecked(
            bool(s.value("stitch/transition_random", False, type=bool))
        )
        self.transition_random.blockSignals(False)
        self.text_overlay_enabled.setChecked(
            bool(s.value("stitch/text_overlay_enabled", True, type=bool))
        )
        self._apply_fixed_text_overlay_defaults()
        self.text_overlay_from_middle.setChecked(
            bool(s.value("stitch/text_overlay_from_middle", True, type=bool))
        )
        self.text_overlay_after_frame_change.setChecked(
            bool(s.value("stitch/text_overlay_after_frame_change", False, type=bool))
        )
        # Взаимоисключающие режимы появления текста.
        if (
            self.text_overlay_after_frame_change.isChecked()
            and self.text_overlay_from_middle.isChecked()
        ):
            self.text_overlay_from_middle.setChecked(False)
        orient = s.value("stitch/text_overlay_orientation", "vertical", type=str) or "vertical"
        idx = self.text_overlay_orientation.findData(
            "horizontal" if orient == "horizontal" else "vertical"
        )
        if idx >= 0:
            self.text_overlay_orientation.setCurrentIndex(idx)
        self._text_glow_color = (
            s.value("stitch/text_overlay_glow_color", "#00FFFF", type=str) or "#00FFFF"
        )
        self._text_text_color = (
            s.value("stitch/text_overlay_text_color", "#FFFFFF", type=str) or "#FFFFFF"
        )
        self._text_font_path = (
            s.value("stitch/text_overlay_font_path", "", type=str) or ""
        ).strip()
        self._populate_text_font_combo()
        self.text_overlay_font_bold.setChecked(
            bool(s.value("stitch/text_overlay_font_bold", True, type=bool))
        )
        self._sync_color_btn(self.text_overlay_glow_btn, self._text_glow_color)
        self._sync_color_btn(self.text_overlay_text_btn, self._text_text_color)
        self._sync_wave_labels()
        self._update_text_overlay_controls()
        self._sync_text_overlay_preview(
            DEFAULT_STITCH_TEXT_OVERLAY_ANCHOR_X,
            DEFAULT_STITCH_TEXT_OVERLAY_ANCHOR_Y,
        )
        try:
            cp = int(s.value("stitch/copies_per_track", 1, type=int))
        except Exception:
            cp = 1
        self.copies_per_track.setValue(max(1, cp))
        if hasattr(self, "delete_after_upload"):
            self.delete_after_upload.setChecked(
                bool(s.value("stitch/delete_after_upload", False, type=bool))
            )
        self._set_transition(saved_transition)
        self._sync_transition_controls()

    def save_settings(self) -> None:
        # Во время _build_ui / load_settings слоты не должны писать настройки.
        if getattr(self, "_loading_settings", False):
            return
        if not hasattr(self, "text_overlay_enabled"):
            return
        s = self._settings
        s.setValue("stitch/output_folder", self.output_dir_edit.text().strip())
        s.setValue("stitch/part1_files", list(self._part1_files))
        s.setValue("stitch/part2_files", list(self._part2_files))
        s.setValue("stitch/music_files", list(self._music_files))
        s.setValue("stitch/copies_per_track", int(self.copies_per_track.value()))
        if hasattr(self, "delete_after_upload"):
            s.setValue(
                "stitch/delete_after_upload", bool(self.delete_after_upload.isChecked())
            )
        if hasattr(self, "_transition_group"):
            # При рандоме сохраняем последний явный выбор (не «пусто»).
            s.setValue(
                "stitch/transition",
                self._selected_transition()
                if self._transition_group.checkedButton() is not None
                else normalize_stitch_transition(self._last_transition),
            )
        if hasattr(self, "transition_random"):
            s.setValue(
                "stitch/transition_random", bool(self.transition_random.isChecked())
            )
        s.setValue("stitch/text_overlay_enabled", bool(self.text_overlay_enabled.isChecked()))
        s.setValue(
            "stitch/text_overlay_from_middle",
            bool(self.text_overlay_from_middle.isChecked()),
        )
        s.setValue(
            "stitch/text_overlay_after_frame_change",
            bool(self.text_overlay_after_frame_change.isChecked()),
        )
        orient = self.text_overlay_orientation.currentData()
        s.setValue(
            "stitch/text_overlay_orientation",
            orient if isinstance(orient, str) else "vertical",
        )
        s.setValue("stitch/text_overlay_glow_color", self._text_glow_color)
        s.setValue("stitch/text_overlay_text_color", self._text_text_color)
        s.setValue("stitch/text_overlay_font_path", self._text_font_path)
        s.setValue(
            "stitch/text_overlay_font_bold", bool(self.text_overlay_font_bold.isChecked())
        )

    def _set_transition(self, transition_id: str) -> None:
        key = normalize_stitch_transition(transition_id)
        self._last_transition = key
        self._transition_group.setExclusive(True)
        for btn in self._transition_group.buttons():
            if str(btn.property("transition_id") or "") == key:
                btn.blockSignals(True)
                btn.setChecked(True)
                btn.blockSignals(False)
                return
        # fallback
        self._last_transition = DEFAULT_STITCH_TRANSITION
        for btn in self._transition_group.buttons():
            if str(btn.property("transition_id") or "") == DEFAULT_STITCH_TRANSITION:
                btn.blockSignals(True)
                btn.setChecked(True)
                btn.blockSignals(False)
                return

    def _on_transition_toggled(self, checked: bool) -> None:
        if checked:
            btn = self.sender()
            if btn is not None:
                self._last_transition = normalize_stitch_transition(
                    btn.property("transition_id")
                )
            self.save_settings()

    def _on_transition_random_toggled(self, *_args) -> None:
        self._sync_transition_controls()
        self.save_settings()

    def set_running(self, *, running: bool) -> None:
        if running:
            self.set_busy()
        else:
            self.set_idle()

    def set_busy(self) -> None:
        """Склейка или залив: Старт выключен, Отмена включена."""
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)

    def set_idle(self) -> None:
        """Готов к новому запуску."""
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)

    def _make_clip_row(
        self,
        *,
        pick_label: str,
        on_pick,
        on_add,
        on_clear,
    ) -> tuple[QWidget, QLabel, QPushButton, QPushButton, QPushButton]:
        btn_pick = QPushButton(pick_label)
        btn_pick.setObjectName("secondary")
        btn_pick.clicked.connect(on_pick)
        btn_add = QPushButton("Добавить еще файлы…")
        btn_add.setObjectName("secondary")
        btn_add.clicked.connect(on_add)
        btn_clear = QPushButton("Очистить")
        btn_clear.setObjectName("secondary")
        btn_clear.clicked.connect(on_clear)
        btns = FlowLayout(hspacing=6, vspacing=6)
        btns.addWidget(btn_pick)
        btns.addWidget(btn_add)
        btns.addWidget(btn_clear)
        btns_w = QWidget()
        btns_w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        btns_w.setLayout(btns)
        hint = QLabel("")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        hint.setMinimumWidth(0)
        hint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        return btns_w, hint, btn_pick, btn_add, btn_clear

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
            ["Исходники", "Текст", "Музыка", "Переходы"],
            parent=self,
        )
        header_block.addWidget(section_nav)
        root.addLayout(header_block)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        io = QGroupBox("Файлы и папка результата")
        io_grid = QGridLayout(io)
        io_grid.setHorizontalSpacing(8)
        io_grid.setVerticalSpacing(8)

        (
            part1_btns_w,
            self._part1_hint,
            self._btn_pick_part1,
            self._btn_add_part1,
            self._btn_clear_part1,
        ) = self._make_clip_row(
            pick_label="Выбрать клипы (часть 1)…",
            on_pick=self._browse_part1,
            on_add=self._add_part1,
            on_clear=self._clear_part1,
        )
        (
            part2_btns_w,
            self._part2_hint,
            self._btn_pick_part2,
            self._btn_add_part2,
            self._btn_clear_part2,
        ) = self._make_clip_row(
            pick_label="Выбрать клипы (часть 2)…",
            on_pick=self._browse_part2,
            on_add=self._add_part2,
            on_clear=self._clear_part2,
        )

        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setObjectName("ioPathEdit")
        self.output_dir_edit.setPlaceholderText("Папка для склеенных роликов…")
        self.output_dir_edit.setMinimumWidth(0)
        self.output_dir_edit.setMaximumWidth(480)
        self.output_dir_edit.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        btn_out = QPushButton("Выходная папка…")
        btn_out.setObjectName("secondary")
        btn_out.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        btn_out.clicked.connect(self._browse_output_dir)
        out_row = QHBoxLayout()
        out_row.setContentsMargins(0, 0, 0, 0)
        out_row.setSpacing(8)
        out_row.addWidget(self.output_dir_edit, 1)
        out_row.addWidget(btn_out, 0)
        out_row.addStretch(1)

        io_grid.addWidget(QLabel("Часть 1:"), 0, 0, Qt.AlignmentFlag.AlignTop)
        io_grid.addWidget(self._part1_hint, 0, 1)
        io_grid.addWidget(part1_btns_w, 1, 1)
        io_grid.addWidget(QLabel("Часть 2:"), 2, 0, Qt.AlignmentFlag.AlignTop)
        io_grid.addWidget(self._part2_hint, 2, 1)
        io_grid.addWidget(part2_btns_w, 3, 1)
        io_grid.addWidget(QLabel("Выходная папка:"), 4, 0)
        io_grid.addLayout(out_row, 4, 1)
        io_grid.setColumnStretch(1, 1)
        self.copies_per_track = QSpinBox()
        self.copies_per_track.setRange(1, _INT_MAX)
        self.copies_per_track.setValue(1)
        self.copies_per_track.setMaximumWidth(120)
        self.copies_per_track.valueChanged.connect(lambda *_: self.save_settings())
        io_grid.addWidget(QLabel("Количество роликов:"), 5, 0)
        io_grid.addWidget(self.copies_per_track, 5, 1)
        self.delete_after_upload = QCheckBox("Удалять после залива")
        self.delete_after_upload.setChecked(False)
        self.delete_after_upload.setToolTip(
            "После полного завершения очереди залива успешно загруженные файлы "
            "удаляются из выходной папки."
        )
        self.delete_after_upload.toggled.connect(self.save_settings)
        io_grid.addWidget(self.delete_after_upload, 6, 0, 1, 2)

        music_gb = QGroupBox("Треки для склейки")
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
            "Случайный полный клип из части 1 и из части 2 склеиваются; "
            "длительность ролика — их сумма. "
            "Переход ставится на бит трека; при нехватке музыки хвост зацикливается."
        )
        music_desc.setObjectName("hint")
        music_desc.setWordWrap(True)
        music_grid.addWidget(music_desc, 1, 0, 1, 3)

        transitions_gb = QGroupBox("Переход между частями")
        transitions_layout = QVBoxLayout(transitions_gb)
        transitions_layout.setSpacing(8)
        self.transition_random = QCheckBox("Выбирать рандомно")
        self.transition_random.setToolTip(
            "Перед каждым роликом случайно выбирается любой переход, "
            "включая простую склейку."
        )
        self.transition_random.toggled.connect(self._on_transition_random_toggled)
        transitions_layout.addWidget(self.transition_random)
        self._transition_group = QButtonGroup(self)
        self._transition_radios: dict[str, QRadioButton] = {}
        transition_hints = {
            STITCH_TRANSITION_CUT: "Жёсткий стык двух клипов без эффекта.",
            STITCH_TRANSITION_FADE: "Плавное растворение одного кадра в другой (~0.4с).",
            STITCH_TRANSITION_CIRCLE: "Вторая часть открывается кругом из центра (~0.4с).",
            STITCH_TRANSITION_ZOOM: "Punch-zoom как в эдитах: наезд в стык (~0.4с).",
            STITCH_TRANSITION_FLASH: "Белая вспышка на бите — классика эдитов (~0.4с).",
            STITCH_TRANSITION_WHIP: "Горизонтальный смаз, как whip-pan между кадрами (~0.4с).",
        }
        for tid in STITCH_TRANSITIONS:
            rb = QRadioButton(STITCH_TRANSITION_LABELS.get(tid, tid))
            rb.setProperty("transition_id", tid)
            rb.setToolTip(transition_hints.get(tid, ""))
            self._transition_group.addButton(rb)
            self._transition_radios[tid] = rb
            row = QVBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            row.addWidget(rb)
            hint = QLabel(transition_hints.get(tid, ""))
            hint.setObjectName("hint")
            hint.setWordWrap(True)
            row.addWidget(hint)
            wrap = QWidget()
            wrap.setLayout(row)
            transitions_layout.addWidget(wrap)
        # Не вызываем save_settings до полной сборки UI (иначе краш в слоте Qt).
        self._transition_radios[DEFAULT_STITCH_TRANSITION].setChecked(True)
        for rb in self._transition_group.buttons():
            rb.toggled.connect(self._on_transition_toggled)
        transitions_note = QLabel(
            "Если клипы слишком короткие — автоматически останется простая склейка. "
            "Галочка «Выбирать рандомно» — свой эффект на каждый ролик."
        )
        transitions_note.setObjectName("hint")
        transitions_note.setWordWrap(True)
        transitions_layout.addWidget(transitions_note)
        transitions_layout.addStretch(1)

        text_gb = QGroupBox("Текст на видео")
        text_outer = QVBoxLayout(text_gb)
        text_outer.setSpacing(8)
        self.text_overlay_enabled = ToggleSwitch("Накладывать текст на каждый ролик")
        self.text_overlay_enabled.setChecked(True)
        self.text_overlay_enabled.toggled.connect(self._update_text_overlay_controls)
        self.text_overlay_enabled.toggled.connect(self.save_settings)
        self.text_overlay_from_middle = QCheckBox("Текст с середины видео до конца")
        self.text_overlay_from_middle.setChecked(True)
        self.text_overlay_from_middle.toggled.connect(self._on_from_middle_toggled)
        self.text_overlay_from_middle.toggled.connect(self.save_settings)
        self.text_overlay_after_frame_change = QCheckBox("Текст после смены кадра")
        self.text_overlay_after_frame_change.setChecked(False)
        self.text_overlay_after_frame_change.setToolTip(
            "Показывать текст только после перехода с части 1 на часть 2 "
            "(в момент смены кадра по ритму)."
        )
        self.text_overlay_after_frame_change.toggled.connect(
            self._on_after_frame_change_toggled
        )
        self.text_overlay_after_frame_change.toggled.connect(self.save_settings)
        text_top_row = QHBoxLayout()
        text_top_row.setContentsMargins(0, 0, 0, 0)
        text_top_row.setSpacing(16)
        text_top_row.addWidget(self.text_overlay_enabled, 0, Qt.AlignmentFlag.AlignVCenter)
        text_top_row.addWidget(
            self.text_overlay_from_middle, 0, Qt.AlignmentFlag.AlignVCenter
        )
        text_top_row.addWidget(
            self.text_overlay_after_frame_change, 0, Qt.AlignmentFlag.AlignVCenter
        )
        text_top_row.addStretch(1)
        text_top_w = QWidget()
        text_top_w.setLayout(text_top_row)
        text_outer.addWidget(text_top_w)
        self._text_panel = QWidget()
        tp = QVBoxLayout(self._text_panel)
        tp.setContentsMargins(0, 0, 0, 0)
        self.text_overlay_edit = QPlainTextEdit()
        self.text_overlay_edit.setPlaceholderText("Текст для наложения…")
        self.text_overlay_edit.setPlainText(DEFAULT_STITCH_TEXT_OVERLAY_TEXT)
        self.text_overlay_edit.setMaximumHeight(72)
        self.text_overlay_edit.textChanged.connect(self._schedule_preview)
        tp.addWidget(self.text_overlay_edit)
        opts = QGridLayout()
        self.text_overlay_font_size = QSpinBox()
        self.text_overlay_font_size.setRange(12, 240)
        self.text_overlay_font_size.setValue(DEFAULT_STITCH_TEXT_OVERLAY_FONT_SIZE)
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
        self.text_overlay_wave_amp.setValue(int(round(DEFAULT_STITCH_WAVE_AMP_FRAC * 100)))
        self.text_overlay_wave_amp.valueChanged.connect(self._on_wave_changed)
        self.text_overlay_wave_amp_label = QLabel()
        self.text_overlay_wave_speed = SmoothSlider(Qt.Orientation.Horizontal)
        self.text_overlay_wave_speed.setRange(0, 25)
        self.text_overlay_wave_speed.setValue(
            int(round(DEFAULT_STITCH_WAVE_FRAME_SPEED * 100))
        )
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

        previews_row = QHBoxLayout()
        previews_row.setContentsMargins(0, 0, 0, 0)
        previews_row.setSpacing(12)
        part1_col, self.text_overlay_preview_part1 = self._build_text_preview_column(
            title="Часть 1",
            part=1,
        )
        part2_col, self.text_overlay_preview_part2 = self._build_text_preview_column(
            title="Часть 2",
            part=2,
        )
        # Совместимость: основной якорь берём из части 1.
        self.text_overlay_preview = self.text_overlay_preview_part1
        previews_row.addWidget(part1_col, 1)
        previews_row.addWidget(part2_col, 1)
        previews_w = QWidget()
        previews_w.setLayout(previews_row)
        tp.addWidget(previews_w)
        text_hint = QLabel(
            "Перетащите текст · ▶ смотреть ролик · стрелки листают исходники каждой части"
        )
        text_hint.setObjectName("hint")
        text_hint.setWordWrap(True)
        tp.addWidget(text_hint)
        text_outer.addWidget(self._text_panel)
        self._update_text_overlay_controls()
        self._sync_color_btn(self.text_overlay_glow_btn, self._text_glow_color)
        self._sync_color_btn(self.text_overlay_text_btn, self._text_text_color)

        self._stitch_section_stack = QStackedWidget()
        self._stitch_section_stack.addWidget(wrap_work_section_page(io))
        self._stitch_section_stack.addWidget(wrap_work_section_page(text_gb))
        self._stitch_section_stack.addWidget(wrap_work_section_page(music_gb))
        self._stitch_section_stack.addWidget(wrap_work_section_page(transitions_gb))
        section_nav_group.idClicked.connect(self._stitch_section_stack.setCurrentIndex)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setMinimumWidth(0)
        inner = QWidget()
        inner.setMinimumWidth(0)
        inner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 0, 0)
        il.addWidget(self._stitch_section_stack)
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
                default_filename="zaliver_stitching_log.txt",
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

    def _part_start_dir(self, files: list[str]) -> str:
        if files:
            return str(Path(files[0]).parent)
        return str(Path.home())

    def _music_start_dir(self) -> str:
        return self._part_start_dir(self._music_files)

    def _pick_part_files(
        self, *, title: str, existing: list[str], replace: bool
    ) -> list[str] | None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            title,
            self._part_start_dir(existing),
            self._clips_dialog_filter(),
        )
        if not files:
            return None
        if replace:
            return self._merge_unique_paths([], files)
        return self._merge_unique_paths(existing, files)

    def _browse_part1(self) -> None:
        files = self._pick_part_files(
            title="Выберите видео для первой части",
            existing=self._part1_files,
            replace=True,
        )
        if files is not None:
            self._part1_files = files
            self._sync_part1_hint()
            self._sync_text_overlay_preview()
            self.save_settings()

    def _add_part1(self) -> None:
        files = self._pick_part_files(
            title="Добавить видео к первой части",
            existing=self._part1_files,
            replace=False,
        )
        if files is not None:
            self._part1_files = files
            self._sync_part1_hint()
            self._sync_text_overlay_preview()
            self.save_settings()

    def _clear_part1(self) -> None:
        if not self._part1_files:
            return
        self._part1_files = []
        self._sync_part1_hint()
        self._sync_text_overlay_preview()
        self.save_settings()

    def _browse_part2(self) -> None:
        files = self._pick_part_files(
            title="Выберите видео для второй части",
            existing=self._part2_files,
            replace=True,
        )
        if files is not None:
            self._part2_files = files
            self._sync_part2_hint()
            self._sync_text_overlay_preview()
            self.save_settings()

    def _add_part2(self) -> None:
        files = self._pick_part_files(
            title="Добавить видео ко второй части",
            existing=self._part2_files,
            replace=False,
        )
        if files is not None:
            self._part2_files = files
            self._sync_part2_hint()
            self._sync_text_overlay_preview()
            self.save_settings()

    def _clear_part2(self) -> None:
        if not self._part2_files:
            return
        self._part2_files = []
        self._sync_part2_hint()
        self._sync_text_overlay_preview()
        self.save_settings()

    def _browse_output_dir(self) -> None:
        start = self.output_dir_edit.text().strip() or self._part_start_dir(
            self._part1_files or self._part2_files
        )
        path = QFileDialog.getExistingDirectory(self, "Папка для склеенных роликов", start)
        if path:
            self.output_dir_edit.setText(path)
            self.save_settings()

    def _browse_music(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите аудиотреки для склейки (можно несколько)",
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

    def _sync_part_hint(
        self,
        files: list[str],
        hint: QLabel,
        btn_add: QPushButton,
        btn_clear: QPushButton,
        empty_text: str,
    ) -> None:
        n = len(files)
        has_files = n > 0
        btn_add.setVisible(has_files)
        btn_clear.setVisible(has_files)
        if n <= 0:
            hint.setText(empty_text)
            hint.setToolTip("")
            self._schedule_preview()
            return
        names = [Path(p).name for p in files]
        preview = ", ".join(names[:4])
        if n > 4:
            preview = f"{preview} и ещё {n - 4}"
        hint.setText(f"Выбрано: {n} ({preview})")
        hint.setToolTip("\n".join(names))
        self._schedule_preview()

    def _sync_part1_hint(self) -> None:
        self._sync_part_hint(
            self._part1_files,
            self._part1_hint,
            self._btn_add_part1,
            self._btn_clear_part1,
            "Не выбрано — нажмите «Выбрать клипы (часть 1)…»",
        )

    def _sync_part2_hint(self) -> None:
        self._sync_part_hint(
            self._part2_files,
            self._part2_hint,
            self._btn_add_part2,
            self._btn_clear_part2,
            "Не выбрано — нажмите «Выбрать клипы (часть 2)…»",
        )

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
        self.text_overlay_wave_amp_label.setText(f"{self.text_overlay_wave_amp.value()} %")
        self.text_overlay_wave_speed_label.setText(
            f"{self.text_overlay_wave_speed.value() / 100.0:.2f}"
        )

    def _update_text_overlay_controls(self, _checked: bool = False) -> None:
        on = bool(self.text_overlay_enabled.isChecked())
        self._text_panel.setEnabled(on)
        self.text_overlay_from_middle.setEnabled(on)
        self.text_overlay_after_frame_change.setEnabled(on)
        glow_on = bool(self.text_overlay_glow_enabled.isChecked())
        self.text_overlay_glow_btn.setEnabled(glow_on)
        self._sync_text_overlay_preview()

    def _on_from_middle_toggled(self, checked: bool) -> None:
        if checked and self.text_overlay_after_frame_change.isChecked():
            self.text_overlay_after_frame_change.blockSignals(True)
            self.text_overlay_after_frame_change.setChecked(False)
            self.text_overlay_after_frame_change.blockSignals(False)
        self._sync_text_overlay_preview()

    def _on_after_frame_change_toggled(self, checked: bool) -> None:
        if checked and self.text_overlay_from_middle.isChecked():
            self.text_overlay_from_middle.blockSignals(True)
            self.text_overlay_from_middle.setChecked(False)
            self.text_overlay_from_middle.blockSignals(False)
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
        _ax, ay = self.text_overlay_preview_part1.anchor()
        self._set_both_preview_anchors(0.5, ay)
        self.save_settings()

    def _center_text_vertically(self) -> None:
        ax, _ay = self.text_overlay_preview_part1.anchor()
        self._set_both_preview_anchors(ax, 0.5)
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

    def _build_text_preview_column(
        self, *, title: str, part: int
    ) -> tuple[QWidget, TextOverlayPreviewWidget]:
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("hint")
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(title_lbl)

        preview = TextOverlayPreviewWidget()
        preview.setMinimumHeight(220)
        preview.setMaximumHeight(320)
        preview.positionChanged.connect(self._on_preview_position_changed)
        col.addWidget(preview)

        btn_prev = QPushButton("‹")
        btn_prev.setObjectName("textPreviewNav")
        btn_prev.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_prev.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_prev.setFixedSize(36, 36)
        btn_prev.setToolTip("Предыдущий исходник")
        btn_prev.setAutoDefault(False)
        btn_prev.setDefault(False)
        btn_next = QPushButton("›")
        btn_next.setObjectName("textPreviewNav")
        btn_next.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_next.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_next.setFixedSize(36, 36)
        btn_next.setToolTip("Следующий исходник")
        btn_next.setAutoDefault(False)
        btn_next.setDefault(False)
        meta = QLabel("")
        meta.setObjectName("hint")
        meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        meta.setWordWrap(True)

        if part == 1:
            btn_prev.clicked.connect(self._text_overlay_preview_prev_part1)
            btn_next.clicked.connect(self._text_overlay_preview_next_part1)
            self._btn_text_preview_prev_part1 = btn_prev
            self._btn_text_preview_next_part1 = btn_next
            self._text_overlay_preview_meta_part1 = meta
        else:
            btn_prev.clicked.connect(self._text_overlay_preview_prev_part2)
            btn_next.clicked.connect(self._text_overlay_preview_next_part2)
            self._btn_text_preview_prev_part2 = btn_prev
            self._btn_text_preview_next_part2 = btn_next
            self._text_overlay_preview_meta_part2 = meta

        nav = QHBoxLayout()
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(8)
        nav.addWidget(btn_prev, 0)
        nav.addWidget(meta, 1)
        nav.addWidget(btn_next, 0)
        nav_w = QWidget()
        nav_w.setLayout(nav)
        col.addWidget(nav_w)

        wrap = QWidget()
        wrap.setLayout(col)
        return wrap, preview

    def _on_preview_position_changed(self, ax: float, ay: float) -> None:
        self._set_both_preview_anchors(ax, ay, source=self.sender())
        self.save_settings()

    def _set_both_preview_anchors(
        self,
        ax: float,
        ay: float,
        *,
        source: object | None = None,
    ) -> None:
        for preview in (self.text_overlay_preview_part1, self.text_overlay_preview_part2):
            if preview is source:
                continue
            preview.blockSignals(True)
            preview.set_anchor(ax, ay)
            preview.blockSignals(False)

    def _text_overlay_preview_videos(self, part: int) -> list[str]:
        files = self._part1_files if part == 1 else self._part2_files
        out: list[str] = []
        seen: set[str] = set()
        for raw in files:
            try:
                path = Path(str(raw))
                if not path.is_file():
                    continue
                resolved = str(path.resolve())
            except OSError:
                continue
            key = os.path.normcase(os.path.normpath(resolved))
            if key in seen:
                continue
            seen.add(key)
            out.append(resolved)
        return out

    def _clamp_text_overlay_preview_index(self, part: int) -> None:
        videos = self._text_overlay_preview_videos(part)
        n = len(videos)
        attr = (
            "_text_overlay_preview_index_part1"
            if part == 1
            else "_text_overlay_preview_index_part2"
        )
        if n <= 0:
            setattr(self, attr, 0)
            return
        setattr(self, attr, int(getattr(self, attr)) % n)

    def _update_text_overlay_preview_nav(self, part: int) -> None:
        videos = self._text_overlay_preview_videos(part)
        n = len(videos)
        self._clamp_text_overlay_preview_index(part)
        idx = int(
            self._text_overlay_preview_index_part1
            if part == 1
            else self._text_overlay_preview_index_part2
        )
        can_cycle = n > 1
        if part == 1:
            btn_prev = self._btn_text_preview_prev_part1
            btn_next = self._btn_text_preview_next_part1
            meta = self._text_overlay_preview_meta_part1
            empty_msg = "Нет исходников части 1"
        else:
            btn_prev = self._btn_text_preview_prev_part2
            btn_next = self._btn_text_preview_next_part2
            meta = self._text_overlay_preview_meta_part2
            empty_msg = "Нет исходников части 2"
        btn_prev.setEnabled(can_cycle)
        btn_next.setEnabled(can_cycle)
        if n <= 0:
            meta.setText(empty_msg)
        else:
            meta.setText(f"{idx + 1} / {n} · {Path(videos[idx]).name}")

    def _text_overlay_preview_step(self, part: int, delta: int) -> None:
        videos = self._text_overlay_preview_videos(part)
        n = len(videos)
        attr = (
            "_text_overlay_preview_index_part1"
            if part == 1
            else "_text_overlay_preview_index_part2"
        )
        if n <= 0:
            self._update_text_overlay_preview_nav(part)
            return
        if n == 1:
            setattr(self, attr, 0)
            self._apply_text_overlay_preview_video(part, force=True)
            return
        setattr(self, attr, (int(getattr(self, attr)) + delta) % n)
        self._apply_text_overlay_preview_video(part, force=True)

    def _text_overlay_preview_prev_part1(self) -> None:
        self._text_overlay_preview_step(1, -1)

    def _text_overlay_preview_next_part1(self) -> None:
        self._text_overlay_preview_step(1, 1)

    def _text_overlay_preview_prev_part2(self) -> None:
        self._text_overlay_preview_step(2, -1)

    def _text_overlay_preview_next_part2(self) -> None:
        self._text_overlay_preview_step(2, 1)

    def _apply_text_overlay_preview_video(self, part: int, *, force: bool = False) -> None:
        preview = (
            self.text_overlay_preview_part1
            if part == 1
            else self.text_overlay_preview_part2
        )
        videos = self._text_overlay_preview_videos(part)
        self._update_text_overlay_preview_nav(part)
        current = None
        if videos:
            self._clamp_text_overlay_preview_index(part)
            idx = int(
                self._text_overlay_preview_index_part1
                if part == 1
                else self._text_overlay_preview_index_part2
            )
            current = videos[idx]
        preview.set_background_video(current, force=force)
        overlay_on = bool(self.text_overlay_enabled.isChecked())
        after_cut = bool(self.text_overlay_after_frame_change.isChecked())
        # «После смены кадра» — текст только на части 2.
        preview.set_text_visible(overlay_on and not (part == 1 and after_cut))

    def _sync_text_overlay_preview(
        self, anchor_x: float | None = None, anchor_y: float | None = None
    ) -> None:
        self._apply_text_overlay_preview_video(1, force=False)
        self._apply_text_overlay_preview_video(2, force=False)
        overlay_on = bool(self.text_overlay_enabled.isChecked())
        if not overlay_on:
            return
        orient = self.text_overlay_orientation.currentData()
        orientation = orient if isinstance(orient, str) else "vertical"
        font_size = int(self.text_overlay_font_size.value())
        glow_on = bool(self.text_overlay_glow_enabled.isChecked())
        letter_spacing = int(self.text_overlay_letter_spacing.value())
        font_bold = bool(self.text_overlay_font_bold.isChecked())
        wave_amp = self.text_overlay_wave_amp.value() / 100.0
        wave_speed = self.text_overlay_wave_speed.value() / 100.0
        text = self.text_overlay_edit.toPlainText()
        for preview in (self.text_overlay_preview_part1, self.text_overlay_preview_part2):
            preview.blockSignals(True)
            preview.set_orientation(orientation)
            preview.set_font_size(font_size)
            preview.set_glow_enabled(glow_on)
            preview.set_glow_color(self._text_glow_color)
            preview.set_text_color(self._text_text_color)
            preview.set_letter_spacing(letter_spacing)
            preview.set_font_path(self._text_font_path)
            preview.set_font_bold(font_bold)
            preview.set_wave_settings(wave_amp, wave_speed)
            preview.set_text(text)
            if anchor_x is not None and anchor_y is not None:
                preview.set_anchor(anchor_x, anchor_y)
            preview.blockSignals(False)
