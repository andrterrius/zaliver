"""Antidetect-style profile row for QListWidget."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from zaliver.ui.antic_profile_row import (
    _profile_description,
    _profile_id,
    _profile_name,
    _profile_tag_list,
    format_upload_cooldown_line,
)
from zaliver.ui.profile_list_helpers import (
    make_profile_copy_id_button,
    make_profile_tags_widget,
    profile_row_title_text,
    proxy_health_dot_ui,
)


class ProfileListRow(QWidget):
    """Checkbox + title/desc/tags + proxy dot + «Пауза 3 ч» (objectName profileRow)."""

    def __init__(
        self,
        profile: dict[str, object],
        parent: QWidget | None = None,
        *,
        last_uploaded_at: str | None = None,
        on_upload_pause_click: Callable[[], None] | None = None,
        show_account_data_button: bool = False,
        on_account_data_click: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("profileRow")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setMouseTracking(True)

        upload_text, upload_kind = format_upload_cooldown_line(last_uploaded_at)
        self._upload_cooldown_kind = upload_kind
        self._upload_pause_cb = on_upload_pause_click
        self._account_data_cb = on_account_data_click

        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 6)
        outer.setSpacing(10)

        self.checkbox = QCheckBox()
        self.checkbox.setToolTip(
            "Отметить профиль для залива. Клик по любой части строки — отметить/снять; "
            "удерживайте ЛКМ и ведите по строкам — отметятся все на пути (Ctrl — добавить)."
        )
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        outer.addWidget(self.checkbox, 0, Qt.AlignmentFlag.AlignVCenter)

        info = QWidget()
        info.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        info_l = QVBoxLayout(info)
        info_l.setContentsMargins(0, 0, 0, 0)
        info_l.setSpacing(4)

        pid = _profile_id(profile)
        name = _profile_name(profile)
        title_row_w = QWidget(info)
        title_row = QHBoxLayout(title_row_w)
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(4)

        self.title_label = QLabel(name)
        self.title_label.setObjectName("profileRowTitle")
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByKeyboard)
        title_row.addWidget(self.title_label, 0)

        self.id_label: QLabel | None = None
        self.copy_id_btn: QToolButton | None = None
        if pid:
            self.id_label = QLabel(f"({pid})")
            self.id_label.setObjectName("profileRowId")
            self.id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByKeyboard)
            title_row.addWidget(self.id_label, 0)
            self.copy_id_btn = make_profile_copy_id_button(pid, title_row_w)

        if self.copy_id_btn is not None:
            title_row.addWidget(self.copy_id_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        title_row.addStretch(1)
        info_l.addWidget(title_row_w, 0)

        desc = (_profile_description(profile) or "").strip()
        if desc:
            desc_lbl = QLabel(desc)
            desc_lbl.setObjectName("profileRowDesc")
            desc_lbl.setWordWrap(True)
            desc_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByKeyboard)
            info_l.addWidget(desc_lbl, 0)

        tag_strings = _profile_tag_list(profile)
        tags_w = make_profile_tags_widget(tag_strings, info)
        if tags_w is not None:
            info_l.addWidget(tags_w, 0, Qt.AlignmentFlag.AlignLeft)

        info_l.addStretch(0)
        outer.addWidget(info, 1)

        dot_text, dot_css, dot_tip = proxy_health_dot_ui(profile)
        self.proxy_dot = QLabel(dot_text)
        self.proxy_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.proxy_dot.setFixedWidth(18)
        if dot_css:
            self.proxy_dot.setStyleSheet(dot_css)
        self.proxy_dot.setToolTip(dot_tip)
        outer.addWidget(self.proxy_dot, 0, Qt.AlignmentFlag.AlignVCenter)

        self.upload_label = QLabel(upload_text)
        self.upload_label.setObjectName("profileListUpload")
        self.upload_label.setProperty("uploadCooldown", upload_kind)
        self._apply_upload_pause_interaction()
        outer.addWidget(self.upload_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self.account_data_btn: QPushButton | None = None
        if show_account_data_button and on_account_data_click is not None:
            self.account_data_btn = QPushButton("Данные учетки")
            self.account_data_btn.setObjectName("secondary")
            self.account_data_btn.setAutoDefault(False)
            self.account_data_btn.setDefault(False)
            self.account_data_btn.setToolTip(
                "Логин, пароль и 2FA YouTube (custom_data локального антидетекта)"
            )
            self.account_data_btn.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            outer.addWidget(self.account_data_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        tip_lines = [profile_row_title_text(profile), upload_text]
        if dot_tip:
            tip_lines.append(dot_tip)
        self.setToolTip("\n".join(tip_lines))

    def _apply_upload_pause_interaction(self) -> None:
        if self._upload_pause_cb and self._upload_cooldown_kind == "wait":
            self.upload_label.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self.upload_label.setToolTip(
                "Нажмите, чтобы обновить время паузы с последнего залива "
                "(как если бы прошли 3 часа) и снова разрешить загрузку с этого профиля."
            )
        else:
            self.upload_label.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            self.upload_label.setToolTip("")

    def set_last_upload_cooldown(self, last_uploaded_iso: str | None) -> None:
        text, kind = format_upload_cooldown_line(last_uploaded_iso)
        self.upload_label.setText(text)
        self.upload_label.setProperty("uploadCooldown", kind)
        self._upload_cooldown_kind = kind
        self.upload_label.style().unpolish(self.upload_label)
        self.upload_label.style().polish(self.upload_label)
        self._apply_upload_pause_interaction()

    def try_handle_upload_pause_click(self, watched: object) -> bool:
        if watched is not self.upload_label:
            return False
        if self._upload_pause_cb is None or self._upload_cooldown_kind != "wait":
            return False
        self._upload_pause_cb()
        return True
