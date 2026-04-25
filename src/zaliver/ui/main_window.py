"""Main application window."""

from __future__ import annotations

import os
import sys
from functools import partial
from pathlib import Path

from PyQt6.QtCore import QSettings, QThread, Qt
from PyQt6.QtGui import QShowEvent
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from zaliver.processing.ffmpeg_merge import check_ffmpeg_tools
from zaliver.processing.pipeline import RandomUniquifyBounds, UniquifySettings
from zaliver.processing.thread_worker import ProcessingController
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


def _default_workers() -> int:
    # Для одиночного длинного ролика приложение умеет нарезать на части (если есть ffmpeg)
    # и тем самым эффективно загрузить все CPU. Поэтому по умолчанию используем все
    # логические ядра, а не (CPU-1).
    return max(1, os.cpu_count() or 2)


def _max_worker_slider() -> int:
    # До всех логических CPU: при разбиении ролика на части полезнее занять последнее ядро.
    return max(1, os.cpu_count() or 2)


class MainWindow(QWidget):
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

        self._settings = QSettings("Zaliver", "Zaliver")
        self._build_ui()
        self._apply_theme()
        self.showMaximized()
        self._load_folder_settings()
        self._sync_ffmpeg_install_row()

    def _theme_path(self) -> Path:
        return Path(__file__).with_name("theme.qss")

    def _apply_theme(self) -> None:
        p = self._theme_path()
        if p.is_file():
            self.setStyleSheet(p.read_text(encoding="utf-8"))

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(20, 16, 20, 16)

        title = QLabel("Zaliver")
        title.setObjectName("title")
        sub = QLabel("Выбор видео → папка результатов · случайная уникализация ")
        sub.setObjectName("hint")
        root.addWidget(title)
        root.addWidget(sub)

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
            "Имена: имя_unique.mp4 при одной копии; при нескольких — "
            "имя_unique_001.mp4 …"
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
        self.rb_noise_min, self.rb_noise_max = _dspin(0.15, 4.0, 0.05, 2)
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
        run_row = QHBoxLayout()
        self.btn_start = QPushButton("Старт")
        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setEnabled(False)
        self.btn_start.clicked.connect(self._start)
        self.btn_cancel.clicked.connect(self._cancel)
        run_row.addWidget(self.btn_start)
        run_row.addWidget(self.btn_cancel)
        run_row.addStretch()
        rl.addLayout(run_row)

        self.progress = AnimatedProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValueImmediate(0)
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("hint")
        rl.addWidget(self.progress)
        rl.addWidget(self.progress_label)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(220)
        self.log.setPlaceholderText("Лог…")
        rl.addWidget(self.log, 1)

        splitter.addWidget(scroll_left)
        splitter.addWidget(right)
        splitter.setSizes([420, 580])
        root.addWidget(splitter, 1)

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
