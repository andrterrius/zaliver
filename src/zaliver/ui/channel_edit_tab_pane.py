"""Вкладка «Редактирование каналов» — настройка канала в YouTube Studio."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QTimer, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QResizeEvent, QShowEvent
from PyQt6.QtWidgets import (
    QAbstractScrollArea,
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
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
    parse_cycling_field_lines,
)
from zaliver.ui.channel_setup_helpers import (
    field_with_recent_picker,
    fill_recent_values_picker,
    format_source_files,
    make_magic_wand_button,
    recent_picker_has_items,
)
from zaliver.ui.platform import PLATFORM_INSTAGRAM, normalize_platform
from zaliver.ui.title_variables_ui import make_variables_hint_button
from zaliver.ui.widgets import AnimatedProgressBar, ToggleSwitch

_TEXT_MIN_H = 64
_TEXT_FIELD_H = 110
# Компактная высота названия — вместе с фото профиля в одном ряду.
_NAMES_FIELD_H = 100
_BP_AVATAR_NAMES = 720
_BP_HEADER = 520


def _section_layout(box: QGroupBox) -> QVBoxLayout:
    lay = QVBoxLayout(box)
    lay.setSpacing(2)
    lay.setContentsMargins(0, 0, 0, 0)
    return lay


def _detach_layout(layout) -> None:
    sink = QWidget()
    sink.setLayout(layout)


def _take_layout_widgets(layout) -> list[QWidget]:
    widgets: list[QWidget] = []
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widgets.append(widget)
    return widgets


class ChannelEditTabPane(QWidget):
    """Форма редактирования канала с переключателями разделов."""

    select_profiles_requested = pyqtSignal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        platform: str = "youtube",
        recent_channel_names: list[str] | None = None,
        recent_channel_descriptions: list[str] | None = None,
        recent_link_titles: list[str] | None = None,
        recent_link_urls: list[str] | None = None,
        recent_video_default_titles: list[str] | None = None,
        ai_generate_fn: Callable[..., None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._platform = normalize_platform(platform)
        self._ai_generate_fn = ai_generate_fn
        self._ai_wand_buttons: list[QToolButton] = []
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
        self._desc_source_label = ""
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

        self._avatar_names_horizontal: bool | None = None
        self._avatar_controls_horizontal: bool | None = None
        self._header_horizontal: bool | None = None

        self._build_ui()
        self._connect_section_toggles()
        self._apply_platform_ui()
        self._on_section_toggle()
        self._refresh_assignment()

    def _is_instagram(self) -> bool:
        return self._platform == PLATFORM_INSTAGRAM

    def _names_section_title(self) -> str:
        return "Юзернейм" if self._is_instagram() else "Название канала"

    def _names_placeholder(self) -> str:
        if self._is_instagram():
            return "Юзернейм — по одному на строку…"
        return "Название канала — по одному на строку…"

    def _names_recent_tooltip(self) -> str:
        return (
            "Недавние юзернеймы"
            if self._is_instagram()
            else "Недавние названия каналов"
        )

    def _make_ai_wand(
        self,
        *,
        default_prompt_id: str,
        window_title: str,
        field: QPlainTextEdit,
    ) -> QToolButton:
        btn = make_magic_wand_button(
            tooltip=f"Сгенерировать через ИИ — «{window_title}»"
        )
        btn.setEnabled(self._ai_generate_fn is not None)

        def _on_click(_checked: bool = False) -> None:
            fn = self._ai_generate_fn
            if fn is None:
                return

            def _apply(text: str, target: QPlainTextEdit = field) -> None:
                target.setPlainText(text if text is not None else "")

            fn(
                default_prompt_id=default_prompt_id,
                window_title=window_title,
                apply_text=_apply,
                parent=self,
                ask_reply_lines=True,
                default_reply_lines=max(1, len(self._profiles)),
            )

        btn.clicked.connect(_on_click)
        self._ai_wand_buttons.append(btn)
        return btn

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
            QSizePolicy.Policy.Preferred,
        )
        return edit

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._apply_responsive_layout()
        # После layout окна высота уже реальная.
        QTimer.singleShot(0, self._sync_text_field_heights)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        width = self.width()
        self._layout_header(width >= _BP_HEADER)
        # Instagram: фото и юзернейм всегда отдельными блоками друг под другом.
        avatar_names_horizontal = (
            False if self._is_instagram() else width >= _BP_AVATAR_NAMES
        )
        self._layout_avatar_names(avatar_names_horizontal)
        self._layout_avatar_controls(width >= _BP_AVATAR_NAMES)
        self._sync_text_field_heights()

    def _layout_header(self, horizontal: bool) -> None:
        if self._header_horizontal == horizontal:
            return
        self._header_horizontal = horizontal
        host = self._header_host
        old = host.layout()
        if old is not None:
            widgets = _take_layout_widgets(old)
            _detach_layout(old)
        else:
            widgets = []
        title = self._header_title
        btn = self._btn_select_profiles
        if horizontal:
            lay = QHBoxLayout(host)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(8)
            lay.addWidget(title, 1)
            lay.addWidget(btn, 0, Qt.AlignmentFlag.AlignRight)
        else:
            lay = QVBoxLayout(host)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)
            lay.addWidget(title)
            lay.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
        for w in widgets:
            if w not in (title, btn):
                lay.addWidget(w)

    def _layout_avatar_names(self, horizontal: bool) -> None:
        if self._avatar_names_horizontal == horizontal:
            return
        self._avatar_names_horizontal = horizontal
        host = self._avatar_names_host
        old = host.layout()
        if old is not None:
            _take_layout_widgets(old)
            _detach_layout(old)
        if horizontal:
            lay = QHBoxLayout(host)
            lay.setSpacing(8)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(self._avatar_box, 1, Qt.AlignmentFlag.AlignTop)
            lay.addWidget(self._names_box, 1, Qt.AlignmentFlag.AlignTop)
        else:
            lay = QVBoxLayout(host)
            lay.setSpacing(8)
            lay.setContentsMargins(0, 0, 0, 0)
            # Фото — по содержимому; юзернейм/название забирает свободную высоту.
            lay.addWidget(self._avatar_box, 0)
            lay.addWidget(self._names_box, 1)

    def _layout_avatar_controls(self, horizontal: bool) -> None:
        if self._avatar_controls_horizontal == horizontal:
            return
        self._avatar_controls_horizontal = horizontal
        host = self._avatar_row_host
        old = host.layout()
        if old is not None:
            _take_layout_widgets(old)
            _detach_layout(old)
        path = self._avatar_path
        btn_pick = self._btn_avatar_pick
        btn_preview = self._btn_avatar_preview
        if horizontal:
            lay = QHBoxLayout(host)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(8)
            lay.addWidget(path, 1)
            lay.addWidget(btn_pick)
            lay.addWidget(btn_preview)
        else:
            lay = QVBoxLayout(host)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)
            lay_row = QHBoxLayout()
            lay_row.addWidget(path, 1)
            lay.addLayout(lay_row)
            btn_row = QHBoxLayout()
            btn_row.addWidget(btn_pick)
            btn_row.addWidget(btn_preview)
            btn_row.addStretch()
            lay.addLayout(btn_row)

    def _pane_window_height(self) -> int:
        """Доступная высота вкладки / окна для расчёта полей."""
        h = self.height()
        if h >= 120:
            return h
        win = self.window()
        if win is not None and win.height() >= 120:
            # Минус полоса навигации слева/сверху — грубо, stack почти на всю высоту.
            return max(120, win.height() - 48)
        return 720

    def _form_inner_chrome_height(self) -> int:
        """Хром внутри scroll (без шапки/футера вкладки)."""
        # Заголовки секций: аватар+имя (+ видео/ссылка на YouTube).
        section_headers = 2 if self._is_instagram() else 4
        chrome = 32 * section_headers
        # Отступы между блоками формы.
        chrome += 8 * (3 if self._is_instagram() else 5)
        # Path + кнопки выбора аватарок.
        chrome += 48
        # Запас: frame GroupBox, «Не обрезать», подписи, округление, низ окна.
        chrome += 64
        if not self._avatar_names_horizontal:
            chrome += 56
        return chrome

    def _non_text_chrome_height(self) -> int:
        """Шапка + футер + хром внутри формы (fallback, если viewport ещё 0)."""
        root = self.layout()
        chrome = 0
        if root is not None:
            m = root.contentsMargins()
            chrome += m.top() + m.bottom()
            chrome += max(0, root.spacing()) * 2

        header_h = self._header_host.height()
        if header_h < 20:
            header_h = max(36, self._header_host.sizeHint().height())
        chrome += header_h

        footer_h = max(28, self._status.sizeHint().height() + 10)
        if self._progress_bar.isVisible():
            footer_h = max(footer_h, 32)
        chrome += footer_h
        chrome += self._form_inner_chrome_height()
        # Чекбокс «Поменять язык» внутри scroll.
        chrome += 28
        return chrome

    def _sync_text_field_heights(self) -> None:
        if not hasattr(self, "_scroll") or not hasattr(self, "_link_title_edit"):
            return

        is_ig = self._is_instagram()
        viewport_h = self._scroll.viewport().height()
        if viewport_h < 80:
            viewport_h = max(120, self._pane_window_height() - 80)

        if is_ig:
            self._apply_instagram_adaptive_layout(viewport_h)
            return

        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        if hasattr(self, "_form_host"):
            self._form_host.setMinimumHeight(0)
            self._form_host.setMaximumHeight(16777215)
            self._form_host.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Minimum,
            )

        available = max(0, viewport_h - self._form_inner_chrome_height() - 28)
        # Доп. отступ снизу + компактное название канала.
        available = max(0, available - 28 - _NAMES_FIELD_H)
        stretch_edits = (
            self._desc_edit,
            self._video_title_edit,
            self._link_title_edit,
            self._link_url_edit,
        )
        slots = len(stretch_edits)
        height = available // slots if slots else _TEXT_MIN_H
        if height < _TEXT_MIN_H:
            height = _TEXT_MIN_H
        else:
            height = min(height, max(_TEXT_MIN_H, viewport_h // 2))

        self._names_edit.setMinimumHeight(_NAMES_FIELD_H)
        self._names_edit.setMaximumHeight(_NAMES_FIELD_H)
        self._names_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        for edit in stretch_edits:
            edit.setMinimumHeight(height)
            edit.setMaximumHeight(height)
            edit.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )

        # YouTube: лишний воздух уходит в stretch внизу формы.
        if hasattr(self, "_form_layout"):
            self._form_layout.setStretch(self._form_idx_avatar_names, 0)
            self._form_layout.setStretch(self._form_idx_desc, 0)
            self._form_layout.setStretch(self._form_bottom_stretch_index, 1)

    def _set_expanding_edit(self, edit: QPlainTextEdit, *, min_h: int) -> None:
        edit.setMinimumHeight(min_h)
        edit.setMaximumHeight(16777215)
        edit.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        parent = edit.parentWidget()
        if parent is not None:
            parent.setMinimumHeight(min_h)
            parent.setMaximumHeight(16777215)
            parent.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Expanding,
            )

    def _apply_instagram_adaptive_layout(self, viewport_h: int) -> None:
        """Адаптивно: форма = 100% viewport, юзернейм/описание делят остаток через stretch."""
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        vp = max(1, int(viewport_h))

        # Ровно высота видимой области — layout сам перераспределяет при ресайзе.
        self._form_host.setMinimumHeight(vp)
        self._form_host.setMaximumHeight(vp)
        self._form_host.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )

        self._avatar_box.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self._names_box.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._desc_box.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._avatar_names_host.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

        min_edit = 48
        self._set_expanding_edit(self._names_edit, min_h=min_edit)
        self._set_expanding_edit(self._desc_edit, min_h=min_edit)

        # stretch: фото по содержимому, юзернейм : описание ≈ 2 : 3
        self._form_layout.setStretch(self._form_idx_language, 0)
        self._form_layout.setStretch(self._form_idx_avatar_names, 2)
        self._form_layout.setStretch(self._form_idx_video, 0)
        self._form_layout.setStretch(self._form_idx_desc, 3)
        self._form_layout.setStretch(self._form_idx_link, 0)
        self._form_layout.setStretch(self._form_bottom_stretch_index, 0)

        host_lay = self._avatar_names_host.layout()
        if isinstance(host_lay, QVBoxLayout) and host_lay.count() >= 2:
            host_lay.setStretch(0, 0)
            host_lay.setStretch(1, 1)

        if hasattr(self, "_names_section_layout"):
            # header=0, field=1, source label=2
            self._names_section_layout.setStretch(1, 1)
        if hasattr(self, "_desc_section_layout"):
            self._desc_section_layout.setStretch(1, 1)

    def _build_ui(self) -> None:
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setMinimumHeight(0)
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        self._header_host = QWidget()
        self._header_title = QLabel("Редактирование канала")
        self._header_title.setObjectName("title")
        self._header_title.setStyleSheet("font-size: 18px; font-weight: 700; color: #f1f5f9;")
        self._header_title.setWordWrap(True)
        self._btn_select_profiles = QPushButton("Выбрать профили")
        self._btn_select_profiles.setEnabled(False)
        self._btn_select_profiles.clicked.connect(self._on_select_profiles)
        root.addWidget(self._header_host)

        self._change_language = QCheckBox("Поменять язык")
        self._change_language.setToolTip(self._change_language_tooltip())
        self._change_language.toggled.connect(self._on_assignment_options_changed)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setSizeAdjustPolicy(
            QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored
        )
        self._scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._scroll.setMinimumHeight(0)
        form_host = QWidget()
        form_host.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        self._form_host = form_host
        form = QVBoxLayout(form_host)
        self._form_layout = form
        form.setSpacing(6)
        form.setContentsMargins(0, 0, 0, 0)

        form.addWidget(self._change_language)
        self._form_idx_language = form.count() - 1
        form.addWidget(self._build_avatar_names_row())
        self._form_idx_avatar_names = form.count() - 1
        self._video_title_box = self._build_video_title_section()
        form.addWidget(self._video_title_box)
        self._form_idx_video = form.count() - 1
        form.addWidget(self._build_desc_section())
        self._form_idx_desc = form.count() - 1
        self._link_box = self._build_link_section()
        form.addWidget(self._link_box)
        self._form_idx_link = form.count() - 1
        form.addStretch(0)
        self._form_bottom_stretch_index = form.count() - 1
        self._scroll.setWidget(form_host)
        root.addWidget(self._scroll, 1)

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
        self._status.setWordWrap(True)
        footer.addWidget(self._progress_bar, 1)
        footer.addWidget(self._status, 1)
        root.addLayout(footer)

        self._apply_responsive_layout()

    def _section_header(
        self,
        toggle: ToggleSwitch,
        label: str,
        *trailing: QWidget,
    ) -> tuple[QHBoxLayout, QLabel]:
        row = QHBoxLayout()
        row.setSpacing(6)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(toggle)
        lbl = QLabel(label)
        lbl.setStyleSheet("font-size: 13px; font-weight: 600; color: #e4e6ef;")
        row.addWidget(lbl)
        row.addStretch()
        for widget in trailing:
            row.addWidget(widget, 0, Qt.AlignmentFlag.AlignRight)
        return row, lbl

    def _change_language_tooltip(self) -> str:
        if self._is_instagram():
            return (
                "Перед редактированием профиля: Language preferences → «Русский». "
                "После обновления страницы должно появиться слово «язык»."
            )
        return (
            "Перед настройкой канала: главная YouTube → язык интерфейса «Русский», "
            "затем переход в креативную студию и остальные шаги."
        )

    def _apply_platform_ui(self) -> None:
        """Instagram: без названия видео и ссылки; «название канала» → юзернейм."""
        is_ig = self._is_instagram()
        self._video_title_box.setVisible(not is_ig)
        self._link_box.setVisible(not is_ig)
        if is_ig:
            self._toggle_video_title.setChecked(False)
            self._toggle_link.setChecked(False)
        self._change_language.setToolTip(self._change_language_tooltip())
        self._names_section_label.setText(self._names_section_title())
        self._names_edit.setPlaceholderText(self._names_placeholder())
        self._names_recent_combo.setToolTip(self._names_recent_tooltip())
        self._btn_names_wand.setToolTip(
            f"Сгенерировать через ИИ — «{self._names_section_title()}»"
        )
        self._sync_text_field_heights()

    def _make_import_button(self, slot) -> QPushButton:
        btn = QPushButton("Импорт…")
        btn.setObjectName("secondary")
        btn.clicked.connect(slot)
        return btn

    def _build_avatar_names_row(self) -> QWidget:
        self._avatar_names_host = QWidget()
        self._avatar_names_host.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        self._avatar_box = QGroupBox()
        self._avatar_box.setObjectName("channelEditSection")
        self._avatar_box.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        avatar_l = _section_layout(self._avatar_box)
        self._toggle_avatar = ToggleSwitch()
        self._toggle_avatar.setChecked(True)
        self._no_crop = QCheckBox("Не обрезать")
        self._no_crop.setToolTip("1 файл = 1 профиль, без вырезки иконок")
        self._no_crop.toggled.connect(self._on_assignment_options_changed)
        self._no_crop.toggled.connect(lambda: self._file_crop_previews.clear())
        avatar_l.addLayout(
            self._section_header(self._toggle_avatar, "Фото профиля", self._no_crop)[0]
        )

        self._avatar_path = QLineEdit()
        self._avatar_path.setReadOnly(True)
        self._avatar_path.setPlaceholderText("Файлы не выбраны")
        self._btn_avatar_pick = QPushButton("Выбрать")
        self._btn_avatar_pick.setObjectName("secondary")
        self._btn_avatar_pick.setToolTip("Выбрать файлы с фото профиля")
        self._btn_avatar_pick.clicked.connect(self._pick_avatar_files)
        self._btn_avatar_preview = QPushButton("Предпросмотр")
        self._btn_avatar_preview.setObjectName("secondary")
        self._btn_avatar_preview.setEnabled(False)
        self._btn_avatar_preview.setToolTip("Показать, как обрезаны фото профиля")
        self._btn_avatar_preview.clicked.connect(self._show_avatar_preview)
        self._avatar_row_host = QWidget()
        avatar_l.addWidget(self._avatar_row_host)

        self._names_box = QGroupBox()
        self._names_box.setObjectName("channelEditSection")
        self._names_box.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        names_l = _section_layout(self._names_box)
        self._names_section_layout = names_l
        self._toggle_names = ToggleSwitch()
        self._toggle_names.setChecked(True)
        self._btn_pick_names = self._make_import_button(self._pick_names_file)
        self._btn_names_hints = make_variables_hint_button(parent=self, field=None)
        names_header, self._names_section_label = self._section_header(
            self._toggle_names,
            self._names_section_title(),
            self._btn_pick_names,
            self._btn_names_hints,
        )
        names_l.addLayout(names_header, 0)
        self._names_edit = self._compact_text_edit(
            self._names_placeholder(),
            fixed_height=_NAMES_FIELD_H,
        )
        self._names_edit.textChanged.connect(self._on_names_text_changed)
        self._btn_names_wand = self._make_ai_wand(
            default_prompt_id="builtin_channel_name",
            window_title=self._names_section_title(),
            field=self._names_edit,
        )
        names_field_row, self._names_recent_combo = field_with_recent_picker(
            self._names_edit,
            recent=self._recent_channel_names,
            tooltip=self._names_recent_tooltip(),
            on_filled=self._on_names_text_changed,
            side_extras=[self._btn_names_wand],
        )
        self._names_field_row = names_field_row
        self._wire_hint_button(self._btn_names_hints, self._names_edit)
        names_l.addWidget(names_field_row, 1)

        self._names_source_label_widget = QLabel("")
        self._names_source_label_widget.setObjectName("hint")
        self._names_source_label_widget.setVisible(False)
        names_l.addWidget(self._names_source_label_widget, 0)

        return self._avatar_names_host

    def _wire_hint_button(self, btn: QPushButton, field: QPlainTextEdit) -> None:
        from zaliver.ui.title_variables_ui import (
            TitleVariablesDialog,
            capture_field_cursor_state,
            insert_text_at_field_cursor,
        )

        try:
            btn.clicked.disconnect()
        except TypeError:
            pass

        def _open_hints() -> None:
            cursor_state = capture_field_cursor_state(field)

            def _insert(token: str) -> None:
                insert_text_at_field_cursor(field, token, cursor_state=cursor_state)

            dlg = TitleVariablesDialog(on_insert=_insert, parent=self)
            dlg.exec()

        btn.clicked.connect(_open_hints)

    def _build_desc_section(self) -> QGroupBox:
        self._desc_box = QGroupBox()
        self._desc_box.setObjectName("channelEditSection")
        desc_l = _section_layout(self._desc_box)
        self._desc_section_layout = desc_l
        self._toggle_desc = ToggleSwitch()
        self._toggle_desc.setChecked(True)
        self._btn_pick_desc = self._make_import_button(self._pick_desc_file)
        self._btn_desc_hints = make_variables_hint_button(parent=self, field=None)
        desc_l.addLayout(
            self._section_header(
                self._toggle_desc,
                "Описание канала",
                self._btn_pick_desc,
                self._btn_desc_hints,
            )[0],
            0,
        )
        self._desc_edit = self._compact_text_edit(
            "Описание канала — по одному на строку…",
            fixed_height=_TEXT_FIELD_H,
        )
        if self._recent_descriptions:
            self._desc_edit.setPlainText(self._recent_descriptions[0])
        self._desc_edit.textChanged.connect(self._on_channel_fields_changed)
        self._btn_desc_wand = self._make_ai_wand(
            default_prompt_id="builtin_channel_description",
            window_title="Описание канала",
            field=self._desc_edit,
        )
        desc_field_row, self._desc_recent_combo = field_with_recent_picker(
            self._desc_edit,
            recent=self._recent_descriptions,
            tooltip="Недавние описания канала",
            on_filled=self._on_channel_fields_changed,
            side_extras=[self._btn_desc_wand],
        )
        self._desc_field_row = desc_field_row
        self._wire_hint_button(self._btn_desc_hints, self._desc_edit)
        desc_l.addWidget(desc_field_row, 1)

        self._desc_source_label_widget = QLabel("")
        self._desc_source_label_widget.setObjectName("hint")
        self._desc_source_label_widget.setVisible(False)
        desc_l.addWidget(self._desc_source_label_widget, 0)
        return self._desc_box

    def _build_link_section(self) -> QGroupBox:
        box = QGroupBox()
        box.setObjectName("channelEditSection")
        lay = _section_layout(box)
        self._toggle_link = ToggleSwitch()
        self._toggle_link.setChecked(True)
        lay.addLayout(self._section_header(self._toggle_link, "Ссылка")[0])

        row = QHBoxLayout()
        row.setSpacing(8)

        self._link_title_edit = self._compact_text_edit(
            "Название ссылки — по одному на строку (строка = профиль)…",
            fixed_height=_TEXT_FIELD_H,
        )
        if self._recent_link_titles:
            self._link_title_edit.setPlainText(self._recent_link_titles[0])
        self._link_title_edit.textChanged.connect(self._on_channel_fields_changed)
        self._btn_link_title_wand = self._make_ai_wand(
            default_prompt_id="builtin_link_title",
            window_title="Название ссылки",
            field=self._link_title_edit,
        )
        title_field_row, self._link_title_recent_combo = field_with_recent_picker(
            self._link_title_edit,
            recent=self._recent_link_titles,
            tooltip="Недавние названия ссылок",
            on_filled=self._on_channel_fields_changed,
            side_extras=[self._btn_link_title_wand],
        )

        self._link_url_edit = self._compact_text_edit(
            "https://… — по одному URL на строку (строка = профиль)",
            fixed_height=_TEXT_FIELD_H,
        )
        if self._recent_link_urls:
            self._link_url_edit.setPlainText(self._recent_link_urls[0])
        self._link_url_edit.textChanged.connect(self._on_channel_fields_changed)
        url_field_row, self._link_url_recent_combo = field_with_recent_picker(
            self._link_url_edit,
            recent=self._recent_link_urls,
            tooltip="Недавние URL ссылок",
            on_filled=self._on_channel_fields_changed,
        )

        row.addWidget(title_field_row, 1)
        row.addWidget(url_field_row, 1)
        lay.addLayout(row)
        return box

    def _build_video_title_section(self) -> QGroupBox:
        box = QGroupBox()
        box.setObjectName("channelEditSection")
        lay = _section_layout(box)
        self._toggle_video_title = ToggleSwitch()
        self._toggle_video_title.setChecked(True)
        self._btn_pick_video_titles = self._make_import_button(self._pick_video_titles_file)
        self._btn_video_title_hints = make_variables_hint_button(parent=self, field=None)
        lay.addLayout(
            self._section_header(
                self._toggle_video_title,
                "Название видео",
                self._btn_pick_video_titles,
                self._btn_video_title_hints,
            )[0]
        )
        self._video_title_body = QWidget()
        body_l = QVBoxLayout(self._video_title_body)
        body_l.setSpacing(2)
        body_l.setContentsMargins(0, 0, 0, 0)
        self._video_title_edit = self._compact_text_edit(
            "Название видео — по одному на строку. "
            "Переменные: {date}, {profile}, {video}, {index}…",
            fixed_height=_TEXT_FIELD_H,
        )
        self._video_title_edit.textChanged.connect(self._on_video_titles_text_changed)
        self._btn_video_title_wand = self._make_ai_wand(
            default_prompt_id="builtin_video_title",
            window_title="Название видео",
            field=self._video_title_edit,
        )
        video_field_row, self._video_title_recent_combo = field_with_recent_picker(
            self._video_title_edit,
            recent=self._recent_video_titles,
            tooltip="Недавние названия видео",
            on_filled=self._on_video_titles_text_changed,
            side_extras=[self._btn_video_title_wand],
        )
        self._wire_hint_button(self._btn_video_title_hints, self._video_title_edit)
        body_l.addWidget(video_field_row, 1)

        self._video_titles_source_label_widget = QLabel("")
        self._video_titles_source_label_widget.setObjectName("hint")
        self._video_titles_source_label_widget.setVisible(False)
        body_l.addWidget(self._video_titles_source_label_widget)

        lay.addWidget(self._video_title_body, 1)
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
        self._btn_avatar_pick.setEnabled(enabled and not self._is_detecting())

        self._desc_edit.setEnabled(self._toggle_desc.isChecked())
        self._desc_recent_combo.setEnabled(
            self._toggle_desc.isChecked() and recent_picker_has_items(self._desc_recent_combo)
        )
        self._btn_desc_hints.setEnabled(self._toggle_desc.isChecked())
        self._btn_desc_wand.setEnabled(
            self._toggle_desc.isChecked() and self._ai_generate_fn is not None
        )
        self._btn_pick_desc.setEnabled(
            self._toggle_desc.isChecked() and not self._is_detecting()
        )

        names_on = self._toggle_names.isChecked()
        self._names_edit.setEnabled(names_on)
        self._names_recent_combo.setEnabled(
            names_on and recent_picker_has_items(self._names_recent_combo)
        )
        self._btn_names_hints.setEnabled(names_on)
        self._btn_names_wand.setEnabled(names_on and self._ai_generate_fn is not None)
        self._btn_pick_names.setEnabled(names_on and not self._is_detecting())

        link_on = self._toggle_link.isChecked()
        self._link_title_edit.setEnabled(link_on)
        self._link_url_edit.setEnabled(link_on)
        self._link_title_recent_combo.setEnabled(
            link_on and recent_picker_has_items(self._link_title_recent_combo)
        )
        self._link_url_recent_combo.setEnabled(
            link_on and recent_picker_has_items(self._link_url_recent_combo)
        )
        self._btn_link_title_wand.setEnabled(
            link_on and self._ai_generate_fn is not None
        )

        vt_on = self._toggle_video_title.isChecked()
        self._video_title_edit.setEnabled(vt_on)
        self._video_title_recent_combo.setEnabled(
            vt_on and recent_picker_has_items(self._video_title_recent_combo)
        )
        self._btn_video_title_hints.setEnabled(vt_on)
        self._btn_video_title_wand.setEnabled(vt_on and self._ai_generate_fn is not None)
        self._btn_pick_video_titles.setEnabled(vt_on and not self._is_detecting())

        self._sync_text_field_heights()
        self._refresh_assignment()
        self._update_select_button()

    def set_selected_profiles(self, profiles: list[dict[str, object]]) -> None:
        self._profiles = list(profiles)
        self._rows = build_selected_profile_avatar_rows(self._profiles)
        self._refresh_assignment()

    def set_running(self, running: bool) -> None:
        self._running = running
        self._btn_select_profiles.setEnabled(not running and self._can_select_profiles())
        self._change_language.setEnabled(not running)
        self._btn_pick_names.setEnabled(
            not running and self._toggle_names.isChecked() and not self._is_detecting()
        )
        self._btn_pick_desc.setEnabled(
            not running and self._toggle_desc.isChecked() and not self._is_detecting()
        )
        self._btn_pick_video_titles.setEnabled(
            not running
            and self._toggle_video_title.isChecked()
            and not self._is_detecting()
        )
        self._update_avatar_preview_button()

    def set_status(self, text: str) -> None:
        if hasattr(self, "_status"):
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
            fill_recent_values_picker(
                self._link_title_recent_combo, self._recent_link_titles
            )
            if not self._link_title_edit.toPlainText().strip():
                self._link_title_edit.setPlainText(link_titles[0])
        if link_urls:
            self._recent_link_urls = list(link_urls)
            fill_recent_values_picker(
                self._link_url_recent_combo, self._recent_link_urls
            )
            if not self._link_url_edit.toPlainText().strip():
                self._link_url_edit.setPlainText(link_urls[0])
        if channel_names:
            self._recent_channel_names = list(channel_names)
            fill_recent_values_picker(self._names_recent_combo, self._recent_channel_names)
        if video_titles:
            self._recent_video_titles = list(video_titles)
            fill_recent_values_picker(
                self._video_title_recent_combo, self._recent_video_titles
            )
        self._on_section_toggle()

    def change_language_before_edit(self) -> bool:
        return bool(self._change_language.isChecked())

    def validate_form(self) -> str | None:
        """Проверка формы; None — всё ок, иначе текст ошибки."""
        links = self.channel_links()
        video_titles = self._current_video_titles()
        has_avatar = self._toggle_avatar.isChecked() and bool(self._avatar_pngs)
        has_names = self._toggle_names.isChecked() and bool(self._current_channel_names())
        has_descriptions = self._toggle_desc.isChecked() and bool(
            self._current_channel_descriptions()
        )

        if (
            not has_descriptions
            and not links
            and not video_titles
            and not has_avatar
            and not has_names
            and not self.change_language_before_edit()
        ):
            return "Включите и заполните хотя бы один раздел или отметьте «Поменять язык»."
        if self._toggle_link.isChecked():
            titles = parse_cycling_field_lines(self._link_title_edit.toPlainText())
            urls = parse_cycling_field_lines(self._link_url_edit.toPlainText())
            if (titles and not urls) or (urls and not titles):
                return (
                    "Нужны и названия ссылок, и URL "
                    "(строка = профиль; короткий список зацикливается)."
                )
        return None

    def confirm_message_for_profiles(self, profile_count: int) -> str:
        assignments = self.profile_assignments()
        links = self.channel_links()
        video_titles = self._current_video_titles()
        msg_parts: list[str] = []
        desc_count = len(self._current_channel_descriptions())
        if self._is_instagram():
            if self.change_language_before_edit():
                msg_parts.append(
                    f"• смена языка Instagram на русский — для всех {profile_count} "
                    "профилей (перед остальными шагами)"
                )
            with_username = sum(
                1
                for a in assignments
                if str(a.get("channel_name") or "").strip()
                and not a.get("skip_name_change")
            )
            if with_username:
                msg_parts.append(
                    f"• юзернейм — {with_username} из {profile_count}"
                )
            if desc_count > 1:
                msg_parts.append(f"• bio — {desc_count} вариантов по профилям")
            elif desc_count == 1:
                msg_parts.append(f"• bio — для всех {profile_count} профилей")
            with_avatar = sum(1 for a in assignments if a.get("avatar_png"))
            if with_avatar:
                msg_parts.append(f"• фото профиля — {with_avatar} из {profile_count}")
            return (
                "Применить настройки профиля в Instagram?\n\n"
                + "\n".join(msg_parts)
            )

        if self.change_language_before_edit():
            msg_parts.append(
                f"• смена языка YouTube на русский — для всех {profile_count} профилей "
                "(перед остальными шагами)"
            )

        if desc_count > 1:
            msg_parts.append(f"• описание канала — {desc_count} вариантов по профилям")
        elif desc_count == 1:
            msg_parts.append(f"• описание канала — для всех {profile_count} профилей")
        if links:
            if len(links) > 1:
                msg_parts.append(
                    f"• ссылка — {len(links)} вариантов по профилям "
                    f"(по одной на аккаунт)"
                )
            else:
                msg_parts.append(
                    f"• ссылка — для всех {profile_count} профилей"
                )
        if video_titles:
            with_video = sum(1 for a in assignments if a.get("video_default_title"))
            msg_parts.append(
                f"• названия видео — {with_video} из {profile_count}"
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

    def channel_links(self) -> list[tuple[str, str]]:
        if not self._toggle_link.isChecked():
            return []
        titles = parse_cycling_field_lines(self._link_title_edit.toPlainText())
        urls = parse_cycling_field_lines(self._link_url_edit.toPlainText())
        if not titles or not urls:
            return []
        # Пары название×URL; короткий список зацикливается.
        # Каждая пара потом идёт на свой профиль (см. worker).
        n = max(len(titles), len(urls))
        return [(titles[i % len(titles)], urls[i % len(urls)]) for i in range(n)]

    def channel_link_title(self) -> str:
        links = self.channel_links()
        return links[0][0] if links else ""

    def channel_link_url(self) -> str:
        links = self.channel_links()
        return links[0][1] if links else ""

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
        return bool(desc_lines) or bool(self.channel_links())

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
        import_title = (
            "Импорт юзернеймов"
            if self._is_instagram()
            else "Импорт названий каналов"
        )
        empty_msg = (
            "В файле не найдено юзернеймов."
            if self._is_instagram()
            else "В файле не найдено названий."
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            import_title,
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
            QMessageBox.warning(self, "Редактирование канала", empty_msg)
            return
        self._names_edit.setPlainText("\n".join(names))
        self._names_source_label = path
        self._names_source_label_widget.setText(Path(path).name)
        self._names_source_label_widget.setVisible(True)
        self._on_names_text_changed()

    def _pick_desc_file(self) -> None:
        if self._is_detecting():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Импорт описаний канала",
            "",
            "Текстовые файлы (*.txt *.csv);;Все файлы (*.*)",
        )
        if not path:
            return
        try:
            lines = parse_channel_names_file(path)
        except OSError as exc:
            QMessageBox.warning(self, "Редактирование канала", f"Не удалось прочитать файл:\n{exc}")
            return
        if not lines:
            QMessageBox.warning(self, "Редактирование канала", "В файле не найдено описаний.")
            return
        self._desc_edit.setPlainText("\n".join(lines))
        self._desc_source_label = path
        self._desc_source_label_widget.setText(Path(path).name)
        self._desc_source_label_widget.setVisible(True)
        self._on_channel_fields_changed()

    def _pick_video_titles_file(self) -> None:
        if self._is_detecting():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Импорт названий видео",
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
            "Выберите картинки для фото профиля",
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
                unit = "юзернеймов" if self._is_instagram() else "названий"
                self._names_source_label_widget.setText(
                    f"{len(self._channel_names)} {unit}"
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
            shuffle=False,
            channel_names=names,
            shuffle_names=False,
            channel_descriptions=descriptions,
            video_default_titles=titles,
            shuffle_video_titles=False,
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
        if not hasattr(self, "_status"):
            return
        parts: list[str] = []
        if self._avatar_pngs and self._toggle_avatar.isChecked():
            parts.append(f"Фото профиля: {len(self._avatar_pngs)}.")
        if self._channel_names:
            if self._is_instagram():
                parts.append(f"Юзернеймов: {len(self._channel_names)}.")
            else:
                parts.append(f"Названий каналов: {len(self._channel_names)}.")
        desc_count = len(self._current_channel_descriptions()) if self._toggle_desc.isChecked() else 0
        if desc_count:
            parts.append(f"Описаний: {desc_count}.")
        links = self.channel_links()
        if links:
            parts.append(f"Ссылок: {len(links)}.")
        if self._video_titles:
            parts.append(f"Названий видео: {len(self._video_titles)}.")
        if self.change_language_before_edit():
            parts.append("Смена языка перед редактированием.")
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
