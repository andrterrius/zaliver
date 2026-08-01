"""Helpers for antidetect-style profile list (tags, errors, upload cooldown)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QCursor, QFont, QFontMetrics, QTextDocument, QTextOption
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
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
    _proxy_state,
    _tag_semantic_kind,
    format_upload_cooldown_line,
)

if TYPE_CHECKING:
    from zaliver.db.upload_store import UploadStore

_PROFILE_TAG_CHIP_MIN_WIDTH = 88
_TAG_CHIP_MARGIN_H = 6
_TAG_CHIP_MARGIN_V = 4
_TAG_CHIP_LBL_PAD_H = 6
_TAG_CHIP_LBL_PAD_V = 4
_TAG_CHIP_BORDER_W = 2
_TAG_CHIP_HEIGHT_SLACK = 4
_TAG_CHIP_FONT_PX = 12


def _tag_chip_object_name(tag: str) -> str:
    kind = _tag_semantic_kind(tag)
    if kind == "error":
        return "tagChipError"
    if kind == "success":
        return "tagChipSuccess"
    return "tagChip"


def _tag_chip_font(base: QFont | None = None) -> QFont:
    font = QFont(base) if base is not None else QFont()
    font.setPixelSize(_TAG_CHIP_FONT_PX)
    return font


def _tag_chip_extra_horizontal() -> int:
    return _TAG_CHIP_MARGIN_H * 2 + _TAG_CHIP_BORDER_W + _TAG_CHIP_LBL_PAD_H * 2


def _tag_chip_text_width_for_frame(frame_w: int) -> int:
    return max(
        1,
        int(frame_w)
        - _TAG_CHIP_MARGIN_H * 2
        - _TAG_CHIP_BORDER_W
        - _TAG_CHIP_LBL_PAD_H * 2,
    )


def _tag_chip_text_height(tag: str, text_w: int, font: QFont) -> int:
    doc = QTextDocument()
    doc.setDefaultFont(font)
    doc.setDocumentMargin(0)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    doc.setDefaultTextOption(opt)
    doc.setPlainText(tag)
    doc.setTextWidth(float(max(1, text_w)))
    return max(1, int(doc.size().height()))


def _tag_chip_frame_height(tag: str, frame_w: int, font: QFont) -> int:
    text_h = _tag_chip_text_height(tag, _tag_chip_text_width_for_frame(frame_w), font)
    return (
        text_h
        + 2 * (_TAG_CHIP_LBL_PAD_V + _TAG_CHIP_MARGIN_V)
        + _TAG_CHIP_HEIGHT_SLACK
    )


class _TagChip(QFrame):
    def __init__(self, tag: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tag = tag
        self._chip_font = _tag_chip_font(parent.font() if parent is not None else None)
        self.setObjectName(_tag_chip_object_name(tag))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(
            _TAG_CHIP_MARGIN_H, _TAG_CHIP_MARGIN_V, _TAG_CHIP_MARGIN_H, _TAG_CHIP_MARGIN_V
        )
        lbl = QLabel(tag)
        lbl.setFont(self._chip_font)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        lbl.setToolTip(tag)
        lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)
        lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._lbl = lbl
        lay.addWidget(lbl, 1)
        natural = QFontMetrics(self._chip_font).horizontalAdvance(tag) + _tag_chip_extra_horizontal()
        self.setMinimumWidth(max(_PROFILE_TAG_CHIP_MIN_WIDTH, min(natural, 240)))
        self.setMinimumHeight(_tag_chip_frame_height(tag, self.minimumWidth(), self._chip_font))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return _tag_chip_frame_height(self._tag, max(1, width), self._chip_font)

    def minimumSizeHint(self) -> QSize:
        w = max(self.minimumWidth(), 1)
        return QSize(w, self.heightForWidth(w))

    def sizeHint(self) -> QSize:
        w = max(self.width(), self.minimumWidth(), 1)
        return QSize(w, self.heightForWidth(w))

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        h = self.heightForWidth(max(1, self.width()))
        if self.minimumHeight() != h:
            self.setMinimumHeight(h)
        self._lbl.setMinimumHeight(
            _tag_chip_text_height(
                self._tag,
                _tag_chip_text_width_for_frame(max(1, self.width())),
                self._chip_font,
            )
        )


class _ProfileTagsWidget(QWidget):
    def __init__(self, tags: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._chips: list[_TagChip] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        for tag in tags:
            chip = _TagChip(tag, self)
            self._chips.append(chip)
            lay.addWidget(chip, 0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        if not self._chips:
            return 0
        spacing = self.layout().spacing() if self.layout() is not None else 4
        total = sum(chip.heightForWidth(width) for chip in self._chips)
        total += max(0, len(self._chips) - 1) * spacing
        return total

    def minimumSizeHint(self) -> QSize:
        w = max((chip.minimumWidth() for chip in self._chips), default=_PROFILE_TAG_CHIP_MIN_WIDTH)
        return QSize(w, self.heightForWidth(w))

    def sizeHint(self) -> QSize:
        w = max(self.width(), self.minimumWidth(), 1)
        return QSize(w, self.heightForWidth(w))

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        h = self.heightForWidth(max(1, self.width()))
        if self.minimumHeight() != h:
            self.setMinimumHeight(h)


def make_profile_tags_widget(tags: list[str], parent: QWidget | None = None) -> QWidget | None:
    if not tags:
        return None
    return _ProfileTagsWidget(tags, parent)


def proxy_health_dot_ui(profile: dict[str, object]) -> tuple[str, str, str]:
    """Returns (glyph, inline_css, tooltip)."""
    proxy = profile.get("proxy")
    if not isinstance(proxy, dict) or not proxy:
        return "", "", "Прокси не задан"
    _, kind, extra = _proxy_state(profile)
    server = ""
    if isinstance(proxy.get("host"), str):
        server = proxy.get("host") or ""
    elif isinstance(proxy.get("server"), str):
        server = proxy.get("server") or ""
    tip = (extra or "").strip()
    if server:
        tip = f"{tip}\n{server}".strip() if tip else server
    if kind == "ok":
        return "●", "color: #6c6;", tip or "Прокси активен"
    if kind == "bad":
        return "●", "color: #c66;", tip or "Прокси не активен"
    return "●", "color: #888;", tip or "Прокси не проверен"


def profile_upload_cooldown_kind(
    last_uploaded_iso: str | None,
    *,
    pause: timedelta | None = None,
) -> str:
    return format_upload_cooldown_line(last_uploaded_iso, pause=pause)[1]


def profile_is_upload_available(
    last_uploaded_iso: str | None,
    *,
    pause: timedelta | None = None,
) -> bool:
    """True if upload pause elapsed or no prior upload (can upload now)."""
    return profile_upload_cooldown_kind(last_uploaded_iso, pause=pause) != "wait"


def profile_has_tag_error(profile: dict[str, object]) -> bool:
    for tag in _profile_tag_list(profile):
        if _tag_semantic_kind(tag) == "error":
            return True
    return False


def _profile_custom_data(profile: dict[str, object]) -> dict[str, object]:
    cd = profile.get("custom_data")
    return cd if isinstance(cd, dict) else {}


def profile_has_account_data(profile: dict[str, object]) -> bool:
    """В custom_data есть логин, пароль или 2FA YouTube."""
    from zaliver.core.profiles.account_data import (
        YT_2FA_KEY,
        YT_LOGIN_KEY,
        YT_PASSWORD_KEY,
    )

    cd = _profile_custom_data(profile)
    login = str(cd.get(YT_LOGIN_KEY) or "").strip()
    password = str(cd.get(YT_PASSWORD_KEY) or "").strip()
    twofa = str(cd.get(YT_2FA_KEY) or "").strip()
    return bool(login or password or twofa)


def profile_has_instagram_account_data(profile: dict[str, object]) -> bool:
    """В custom_data есть логин/пароль/2FA Instagram."""
    from zaliver.core.profiles.account_data import (
        INST_2FA_KEY,
        INST_LOGIN_KEY,
        INST_PASSWORD_KEY,
    )

    cd = _profile_custom_data(profile)
    login = str(cd.get(INST_LOGIN_KEY) or "").strip()
    password = str(cd.get(INST_PASSWORD_KEY) or "").strip()
    twofa = str(cd.get(INST_2FA_KEY) or "").strip()
    return bool(login or password or twofa)


def profile_instagram_ready_for_checker(profile: dict[str, object]) -> bool:
    """
    Профиль подходит для IG-чекера: успешная проверка доступности Instagram
    или есть данные входа (можно восстановить сессию).
    """
    from zaliver.antydetect.profile_tags import INSTAGRAM_AVAILABILITY_SUCCESS_TAG

    tags = set(_profile_tag_list(profile, limit=10_000))
    if INSTAGRAM_AVAILABILITY_SUCCESS_TAG in tags:
        return True
    # legacy до суффикса Instagram
    if "УСПЕШНАЯ ПРОВЕРКА ДОСТУПНОСТИ INSTAGRAM" in tags:
        return True
    return profile_has_instagram_account_data(profile)


def profile_has_yt_oldest_name(profile: dict[str, object]) -> bool:
    """В custom_data сохранено имя самого старого канала (yt_oldest_name)."""
    from zaliver.youtube_upload.google_login import YT_OLDEST_NAME_KEY

    cd = _profile_custom_data(profile)
    return bool(str(cd.get(YT_OLDEST_NAME_KEY) or "").strip())


def profile_has_any_status_error(
    profile: dict[str, object],
    *,
    upload_store: UploadStore | None = None,
) -> bool:
    _, proxy_kind, _ = _proxy_state(profile)
    if proxy_kind == "bad":
        return True
    if profile_has_tag_error(profile):
        return True
    if upload_store is not None:
        pid = _profile_id(profile)
        if pid and upload_store.is_profile_upload_error_flagged(profile_id=pid):
            return True
    return False


def profile_search_tokens(needle: str) -> list[str]:
    return [t for t in (needle or "").lower().strip().split() if t]


def profile_matches_search(profile: dict[str, object], tokens: list[str]) -> bool:
    if not tokens:
        return True
    pid = _profile_id(profile).lower()
    name = _profile_name(profile).lower()
    desc = _profile_description(profile).lower()
    tags = ", ".join(_profile_tag_list(profile)).lower()
    hay = " ".join([pid, name, desc, tags])
    return all(t in hay for t in tokens)


def profile_matches_tag_filter(
    profile: dict[str, object],
    selected_tags: frozenset[str] | set[str] | None,
) -> bool:
    """True if no tag filter, or profile has at least one of the selected tags."""
    if not selected_tags:
        return True
    profile_tags = set(_profile_tag_list(profile, limit=10_000))
    return bool(profile_tags & set(selected_tags))


def profile_search_rank(profile: dict[str, object], tokens: list[str], q_raw: str, original_index: int) -> tuple[int, int]:
    if not tokens:
        return (original_index, original_index)
    q = q_raw.lower().strip()
    pid = _profile_id(profile).lower()
    name = _profile_name(profile).lower()
    desc = _profile_description(profile).lower()
    tags = ", ".join(_profile_tag_list(profile)).lower()
    if q and q in pid:
        return (0, original_index)
    if q and q in name:
        return (1, original_index)
    if q and (q in desc or q in tags):
        return (2, original_index)
    if all(t in pid for t in tokens):
        return (0, original_index)
    if all(t in name for t in tokens):
        return (1, original_index)
    if all((t in desc) or (t in tags) for t in tokens):
        return (2, original_index)
    return (3, original_index)


def profile_row_title_text(profile: dict[str, object]) -> str:
    pid = _profile_id(profile)
    name = _profile_name(profile)
    return f"{name}  ({pid})" if pid else name


def copy_text_to_clipboard(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    QApplication.clipboard().setText(value)
    return True


def make_profile_copy_id_button(profile_id: str, parent: QWidget | None = None) -> QToolButton | None:
    pid = (profile_id or "").strip()
    if not pid:
        return None
    btn = QToolButton(parent)
    btn.setObjectName("profileRowCopyId")
    btn.setText("⧉")
    btn.setToolTip("Скопировать ID профиля")
    btn.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
    btn.setAutoRaise(True)
    btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
    btn.clicked.connect(lambda _checked=False, p=pid, b=btn: _on_copy_id_clicked(p, b))
    return btn


def _on_copy_id_clicked(profile_id: str, btn: QToolButton) -> None:
    if not copy_text_to_clipboard(profile_id):
        return
    prev_tip = btn.toolTip() or "Скопировать ID профиля"
    btn.setToolTip("Скопировано!")
    QTimer.singleShot(1500, lambda: btn.setToolTip(prev_tip))
