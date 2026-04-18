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

from zaliver.processing.ffmpeg_merge import check_ffmpeg
from zaliver.processing.pipeline import UniquifySettings
from zaliver.processing.thread_worker import ProcessingController
from zaliver.ui.ffmpeg_install_worker import FfmpegInstallWorker
from zaliver.ui.widgets import (
    AnimatedProgressBar,
    CollapsibleSection,
    SmoothSlider,
    ToggleSwitch,
)


def _default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


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

        self._settings = QSettings("Zaliver", "Zaliver")
        self._build_ui()
        self._apply_theme()
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
        sub = QLabel("Папка с видео → папка результатов · случайная уникализация ")
        sub.setObjectName("hint")
        root.addWidget(title)
        root.addWidget(sub)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        io = QGroupBox("Папки")
        io_grid = QGridLayout(io)
        self.input_dir_edit = QLineEdit()
        self.input_dir_edit.setPlaceholderText("Папка с исходными видео…")
        btn_in = QPushButton("Обзор…")
        btn_in.setObjectName("secondary")
        btn_in.clicked.connect(self._browse_input_dir)
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText("Папка для уникализированных файлов…")
        btn_out = QPushButton("Обзор…")
        btn_out.setObjectName("secondary")
        btn_out.clicked.connect(self._browse_output_dir)
        io_grid.addWidget(QLabel("Входная папка:"), 0, 0)
        io_grid.addWidget(self.input_dir_edit, 0, 1)
        io_grid.addWidget(btn_in, 0, 2)
        io_grid.addWidget(QLabel("Выходная папка:"), 1, 0)
        io_grid.addWidget(self.output_dir_edit, 1, 1)
        io_grid.addWidget(btn_out, 1, 2)
        self.copies_per_file = QSpinBox()
        self.copies_per_file.setRange(1, 500)
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
        self.thread_slider.setMaximum(_max_worker_slider())
        self.thread_slider.setValue(_default_workers())
        self.thread_label = QLabel()
        self._update_thread_label(self.thread_slider.value())
        self.thread_slider.valueChanged.connect(self._update_thread_label)

        proc_hint = QLabel(
            "Несколько роликов — параллельно по файлам. Длинный одиночный ролик "
            "при наличии ffmpeg в системе режется на части и тоже грузит несколько "
            "ядер, затем быстро склеивается. Результат — MP4 (mp4v), только видео."
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

        pg.addWidget(QLabel("Потоков процессов:"), 2, 0)
        thr_row = QHBoxLayout()
        thr_row.addWidget(self.thread_slider, 1)
        thr_row.addWidget(self.thread_label)
        w_thr = QWidget()
        w_thr.setLayout(thr_row)
        pg.addWidget(w_thr, 2, 1)

        fx = QGroupBox("Уникализация (лёгкие эффекты)")
        fx_layout = QVBoxLayout(fx)
        fx_layout.setSpacing(8)

        auto_grid = QGridLayout()
        self.auto_color = ToggleSwitch(
            "Автокоррекция цвета (выборка кадров из ролика, единые параметры)"
        )
        self.auto_color.setChecked(False)
        self.auto_color_strength = QDoubleSpinBox()
        self.auto_color_strength.setRange(0.25, 1.0)
        self.auto_color_strength.setSingleStep(0.05)
        self.auto_color_strength.setValue(0.85)
        self.auto_color_strength.setDecimals(2)
        self.auto_color_strength.setButtonSymbols(
            QAbstractSpinBox.ButtonSymbols.NoButtons
        )
        auto_grid.addWidget(self.auto_color, 0, 0, 1, 2)
        auto_grid.addWidget(QLabel("Сила автоколора (1 = полностью):"), 1, 0)
        auto_grid.addWidget(self.auto_color_strength, 1, 1)
        self.auto_color_frames = QSpinBox()
        self.auto_color_frames.setRange(16, 200)
        self.auto_color_frames.setValue(48)
        auto_grid.addWidget(QLabel("Кадров для анализа колора:"), 2, 0)
        auto_grid.addWidget(self.auto_color_frames, 2, 1)
        fx_layout.addLayout(auto_grid)

        self.random_uniquify = ToggleSwitch(
            "Случайные параметры для каждого файла (каждый запуск — новый набор)"
        )
        self.random_uniquify.setChecked(True)
        self.random_uniquify.toggled.connect(self._on_random_uniquify_toggled)
        fx_layout.addWidget(self.random_uniquify)

        self._manual_section = CollapsibleSection("Ручные параметры и аудио")
        manual_inner = QWidget()
        mg = QGridLayout(manual_inner)

        self.brightness = QSpinBox()
        self.brightness.setRange(-40, 40)
        self.brightness.setValue(0)
        self.contrast = QDoubleSpinBox()
        self.contrast.setRange(0.85, 1.15)
        self.contrast.setSingleStep(0.01)
        self.contrast.setValue(1.0)
        self.contrast.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.saturation = QDoubleSpinBox()
        self.saturation.setRange(0.9, 1.1)
        self.saturation.setSingleStep(0.01)
        self.saturation.setValue(1.0)
        self.saturation.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.crop_jitter = QSpinBox()
        self.crop_jitter.setRange(0, 4)
        self.crop_jitter.setValue(1)
        self.scale_pct = QDoubleSpinBox()
        self.scale_pct.setRange(99.5, 100.5)
        self.scale_pct.setDecimals(2)
        self.scale_pct.setValue(100.0)
        self.scale_pct.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.noise = QDoubleSpinBox()
        self.noise.setRange(0.0, 6.0)
        self.noise.setSingleStep(0.5)
        self.noise.setValue(1.0)
        self.noise.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.seed = QSpinBox()
        self.seed.setRange(0, 999_999)
        self.seed.setValue(42)

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

        self._manual_section.content_layout().addWidget(manual_inner)
        fx_layout.addWidget(self._manual_section)
        self._manual_panel = manual_inner
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
        if check_ffmpeg():
            self._ffmpeg_row.setVisible(False)
            return
        self._ffmpeg_row.setVisible(True)
        if sys.platform == "darwin":
            hint = (
                "ffmpeg не найден — без него длинные ролики не режутся на части. "
                "Кнопка справа: сначала Homebrew (brew install ffmpeg), иначе "
                "скачивание статической сборки (нужен интернет). На Apple Silicon "
                "лучше поставить brew."
            )
        else:
            hint = (
                "ffmpeg не найден — без него длинные ролики не режутся на части для "
                "ускорения. Нажмите кнопку справа (winget или pip, нужен интернет)."
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
        if check_ffmpeg():
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
        self.thread_label.setText(f"{v} / {mx}")

    def _load_folder_settings(self) -> None:
        inp = self._settings.value("input_folder", "", type=str) or ""
        out = self._settings.value("output_folder", "", type=str) or ""
        self.input_dir_edit.setText(inp)
        self.output_dir_edit.setText(out)

    def _save_folder_settings(self) -> None:
        self._settings.setValue("input_folder", self.input_dir_edit.text().strip())
        self._settings.setValue("output_folder", self.output_dir_edit.text().strip())

    def _browse_input_dir(self) -> None:
        start = self.input_dir_edit.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Папка с исходными видео", start)
        if path:
            self.input_dir_edit.setText(path)
            self._save_folder_settings()

    def _browse_output_dir(self) -> None:
        start = self.output_dir_edit.text().strip() or self.input_dir_edit.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Папка для результатов", start)
        if path:
            self.output_dir_edit.setText(path)
            self._save_folder_settings()

    def _on_random_uniquify_toggled(self, random_on: bool) -> None:
        self._manual_panel.setEnabled(not random_on)
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
            auto_color_grade=self.auto_color.isChecked(),
            auto_color_strength=float(self.auto_color_strength.value()),
        )
        return {
            "input_dir": self.input_dir_edit.text().strip(),
            "output_dir": self.output_dir_edit.text().strip(),
            "num_workers": int(self.thread_slider.value()),
            "settings": st.to_dict(),
            "randomize_uniquify": self.random_uniquify.isChecked(),
            "auto_color_sample_frames": int(self.auto_color_frames.value()),
            "copies_per_file": int(self.copies_per_file.value()),
        }

    def _start(self) -> None:
        self._save_folder_settings()
        opts = self._build_options()
        if not opts["input_dir"] or not opts["output_dir"]:
            QMessageBox.warning(
                self, "Zaliver", "Укажите входную и выходную папку."
            )
            return
        if Path(opts["input_dir"]).resolve() == Path(opts["output_dir"]).resolve():
            QMessageBox.warning(
                self,
                "Zaliver",
                "Входная и выходная папки не должны совпадать.",
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
