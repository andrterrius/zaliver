"""Вкладка «Редактирование каналов» — настройка канала в YouTube Studio."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from zaliver.ui.avatar_crop_preview_dialog import AvatarCropPreviewDialog
from zaliver.ui.avatar_detection_worker import (
    AvatarDetectionCancel,
    AvatarDetectionWorker,
)
from zaliver.ui.avatar_import_parser import (
    assign_avatars_to_selected_profiles,
    build_selected_profile_avatar_rows,
    parse_channel_names_file,
    parse_channel_names_text,
    parse_cycling_field_lines,
)
from zaliver.ui.channel_setup_helpers import (
    field_with_recent_picker,
    fill_recent_values_picker,
    format_source_files,
    recent_picker_has_items,
)
from zaliver.ui.widgets import AnimatedProgressBar, ToggleSwitch

_TEXT_FIELD_H = 250


def _section_layout(box: QGroupBox) -> QVBoxLayout:
    lay = QVBoxLayout(box)
    lay.setSpacing(2)
    lay.setContentsMargins(0, 0, 0, 0)
    return lay


class ChannelEditTabPane(QWidget):
    """Форма редактирования канала с переключателями разделов."""

    select_profiles_requested = pyqtSignal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        recent_channel_names: list[str] | None = None,
        recent_channel_descriptions: list[str] | None = None,
        recent_link_titles: list[str] | None = None,
        recent_link_urls: list[str] | None = None,
        recent_video_default_titles: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._profiles: list[dict[str, object]] = []
        self._rows = build_selected_profile_avatar_rows([])
        self._avatar_count = 0
        self._avatar_pngs: list[bytes] = []
        self._file_crop_previews: list[tuple[str, bytes]] = []
        self._source_paths: list[str] = []
        self._channel_names: list[str] = []
        self._names_source_label = ""
        self._video_titles: list[str] = []
        self._video_titles_source_label = ""
        self._detect_thread: QThread | None = None
        self._detect_worker: AvatarDetectionWorker | None = None
        self._detect_cancel = AvatarDetectionCancel()
        self._detect_aborted = False
        self._running = False

        self._recent_channel_names = list(recent_channel_names or [])
        self._recent_descriptions = list(recent_channel_descriptions or [])
        self._recent_link_titles = list(recent_link_titles or [])
        self._recent_link_urls = list(recent_link_urls or [])
        self._recent_video_titles = list(recent_video_default_titles or [])

        self._build_ui()
        self._connect_section_toggles()
        self._on_section_toggle()
        self._refresh_assignment()

    def _compact_text_edit(
        self,
        placeholder: str,
        *,
        min_height: int | None = None,
        fixed_height: int | None = None,
    ) -> QPlainTextEdit:
        edit = QPlainTextEdit()
        edit.setPlaceholderText(placeholder)
        height = fixed_height if fixed_height is not None else min_height
        if height is None:
            height = _TEXT_FIELD_H
        edit.setMinimumHeight(height)
        edit.setMaximumHeight(height)
        edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        edit.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        return edit

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        header = QHBoxLayout()
        title = QLabel("Редактирование канала")
        title.setObjectName("title")
        title.setStyleSheet("font-size: 18px; font-weight: 700; color: #f1f5f9;")
        header.addWidget(title)
        header.addStretch()
        self._btn_select_profiles = QPushButton("Выбрать профили")
        self._btn_select_profiles.setEnabled(False)
        self._btn_select_profiles.clicked.connect(self._on_select_profiles)
        header.addWidget(self._btn_select_profiles)
        root.addLayout(header)

        form = QVBoxLayout()
        form.setSpacing(6)

        form.addWidget(self._build_media_section())
        form.addLayout(self._build_desc_names_row())
        form.addWidget(self._build_link_section())
        self._video_title_box = self._build_video_title_section()
        form.addWidget(self._video_title_box)
        root.addLayout(form, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self._progress_bar = AnimatedProgressBar()
        self._progress_bar.setRange(0, 1)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("Ожидание…")
        self._progress_bar.setVisible(False)
        self._status = QLabel("")
        self._status.setObjectName("hint")
        self._status.setWordWrap(False)
        footer.addWidget(self._progress_bar, 1)
        footer.addWidget(self._status, 1)
        root.addLayout(footer)

    def _section_header(self, toggle: ToggleSwitch, label: str, hint: str = "") -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(toggle)
        lbl = QLabel(label)
        lbl.setStyleSheet("font-size: 13px; font-weight: 600; color: #e4e6ef;")
        row.addWidget(lbl)
        if hint:
            hint_lbl = QLabel(hint)
            hint_lbl.setObjectName("hint")
            row.addWidget(hint_lbl)
        row.addStretch()
        return row

    def _build_media_section(self) -> QGroupBox:
        box = QGroupBox("Медиафайлы")
        box.setObjectName("channelEditPanel")
        lay = _section_layout(box)

        self._toggle_avatar = ToggleSwitch()
        self._toggle_avatar.setChecked(True)
        lay.addLayout(self._section_header(self._toggle_avatar, "Аватарка"))

        self._avatar_path = QLineEdit()
        self._avatar_path.setReadOnly(True)
        self._avatar_path.setPlaceholderText("Файлы не выбраны")
        btn_avatar = QPushButton("Выбрать")
        btn_avatar.setObjectName("secondary")
        btn_avatar.setToolTip("Выбрать файлы с аватарками")
        btn_avatar.clicked.connect(self._pick_avatar_files)
        avatar_row = QHBoxLayout()
        avatar_row.addWidget(self._avatar_path, 1)
        avatar_row.addWidget(btn_avatar)
        self._btn_avatar_preview = QPushButton("Предпросмотр")
        self._btn_avatar_preview.setObjectName("secondary")
        self._btn_avatar_preview.setEnabled(False)
        self._btn_avatar_preview.setToolTip("Показать, как обрезаны аватарки")
        self._btn_avatar_preview.clicked.connect(self._show_avatar_preview)
        avatar_row.addWidget(self._btn_avatar_preview)
        lay.addLayout(avatar_row)

        opts = QHBoxLayout()
        self._no_crop = QCheckBox("Не обрезать")
        self._no_crop.setToolTip("1 файл = 1 профиль, без вырезки иконок")
        self._no_crop.toggled.connect(self._on_assignment_options_changed)
        self._no_crop.toggled.connect(lambda: self._file_crop_previews.clear())
        self._shuffle = QCheckBox("Перемешать")
        self._shuffle.setChecked(True)
        self._shuffle.setToolTip("Перемешать аватарки перед сопоставлением")
        self._shuffle.toggled.connect(self._on_assignment_options_changed)
        opts.addWidget(self._no_crop)
        opts.addWidget(self._shuffle)
        opts.addStretch()
        lay.addLayout(opts)

        return box

    def _build_desc_names_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        desc_box = QGroupBox()
        desc_box.setObjectName("channelEditSection")
        desc_box.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        desc_l = _section_layout(desc_box)
        self._toggle_desc = ToggleSwitch()
        self._toggle_desc.setChecked(True)
        desc_l.addLayout(
            self._section_header(self._toggle_desc, "Описание", "одно на строку")
        )
        self._desc_edit = self._compact_text_edit(
            "Описание канала…",
            fixed_height=_TEXT_FIELD_H,
        )
        if self._recent_descriptions:
            self._desc_edit.setPlainText(self._recent_descriptions[0])
        self._desc_edit.textChanged.connect(self._on_channel_fields_changed)
        desc_field_row, self._desc_recent_combo = field_with_recent_picker(
            self._desc_edit,
            recent=self._recent_descriptions,
            tooltip="Недавние описания канала",
            on_filled=self._on_channel_fields_changed,
        )
        desc_l.addWidget(desc_field_row)

        names_box = QGroupBox()
        names_box.setObjectName("channelEditSection")
        names_box.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        names_l = _section_layout(names_box)
        self._toggle_names = ToggleSwitch()
        self._toggle_names.setChecked(True)
        names_l.addLayout(
            self._section_header(self._toggle_names, "Название", "одно на строку")
        )
        self._names_edit = self._compact_text_edit(
            "Название канала — по одному на строку…",
            fixed_height=_TEXT_FIELD_H,
        )
        self._names_edit.textChanged.connect(self._on_names_text_changed)
        names_field_row, self._names_recent_combo = field_with_recent_picker(
            self._names_edit,
            recent=self._recent_channel_names,
            tooltip="Недавние названия каналов",
            on_filled=self._on_names_text_changed,
        )
        names_l.addWidget(names_field_row)

        names_tools = QHBoxLayout()
        names_tools.setSpacing(6)
        self._btn_pick_names = QPushButton("Импорт…")
        self._btn_pick_names.setObjectName("secondary")
        self._btn_pick_names.clicked.connect(self._pick_names_file)
        names_tools.addWidget(self._btn_pick_names)
        self._shuffle_names = QCheckBox("Перемешать")
        self._shuffle_names.setChecked(True)
        self._shuffle_names.toggled.connect(self._on_assignment_options_changed)
        names_tools.addWidget(self._shuffle_names)
        names_tools.addStretch()
        names_l.addLayout(names_tools)

        self._names_source_label_widget = QLabel("")
        self._names_source_label_widget.setObjectName("hint")
        self._names_source_label_widget.setVisible(False)
        names_l.addWidget(self._names_source_label_widget)

        row.addWidget(desc_box, 1)
        row.addWidget(names_box, 1)
        return row

    def _build_link_section(self) -> QGroupBox:
        box = QGroupBox()
        box.setObjectName("channelEditSection")
        lay = _section_layout(box)
        self._toggle_link = ToggleSwitch()
        self._toggle_link.setChecked(True)
        lay.addLayout(self._section_header(self._toggle_link, "Ссылка"))

        link_row = QHBoxLayout()
        link_icon = QLabel("🔗")
        link_icon.setFixedWidth(24)
        self._link_url_edit = QLineEdit()
        self._link_url_edit.setPlaceholderText("https://…")
        if self._recent_link_urls:
            self._link_url_edit.setText(self._recent_link_urls[0])
        self._link_url_edit.textChanged.connect(self._on_channel_fields_changed)
        link_url_row, self._link_url_recent_combo = field_with_recent_picker(
            self._link_url_edit,
            recent=self._recent_link_urls,
            tooltip="Недавние URL ссылок",
            on_filled=self._on_channel_fields_changed,
        )
        self._link_title_edit = QLineEdit()
        self._link_title_edit.setPlaceholderText("ИГРА")
        if self._recent_link_titles:
            self._link_title_edit.setText(self._recent_link_titles[0])
        self._link_title_edit.textChanged.connect(self._on_channel_fields_changed)
        link_title_row, self._link_title_recent_combo = field_with_recent_picker(
            self._link_title_edit,
            recent=self._recent_link_titles,
            tooltip="Недавние названия ссылок",
            on_filled=self._on_channel_fields_changed,
        )
        link_row.addWidget(link_icon)
        link_row.addWidget(link_url_row, 1)
        link_row.addWidget(link_title_row, 1)
        lay.addLayout(link_row)
        return box

    def _build_video_title_section(self) -> QGroupBox:
        box = QGroupBox()
        box.setObjectName("channelEditSection")
        lay = _section_layout(box)
        self._toggle_video_title = ToggleSwitch()
        self._toggle_video_title.setChecked(True)
        lay.addLayout(
            self._section_header(
                self._toggle_video_title, "Название для видео", "одно на строку"
            )
        )
        self._video_title_body = QWidget()
        body_l = QVBoxLayout(self._video_title_body)
        body_l.setSpacing(2)
        body_l.setContentsMargins(0, 0, 0, 0)
        self._video_title_edit = self._compact_text_edit(
            "Название по умолчанию при загрузке…",
            fixed_height=_TEXT_FIELD_H,
        )
        self._video_title_edit.textChanged.connect(self._on_video_titles_text_changed)
        video_field_row, self._video_title_recent_combo = field_with_recent_picker(
            self._video_title_edit,
            recent=self._recent_video_titles,
            tooltip="Недавние названия для видео",
            on_filled=self._on_video_titles_text_changed,
        )
        body_l.addWidget(video_field_row, 1)

        vt_row = QHBoxLayout()
        vt_row.setSpacing(6)
        self._video_titles_source_label_widget = QLabel("")
        self._video_titles_source_label_widget.setObjectName("hint")
        self._video_titles_source_label_widget.setVisible(False)
        self._btn_pick_video_titles = QPushButton("Импорт…")
        self._btn_pick_video_titles.setObjectName("secondary")
        self._btn_pick_video_titles.clicked.connect(self._pick_video_titles_file)
        self._shuffle_video_titles = QCheckBox("Перемешать")
        self._shuffle_video_titles.setChecked(True)
        self._shuffle_video_titles.toggled.connect(self._on_assignment_options_changed)
        vt_row.addWidget(self._video_titles_source_label_widget, 1)
        vt_row.addWidget(self._btn_pick_video_titles)
        vt_row.addWidget(self._shuffle_video_titles)
        vt_row.addStretch()
        body_l.addLayout(vt_row)
        lay.addWidget(self._video_title_body, 1)
        self._video_title_body.setVisible(True)
        return box

    def _connect_section_toggles(self) -> None:
        self._toggle_avatar.toggled.connect(self._on_section_toggle)
        self._toggle_desc.toggled.connect(self._on_section_toggle)
        self._toggle_names.toggled.connect(self._on_section_toggle)
        self._toggle_link.toggled.connect(self._on_section_toggle)
        self._toggle_video_title.toggled.connect(self._on_section_toggle)

    def _on_section_toggle(self, _checked: bool = False) -> None:
        enabled = self._toggle_avatar.isChecked()
        self._avatar_path.setEnabled(enabled)
        self._no_crop.setEnabled(enabled and not self._is_detecting())
        self._shuffle.setEnabled(enabled and not self._is_detecting())

        self._desc_edit.setEnabled(self._toggle_desc.isChecked())
        self._desc_recent_combo.setEnabled(
            self._toggle_desc.isChecked() and recent_picker_has_items(self._desc_recent_combo)
        )

        names_on = self._toggle_names.isChecked()
        self._names_edit.setEnabled(names_on)
        self._names_recent_combo.setEnabled(
            names_on and recent_picker_has_items(self._names_recent_combo)
        )
        self._btn_pick_names.setEnabled(names_on and not self._is_detecting())
        self._shuffle_names.setEnabled(names_on)

        link_on = self._toggle_link.isChecked()
        self._link_url_edit.setEnabled(link_on)
        self._link_title_edit.setEnabled(link_on)
        self._link_url_recent_combo.setEnabled(
            link_on and recent_picker_has_items(self._link_url_recent_combo)
        )
        self._link_title_recent_combo.setEnabled(
            link_on and recent_picker_has_items(self._link_title_recent_combo)
        )

        vt_on = self._toggle_video_title.isChecked()
        self._video_title_edit.setEnabled(vt_on)
        self._video_title_recent_combo.setEnabled(
            vt_on and recent_picker_has_items(self._video_title_recent_combo)
        )
        self._btn_pick_video_titles.setEnabled(vt_on and not self._is_detecting())
        self._shuffle_video_titles.setEnabled(vt_on)

        self._refresh_assignment()
        self._update_select_button()

    def set_selected_profiles(self, profiles: list[dict[str, object]]) -> None:
        self._profiles = list(profiles)
        self._rows = build_selected_profile_avatar_rows(self._profiles)
        self._refresh_assignment()

    def set_running(self, running: bool) -> None:
        self._running = running
        self._btn_select_profiles.setEnabled(not running and self._can_select_profiles())
        self._btn_pick_names.setEnabled(
            not running and self._toggle_names.isChecked() and not self._is_detecting()
        )
        self._btn_pick_video_titles.setEnabled(
            not running
            and self._toggle_video_title.isChecked()
            and not self._is_detecting()
        )
        self._update_avatar_preview_button()

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def load_recent_values(
        self,
        *,
        channel_names: list[str] | None = None,
        descriptions: list[str] | None = None,
        link_titles: list[str] | None = None,
        link_urls: list[str] | None = None,
        video_titles: list[str] | None = None,
    ) -> None:
        if descriptions:
            self._recent_descriptions = list(descriptions)
            fill_recent_values_picker(self._desc_recent_combo, self._recent_descriptions)
            if not self._desc_edit.toPlainText().strip():
                self._desc_edit.setPlainText(descriptions[0])
        if link_titles:
            self._recent_link_titles = list(link_titles)
            fill_recent_values_picker(self._link_title_recent_combo, self._recent_link_titles)
            if not self._link_title_edit.text().strip():
                self._link_title_edit.setText(link_titles[0])
        if link_urls:
            self._recent_link_urls = list(link_urls)
            fill_recent_values_picker(self._link_url_recent_combo, self._recent_link_urls)
            if not self._link_url_edit.text().strip():
                self._link_url_edit.setText(link_urls[0])
        if channel_names:
            self._recent_channel_names = list(channel_names)
            fill_recent_values_picker(self._names_recent_combo, self._recent_channel_names)
        if video_titles:
            self._recent_video_titles = list(video_titles)
            fill_recent_values_picker(
                self._video_title_recent_combo, self._recent_video_titles
            )
        self._on_section_toggle()

    def validate_form(self) -> str | None:
        """Проверка формы; None — всё ок, иначе текст ошибки."""
        desc = self.channel_description()
        link_title = self.channel_link_title()
        link_url = self.channel_link_url()
        video_titles = self._current_video_titles()
        has_avatar = self._toggle_avatar.isChecked() and bool(self._avatar_pngs)
        has_names = self._toggle_names.isChecked() and bool(self._current_channel_names())
        has_descriptions = self._toggle_desc.isChecked() and bool(
            self._current_channel_descriptions()
        )

        if (
            not has_descriptions
            and not (link_title and link_url)
            and not video_titles
            and not has_avatar
            and not has_names
        ):
            return "Включите и заполните хотя бы один раздел."
        if (link_title and not link_url) or (link_url and not link_title):
            return "Для ссылки нужны и название, и URL."
        return None

    def confirm_message_for_profiles(self, profile_count: int) -> str:
        assignments = self.profile_assignments()
        desc = self.channel_description()
        link_title = self.channel_link_title()
        link_url = self.channel_link_url()
        video_titles = self._current_video_titles()
        msg_parts: list[str] = []
        desc_count = len(self._current_channel_descriptions())
        if desc_count > 1:
            msg_parts.append(f"• описание — {desc_count} вариантов по профилям")
        elif desc_count == 1:
            msg_parts.append(f"• описание — для всех {profile_count} профилей")
        if link_title and link_url:
            msg_parts.append(f"• ссылка — для всех {profile_count} профилей")
        if video_titles:
            with_video = sum(1 for a in assignments if a.get("video_default_title"))
            msg_parts.append(
                f"• названия для видео — {with_video} из {profile_count}"
            )
        if assignments:
            msg_parts.append(f"• персональные изменения — {len(assignments)} профилей")
        return "Применить настройки канала в YouTube Studio?\n\n" + "\n".join(msg_parts)

    def channel_description(self) -> str:
        if not self._toggle_desc.isChecked():
            return ""
        return self._desc_edit.toPlainText().strip()

    def channel_description_lines(self) -> list[str]:
        if not self._toggle_desc.isChecked():
            return []
        return self._current_channel_descriptions()

    def channel_description_field_text(self) -> str:
        if not self._toggle_desc.isChecked():
            return ""
        return self._desc_edit.toPlainText()

    def channel_link_title(self) -> str:
        if not self._toggle_link.isChecked():
            return ""
        return self._link_title_edit.text().strip()

    def channel_link_url(self) -> str:
        if not self._toggle_link.isChecked():
            return ""
        return self._link_url_edit.text().strip()

    def channel_names_field_text(self) -> str:
        if not self._toggle_names.isChecked():
            return ""
        return self._names_edit.toPlainText()

    def video_default_titles_field_text(self) -> str:
        if not self._toggle_video_title.isChecked():
            return ""
        return self._video_title_edit.toPlainText()

    def video_default_titles_for_remember(self) -> list[str]:
        return self._current_video_titles()

    def channel_names_for_remember(self) -> list[str]:
        return self._current_channel_names()

    def has_channel_text_fill(self) -> bool:
        desc_lines = self.channel_description_lines()
        lt = self.channel_link_title()
        lu = self.channel_link_url()
        return bool(desc_lines) or bool(lt and lu)

    def has_video_default_title(self) -> bool:
        if not self._toggle_video_title.isChecked():
            return False
        return bool(self._current_video_titles())

    def has_profile_customization(self) -> bool:
        if not self._toggle_avatar.isChecked() and not self._toggle_names.isChecked():
            return False
        return bool(self.profile_assignments())

    def profile_assignments(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        avatar_on = self._toggle_avatar.isChecked()
        names_on = self._toggle_names.isChecked()
        desc_on = self._toggle_desc.isChecked()
        video_on = self._toggle_video_title.isChecked()
        for row in self._rows:
            if not row.get("can_save"):
                continue
            pid = str(row.get("profile_id") or "").strip()
            if not pid:
                continue
            png = row.get("avatar_png")
            avatar_bytes = (
                bytes(png)
                if avatar_on and isinstance(png, (bytes, bytearray)) and png
                else None
            )
            channel_name = str(row.get("channel_name") or "").strip()
            if not names_on:
                channel_name = ""
            channel_description = str(row.get("channel_description") or "").strip()
            if not desc_on:
                channel_description = ""
            video_default_title = str(row.get("video_default_title") or "").strip()
            if not video_on:
                video_default_title = ""
            skip_name = bool(row.get("skip_name_change"))
            if (
                not avatar_bytes
                and not channel_description
                and not (channel_name and not skip_name)
                and not video_default_title
            ):
                continue
            out.append(
                {
                    "profile_id": pid,
                    "avatar_png": avatar_bytes,
                    "channel_name": channel_name or None,
                    "channel_description": channel_description or None,
                    "skip_name_change": skip_name,
                    "video_default_title": video_default_title or None,
                }
            )
        return out

    def _current_channel_descriptions(self) -> list[str]:
        if not self._toggle_desc.isChecked():
            return []
        return parse_cycling_field_lines(self._desc_edit.toPlainText())

    def _current_channel_names(self) -> list[str]:
        if not self._toggle_names.isChecked():
            return []
        return parse_cycling_field_lines(self._names_edit.toPlainText())

    def _current_video_titles(self) -> list[str]:
        if not self._toggle_video_title.isChecked():
            return []
        return parse_cycling_field_lines(self._video_title_edit.toPlainText())

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
            QMessageBox.warning(self, "Редактирование канала", f"Не удалось прочитать файл:\n{exc}")
            return
        if not names:
            QMessageBox.warning(self, "Редактирование канала", "В файле не найдено названий.")
            return
        self._names_edit.setPlainText("\n".join(names))
        self._names_source_label = path
        self._names_source_label_widget.setText(Path(path).name)
        self._names_source_label_widget.setVisible(True)
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
            QMessageBox.warning(self, "Редактирование канала", f"Не удалось прочитать файл:\n{exc}")
            return
        if not titles:
            QMessageBox.warning(self, "Редактирование канала", "В файле не найдено названий.")
            return
        self._video_title_edit.setPlainText("\n".join(titles))
        self._video_titles_source_label = path
        self._video_titles_source_label_widget.setText(Path(path).name)
        self._video_titles_source_label_widget.setVisible(True)
        self._on_video_titles_text_changed()

    def _update_avatar_preview_button(self) -> None:
        has_avatars = bool(self._avatar_pngs) and self._toggle_avatar.isChecked()
        self._btn_avatar_preview.setEnabled(
            has_avatars and not self._is_detecting() and not self._running
        )

    def _show_avatar_preview(self) -> None:
        if not self._avatar_pngs:
            return
        crop_mode = not self._no_crop.isChecked()
        dlg = AvatarCropPreviewDialog(
            file_previews=list(self._file_crop_previews),
            crop_mode=crop_mode,
            parent=self,
        )
        dlg.exec()

    def _on_detection_file_preview(self, preview_png: bytes, path_str: str) -> None:
        if self._detect_aborted or not preview_png:
            return
        name = Path(path_str).name
        self._file_crop_previews.append((name, bytes(preview_png)))

    def _pick_avatar_files(self) -> None:
        if self._is_detecting() or not self._toggle_avatar.isChecked():
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
        self._avatar_path.setText(format_source_files([str(p) for p in file_paths]))
        self._start_detection(file_paths)

    def _start_detection(self, paths: list[Path]) -> None:
        self._stop_detection_thread()
        self._detect_aborted = False
        self._detect_cancel = AvatarDetectionCancel()
        self._file_crop_previews = []
        self._source_paths = [str(p) for p in paths]
        self._avatar_path.setText(format_source_files(self._source_paths))
        self._status.setText("Обработка изображений…")
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
        self._update_select_button()
        self._update_avatar_preview_button()

    def _on_detection_progress(self, done: int, total: int, filename: str) -> None:
        if self._detect_aborted:
            return
        max_files = max(1, total)
        self._progress_bar.setRange(0, max_files)
        self._progress_bar.setValue(min(done, max_files))
        if filename:
            self._status.setText(f"Обработка: {filename}…")

    def _on_detection_aborted(self) -> None:
        self._detect_aborted = True
        self._set_busy(False)

    def _on_detection_failed(self, message: str) -> None:
        if self._detect_aborted:
            return
        self._set_busy(False)
        QMessageBox.warning(self, "Редактирование канала", message)

    def _on_detection_finished(
        self,
        pngs: list,
        _preview_png: bytes,
        _last_path: str,
        source_paths: list,
        errors: list,
    ) -> None:
        if self._detect_aborted:
            return
        if isinstance(source_paths, list):
            self._source_paths = [str(p) for p in source_paths if str(p).strip()]
            self._avatar_path.setText(format_source_files(self._source_paths))

        avatar_pngs = [bytes(p) for p in pngs if p]
        if not avatar_pngs:
            self._avatar_count = 0
            self._avatar_pngs = []
            self._file_crop_previews = []
            self._set_busy(False)
            self._refresh_assignment()
            return

        self._avatar_count = len(avatar_pngs)
        self._avatar_pngs = avatar_pngs
        self._set_busy(False)
        self._refresh_assignment()
        self._update_avatar_preview_button()

        warn_lines = [str(e) for e in errors if str(e).strip()]
        if warn_lines:
            preview = "\n".join(warn_lines[:5])
            QMessageBox.warning(
                self,
                "Редактирование канала",
                "Часть файлов обработана с предупреждениями:\n\n" + preview,
            )

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

    def _is_detecting(self) -> bool:
        return self._detect_thread is not None and self._detect_thread.isRunning()

    def _set_busy(self, busy: bool) -> None:
        self._no_crop.setEnabled(not busy and self._toggle_avatar.isChecked())
        self._shuffle.setEnabled(not busy and self._toggle_avatar.isChecked())
        self._progress_bar.setVisible(busy)
        if busy:
            self._btn_select_profiles.setEnabled(False)
            self._btn_avatar_preview.setEnabled(False)
        else:
            self._progress_bar.setValue(0)
            self._update_select_button()
            self._update_avatar_preview_button()

    def _on_channel_fields_changed(self) -> None:
        self._update_select_button()
        self._refresh_assignment()

    def _on_names_text_changed(self) -> None:
        if self._is_detecting():
            return
        self._channel_names = self._current_channel_names()
        if not self._names_source_label:
            if self._channel_names:
                self._names_source_label_widget.setText(
                    f"{len(self._channel_names)} названий"
                )
                self._names_source_label_widget.setVisible(True)
            else:
                self._names_source_label_widget.clear()
                self._names_source_label_widget.setVisible(False)
        self._refresh_assignment()

    def _on_video_titles_text_changed(self) -> None:
        if self._is_detecting():
            return
        self._video_titles = self._current_video_titles()
        if not self._video_titles_source_label:
            if self._video_titles:
                self._video_titles_source_label_widget.setText(
                    f"{len(self._video_titles)} названий"
                )
                self._video_titles_source_label_widget.setVisible(True)
            else:
                self._video_titles_source_label_widget.clear()
                self._video_titles_source_label_widget.setVisible(False)
        self._refresh_assignment()

    def _on_assignment_options_changed(self, _checked: bool = False) -> None:
        if self._is_detecting():
            return
        self._refresh_assignment()

    def _refresh_assignment(self) -> None:
        avatars = self._avatar_pngs if self._toggle_avatar.isChecked() else []
        names = self._current_channel_names() if self._toggle_names.isChecked() else []
        descriptions = (
            self._current_channel_descriptions() if self._toggle_desc.isChecked() else []
        )
        titles = (
            self._current_video_titles() if self._toggle_video_title.isChecked() else []
        )
        self._rows = assign_avatars_to_selected_profiles(
            self._profiles,
            avatars,
            shuffle=self._shuffle.isChecked(),
            channel_names=names,
            shuffle_names=self._shuffle_names.isChecked(),
            channel_descriptions=descriptions,
            video_default_titles=titles,
            shuffle_video_titles=self._shuffle_video_titles.isChecked(),
        )
        self._update_select_button()
        self._update_status_summary()
        self._update_avatar_preview_button()

    def _can_select_profiles(self) -> bool:
        if self._is_detecting():
            return False
        return self.validate_form() is None

    def _update_select_button(self) -> None:
        if not self._running:
            self._btn_select_profiles.setEnabled(self._can_select_profiles())

    def _update_status_summary(self) -> None:
        parts: list[str] = []
        if self._avatar_pngs and self._toggle_avatar.isChecked():
            parts.append(f"Аватарок: {len(self._avatar_pngs)}.")
        if self._channel_names:
            parts.append(f"Названий: {len(self._channel_names)}.")
        desc_count = len(self._current_channel_descriptions()) if self._toggle_desc.isChecked() else 0
        if desc_count:
            parts.append(f"Описаний: {desc_count}.")
        if self._video_titles:
            parts.append(f"Названий для видео: {len(self._video_titles)}.")
        if parts:
            self._status.setText(" ".join(parts))
        else:
            self._status.clear()

    def _on_select_profiles(self) -> None:
        if self._is_detecting() or self._running:
            return
        err = self.validate_form()
        if err:
            QMessageBox.warning(self, "Редактирование канала", err)
            return
        self.select_profiles_requested.emit()
