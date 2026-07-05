"""Диалог настройки канала: описание, ссылка, аватарки и названия."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QThread
from PyQt6.QtGui import QCloseEvent, QColor, QPixmap, QResizeEvent, QShowEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
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
    QApplication,
)

from zaliver.ui.avatar_detection_worker import (
    AvatarDetectionCancel,
    AvatarDetectionWorker,
)
from zaliver.ui.avatar_import_parser import (
    assign_avatars_to_selected_profiles,
    build_selected_profile_avatar_rows,
    parse_channel_names_file,
    parse_channel_names_text,
)
from zaliver.ui.widgets import AnimatedProgressBar, CollapsibleSection

# Длинная сторона меньше порога — в превью показываем заметно меньше натурального размера.
_SMALL_PREVIEW_MAX_SIDE = 520
_PREVIEW_VIEWPORT_MARGIN = 20


def _recent_editable_combo(*, placeholder: str, recent: list[str]) -> QComboBox:
    combo = QComboBox()
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    line_edit = combo.lineEdit()
    if line_edit is not None:
        line_edit.setPlaceholderText(placeholder)
    for value in recent:
        combo.addItem(value)
    combo.setCurrentIndex(-1)
    if line_edit is not None:
        line_edit.clear()
    return combo


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


class ProfileChannelSetupDialog(QDialog):
    def __init__(
        self,
        *,
        selected_profiles: list[dict[str, object]],
        recent_channel_names: list[str] | None = None,
        recent_channel_descriptions: list[str] | None = None,
        recent_link_titles: list[str] | None = None,
        recent_link_urls: list[str] | None = None,
        recent_video_default_titles: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройка канала")
        self.setModal(True)
        self.setMinimumSize(760, 720)
        self.resize(820, 780)

        self._profiles = list(selected_profiles)
        self._rows = build_selected_profile_avatar_rows(self._profiles)
        self._avatar_count = 0
        self._avatar_pngs: list[bytes] = []
        self._source_paths: list[str] = []
        self._channel_names: list[str] = []
        self._names_source_label = ""
        self._video_titles: list[str] = []
        self._video_titles_source_label = ""
        self._detect_thread: QThread | None = None
        self._detect_worker: AvatarDetectionWorker | None = None
        self._detect_cancel = AvatarDetectionCancel()
        self._detect_aborted = False
        self._last_preview_png = b""
        self._last_preview_subtitle = ""
        self._initial_placement_done = False

        root = QVBoxLayout(self)
        root.setSpacing(12)

        hint = QLabel(
            "Настройка отмеченных профилей в YouTube Studio («Настройка канала»). "
            "Заполните нужные разделы — можно один или несколько сразу.\n\n"
            "Описание и ссылка применяются ко всем отмеченным профилям одинаково. "
            "Аватарки, названия каналов и названия для видео сопоставляются с профилями "
            "по порядку в таблице; при нехватке элементов список зацикливается. "
            "Несколько значений — через запятую, точку с запятой "
            "или с новой строки, либо импорт из файла). "
            "Смена названия ограничена раз в 14 дней (в предпросмотре отмечается лимит)."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        root.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_content = QWidget()
        sections = QVBoxLayout(scroll_content)
        sections.setSpacing(8)

        desc_section = CollapsibleSection("Описание")
        desc_section.set_expanded(True)
        self._desc_edit = _recent_editable_combo(
            placeholder="Описание канала…",
            recent=list(recent_channel_descriptions or []),
        )
        self._desc_edit.currentTextChanged.connect(self._on_channel_fields_changed)
        desc_section.content_layout().addWidget(self._desc_edit)
        sections.addWidget(desc_section)

        link_section = CollapsibleSection("Ссылка")
        self._link_title_edit = _recent_editable_combo(
            placeholder="Название ссылки…",
            recent=list(recent_link_titles or []),
        )
        self._link_title_edit.currentTextChanged.connect(self._on_channel_fields_changed)
        link_section.content_layout().addWidget(QLabel("Название ссылки"))
        link_section.content_layout().addWidget(self._link_title_edit)
        self._link_url_edit = _recent_editable_combo(
            placeholder="https://…",
            recent=list(recent_link_urls or []),
        )
        self._link_url_edit.currentTextChanged.connect(self._on_channel_fields_changed)
        link_section.content_layout().addWidget(QLabel("URL"))
        link_section.content_layout().addWidget(self._link_url_edit)
        sections.addWidget(link_section)

        video_title_section = CollapsibleSection("Название для видео")
        self._video_title_edit = _recent_editable_combo(
            placeholder="Название или несколько через запятую, ; или с новой строки…",
            recent=list(recent_video_default_titles or []),
        )
        self._video_title_edit.currentTextChanged.connect(self._on_video_titles_text_changed)
        video_title_section.content_layout().addWidget(self._video_title_edit)

        video_titles_row = QHBoxLayout()
        self._video_titles_source_label_widget = QLabel("Список не задан")
        self._video_titles_source_label_widget.setObjectName("hint")
        self._video_titles_source_label_widget.setWordWrap(True)
        self._btn_pick_video_titles = QPushButton("Импорт из файла…")
        self._btn_pick_video_titles.clicked.connect(self._pick_video_titles_file)
        video_titles_row.addWidget(self._video_titles_source_label_widget, 1)
        video_titles_row.addWidget(self._btn_pick_video_titles)
        video_title_section.content_layout().addLayout(video_titles_row)

        self._shuffle_video_titles = QCheckBox("Перемешать названия для видео")
        self._shuffle_video_titles.setChecked(True)
        self._shuffle_video_titles.setToolTip(
            "Случайно перемешать названия для видео перед сопоставлением с профилями."
        )
        self._shuffle_video_titles.toggled.connect(self._on_assignment_options_changed)
        video_title_section.content_layout().addWidget(self._shuffle_video_titles)
        sections.addWidget(video_title_section)

        avatars_section = CollapsibleSection("Аватарки")
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
        avatars_section.content_layout().addWidget(self._preview_scroll)

        source_row = QHBoxLayout()
        self._source_label = QLabel("Файлы не выбраны")
        self._source_label.setObjectName("hint")
        self._source_label.setWordWrap(True)
        self._btn_pick = QPushButton("Выбрать файлы…")
        self._btn_pick.clicked.connect(self._pick_files)
        source_row.addWidget(self._source_label, 1)
        source_row.addWidget(self._btn_pick)
        avatars_section.content_layout().addLayout(source_row)

        self._no_crop = QCheckBox("Не обрезать аватарки (1 файл = 1 профиль)")
        self._no_crop.setToolTip(
            "Каждый выбранный файл загружается целиком и назначается одному профилю "
            "без поиска и вырезки иконок на спрайт-листе."
        )
        self._no_crop.toggled.connect(self._on_assignment_options_changed)
        avatars_section.content_layout().addWidget(self._no_crop)

        self._shuffle = QCheckBox("Перемешать аватарки")
        self._shuffle.setChecked(True)
        self._shuffle.setToolTip(
            "Случайно перемешать аватарки перед сопоставлением с профилями."
        )
        self._shuffle.toggled.connect(self._on_assignment_options_changed)
        avatars_section.content_layout().addWidget(self._shuffle)
        sections.addWidget(avatars_section)

        names_section = CollapsibleSection("Названия")
        self._names_edit = _recent_editable_combo(
            placeholder="Название или несколько через запятую, ; или с новой строки…",
            recent=list(recent_channel_names or []),
        )
        self._names_edit.currentTextChanged.connect(self._on_names_text_changed)
        names_section.content_layout().addWidget(self._names_edit)

        names_row = QHBoxLayout()
        self._names_source_label_widget = QLabel("Список не задан")
        self._names_source_label_widget.setObjectName("hint")
        self._names_source_label_widget.setWordWrap(True)
        self._btn_pick_names = QPushButton("Импорт из файла…")
        self._btn_pick_names.clicked.connect(self._pick_names_file)
        names_row.addWidget(self._names_source_label_widget, 1)
        names_row.addWidget(self._btn_pick_names)
        names_section.content_layout().addLayout(names_row)

        self._shuffle_names = QCheckBox("Перемешать названия")
        self._shuffle_names.setChecked(True)
        self._shuffle_names.setToolTip(
            "Случайно перемешать названия перед сопоставлением с профилями."
        )
        self._shuffle_names.toggled.connect(self._on_assignment_options_changed)
        names_section.content_layout().addWidget(self._shuffle_names)
        sections.addWidget(names_section)

        sections.addStretch()
        scroll.setWidget(scroll_content)
        root.addWidget(scroll, 0)

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

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["#", "Аватарка", "Название", "Профиль", "Статус"]
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setColumnWidth(0, 36)
        self._table.setColumnWidth(1, 72)
        self._table.setMinimumHeight(250)
        self._table.verticalHeader().setDefaultSectionSize(60)
        root.addWidget(self._table, 2)

        btns = QHBoxLayout()
        btns.addStretch()
        self._btn_cancel = QPushButton("Отмена")
        self._btn_cancel.setObjectName("secondary")
        self._btn_save = QPushButton("Применить в Studio")
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
            f"Отмечено профилей: {len(self._profiles)}. "
            "Заполните нужные разделы."
        )
        self._refresh_assignment()

    def _place_near_screen_top(self, *, margin: int = 12) -> None:
        screen = self.screen()
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        frame = self.frameGeometry()
        x = area.x() + max(0, (area.width() - frame.width()) // 2)
        y = area.y() + margin
        bottom_limit = area.bottom() - frame.height() + 1
        if y > bottom_limit:
            y = max(area.y() + margin, bottom_limit)
        self.move(x, y)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._initial_placement_done:
            self._place_near_screen_top()
            self._initial_placement_done = True

    def channel_description(self) -> str:
        return (self._desc_edit.currentText() or "").strip()

    def channel_link_title(self) -> str:
        return (self._link_title_edit.currentText() or "").strip()

    def channel_link_url(self) -> str:
        return (self._link_url_edit.currentText() or "").strip()

    def video_default_title(self) -> str:
        return (self._video_title_edit.currentText() or "").strip()

    def video_default_titles_for_remember(self) -> list[str]:
        return self._current_video_titles()

    def channel_names_for_remember(self) -> list[str]:
        return self._current_channel_names()

    def has_channel_text_fill(self) -> bool:
        desc = self.channel_description()
        lt = self.channel_link_title()
        lu = self.channel_link_url()
        return bool(desc) or bool(lt and lu)

    def has_video_default_title(self) -> bool:
        return bool(self._current_video_titles())

    def has_profile_customization(self) -> bool:
        return bool(self.profile_assignments())

    def _on_channel_fields_changed(self) -> None:
        self._update_save_button()

    def profile_assignments(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for row in self._rows:
            if not row.get("can_save"):
                continue
            pid = str(row.get("profile_id") or "").strip()
            if not pid:
                continue
            png = row.get("avatar_png")
            avatar_bytes = (
                bytes(png) if isinstance(png, (bytes, bytearray)) and png else None
            )
            channel_name = str(row.get("channel_name") or "").strip()
            video_default_title = str(row.get("video_default_title") or "").strip()
            skip_name = bool(row.get("skip_name_change"))
            if (
                not avatar_bytes
                and not (channel_name and not skip_name)
                and not video_default_title
            ):
                continue
            out.append(
                {
                    "profile_id": pid,
                    "avatar_png": avatar_bytes,
                    "channel_name": channel_name or None,
                    "skip_name_change": skip_name,
                    "video_default_title": video_default_title or None,
                }
            )
        return out

    def upload_assignments(self) -> list[tuple[str, bytes]]:
        """Обратная совместимость: только пары (profile_id, avatar_png)."""
        out: list[tuple[str, bytes]] = []
        for item in self.profile_assignments():
            pid = str(item.get("profile_id") or "")
            png = item.get("avatar_png")
            if pid and isinstance(png, (bytes, bytearray)) and png:
                out.append((pid, bytes(png)))
        return out

    def _dialog_title(self) -> str:
        return "Настройка канала"

    def _current_channel_names(self) -> list[str]:
        if self._names_source_label:
            from_items = [
                self._names_edit.itemText(i).strip()
                for i in range(self._names_edit.count())
            ]
            return [name for name in from_items if name]
        return parse_channel_names_text(self._names_edit.currentText())

    def _current_video_titles(self) -> list[str]:
        if self._video_titles_source_label:
            from_items = [
                self._video_title_edit.itemText(i).strip()
                for i in range(self._video_title_edit.count())
            ]
            return [title for title in from_items if title]
        return parse_channel_names_text(self._video_title_edit.currentText())

    def _pick_names_file(self) -> None:
        if self._is_detecting():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Импорт названий каналов",
            "",
            "Текстовые файлы (*.txt *.csv);;Все файлы (*.*)",
        )
        if not path:
            return
        try:
            names = parse_channel_names_file(path)
        except OSError as exc:
            QMessageBox.warning(
                self,
                self._dialog_title(),
                f"Не удалось прочитать файл:\n{exc}",
            )
            return
        if not names:
            QMessageBox.warning(
                self,
                self._dialog_title(),
                "В файле не найдено названий каналов.",
            )
            return
        self._names_edit.blockSignals(True)
        try:
            self._names_edit.clear()
            for name in names:
                self._names_edit.addItem(name)
            if names:
                self._names_edit.setCurrentIndex(0)
        finally:
            self._names_edit.blockSignals(False)
        self._names_source_label = path
        self._names_source_label_widget.setText(path)
        self._on_names_text_changed()

    def _pick_video_titles_file(self) -> None:
        if self._is_detecting():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Импорт названий для видео",
            "",
            "Текстовые файлы (*.txt *.csv);;Все файлы (*.*)",
        )
        if not path:
            return
        try:
            titles = parse_channel_names_file(path)
        except OSError as exc:
            QMessageBox.warning(
                self,
                self._dialog_title(),
                f"Не удалось прочитать файл:\n{exc}",
            )
            return
        if not titles:
            QMessageBox.warning(
                self,
                self._dialog_title(),
                "В файле не найдено названий для видео.",
            )
            return
        self._video_title_edit.blockSignals(True)
        try:
            self._video_title_edit.clear()
            for title in titles:
                self._video_title_edit.addItem(title)
            if titles:
                self._video_title_edit.setCurrentIndex(0)
        finally:
            self._video_title_edit.blockSignals(False)
        self._video_titles_source_label = path
        self._video_titles_source_label_widget.setText(path)
        self._on_video_titles_text_changed()

    def _on_video_titles_text_changed(self) -> None:
        if self._is_detecting():
            return
        self._video_titles = self._current_video_titles()
        if not self._video_titles_source_label:
            if self._video_titles:
                self._video_titles_source_label_widget.setText(
                    f"В списке: {len(self._video_titles)} названий"
                )
            else:
                self._video_titles_source_label_widget.setText("Список не задан")
        self._refresh_assignment()

    def _on_names_text_changed(self) -> None:
        if self._is_detecting():
            return
        self._channel_names = self._current_channel_names()
        if not self._names_source_label:
            if self._channel_names:
                self._names_source_label_widget.setText(
                    f"В списке: {len(self._channel_names)} названий"
                )
            else:
                self._names_source_label_widget.setText("Список не задан")
        self._refresh_assignment()

    def _refresh_assignment(self) -> None:
        self._channel_names = self._current_channel_names()
        self._video_titles = self._current_video_titles()
        self._rows = assign_avatars_to_selected_profiles(
            self._profiles,
            self._avatar_pngs,
            shuffle=self._shuffle.isChecked(),
            channel_names=self._channel_names,
            shuffle_names=self._shuffle_names.isChecked(),
            video_default_titles=self._video_titles,
            shuffle_video_titles=self._shuffle_video_titles.isChecked(),
        )
        self._populate_table()
        self._update_save_button()
        self._update_status_summary()

    def _update_save_button(self) -> None:
        has_text = self.has_channel_text_fill()
        has_video_title = self.has_video_default_title()
        has_assignments = any(row.get("can_save") for row in self._rows)
        self._btn_save.setEnabled(has_text or has_video_title or has_assignments)

    def _update_status_summary(self) -> None:
        matched = sum(1 for row in self._rows if row.get("can_save"))
        parts = [f"Отмечено профилей: {len(self._profiles)}."]
        if self._avatar_pngs:
            parts.append(f"Аватарок: {len(self._avatar_pngs)}.")
        if self._channel_names:
            parts.append(f"Названий: {len(self._channel_names)}.")
        if self._video_titles:
            parts.append(f"Названий для видео: {len(self._video_titles)}.")
        parts.append(f"Готово к применению: {matched}.")
        self._status.setText(" ".join(parts))

    def _on_assignment_options_changed(self, _checked: bool = False) -> None:
        if self._is_detecting():
            return
        self._refresh_assignment()

    def avatar_count(self) -> int:
        return self._avatar_count

    def _is_detecting(self) -> bool:
        return self._detect_thread is not None and self._detect_thread.isRunning()

    def _set_busy(self, busy: bool) -> None:
        self._btn_pick.setEnabled(not busy)
        self._btn_pick_names.setEnabled(not busy)
        self._btn_pick_video_titles.setEnabled(not busy)
        self._desc_edit.setEnabled(not busy)
        self._link_title_edit.setEnabled(not busy)
        self._link_url_edit.setEnabled(not busy)
        self._video_title_edit.setEnabled(not busy)
        self._names_edit.setEnabled(not busy)
        self._no_crop.setEnabled(not busy)
        self._shuffle.setEnabled(not busy)
        self._shuffle_names.setEnabled(not busy)
        self._shuffle_video_titles.setEnabled(not busy)
        self._progress_bar.setVisible(busy)
        if busy:
            self._btn_save.setEnabled(False)
        else:
            self._progress_bar.setValue(0)
            self._progress_bar.setFormat("Ожидание…")
            self._update_save_button()

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
        self._update_save_button()

    def _on_detection_progress(self, done: int, total: int, filename: str) -> None:
        if self._detect_aborted:
            return
        max_files = max(1, total)
        self._progress_bar.setRange(0, max_files)
        self._progress_bar.setValue(min(done, max_files))
        no_crop = self._no_crop.isChecked()
        verb = "Загрузка" if no_crop else "Обработка"
        if done < total:
            label = "Загрузка" if no_crop else "Файл"
            if done <= 0:
                self._progress_bar.setFormat(f"{label} %v из %m")
                if filename:
                    self._status.setText(f"{verb}: {filename}…")
                else:
                    self._status.setText(f"{verb} {total} файлов…")
            else:
                self._progress_bar.setFormat(
                    f"{label} %v из %m"
                    + (f" — {filename}" if filename else "")
                )
                self._status.setText(
                    f"{verb}: {done} из {total}"
                    + (f" — {filename}" if filename else "")
                    + "…"
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
        QMessageBox.warning(self, self._dialog_title(), message)
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
        if isinstance(source_paths, list):
            self._source_paths = [str(p) for p in source_paths if str(p).strip()]
            self._source_label.setText(_format_source_files(self._source_paths))

        avatar_pngs = [bytes(p) for p in pngs if p]
        if not avatar_pngs:
            self._avatar_count = 0
            self._avatar_pngs = []
            self._set_busy(False)
            self._refresh_assignment()
            self._clear_preview(
                placeholder=(
                    "Файлы не загружены"
                    if self._no_crop.isChecked()
                    else "Аватарки не найдены"
                )
            )
            if not any(row.get("can_save") for row in self._rows):
                self._status.setText(
                    "Не удалось загрузить выбранные файлы. Попробуйте другие."
                    if self._no_crop.isChecked()
                    else "В выбранных файлах не найдено аватарок. Попробуйте другие файлы."
                )
            return

        self._avatar_count = len(avatar_pngs)
        self._avatar_pngs = avatar_pngs
        self._set_busy(False)
        self._refresh_assignment()
        self._apply_preview_pixmap(bytes(preview_png) if preview_png else b"")

        warn_lines = [str(e) for e in errors if str(e).strip()]
        if warn_lines:
            preview = "\n".join(warn_lines[:8])
            if len(warn_lines) > 8:
                preview += f"\n… и ещё {len(warn_lines) - 8}."
            QMessageBox.warning(
                self,
                self._dialog_title(),
                "Часть файлов обработана с предупреждениями:\n\n" + preview,
            )

    def _apply_avatar_assignment(self) -> None:
        self._refresh_assignment()

    def _on_shuffle_toggled(self, _checked: bool) -> None:
        self._on_assignment_options_changed(_checked)

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
            channel_name = str(row.get("channel_name") or "")
            video_default_title = str(row.get("video_default_title") or "")
            name_parts: list[str] = []
            if channel_name:
                if row.get("skip_name_change"):
                    name_parts.append(f"{channel_name}\n(не будет изменено)")
                else:
                    name_parts.append(channel_name)
            if video_default_title:
                name_parts.append(f"Видео: {video_default_title}")
            name_cell = "\n".join(name_parts) if name_parts else "—"
            status = str(row.get("status") or "")

            thumb_label = QLabel()
            thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pix = _pixmap_from_png(png_bytes, size=56)
            if not pix.isNull():
                thumb_label.setPixmap(pix)
            else:
                thumb_label.setText("—")
            self._table.setCellWidget(i, 1, thumb_label)

            for col, value in enumerate(
                [index_text, "", name_cell, profile_cell, status]
            ):
                if col == 1:
                    continue
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
                    )
                if col == 2 and row.get("skip_name_change") and channel_name:
                    item.setForeground(QColor(180, 130, 0))
                if not row.get("can_save") and col == 4:
                    item.setForeground(Qt.GlobalColor.darkRed)
                self._table.setItem(i, col, item)

        self._table.resizeColumnsToContents()
        self._table.setColumnWidth(1, 72)
        self._table.setColumnWidth(2, 160)

    def _on_save(self) -> None:
        if self._is_detecting():
            return
        desc = self.channel_description()
        link_title = self.channel_link_title()
        link_url = self.channel_link_url()
        video_titles = self._current_video_titles()
        assignments = self.profile_assignments()

        if (
            not desc
            and not (link_title and link_url)
            and not video_titles
            and not assignments
        ):
            QMessageBox.warning(
                self,
                self._dialog_title(),
                "Заполните хотя бы один раздел: описание, ссылку "
                "(название + URL), название для видео или аватарки/названия.",
            )
            return
        if (link_title and not link_url) or (link_url and not link_title):
            QMessageBox.warning(
                self,
                self._dialog_title(),
                "Для ссылки нужны и название, и URL.",
            )
            return

        msg_parts: list[str] = []
        if desc or (link_title and link_url):
            msg_parts.append(
                f"• описание/ссылка — для всех {len(self._profiles)} профилей"
            )
        if video_titles:
            with_video_title = sum(
                1 for a in assignments if a.get("video_default_title")
            )
            msg_parts.append(
                f"• названия для видео — {with_video_title} из "
                f"{len(self._profiles)} профилей"
            )
        if assignments:
            with_avatar = sum(1 for a in assignments if a.get("avatar_png"))
            with_name = sum(
                1
                for a in assignments
                if a.get("channel_name") and not a.get("skip_name_change")
            )
            skipped_names = sum(
                1
                for a in assignments
                if a.get("channel_name") and a.get("skip_name_change")
            )
            msg_parts.append(f"• персональные изменения — {len(assignments)} профилей")
            details: list[str] = []
            if with_avatar:
                details.append(f"  — с аватаркой: {with_avatar}")
            if with_name:
                details.append(f"  — с названием: {with_name}")
            if skipped_names:
                details.append(
                    f"  — название пропущено (лимит 14 дн.): {skipped_names}"
                )
            msg_parts.extend(details)

        msg = "Применить настройки канала в YouTube Studio?\n\n" + "\n".join(msg_parts)
        answer = QMessageBox.question(
            self,
            self._dialog_title(),
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


ProfileAvatarsImportDialog = ProfileChannelSetupDialog
