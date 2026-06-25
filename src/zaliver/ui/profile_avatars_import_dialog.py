"""Диалог импорта аватарок из спрайт-листа в отмеченные профили."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QThread
from PyQt6.QtGui import QCloseEvent, QPixmap, QResizeEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from zaliver.ui.avatar_detection_worker import (
    AvatarDetectionCancel,
    AvatarDetectionWorker,
)
from zaliver.ui.avatar_import_parser import (
    assign_avatars_to_selected_profiles,
    build_selected_profile_avatar_rows,
)
from zaliver.ui.widgets import AnimatedProgressBar

# Длинная сторона меньше порога — в превью показываем заметно меньше натурального размера.
_SMALL_PREVIEW_MAX_SIDE = 520
_PREVIEW_VIEWPORT_MARGIN = 20


def _fit_preview_pixmap(pix: QPixmap, max_w: int, max_h: int) -> QPixmap:
    if pix.isNull() or max_w < 1 or max_h < 1:
        return pix
    w, h = pix.width(), pix.height()
    if w < 1 or h < 1:
        return pix

    # Вписать в область просмотра, никогда не увеличивать.
    scale = min(max_w / w, max_h / h, 1.0)
    max_side = max(w, h)
    if max_side < _SMALL_PREVIEW_MAX_SIDE:
        small_cap = 0.32 + 0.5 * (max_side / _SMALL_PREVIEW_MAX_SIDE)
        scale = min(scale, small_cap)

    target_w = max(1, int(w * scale))
    target_h = max(1, int(h * scale))
    if target_w == w and target_h == h:
        return pix
    return pix.scaled(
        target_w,
        target_h,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _pixmap_from_png(png_bytes: bytes, size: int = 48) -> QPixmap:
    pix = QPixmap()
    if not png_bytes:
        return pix
    if not pix.loadFromData(png_bytes, "PNG"):
        return pix
    return pix.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _format_source_files(paths: list[str]) -> str:
    if not paths:
        return "Файлы не выбраны"
    if len(paths) == 1:
        return paths[0]
    names = [Path(p).name for p in paths]
    if len(names) <= 3:
        return f"{len(paths)} файла: {', '.join(names)}"
    return f"{len(paths)} файлов: {', '.join(names[:2])}, …"


class ProfileAvatarsImportDialog(QDialog):
    def __init__(
        self,
        *,
        selected_profiles: list[dict[str, object]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Добавить аватарки")
        self.setModal(True)
        self.setMinimumSize(760, 720)
        self.resize(820, 780)

        self._profiles = list(selected_profiles)
        self._rows = build_selected_profile_avatar_rows(self._profiles)
        self._avatar_count = 0
        self._avatar_pngs: list[bytes] = []
        self._source_paths: list[str] = []
        self._detect_thread: QThread | None = None
        self._detect_worker: AvatarDetectionWorker | None = None
        self._detect_cancel = AvatarDetectionCancel()
        self._detect_aborted = False
        self._last_preview_png = b""
        self._last_preview_subtitle = ""

        root = QVBoxLayout(self)
        root.setSpacing(12)

        hint = QLabel(
            "В таблице — отмеченные профили. Выберите один или несколько файлов с аватарками. "
            "По умолчанию каждый файл обрабатывается как спрайт-лист: программа находит "
            "и вырезает иконки, объединяет их и сопоставляет с профилями по порядку.\n\n"
            "Если включить «Не обрезать аватарки», каждый файл целиком назначается одному "
            "профилю (1 файл = 1 профиль, в порядке выбора).\n\n"
            "Аватарки загружаются в YouTube Studio (раздел «Настройка канала» → Picture)."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        root.addWidget(hint)

        self._preview_label = QLabel("Превью появится после выбора файлов")
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setObjectName("profilePreviewImage")
        self._preview_label.setStyleSheet("background: #111; color: #888;")
        self._preview_label.setScaledContents(False)

        self._preview_scroll = QScrollArea()
        self._preview_scroll.setWidgetResizable(False)
        self._preview_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_scroll.setMinimumHeight(140)
        self._preview_scroll.setMaximumHeight(180)
        self._preview_scroll.setWidget(self._preview_label)
        root.addWidget(self._preview_scroll, 0)

        source_row = QHBoxLayout()
        self._source_label = QLabel("Файлы не выбраны")
        self._source_label.setObjectName("hint")
        self._source_label.setWordWrap(True)
        self._btn_pick = QPushButton("Выбрать файлы…")
        self._btn_pick.clicked.connect(self._pick_files)
        source_row.addWidget(self._source_label, 1)
        source_row.addWidget(self._btn_pick)
        root.addLayout(source_row)

        self._no_crop = QCheckBox("Не обрезать аватарки (1 файл = 1 профиль)")
        self._no_crop.setToolTip(
            "Каждый выбранный файл загружается целиком и назначается одному профилю "
            "без поиска и вырезки иконок на спрайт-листе."
        )
        root.addWidget(self._no_crop)

        self._shuffle = QCheckBox("Перемешать аватарки")
        self._shuffle.setChecked(True)
        self._shuffle.setToolTip(
            "Случайно перемешать аватарки перед сопоставлением с профилями."
        )
        self._shuffle.toggled.connect(self._on_shuffle_toggled)
        root.addWidget(self._shuffle)

        self._progress_bar = AnimatedProgressBar()
        self._progress_bar.setRange(0, 1)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("Ожидание…")
        self._progress_bar.setVisible(False)
        root.addWidget(self._progress_bar)

        self._status = QLabel("")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["#", "Аватарка", "Профиль", "Статус"]
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setColumnWidth(0, 36)
        self._table.setColumnWidth(1, 72)
        self._table.setMinimumHeight(320)
        self._table.verticalHeader().setDefaultSectionSize(72)
        root.addWidget(self._table, 2)

        btns = QHBoxLayout()
        btns.addStretch()
        self._btn_cancel = QPushButton("Отмена")
        self._btn_cancel.setObjectName("secondary")
        self._btn_save = QPushButton("Загрузить в Studio")
        self._btn_save.setDefault(True)
        self._btn_save.setAutoDefault(True)
        self._btn_save.setEnabled(False)
        self._btn_cancel.clicked.connect(self.reject)
        self._btn_save.clicked.connect(self._on_save)
        btns.addWidget(self._btn_cancel)
        btns.addWidget(self._btn_save)
        root.addLayout(btns)

        self._populate_table()
        self._status.setText(
            f"Отмечено профилей: {len(self._profiles)}. Выберите файлы с аватарками."
        )

    def upload_assignments(self) -> list[tuple[str, bytes]]:
        out: list[tuple[str, bytes]] = []
        for row in self._rows:
            if not row.get("can_save"):
                continue
            pid = str(row.get("profile_id") or "").strip()
            png = row.get("avatar_png")
            if not pid or not isinstance(png, (bytes, bytearray)) or not png:
                continue
            out.append((pid, bytes(png)))
        return out

    def avatar_count(self) -> int:
        return self._avatar_count

    def _is_detecting(self) -> bool:
        return self._detect_thread is not None and self._detect_thread.isRunning()

    def _set_busy(self, busy: bool) -> None:
        self._btn_pick.setEnabled(not busy)
        self._no_crop.setEnabled(not busy)
        self._shuffle.setEnabled(not busy)
        self._progress_bar.setVisible(busy)
        if busy:
            self._btn_save.setEnabled(False)
        else:
            self._progress_bar.setValue(0)
            self._progress_bar.setFormat("Ожидание…")
            self._btn_save.setEnabled(any(row.get("can_save") for row in self._rows))

    def _stop_detection_thread(self) -> None:
        self._detect_aborted = True
        self._detect_cancel.cancelled = True
        thread = self._detect_thread
        if thread is None:
            return
        if thread.isRunning():
            thread.quit()
            thread.wait(10_000)
        self._detect_thread = None
        self._detect_worker = None

    def _pick_files(self) -> None:
        if self._is_detecting():
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите картинки с аватарками",
            "",
            "Изображения (*.png *.jpg *.jpeg *.webp *.bmp);;Все файлы (*.*)",
        )
        if not paths:
            return
        file_paths = [Path(p) for p in paths if (p or "").strip()]
        if not file_paths:
            return
        self._start_detection(file_paths)

    def _start_detection(self, paths: list[Path]) -> None:
        self._stop_detection_thread()
        self._detect_aborted = False
        self._detect_cancel = AvatarDetectionCancel()
        self._source_paths = [str(p) for p in paths]
        self._source_label.setText(_format_source_files(self._source_paths))
        self._clear_preview(placeholder="Обработка изображений…")
        self._status.setText("Подготовка к обработке…")
        self._progress_bar.setRange(0, max(1, len(paths)))
        self._progress_bar.setValue(0)
        crop = not self._no_crop.isChecked()
        self._progress_bar.setFormat("Загрузка %v из %m" if not crop else "Файл %v из %m")
        self._set_busy(True)

        thread = QThread(self)
        worker = AvatarDetectionWorker(
            paths,
            crop_sprites=crop,
            cancel=self._detect_cancel,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_detection_progress)
        worker.file_preview.connect(self._on_detection_file_preview)
        worker.finished.connect(self._on_detection_finished)
        worker.failed.connect(self._on_detection_failed)
        worker.aborted.connect(self._on_detection_aborted)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.aborted.connect(thread.quit)
        thread.finished.connect(self._on_detection_thread_finished)
        self._detect_thread = thread
        self._detect_worker = worker
        thread.start()

    def _on_detection_thread_finished(self) -> None:
        self._detect_thread = None
        self._detect_worker = None

    def _on_detection_progress(self, done: int, total: int, filename: str) -> None:
        if self._detect_aborted:
            return
        max_files = max(1, total)
        self._progress_bar.setRange(0, max_files)
        self._progress_bar.setValue(min(done, max_files))
        no_crop = self._no_crop.isChecked()
        verb = "Загрузка" if no_crop else "Обработка"
        if filename and done < total:
            self._progress_bar.setFormat(
                f"{'Загрузка' if no_crop else 'Файл'} %v из %m — {filename}"
            )
            self._status.setText(
                f"{verb} файла {done + 1} из {total}: {filename}…"
            )
        elif done >= total and total > 0:
            self._progress_bar.setValue(max_files)
            self._progress_bar.setFormat("Готово")
            self._status.setText("Сборка результатов…")

    def _on_detection_file_preview(self, preview_png: bytes, path_str: str) -> None:
        if self._detect_aborted:
            return
        self._apply_preview_pixmap(preview_png, subtitle=Path(path_str).name)

    def _preview_bounds(self) -> tuple[int, int]:
        vp = self._preview_scroll.viewport()
        margin = _PREVIEW_VIEWPORT_MARGIN
        max_w = max(160, vp.width() - margin * 2)
        max_h = max(120, vp.height() - margin * 2)
        return max_w, max_h

    def _clear_preview(self, *, placeholder: str) -> None:
        self._last_preview_png = b""
        self._last_preview_subtitle = ""
        self._preview_label.clear()
        self._preview_label.setText(placeholder)
        self._preview_label.setPixmap(QPixmap())
        self._preview_label.setMinimumSize(0, 0)
        self._preview_label.resize(1, 1)

    def _apply_preview_pixmap(self, preview_png: bytes, *, subtitle: str = "") -> None:
        self._last_preview_png = preview_png or b""
        self._last_preview_subtitle = subtitle or ""

        preview_pix = QPixmap()
        if preview_png:
            preview_pix.loadFromData(preview_png, "PNG")
        max_w, max_h = self._preview_bounds()
        if not preview_pix.isNull():
            preview_pix = _fit_preview_pixmap(preview_pix, max_w, max_h)
        if preview_pix.isNull():
            self._clear_preview(placeholder="Превью недоступно")
            return

        self._preview_label.setText("")
        self._preview_label.setPixmap(preview_pix)
        self._preview_label.setFixedSize(preview_pix.size())
        tip = (
            "Превью последнего загруженного файла"
            if self._no_crop.isChecked()
            else "Превью последнего обработанного файла"
        )
        if subtitle:
            tip += f": {subtitle}"
        self._preview_label.setToolTip(tip)
    def _on_detection_aborted(self) -> None:
        if not self._detect_aborted:
            self._detect_aborted = True
        self._set_busy(False)

    def _on_detection_failed(self, message: str) -> None:
        if self._detect_aborted:
            return
        self._set_busy(False)
        QMessageBox.warning(self, "Добавить аватарки", message)
        self._status.setText(
            f"Отмечено профилей: {len(self._profiles)}. Выберите другие файлы."
        )
        self._clear_preview(placeholder="Превью появится после выбора файлов")

    def _on_detection_finished(
        self,
        pngs: list,
        preview_png: bytes,
        _last_path: str,
        source_paths: list,
        errors: list,
    ) -> None:
        if self._detect_aborted:
            return
        self._set_busy(False)
        if isinstance(source_paths, list):
            self._source_paths = [str(p) for p in source_paths if str(p).strip()]
            self._source_label.setText(_format_source_files(self._source_paths))

        avatar_pngs = [bytes(p) for p in pngs if p]
        if not avatar_pngs:
            self._rows = build_selected_profile_avatar_rows(self._profiles)
            self._avatar_count = 0
            self._avatar_pngs = []
            self._populate_table()
            self._clear_preview(
                placeholder=(
                    "Файлы не загружены"
                    if self._no_crop.isChecked()
                    else "Аватарки не найдены"
                )
            )
            self._btn_save.setEnabled(False)
            self._status.setText(
                "Не удалось загрузить выбранные файлы. Попробуйте другие."
                if self._no_crop.isChecked()
                else "В выбранных файлах не найдено аватарок. Попробуйте другие файлы."
            )
            return

        self._avatar_count = len(avatar_pngs)
        self._avatar_pngs = avatar_pngs
        self._apply_avatar_assignment()
        self._apply_preview_pixmap(bytes(preview_png) if preview_png else b"")

        matched = sum(1 for row in self._rows if row.get("can_save"))
        extra = sum(
            1
            for row in self._rows
            if not row.get("can_save")
            and str(row.get("status") or "").startswith("Лишняя")
        )
        missing = sum(
            1
            for row in self._rows
            if str(row.get("status") or "") == "Нет аватарки в файле"
        )
        no_crop = self._no_crop.isChecked()
        count_label = "Загружено файлов" if no_crop else "Найдено аватарок"
        parts = [
            f"{count_label}: {len(avatar_pngs)} "
            f"({len(self._source_paths)} файл(ов)).",
            f"Готово к загрузке: {matched}.",
        ]
        if missing:
            label = "Без файла" if no_crop else "Без аватарки"
            parts.append(f"{label}: {missing}.")
        if extra:
            label = "Лишних файлов" if no_crop else "Лишних аватарок"
            parts.append(f"{label}: {extra}.")
        warn_lines = [str(e) for e in errors if str(e).strip()]
        if warn_lines:
            parts.append(f"Предупреждений по файлам: {len(warn_lines)}.")
        self._status.setText(" ".join(parts))
        self._btn_save.setEnabled(matched > 0)

        if warn_lines:
            preview = "\n".join(warn_lines[:8])
            if len(warn_lines) > 8:
                preview += f"\n… и ещё {len(warn_lines) - 8}."
            QMessageBox.warning(
                self,
                "Добавить аватарки",
                "Часть файлов обработана с предупреждениями:\n\n" + preview,
            )

    def _apply_avatar_assignment(self) -> None:
        self._rows = assign_avatars_to_selected_profiles(
            self._profiles,
            self._avatar_pngs,
            shuffle=self._shuffle.isChecked(),
        )
        self._populate_table()
        if not self._is_detecting():
            self._btn_save.setEnabled(any(row.get("can_save") for row in self._rows))

    def _on_shuffle_toggled(self, _checked: bool) -> None:
        if self._is_detecting() or not self._avatar_pngs:
            return
        self._apply_avatar_assignment()

    def _populate_table(self) -> None:
        self._table.setRowCount(len(self._rows))
        for i, row in enumerate(self._rows):
            avatar_index = int(row.get("avatar_index") or 0)
            index_text = str(avatar_index) if avatar_index > 0 else "—"
            png = row.get("avatar_png")
            png_bytes = bytes(png) if isinstance(png, (bytes, bytearray)) else b""

            profile_name = str(row.get("profile_name") or "")
            profile_id = str(row.get("profile_id") or "")
            if profile_name and profile_id:
                profile_cell = f"{profile_name} ({profile_id})"
            elif profile_name:
                profile_cell = profile_name
            else:
                profile_cell = "—"
            status = str(row.get("status") or "")

            thumb_label = QLabel()
            thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pix = _pixmap_from_png(png_bytes, size=56)
            if not pix.isNull():
                thumb_label.setPixmap(pix)
            else:
                thumb_label.setText("—")
            self._table.setCellWidget(i, 1, thumb_label)

            for col, value in enumerate([index_text, "", profile_cell, status]):
                if col == 1:
                    continue
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
                    )
                if not row.get("can_save") and col == 3:
                    item.setForeground(Qt.GlobalColor.darkRed)
                self._table.setItem(i, col, item)

        self._table.resizeColumnsToContents()
        self._table.setColumnWidth(1, 72)

    def _on_save(self) -> None:
        if self._is_detecting():
            return
        matched = sum(1 for row in self._rows if row.get("can_save"))
        if matched <= 0:
            QMessageBox.warning(
                self,
                "Добавить аватарки",
                "Нет аватарок, сопоставленных с профилями. Сохранять нечего.",
            )
            return
        unmatched = len(self._profiles) - matched
        msg = f"Загрузить аватарки в YouTube Studio для {matched} профилей?"
        if unmatched > 0:
            msg += f"\n\n{unmatched} профилей останутся без аватарки."
        extra = self._avatar_count - matched
        if extra > 0:
            msg += f"\n\n{extra} лишних аватарок будут пропущены."
        answer = QMessageBox.question(
            self,
            "Добавить аватарки",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.accept()

    def reject(self) -> None:
        if self._is_detecting():
            self._stop_detection_thread()
            self._set_busy(False)
            self._status.setText("Обработка отменена.")
            self._clear_preview(placeholder="Превью появится после выбора файлов")
            return
        self._stop_detection_thread()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._is_detecting():
            self._stop_detection_thread()
        self._stop_detection_thread()
        super().closeEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._last_preview_png:
            self._apply_preview_pixmap(
                self._last_preview_png,
                subtitle=self._last_preview_subtitle,
            )
