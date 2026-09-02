"""Main application window."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from typing import NamedTuple
from functools import partial
from pathlib import Path

from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPointF,
    QDateTime,
    QSize,
    QThread,
    QTimeZone,
    QTimer,
    Qt,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QDesktopServices, QMouseEvent, QPixmap, QShowEvent, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QColorDialog,
    QDoubleSpinBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QMenu,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from zaliver.core import ZaliverCore
from zaliver.db.upload_store import (
    UploadedVideo,
    upload_pause_from_settings,
    uploaded_at_sort_ts,
)
from zaliver.antydetect.browser_concurrency import (
    DEFAULT_INSTAGRAM_TABS_PER_PROFILE,
    DEFAULT_MAX_CONCURRENT_BROWSERS,
    INSTAGRAM_TABS_PER_PROFILE_MAX,
    INSTAGRAM_TABS_PER_PROFILE_MIN,
    MAX_CONCURRENT_BROWSERS_MAX,
    MAX_CONCURRENT_BROWSERS_MIN,
    SETTINGS_KEY_INSTAGRAM_TABS_PER_PROFILE,
    clamp_instagram_tabs_per_profile,
    clamp_max_concurrent_browsers,
    compute_instagram_tabs_per_profile,
    instagram_tabs_per_profile_from_settings,
    max_concurrent_browsers_from_settings,
)
from zaliver.antydetect.api import DolphinAntyError, DolphinAntyLocalAPI
from zaliver.antydetect.local_antidetect_api import (
    DEFAULT_LOCAL_API_BASE_URL,
    LocalAntidetectError,
    LocalAntidetectHttpAPI,
    RemoteCdpLaunchOptions,
    normalize_local_profile_for_ui,
)
from zaliver.processing.pipeline import RandomUniquifyBounds, UniquifySettings
from zaliver.processing.ready_buffer import compute_ready_buffer_limit
from zaliver.processing.slicing import DEFAULT_SLICE_FPS_MODE
from zaliver.processing.text_overlay import (
    NEON_WAVE_AMP_FRAC,
    NEON_WAVE_CHAR_PHASE,
    NEON_WAVE_FRAME_SPEED,
    TextOverlaySettings,
    list_bundled_overlay_fonts,
)
from zaliver.ui.adapters import ProcessingController, SlicingController, StitchingController
from zaliver.ui.antic_profile_row import (
    _profile_id,
    _profile_name,
    format_upload_pause_human,
    format_upload_pause_short,
)
from zaliver.ui.profile_list_helpers import (
    profile_matches_search,
    profile_matches_tag_filter,
    profile_search_rank,
    profile_search_tokens,
)
from zaliver.instagram_upload.reels_upload import (
    DEFAULT_INSTAGRAM_CROP_ASPECT,
    SETTINGS_KEY_INSTAGRAM_CROP_ASPECT,
    instagram_crop_aspect_from_settings,
    normalize_instagram_crop_aspect,
)
from zaliver.core.profiles.account_data import (
    GMAIL_LOGIN_KEY,
    INST_LOGIN_KEY,
    SECTION_GMAIL,
    SECTION_INSTAGRAM,
    SECTION_YOUTUBE,
    YT_LOGIN_KEY,
)
from zaliver.ui.profile_account_data_dialog import ProfileAccountDataDialog
from zaliver.ui.profile_accounts_import_dialog import ProfileAccountsImportDialog
from zaliver.ui.profile_tags_clear_dialog import (
    ProfileTagsClearDialog,
    ProfileTagsFilterDialog,
    collect_all_tags_from_profiles,
)
from zaliver.ui.profile_cookie_farm_dialog import ProfileCookieFarmDialog
from zaliver.ui.profile_promote_dialog import (
    ProfilePromoteDialog,
    ProfilePromoteSettings,
)
from zaliver.ui.profile_preview_dialog import ProfileCdpPreviewDialog
from zaliver.ui.ig_checker_profile_dialog import IgCheckerProfilePickDialog
from zaliver.ui.profiles_list_interaction import ProfilesListInteraction
from zaliver.ui.ffmpeg_install_worker import FfmpegInstallWorker
from zaliver.stats_server_client import notify_uploaded_video
from zaliver.youtube_upload.schedule_publish import (
    MSK,
    parse_msk_datetime,
    validate_schedule_times,
)
from zaliver.ui.uploaded_instagram_stats_refresh_worker import (
    UploadedInstagramStatsRefreshWorker,
)
from zaliver.ui.uploaded_stats_refresh_worker import UploadedStatsRefreshWorker
from zaliver.ui.widgets import (
    AnimatedProgressBar,
    FlowLayout,
    SmoothSlider,
    ToggleSwitch,
    ValueRangeSlider,
    configure_log_splitter,
    make_log_export_button,
    make_work_section_nav,
    wrap_work_section_page,
)
from zaliver.ui.text_overlay_io import make_text_overlay_io_buttons
from zaliver.ui.text_overlay_preview import TextOverlayPreviewWidget
from zaliver.ui.slicing_tab_pane import SlicingTabPane
from zaliver.ui.stitching_tab_pane import StitchingTabPane
from zaliver.ui.channel_edit_tab_pane import ChannelEditTabPane
from zaliver.ui.ai_tab_pane import AiTabPane
from zaliver.ui.ai_generate_dialog import AiGenerateDialog
from zaliver.core.profiles import ReelsWarmupSettings, ShortsWarmupSettings
from zaliver.ui.channel_setup_helpers import (
    field_with_recent_picker,
    fill_recent_values_picker,
    make_magic_wand_button,
    recent_picker_has_items,
)
from zaliver.ui.title_variables_ui import (
    make_variables_hint_button,
    show_youtube_title_warnings,
)
from zaliver.config.platform_settings import PlatformSettings
from zaliver.ui.platform import (
    PLATFORM_INSTAGRAM,
    PLATFORM_YOUTUBE,
    PLATFORM_YT_INST,
    apply_platform_branding,
    brand_text,
    normalize_platform,
    platform_display_name,
)
from zaliver.title_variables import (
    TitleVariableContext,
    expand_and_limit_title,
    expand_title_variables,
)

from zaliver.processing.ffmpeg_merge import (
    MACOS_BREW_FFMPEG_FORMULA,
    check_ffmpeg_tools,
    macos_ffmpeg_needs_full_install,
    needs_ffmpeg_install_prompt,
)
# Чтобы в UI не было "лимитов", используем максимально широкие диапазоны,
# но оставляем минимальные логические ограничения там, где отрицательные значения
# ломают смысл (например, количество копий).
_INT_MIN = -2_147_483_648
_INT_MAX = 2_147_483_647
_BIG_FLOAT = 1.0e12

_READY_THUMB_W = 176
_READY_THUMB_H = 99

_UPLOADED_ROW_H = 76
_UPLOADED_RENDER_BATCH = 14
_UPLOADED_RENDER_TICK_MS = 16
_UPLOADED_SCROLL_BATCH = 20

_ANTYDETECT_OWN_KINDS = frozenset({"local", "remote"})


def _is_own_antidetect_kind(kind: str) -> bool:
    return (kind or "").strip() in _ANTYDETECT_OWN_KINDS


def _normalize_antidetect_kind(kind: str | None) -> str:
    """Всегда свой антидетект: local | remote (dolphin → local)."""
    k = (kind or "").strip().lower()
    if k == "remote":
        return "remote"
    return "local"


def _own_antidetect_api_label(kind: str) -> str:
    if (kind or "").strip() == "remote":
        return "удалённого"
    return "локального"


def _format_upload_combo_datetime(iso_s: str) -> str:
    """Дата/время для выпадающего списка сессий: ``YYYY-MM-DD HH:MM:SS`` (локально)."""
    if not (iso_s or "").strip():
        return "—"
    s = iso_s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return s.replace("T", " ")[:19]
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_iso_to_local(iso_s: str) -> datetime | None:
    s = (iso_s or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def _format_time_since_uploaded(iso_s: str) -> str:
    dt = _parse_iso_to_local(iso_s)
    if dt is None:
        return "—"
    now = datetime.now(dt.tzinfo)
    sec = int((now - dt).total_seconds())
    if sec < 0:
        return "0 м"
    if sec < 60:
        return f"{max(1, sec)} м"
    if sec < 3600:
        return f"{sec // 60} м"
    if sec < 86400:
        return f"{sec // 3600} ч"
    if sec < 86400 * 14:
        return f"{sec // 86400} д"
    if sec < 86400 * 60:
        return f"{sec // (86400 * 7)} нед"
    mo = sec // (86400 * 30)
    return f"{max(1, mo)} мес"


def _video_might_be_18_plus(title: str, description: str) -> bool:
    blob = f"{title}\n{description}".lower()
    needles = (
        "18+",
        "18 +",
        "age-restricted",
        "age restricted",
        "not for kids",
        "для взрослых",
        "несовершеннолетн",
    )
    return any(x in blob for x in needles)


def _uploaded_counts_as_18_plus_side(v: UploadedVideo) -> bool:
    """Боковая статистика «С меткой 18+»: эвристика по описанию или флаг Data API после «Прочекать»."""
    if v.age_restricted is True:
        return True
    return _video_might_be_18_plus(v.title, v.description)


def _format_int_compact(v: int | None) -> str:
    if v is None:
        return "—"
    try:
        n = int(v)
    except Exception:
        return "—"
    return f"{n:,}".replace(",", " ")


def _uploaded_stats_html(*, views: int | None, likes: int | None, comments: int | None) -> str:
    """
    Маленькие значки + числа, как компактная строка.
    Используем HTML, чтобы значки были визуально "как иконки" и не ломали выравнивание.
    """
    v = _format_int_compact(views)
    l = _format_int_compact(likes)
    c = _format_int_compact(comments)
    # 👁 👍 💬
    return (
        "<span style='white-space:nowrap;'>"
        f"👁&nbsp;{v}&nbsp;&nbsp;👍&nbsp;{l}&nbsp;&nbsp;💬&nbsp;{c}"
        "</span>"
    )


def _sum_optional_int(values: list[int | None]) -> int | None:
    total = 0
    any_val = False
    for v in values:
        if v is None:
            continue
        try:
            total += int(v)
            any_val = True
        except Exception:
            continue
    return total if any_val else None


def _uploaded_stats_error_video_id(line: object) -> str | None:
    """Формат ошибок воркера: ``'{video_id}: {exc}'`` — достаём id для пометки в БД."""
    s = str(line or "").strip()
    if not s or ":" not in s:
        return None
    vid = s.split(":", 1)[0].strip()
    return vid or None


def _uploaded_row_metrics_html(
    *,
    view_count: int | None,
    like_count: int | None,
    comment_count: int | None,
    stats_unavailable: bool = False,
    stats_unavailable_data_api: bool = False,
    age_restricted: bool | None = None,
) -> str:
    if stats_unavailable and stats_unavailable_data_api:
        return (
            "<span style='color:#94a3b8;font-weight:700;'>API</span> "
            "<span style='color:#f0abfc;font-weight:700;'>нет данных</span>"
        )
    if stats_unavailable:
        return "<span style='color:#f0abfc;font-weight:700;'>недоступно</span>"
    v = _format_int_compact(view_count)
    l = _format_int_compact(like_count)
    c = _format_int_compact(comment_count)
    stats_html = (
        "<span style='white-space:nowrap;'>"
        f"<span style='color:#c7d2fe;font-weight:800;'>👁&nbsp;{v}</span>&nbsp;&nbsp;"
        f"<span style='color:#e9d5ff;font-weight:800;'>♥&nbsp;{l}</span>&nbsp;&nbsp;"
        f"<span style='color:#a8b0d4;font-weight:700;'>💬&nbsp;{c}</span>"
        "</span>"
    )
    if age_restricted is True:
        stats_html += (
            "&nbsp;&nbsp;<span style='color:#fb923c;font-weight:800;'>18+</span>"
            "<span style='color:#94a3b8;font-weight:600;font-size:11px;'>&nbsp;YT</span>"
        )
    return stats_html


def _uploaded_row_profile_caption(
    *, profile_id: str, profiles: list[dict[str, object]] | None
) -> str:
    pid = (profile_id or "").strip()
    if not pid:
        return "Профиль при заливе не сохранён"
    for p in profiles or []:
        if _profile_id(p) == pid:
            return f"С профиля «{_profile_name(p)}»"
    return f"С профиля (ID {pid})"


def _format_stored_datetime(iso_s: str) -> str:
    """Человекочитаемая дата/время из ISO-строки БД (в локальном поясе)."""
    if not (iso_s or "").strip():
        return "—"
    s = iso_s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%d.%m.%Y  %H:%M")
    except ValueError:
        return s.replace("T", " ")[:19]


class _ReadyVideoRow(QWidget):
    """Строка готового видео: открыть файл — только клик по превью; Ctrl/Shift — выделение; «Убрать» — из списка."""

    activated = pyqtSignal(str)
    remove_requested = pyqtSignal(int)

    def __init__(
        self,
        video_id: int,
        index: int,
        path: str,
        filename: str,
        when_text: str,
        thumb_path: str | None,
        tooltip: str,
        list_widget: QListWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # После setItemWidget родитель строки — viewport списка, не QListWidget.
        self._list = list_widget
        self._path = path
        self._video_id = video_id
        self._suppress_activate = False
        self._press_on_thumb_plain = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip(tooltip)

        row = QHBoxLayout(self)
        row.setSpacing(14)
        row.setContentsMargins(6, 6, 10, 6)

        num = QLabel(str(index))
        num.setObjectName("readyRowNumber")
        num.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )
        num.setFixedWidth(68)
        num.setMinimumHeight(_READY_THUMB_H)

        thumb = QLabel()
        thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        thumb.setToolTip("Клик — открыть видео в системе")
        thumb.setFixedSize(_READY_THUMB_W, _READY_THUMB_H)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setObjectName("readyThumb")
        pm: QPixmap | None = None
        if thumb_path:
            tp = Path(thumb_path)
            if tp.is_file():
                loaded = QPixmap(str(tp))
                if not loaded.isNull():
                    pm = loaded.scaled(
                        _READY_THUMB_W,
                        _READY_THUMB_H,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
        if pm is not None and not pm.isNull():
            thumb.setPixmap(pm)
        else:
            thumb.setText("нет превью")
            thumb.setObjectName("readyThumbEmpty")

        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        title = QLabel(filename)
        title.setObjectName("readyRowTitle")
        title.setWordWrap(True)
        sub = QLabel(f"Создан: {when_text}")
        sub.setObjectName("readyRowDate")
        sub.setWordWrap(True)
        text_col.addWidget(title)
        text_col.addWidget(sub)
        text_col.addStretch()

        row.addWidget(num)
        row.addWidget(thumb)
        row.addLayout(text_col, 1)

        self._btn_remove = QPushButton("Убрать")
        self._btn_remove.setObjectName("secondary")
        self._btn_remove.setCursor(Qt.CursorShape.ArrowCursor)
        self._btn_remove.setToolTip(
            "Убрать из списка приложения (файл на диске не удаляется)"
        )
        self._btn_remove.clicked.connect(
            lambda: self.remove_requested.emit(self._video_id)
        )
        row.addWidget(self._btn_remove, 0, Qt.AlignmentFlag.AlignTop)

        self._thumb = thumb
        for w in (num, thumb, title, sub):
            w.installEventFilter(self)

    def _own_item(self) -> QListWidgetItem | None:
        lw = self._list
        for i in range(lw.count()):
            it = lw.item(i)
            if lw.itemWidget(it) is self:
                return it
        return None

    def _body_mouse_press(self, event: QMouseEvent, *, on_thumb: bool) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        it = self._own_item()
        if it is None:
            return
        lw = self._list
        mods = event.modifiers()
        ctrl = mods & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
        )
        shift = mods & Qt.KeyboardModifier.ShiftModifier
        if ctrl:
            it.setSelected(not it.isSelected())
            lw.setCurrentItem(it)
            self._suppress_activate = True
            self._press_on_thumb_plain = False
            return
        if shift:
            anchor = lw.currentItem()
            if anchor is None:
                it.setSelected(True)
                lw.setCurrentItem(it)
            else:
                i_a = lw.row(anchor)
                i_b = lw.row(it)
                top, bottom = sorted((i_a, i_b))
                lw.clearSelection()
                for r in range(top, bottom + 1):
                    ri = lw.item(r)
                    if ri is not None:
                        ri.setSelected(True)
                lw.setCurrentItem(it)
            self._suppress_activate = True
            self._press_on_thumb_plain = False
            return
        lw.clearSelection()
        it.setSelected(True)
        lw.setCurrentItem(it)
        self._press_on_thumb_plain = on_thumb

    def _body_mouse_release(self, event: QMouseEvent, *, on_thumb: bool) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._suppress_activate:
            self._suppress_activate = False
            self._press_on_thumb_plain = False
            return
        if on_thumb and self._press_on_thumb_plain:
            self.activated.emit(self._path)
        self._press_on_thumb_plain = False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if isinstance(watched, QWidget) and isinstance(event, QMouseEvent):
            if event.type() == QEvent.Type.MouseButtonPress:
                gp = watched.mapToGlobal(event.position().toPoint())
                local = QPointF(self.mapFromGlobal(gp))
                synth = QMouseEvent(
                    QEvent.Type.MouseButtonPress,
                    local,
                    event.globalPosition(),
                    event.button(),
                    event.buttons(),
                    event.modifiers(),
                )
                on_thumb = watched is self._thumb
                self._body_mouse_press(synth, on_thumb=on_thumb)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                gp = watched.mapToGlobal(event.position().toPoint())
                local = QPointF(self.mapFromGlobal(gp))
                synth = QMouseEvent(
                    QEvent.Type.MouseButtonRelease,
                    local,
                    event.globalPosition(),
                    event.button(),
                    event.buttons(),
                    event.modifiers(),
                )
                on_thumb = watched is self._thumb
                self._body_mouse_release(synth, on_thumb=on_thumb)
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._btn_remove.geometry().contains(event.position().toPoint()):
            return super().mousePressEvent(event)
        on_thumb = self._thumb.geometry().contains(event.position().toPoint())
        self._body_mouse_press(event, on_thumb=on_thumb)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._btn_remove.geometry().contains(event.position().toPoint()):
            return super().mouseReleaseEvent(event)
        on_thumb = self._thumb.geometry().contains(event.position().toPoint())
        self._body_mouse_release(event, on_thumb=on_thumb)


class _UploadedVideoRow(QWidget):
    """
    Строка залитого видео: метрики справа, открыть ролик — кнопка «↗».
    Ctrl/Shift — как в списке «Готовые видео».
    """

    activated = pyqtSignal(str)

    def __init__(
        self,
        *,
        title: str,
        url: str,
        video_id: str,
        uploaded_at_iso: str,
        view_count: int | None,
        like_count: int | None,
        comment_count: int | None,
        stats_unavailable: bool = False,
        stats_unavailable_data_api: bool = False,
        age_restricted: bool | None = None,
        profile_caption: str,
        tooltip: str,
        list_widget: QListWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._list = list_widget
        self._url = (url or "").strip()
        self._suppress_activate = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip(tooltip)

        row = QHBoxLayout(self)
        row.setSpacing(12)
        row.setContentsMargins(10, 8, 10, 8)

        text_col = QVBoxLayout()
        text_col.setSpacing(4)
        ttl = (title or "").strip() or "(без названия)"
        title_lbl = QLabel(ttl)
        title_lbl.setObjectName("uploadedRowTitle")
        title_lbl.setWordWrap(True)

        vid_s = (video_id or "").strip()
        id_lbl = QLabel(vid_s or "—")
        id_lbl.setObjectName("uploadedRowId")

        prof_lbl = QLabel((profile_caption or "").strip() or "—")
        prof_lbl.setObjectName("uploadedRowProfile")
        prof_lbl.setWordWrap(True)

        text_col.addWidget(title_lbl)
        text_col.addWidget(id_lbl)
        text_col.addWidget(prof_lbl)
        text_col.addStretch()

        stats_wrap = QVBoxLayout()
        stats_wrap.setSpacing(6)
        stats_wrap.addStretch()
        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)

        metrics = QLabel(
            _uploaded_row_metrics_html(
                view_count=view_count,
                like_count=like_count,
                comment_count=comment_count,
                stats_unavailable=stats_unavailable,
                stats_unavailable_data_api=stats_unavailable_data_api,
                age_restricted=age_restricted,
            )
        )
        self._metrics = metrics
        metrics.setObjectName("uploadedRowMetrics")
        metrics.setTextFormat(Qt.TextFormat.RichText)
        metrics.setWordWrap(False)

        ago = QLabel(_format_time_since_uploaded(uploaded_at_iso))
        ago.setObjectName("uploadedRowAgo")

        self._btn_open = QPushButton("↗")
        self._btn_open.setObjectName("uploadedOpenBtn")
        self._btn_open.setFixedSize(34, 30)
        self._btn_open.setCursor(Qt.CursorShape.ArrowCursor)
        self._btn_open.setToolTip("Открыть на YouTube")
        self._btn_open.setEnabled(bool(self._url))
        self._btn_open.clicked.connect(lambda: self.activated.emit(self._url))

        stats_row.addStretch()
        stats_row.addWidget(metrics, 0, Qt.AlignmentFlag.AlignVCenter)
        stats_row.addWidget(ago, 0, Qt.AlignmentFlag.AlignVCenter)
        stats_row.addWidget(self._btn_open, 0, Qt.AlignmentFlag.AlignVCenter)
        stats_wrap.addLayout(stats_row)

        row.addLayout(text_col, 1)
        row.addLayout(stats_wrap, 0)

        for w in (title_lbl, id_lbl, prof_lbl, metrics, ago):
            w.installEventFilter(self)

    def update_from_video(self, v: UploadedVideo) -> None:
        self._metrics.setText(
            _uploaded_row_metrics_html(
                view_count=v.view_count,
                like_count=v.like_count,
                comment_count=v.comment_count,
                stats_unavailable=v.stats_unavailable,
                stats_unavailable_data_api=v.stats_unavailable_data_api,
                age_restricted=v.age_restricted,
            )
        )

    def _own_item(self) -> QListWidgetItem | None:
        lw = self._list
        for i in range(lw.count()):
            it = lw.item(i)
            if lw.itemWidget(it) is self:
                return it
        return None

    def _body_mouse_press(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        it = self._own_item()
        if it is None:
            return
        lw = self._list
        mods = event.modifiers()
        ctrl = mods & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
        )
        shift = mods & Qt.KeyboardModifier.ShiftModifier
        if ctrl:
            it.setSelected(not it.isSelected())
            lw.setCurrentItem(it)
            self._suppress_activate = True
            return
        if shift:
            anchor = lw.currentItem()
            if anchor is None:
                it.setSelected(True)
                lw.setCurrentItem(it)
            else:
                i_a = lw.row(anchor)
                i_b = lw.row(it)
                top, bottom = sorted((i_a, i_b))
                lw.clearSelection()
                for r in range(top, bottom + 1):
                    ri = lw.item(r)
                    if ri is not None and ri.flags() & Qt.ItemFlag.ItemIsSelectable:
                        ri.setSelected(True)
                lw.setCurrentItem(it)
            self._suppress_activate = True
            return
        lw.clearSelection()
        it.setSelected(True)
        lw.setCurrentItem(it)

    def _body_mouse_release(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._suppress_activate:
            self._suppress_activate = False
            return

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if isinstance(watched, QWidget) and isinstance(event, QMouseEvent):
            if event.type() == QEvent.Type.MouseButtonPress:
                gp = watched.mapToGlobal(event.position().toPoint())
                local = QPointF(self.mapFromGlobal(gp))
                synth = QMouseEvent(
                    QEvent.Type.MouseButtonPress,
                    local,
                    event.globalPosition(),
                    event.button(),
                    event.buttons(),
                    event.modifiers(),
                )
                self._body_mouse_press(synth)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                gp = watched.mapToGlobal(event.position().toPoint())
                local = QPointF(self.mapFromGlobal(gp))
                synth = QMouseEvent(
                    QEvent.Type.MouseButtonRelease,
                    local,
                    event.globalPosition(),
                    event.button(),
                    event.buttons(),
                    event.modifiers(),
                )
                self._body_mouse_release(synth)
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._btn_open.geometry().contains(event.position().toPoint()):
            return super().mousePressEvent(event)
        self._body_mouse_press(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._btn_open.geometry().contains(event.position().toPoint()):
            return super().mouseReleaseEvent(event)
        self._body_mouse_release(event)


def _default_workers() -> int:
    return _max_worker_slider()


def _max_worker_slider() -> int:
    from zaliver.processing.fd_limit import cap_process_pool_workers

    return cap_process_pool_workers(max(1, os.cpu_count() or 2))


def _apply_thread_slider_fd_cap(slider: "SmoothSlider") -> None:
    """После поднятия ulimit обновить максимум слайдера потоков."""
    cap_max = _max_worker_slider()
    slider.setMaximum(cap_max)
    if slider.value() > cap_max:
        slider.setValue(cap_max)


def _max_concurrent_browsers_label_text(value: int) -> str:
    n = clamp_max_concurrent_browsers(value)
    if n == 1:
        return "1 браузер"
    if 2 <= n <= 4:
        return f"{n} браузера"
    return f"{n} браузеров"


class MainWindow(QWidget):
    _after_video_saved = pyqtSignal()
    _profiles_loaded = pyqtSignal(object)
    _profiles_load_failed = pyqtSignal(str)
    _dolphin_google_ready = pyqtSignal(str)
    _dolphin_google_failed = pyqtSignal(str, str)
    _ui_log_line = pyqtSignal(str)
    _youtube_upload_phase_finished = pyqtSignal(str)
    _studio_availability_progress = pyqtSignal(int, int, str)
    _studio_availability_finished = pyqtSignal(int, int)
    _instagram_register_progress = pyqtSignal(int, int, str)
    _instagram_register_finished = pyqtSignal(int, int)
    _instagram_2fa_progress = pyqtSignal(int, int, str)
    _instagram_2fa_finished = pyqtSignal(int, int)
    _manual_captcha_needed = pyqtSignal(str)
    _studio_channel_setup_progress = pyqtSignal(int, int, str)
    _studio_channel_setup_finished = pyqtSignal(int, int)
    _studio_warmup_progress = pyqtSignal(int, int, str)
    _studio_warmup_finished = pyqtSignal(int, int)
    _studio_promote_progress = pyqtSignal(int, int, str)
    _studio_promote_finished = pyqtSignal(int, int)
    _studio_cookie_farm_progress = pyqtSignal(int, int, str)
    _studio_cookie_farm_finished = pyqtSignal(int, int)
    _zaliver_profile_tags_clear_progress = pyqtSignal(int, int, str)
    _zaliver_profile_tags_clear_finished = pyqtSignal(int, int)
    _profile_zaliver_tags_cache_update = pyqtSignal(str, object)
    back_to_modes = pyqtSignal()

    def __init__(
        self,
        platform: str = PLATFORM_YOUTUBE,
        *,
        embedded: bool = False,
    ) -> None:
        super().__init__()
        self._platform = normalize_platform(platform)
        self._embedded = bool(embedded)
        self.setWindowTitle(
            f"Zaliver — {platform_display_name(self._platform)}"
        )
        self.setObjectName("zaliverRoot")
        self._work_thread: QThread | None = None
        self._processor: ProcessingController | None = None
        self._slice_processor: SlicingController | None = None
        self._stitch_processor: StitchingController | None = None
        self._active_work_mode = "uniquify"
        self._ff_thread: QThread | None = None
        self._ff_worker: FfmpegInstallWorker | None = None
        self._ffmpeg_progress_dlg: QProgressDialog | None = None
        self._stats_thread: QThread | None = None
        self._stats_worker: (
            UploadedStatsRefreshWorker | UploadedInstagramStatsRefreshWorker | None
        ) = None
        self._stats_progress_dlg: QProgressDialog | None = None
        self._selected_input_files: list[str] = []
        self._background_music_files: list[str] = []
        self._core = ZaliverCore.create(self._platform)
        self._video_store = self._core.videos
        self._upload_store = self._core.uploads
        self._upload_session = None
        self._upload_session_processing_done = False
        self._upload_session_upload_done = False
        self._upload_session_upload_expected = False

        self._settings = self._core.settings
        self._profiles_raw: list[dict[str, object]] | None = None
        self._profiles_tag_filter: frozenset[str] = frozenset()
        self._profiles_tag_exclude: frozenset[str] = frozenset()
        self._profiles_list_render_gen: int = 0
        self._profiles_interaction: ProfilesListInteraction | None = None
        self._profile_cdp_previews: dict[str, ProfileCdpPreviewDialog] = {}
        self._profiles_filter_timer = QTimer(self)
        self._profiles_filter_timer.setSingleShot(True)
        self._profiles_filter_timer.timeout.connect(self._apply_profiles_filter)
        self._profiles_availability_running = False
        self._profiles_register_running = False
        self._profiles_2fa_running = False
        self._profiles_channel_setup_running = False
        self._profiles_warmup_running = False
        self._profiles_promote_running = False
        self._profiles_cookie_farm_running = False
        self._profiles_tags_clear_running = False
        self._profiles_refresh_running = False
        self._last_availability_failed_ids: list[str] = []
        self._last_register_failed_ids: list[str] = []
        self._last_channel_setup_failed_ids: list[str] = []
        self._last_warmup_failed_ids: list[str] = []
        self._last_promote_failed_ids: list[str] = []
        self._last_cookie_farm_failed_ids: list[str] = []
        self._pending_captcha_notify_profile_id: str = ""
        self._build_ui()
        apply_platform_branding(self, self._platform)
        self._bootstrap_fd_limits()
        self._ui_log_line.connect(self._route_ui_log_line)
        self._profiles_loaded.connect(self._on_profiles_loaded)
        self._profiles_load_failed.connect(self._on_profiles_load_failed)
        self._dolphin_google_ready.connect(self._on_dolphin_google_ready)
        self._dolphin_google_failed.connect(self._on_dolphin_google_failed)
        self._after_video_saved.connect(self._refresh_ready_list)
        self._apply_theme()
        if not embedded:
            self.showMaximized()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._load_folder_settings()
        self._load_antydetect_settings()
        self._load_youtube_settings()
        self._load_instagram_settings()
        self._load_ai_settings()
        self._update_profiles_section_header()
        self._sync_ffmpeg_install_row()
        self._pending_upload: dict[str, str] | None = None
        self._just_saved_outputs: list[str] = []
        self._upload_delete_after_enabled = False
        # Yt+Inst: пути, успешно залитые на YouTube, ждут конца Instagram перед удалением.
        self._upload_yt_inst_pending_delete: set[str] = set()
        self._upload_success_lock = threading.Lock()
        self._upload_manager = None
        self._upload_streaming_active = False
        self._upload_streaming_title = ""
        self._upload_streaming_description = ""
        self._upload_streaming_min_ready = 1
        self._progress_hold_youtube = False
        self._upload_cancel_kind = ""
        self._upload_cancel_dolphin_token = ""
        self._upload_cancel_profile_ids: list[str] = []
        self._upload_log_mode = ""
        self._youtube_upload_phase_finished.connect(self._on_youtube_upload_phase_finished)
        self._studio_availability_progress.connect(self._on_studio_availability_progress)
        self._studio_availability_finished.connect(self._on_studio_availability_finished)
        self._instagram_register_progress.connect(self._on_instagram_register_progress)
        self._instagram_register_finished.connect(self._on_instagram_register_finished)
        self._instagram_2fa_progress.connect(self._on_instagram_2fa_progress)
        self._instagram_2fa_finished.connect(self._on_instagram_2fa_finished)
        self._manual_captcha_needed.connect(
            self._on_manual_captcha_needed,
            Qt.ConnectionType.QueuedConnection,
        )
        self._studio_channel_setup_progress.connect(self._on_studio_channel_setup_progress)
        self._studio_channel_setup_finished.connect(self._on_studio_channel_setup_finished)
        self._studio_warmup_progress.connect(self._on_studio_warmup_progress)
        self._studio_warmup_finished.connect(self._on_studio_warmup_finished)
        self._studio_promote_progress.connect(self._on_studio_promote_progress)
        self._studio_promote_finished.connect(self._on_studio_promote_finished)
        self._studio_cookie_farm_progress.connect(self._on_studio_cookie_farm_progress)
        self._studio_cookie_farm_finished.connect(self._on_studio_cookie_farm_finished)
        self._zaliver_profile_tags_clear_progress.connect(
            self._on_zaliver_profile_tags_clear_progress
        )
        self._zaliver_profile_tags_clear_finished.connect(
            self._on_zaliver_profile_tags_clear_finished
        )
        self._profile_zaliver_tags_cache_update.connect(
            self._on_profile_zaliver_tags_cache_update
        )
        # Автозагрузка профилей при запуске (асинхронно).
        QTimer.singleShot(0, self._refresh_antydetect_profiles)

    def _theme_path(self) -> Path:
        return Path(__file__).with_name("theme.qss")

    def _apply_theme(self) -> None:
        # Prefer shell theme when embedded; standalone still needs local QSS.
        from zaliver.ui.theme_loader import load_theme_qss

        qss = load_theme_qss()
        if not qss:
            return
        self.setStyleSheet(qss)
        if not getattr(self, "_embedded", False):
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(qss)

    def _brand(self, text: str) -> str:
        """Подмена YouTube → Instagram в UI-тексте."""
        return brand_text(text, getattr(self, "_platform", PLATFORM_YOUTUBE))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if (
            event.type() == QEvent.Type.Show
            and isinstance(watched, (QDialog, QMessageBox, QProgressDialog))
            and not getattr(watched, "_zaliver_platform_branded", False)
        ):
            apply_platform_branding(watched, getattr(self, "_platform", PLATFORM_YOUTUBE))
            setattr(watched, "_zaliver_platform_branded", True)
        return super().eventFilter(watched, event)

    def _build_ui(self) -> None:
        home = QWidget()
        home_l = QVBoxLayout(home)
        home_l.setSpacing(4)
        home_l.setContentsMargins(12, 8, 12, 12)

        title = QLabel("Zaliver")
        title.setObjectName("title")

        self.btn_start = QPushButton("Старт")
        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setEnabled(False)
        self.btn_start.clicked.connect(self._start)
        self.btn_cancel.clicked.connect(self._cancel)

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

        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        header_row.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(self.progress, 1, Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(self.btn_start, 0, Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(self.btn_cancel, 0, Qt.AlignmentFlag.AlignVCenter)

        header_block = QVBoxLayout()
        header_block.setContentsMargins(0, 0, 0, 0)
        header_block.setSpacing(0)
        header_block.addLayout(header_row)
        header_block.addWidget(self.progress_label)

        section_nav, section_nav_group, _section_btns = make_work_section_nav(
            ["Исходники", "Фильтры", "Текст", "Музыка"],
            parent=home,
        )
        header_block.addWidget(section_nav)
        home_l.addLayout(header_block)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        io = QGroupBox("Файлы и папка результата")
        io_grid = QGridLayout(io)
        io_grid.setHorizontalSpacing(8)
        io_grid.setVerticalSpacing(8)
        self._btn_pick_input_files = QPushButton("Выбрать файлы…")
        self._btn_pick_input_files.setObjectName("secondary")
        self._btn_pick_input_files.clicked.connect(self._browse_input_files)
        self._btn_add_input_files = QPushButton("Добавить еще файлы…")
        self._btn_add_input_files.setObjectName("secondary")
        self._btn_add_input_files.clicked.connect(self._add_input_files)
        self._btn_clear_input_files = QPushButton("Очистить")
        self._btn_clear_input_files.setObjectName("secondary")
        self._btn_clear_input_files.clicked.connect(self._clear_input_files)
        input_files_btns = FlowLayout(hspacing=6, vspacing=6)
        input_files_btns.addWidget(self._btn_pick_input_files)
        input_files_btns.addWidget(self._btn_add_input_files)
        input_files_btns.addWidget(self._btn_clear_input_files)
        input_files_btns_w = QWidget()
        input_files_btns_w.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        input_files_btns_w.setLayout(input_files_btns)
        self._input_files_hint = QLabel("")
        self._input_files_hint.setObjectName("hint")
        self._input_files_hint.setWordWrap(True)
        self._input_files_hint.setMinimumWidth(0)
        self._input_files_hint.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setObjectName("ioPathEdit")
        self.output_dir_edit.setPlaceholderText("Папка для уникализированных файлов…")
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

        io_grid.addWidget(QLabel("Исходные видео:"), 0, 0, Qt.AlignmentFlag.AlignTop)
        io_grid.addWidget(self._input_files_hint, 0, 1)
        io_grid.addWidget(input_files_btns_w, 1, 1)
        io_grid.addWidget(QLabel("Выходная папка:"), 2, 0)
        io_grid.addLayout(out_row, 2, 1)
        io_grid.setColumnStretch(1, 1)
        io_grid.setColumnMinimumWidth(0, 0)
        io_grid.setColumnMinimumWidth(1, 0)
        self.copies_per_file = QSpinBox()
        self.copies_per_file.setRange(1, _INT_MAX)
        self.copies_per_file.setValue(1)
        self.copies_per_file.setMaximumWidth(120)
        self.one_copy_no_effects = QCheckBox("1 копия без эффектов")
        self.one_copy_no_effects.setChecked(False)
        self.one_copy_no_effects.setToolTip(
            "Первая копия каждого исходника без уникализации: "
            "яркость, контраст, шум и прочие эффекты не применяются; "
            "добавляются только фоновый трек и текст на видео."
        )
        copies_row = QHBoxLayout()
        copies_row.setContentsMargins(0, 0, 0, 0)
        copies_row.setSpacing(8)
        copies_row.addWidget(self.copies_per_file, 0)
        copies_row.addWidget(self.one_copy_no_effects, 0)
        copies_row.addStretch(1)
        io_grid.addWidget(QLabel("Копий на исходник:"), 3, 0)
        io_grid.addLayout(copies_row, 3, 1)
        copies_hint = QLabel(
            "Каждая копия — отдельный прогон со своими случайными параметрами. "
            "Например: 10 видео × 5 = 50 файлов."
        )
        copies_hint.setObjectName("hint")
        copies_hint.setWordWrap(True)
        io_grid.addWidget(copies_hint, 4, 0, 1, 2)
        io_hint = QLabel(
            "Имена: имя_u_<случайные hex>.mp4 — у каждого выхода свой суффикс (не счётчик)."
        )
        io_hint.setObjectName("hint")
        io_hint.setWordWrap(True)
        io_grid.addWidget(io_hint, 5, 0, 1, 2)
        self.delete_after_upload = QCheckBox("Удалять после залива")
        self.delete_after_upload.setChecked(False)
        self.delete_after_upload.setToolTip(
            "После каждого успешного залива файл сразу удаляется из выходной папки."
        )
        self.delete_after_upload.toggled.connect(self._save_folder_settings)
        io_grid.addWidget(self.delete_after_upload, 6, 0, 1, 2)

        bg_tracks = QGroupBox("Фоновые треки")
        bg_tracks_l = QVBoxLayout(bg_tracks)
        bg_tracks_l.setSpacing(8)
        self.background_music = ToggleSwitch("Добавить музыку")
        self.background_music.setChecked(False)
        self.background_music.toggled.connect(self._update_music_mix_controls)
        self.background_music.toggled.connect(self._save_folder_settings)
        bg_tracks_l.addWidget(self.background_music)

        self._music_settings_panel = QWidget()
        music_panel_l = QVBoxLayout(self._music_settings_panel)
        music_panel_l.setContentsMargins(0, 0, 0, 0)
        music_panel_l.setSpacing(8)
        music_btns = QHBoxLayout()
        self.btn_add_music = QPushButton("Добавить треки…")
        self.btn_add_music.setObjectName("secondary")
        self.btn_add_music.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_add_music.clicked.connect(self._browse_background_music)
        self.btn_remove_music = QPushButton("Удалить выбранные")
        self.btn_remove_music.setObjectName("secondary")
        self.btn_remove_music.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_remove_music.clicked.connect(self._remove_selected_music)
        self.btn_clear_music = QPushButton("Очистить")
        self.btn_clear_music.setObjectName("secondary")
        self.btn_clear_music.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_clear_music.clicked.connect(self._clear_background_music)
        music_btns.addWidget(self.btn_add_music)
        music_btns.addWidget(self.btn_remove_music)
        music_btns.addWidget(self.btn_clear_music)
        music_btns.addStretch()
        mw_music = QWidget()
        mw_music.setLayout(music_btns)
        music_panel_l.addWidget(mw_music)
        self._music_list = QListWidget()
        self._music_list.setObjectName("musicTracksList")
        self._music_list.setSpacing(4)
        self._music_list.setMouseTracking(True)
        self._music_list.setMaximumHeight(100)
        self._music_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._music_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._music_list.setFrameShape(QFrame.Shape.NoFrame)
        music_panel_l.addWidget(self._music_list)
        self._music_hint = QLabel()
        self._music_hint.setObjectName("hint")
        self._music_hint.setWordWrap(True)
        music_panel_l.addWidget(self._music_hint)
        self.background_music_mix = ToggleSwitch(
            "Смешивать с аудио исходника (иначе — полная замена дорожки)"
        )
        self.background_music_mix.setChecked(False)
        self.background_music_mix.toggled.connect(self._update_music_mix_controls)
        self.background_music_mix.toggled.connect(self._save_folder_settings)
        music_panel_l.addWidget(self.background_music_mix)
        self.background_music_volume = ValueRangeSlider(
            minimum=0,
            maximum=100,
            value=35,
            step=1,
            decimals=0,
            suffix=" %",
        )
        self.background_music_volume.setToolTip(
            "Громкость слоя музыки при смешивании (0…100 %). "
            "Разведите точки — случайная громкость в диапазоне на каждый ролик."
        )
        self.background_music_volume.rangeChangeFinished.connect(
            lambda *_: self._on_music_volume_slider_changed()
        )
        vol_row = QHBoxLayout()
        vol_row.setContentsMargins(0, 0, 0, 0)
        vol_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        vol_lbl = QLabel("Громкость музыки:")
        vol_row.addWidget(vol_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        vol_row.addWidget(self.background_music_volume, 1, Qt.AlignmentFlag.AlignVCenter)
        vw_vol = QWidget()
        vw_vol.setLayout(vol_row)
        music_panel_l.addWidget(vw_vol)
        bg_tracks_l.addWidget(self._music_settings_panel)
        self._update_music_mix_controls()
        self._sync_music_list_widget()

        fx = QWidget()
        fx_layout = QVBoxLayout(fx)
        fx_layout.setContentsMargins(0, 0, 0, 0)
        fx_layout.setSpacing(8)

        text_gb = QGroupBox("Текст на видео")
        text_l = QVBoxLayout(text_gb)
        text_l.setSpacing(8)
        self.text_overlay_enabled = ToggleSwitch("Добавить текст")
        self.text_overlay_enabled.setChecked(True)
        self.text_overlay_enabled.toggled.connect(self._update_text_overlay_controls)
        self.text_overlay_enabled.toggled.connect(self._save_folder_settings)
        btn_text_export, btn_text_import = make_text_overlay_io_buttons(
            self,
            get_settings=self._text_overlay_options_dict,
            apply_settings=self._apply_text_overlay_options,
        )
        text_header = QHBoxLayout()
        text_header.setContentsMargins(0, 0, 0, 0)
        text_header.setSpacing(8)
        text_header.addWidget(self.text_overlay_enabled)
        text_header.addStretch(1)
        text_header.addWidget(btn_text_export)
        text_header.addWidget(btn_text_import)
        text_l.addLayout(text_header)

        self._text_overlay_panel = QWidget()
        text_controls_l = QVBoxLayout(self._text_overlay_panel)
        text_controls_l.setContentsMargins(0, 0, 0, 0)
        text_controls_l.setSpacing(8)

        self.text_overlay_from_middle = QCheckBox(
            "Текст с середины видео до конца"
        )
        self.text_overlay_from_middle.setChecked(True)
        self.text_overlay_from_middle.toggled.connect(self._save_folder_settings)
        text_controls_l.addWidget(self.text_overlay_from_middle)

        self._syncing_text_overlay = False
        self.text_overlay_edit = QPlainTextEdit()
        self.text_overlay_edit.setPlaceholderText("Текст для наложения…")
        self.text_overlay_edit.setMaximumHeight(72)
        self.text_overlay_edit.textChanged.connect(self._on_text_overlay_content_changed)
        btn_text_wand = make_magic_wand_button(
            tooltip="Сгенерировать текст на видео через ИИ (промпт «Текст на видео»)"
        )
        btn_text_wand.clicked.connect(
            lambda _checked=False: self._on_ai_magic_generate(
                default_prompt_id="builtin_video_overlay_text",
                window_title="Генерация текста на видео",
                apply_text=self._apply_ai_text_overlay,
                parent=self,
            )
        )
        text_row, self._text_overlay_recent_picker = field_with_recent_picker(
            self.text_overlay_edit,
            recent=self._recent_text_overlay_texts(),
            tooltip="Недавние тексты на видео (общие для всех обработок платформы)",
            on_filled=self._on_text_overlay_content_changed,
            side_extras=[btn_text_wand],
        )
        text_controls_l.addWidget(text_row)

        text_opts = QGridLayout()
        text_opts.setHorizontalSpacing(8)
        self.text_overlay_font_size = QSpinBox()
        self.text_overlay_font_size.setRange(12, 240)
        self.text_overlay_font_size.setValue(95)
        self.text_overlay_font_size.valueChanged.connect(self._on_text_overlay_font_size_changed)
        self.text_overlay_orientation = QComboBox()
        self.text_overlay_orientation.addItem("Вертикальное 9:16", "vertical")
        self.text_overlay_orientation.addItem("Горизонтальное 16:9", "horizontal")
        self.text_overlay_orientation.currentIndexChanged.connect(
            self._on_text_overlay_orientation_changed
        )
        self.text_overlay_glow_btn = QPushButton("Цвет неона…")
        self.text_overlay_glow_btn.setObjectName("secondary")
        self._text_overlay_glow_color = "#00FFFF"
        self._sync_text_overlay_color_btn(self.text_overlay_glow_btn, self._text_overlay_glow_color)
        self.text_overlay_glow_btn.clicked.connect(self._pick_text_overlay_glow_color)
        self.text_overlay_glow_enabled = QCheckBox("Включено")
        self.text_overlay_glow_enabled.setChecked(True)
        self.text_overlay_glow_enabled.toggled.connect(self._on_text_overlay_glow_enabled_changed)
        glow_row = QHBoxLayout()
        glow_row.setContentsMargins(0, 0, 0, 0)
        glow_row.addWidget(self.text_overlay_glow_enabled)
        glow_row.addWidget(self.text_overlay_glow_btn)
        glow_row.addStretch()
        glow_row_w = QWidget()
        glow_row_w.setLayout(glow_row)
        self.text_overlay_text_btn = QPushButton("Цвет текста…")
        self.text_overlay_text_btn.setObjectName("secondary")
        self._text_overlay_text_color = "#FFFFFF"
        self._sync_text_overlay_color_btn(self.text_overlay_text_btn, self._text_overlay_text_color)
        self.text_overlay_text_btn.clicked.connect(self._pick_text_overlay_text_color)
        self.text_overlay_letter_spacing = QSpinBox()
        self.text_overlay_letter_spacing.setRange(-20, 80)
        self.text_overlay_letter_spacing.setValue(0)
        self.text_overlay_letter_spacing.setSuffix(" px")
        self.text_overlay_letter_spacing.valueChanged.connect(
            self._on_text_overlay_letter_spacing_changed
        )
        self._text_overlay_font_path = ""
        self.text_overlay_font_combo = QComboBox()
        self.text_overlay_font_combo.currentIndexChanged.connect(
            self._on_text_overlay_font_changed
        )
        self.text_overlay_font_browse_btn = QPushButton("Файл…")
        self.text_overlay_font_browse_btn.setObjectName("secondary")
        self.text_overlay_font_browse_btn.clicked.connect(self._pick_text_overlay_font_file)
        self.text_overlay_font_bold = QCheckBox("Жирный")
        self.text_overlay_font_bold.setChecked(True)
        self.text_overlay_font_bold.toggled.connect(self._on_text_overlay_font_bold_changed)
        font_row = QHBoxLayout()
        font_row.setContentsMargins(0, 0, 0, 0)
        font_row.addWidget(self.text_overlay_font_combo, 1)
        font_row.addWidget(self.text_overlay_font_bold)
        font_row.addWidget(self.text_overlay_font_browse_btn)
        font_row_w = QWidget()
        font_row_w.setLayout(font_row)
        self._populate_text_overlay_font_combo()
        # orientation остаётся в коде для превью/сохранения, в UI не показываем.
        self.text_overlay_orientation.hide()
        text_opts.addWidget(QLabel("Размер"), 0, 0)
        text_opts.addWidget(self.text_overlay_font_size, 0, 1)
        text_opts.addWidget(QLabel("Свечение"), 1, 0)
        text_opts.addWidget(glow_row_w, 1, 1)
        text_opts.addWidget(QLabel("Цвет"), 2, 0)
        text_opts.addWidget(self.text_overlay_text_btn, 2, 1)
        text_opts.addWidget(QLabel("Межбуквенный интервал"), 3, 0)
        text_opts.addWidget(self.text_overlay_letter_spacing, 3, 1)
        text_opts.addWidget(QLabel("Шрифт"), 4, 0)
        text_opts.addWidget(font_row_w, 4, 1)

        self.text_overlay_wave_amp = ValueRangeSlider(
            minimum=0,
            maximum=35,
            value=int(round(NEON_WAVE_AMP_FRAC * 100)),
            step=1,
            decimals=0,
            suffix=" %",
        )
        self.text_overlay_wave_amp.rangeChanged.connect(
            lambda *_: self._schedule_text_overlay_preview_sync()
        )
        self.text_overlay_wave_amp.rangeChangeFinished.connect(
            self._on_text_overlay_wave_changed
        )
        self.text_overlay_wave_speed = ValueRangeSlider(
            minimum=0,
            maximum=25,
            value=int(round(NEON_WAVE_FRAME_SPEED * 100)),
            step=1,
            decimals=0,
        )
        self.text_overlay_wave_speed.rangeChanged.connect(
            lambda *_: self._schedule_text_overlay_preview_sync()
        )
        self.text_overlay_wave_speed.rangeChangeFinished.connect(
            self._on_text_overlay_wave_changed
        )
        text_opts.addWidget(QLabel("Волна - амплитуда"), 5, 0)
        text_opts.addWidget(self.text_overlay_wave_amp, 5, 1)
        text_opts.addWidget(QLabel("Волна - скорость"), 6, 0)
        text_opts.addWidget(self.text_overlay_wave_speed, 6, 1)

        text_opts_w = QWidget()
        text_opts_w.setLayout(text_opts)
        text_controls_l.addWidget(text_opts_w)

        text_pos_row = QHBoxLayout()
        text_pos_row.setContentsMargins(0, 0, 0, 0)
        self.text_overlay_center_btn = QPushButton("По центру (горизонт.)")
        self.text_overlay_center_btn.setObjectName("secondary")
        self.text_overlay_center_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.text_overlay_center_btn.clicked.connect(self._center_text_overlay_horizontally)
        text_pos_row.addWidget(self.text_overlay_center_btn)
        self.text_overlay_center_v_btn = QPushButton("По центру (вертик.)")
        self.text_overlay_center_v_btn.setObjectName("secondary")
        self.text_overlay_center_v_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.text_overlay_center_v_btn.clicked.connect(self._center_text_overlay_vertically)
        text_pos_row.addWidget(self.text_overlay_center_v_btn)
        text_pos_row.addStretch()
        text_pos_w = QWidget()
        text_pos_w.setLayout(text_pos_row)
        text_controls_l.addWidget(text_pos_w)

        self.text_overlay_preview = TextOverlayPreviewWidget()
        self.text_overlay_preview.setMinimumHeight(240)
        self.text_overlay_preview.setMaximumHeight(340)
        self.text_overlay_preview.positionChanged.connect(self._on_text_overlay_position_changed)
        text_controls_l.addWidget(self.text_overlay_preview)

        self._text_overlay_preview_index = 0
        self._btn_text_preview_prev = QPushButton("‹")
        self._btn_text_preview_prev.setObjectName("textPreviewNav")
        self._btn_text_preview_prev.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_text_preview_prev.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_text_preview_prev.setFixedSize(36, 36)
        self._btn_text_preview_prev.setToolTip("Предыдущий исходник")
        self._btn_text_preview_prev.setAutoDefault(False)
        self._btn_text_preview_prev.setDefault(False)
        self._btn_text_preview_prev.clicked.connect(self._text_overlay_preview_prev)
        self._btn_text_preview_next = QPushButton("›")
        self._btn_text_preview_next.setObjectName("textPreviewNav")
        self._btn_text_preview_next.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_text_preview_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_text_preview_next.setFixedSize(36, 36)
        self._btn_text_preview_next.setToolTip("Следующий исходник")
        self._btn_text_preview_next.setAutoDefault(False)
        self._btn_text_preview_next.setDefault(False)
        self._btn_text_preview_next.clicked.connect(self._text_overlay_preview_next)
        self._text_overlay_preview_meta = QLabel("")
        self._text_overlay_preview_meta.setObjectName("hint")
        self._text_overlay_preview_meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._text_overlay_preview_meta.setWordWrap(True)
        preview_nav = QHBoxLayout()
        preview_nav.setContentsMargins(0, 0, 0, 0)
        preview_nav.setSpacing(8)
        preview_nav.addWidget(self._btn_text_preview_prev, 0)
        preview_nav.addWidget(self._text_overlay_preview_meta, 1)
        preview_nav.addWidget(self._btn_text_preview_next, 0)
        preview_nav_w = QWidget()
        preview_nav_w.setLayout(preview_nav)
        text_controls_l.addWidget(preview_nav_w)

        text_l.addWidget(self._text_overlay_panel)
        self._update_text_overlay_controls()

        bounds_inner = QWidget()
        rg = QGridLayout(bounds_inner)
        rg.setContentsMargins(0, 0, 0, 0)
        rg.setHorizontalSpacing(8)
        rg.setVerticalSpacing(6)

        def _bound_slider(
            *,
            range_min: float,
            range_max: float,
            lo: float,
            hi: float,
            step: float,
            decimals: int,
        ) -> ValueRangeSlider:
            return ValueRangeSlider(
                minimum=range_min,
                maximum=range_max,
                low=lo,
                high=hi,
                step=step,
                decimals=decimals,
            )

        def _fx_enable(tooltip: str) -> QCheckBox:
            cb = QCheckBox()
            cb.setChecked(True)
            cb.setToolTip(tooltip)
            cb.toggled.connect(self._on_fx_enable_toggled)
            return cb

        self.fx_brightness_enabled = _fx_enable(
            "Применять яркость. Выкл. — яркость не меняется."
        )
        self.fx_contrast_enabled = _fx_enable(
            "Применять контраст. Выкл. — контраст без изменений."
        )
        self.fx_saturation_enabled = _fx_enable(
            "Применять насыщенность. Выкл. — насыщенность без изменений."
        )
        self.fx_scale_enabled = _fx_enable(
            "Применять масштаб. Выкл. — масштаб 100%."
        )
        self.fx_noise_enabled = _fx_enable(
            "Применять шум. Выкл. — без шума."
        )
        self.audio_speed = _fx_enable(
            "Применять скорость видео+аудио. Выкл. — скорость 1.0×."
        )
        self._fx_enable_checks = [
            self.fx_brightness_enabled,
            self.fx_contrast_enabled,
            self.fx_saturation_enabled,
            self.fx_scale_enabled,
            self.fx_noise_enabled,
            self.audio_speed,
        ]

        def _bounds_row(row: int, title: str, w: QWidget, enable_cb: QCheckBox) -> int:
            rg.addWidget(enable_cb, row, 0, Qt.AlignmentFlag.AlignVCenter)
            rg.addWidget(QLabel(title), row, 1, Qt.AlignmentFlag.AlignVCenter)
            rg.addWidget(w, row, 2)
            return row + 1

        br = 0
        self.rb_brightness = _bound_slider(
            range_min=-40.0, range_max=40.0, lo=-22.0, hi=22.0, step=0.5, decimals=1
        )
        br = _bounds_row(br, "Яркость", self.rb_brightness, self.fx_brightness_enabled)
        self.rb_contrast = _bound_slider(
            range_min=0.70, range_max=1.40, lo=0.88, hi=1.14, step=0.01, decimals=2
        )
        br = _bounds_row(br, "Контраст", self.rb_contrast, self.fx_contrast_enabled)
        self.rb_saturation = _bound_slider(
            range_min=0.70, range_max=1.40, lo=0.88, hi=1.12, step=0.01, decimals=2
        )
        br = _bounds_row(br, "Насыщенность", self.rb_saturation, self.fx_saturation_enabled)
        self.rb_scale_pct = _bound_slider(
            range_min=90.0, range_max=110.0, lo=95.0, hi=100.6, step=0.1, decimals=1
        )
        br = _bounds_row(br, "Масштаб", self.rb_scale_pct, self.fx_scale_enabled)
        self.rb_noise = _bound_slider(
            range_min=0.0, range_max=10.0, lo=0.5, hi=4.0, step=0.05, decimals=2
        )
        br = _bounds_row(br, "Шум", self.rb_noise, self.fx_noise_enabled)
        self.audio_speed_range = _bound_slider(
            range_min=0.85, range_max=1.25, lo=1.0, hi=1.1, step=0.01, decimals=2
        )
        br = _bounds_row(
            br, "Скорость видео+аудио", self.audio_speed_range, self.audio_speed
        )
        self._random_bound_sliders = [
            self.rb_brightness,
            self.rb_contrast,
            self.rb_saturation,
            self.rb_scale_pct,
            self.rb_noise,
            self.audio_speed_range,
        ]
        fx_layout.addWidget(bounds_inner)
        self._sync_fx_enable_slider_states()

        self._uniquify_section_stack = QStackedWidget()
        self._uniquify_section_stack.addWidget(wrap_work_section_page(io))
        self._uniquify_section_stack.addWidget(wrap_work_section_page(fx))
        self._uniquify_section_stack.addWidget(wrap_work_section_page(text_gb))
        self._uniquify_section_stack.addWidget(wrap_work_section_page(bg_tracks))
        section_nav_group.idClicked.connect(self._uniquify_section_stack.setCurrentIndex)

        scroll_left = QScrollArea()
        scroll_left.setWidgetResizable(True)
        scroll_left.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_left.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        scroll_left.setMinimumWidth(0)
        inner_left = QWidget()
        inner_left.setMinimumWidth(0)
        inner_left.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        inner_left_l = QVBoxLayout(inner_left)
        inner_left_l.setContentsMargins(0, 0, 0, 0)
        inner_left_l.addWidget(self._uniquify_section_stack)
        inner_left_l.addStretch()
        scroll_left.setWidget(inner_left)
        scroll_left.setSizePolicy(
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
                default_filename="zaliver_uniquify_log.txt",
            )
        )
        rl.addLayout(log_header)
        rl.addWidget(self.log, 1)

        splitter.addWidget(scroll_left)
        splitter.addWidget(right)
        configure_log_splitter(splitter, form_panel=scroll_left, log_panel=right)
        home_l.addWidget(splitter, 1)

        self._slice_tab = SlicingTabPane(
            self,
            settings=self._settings,
            platform=self._platform,
            upload_store=self._upload_store,
            ai_generate_fn=self._on_ai_magic_generate,
            on_text_overlay_text_changed=lambda text: self._on_shared_text_overlay_changed(
                text, source="slice"
            ),
        )
        self._slice_tab.start_requested.connect(self._start_slicing)
        self._slice_tab.cancel_requested.connect(self._cancel)
        self._stitch_tab = StitchingTabPane(
            self,
            settings=self._settings,
            platform=self._platform,
            upload_store=self._upload_store,
            ai_generate_fn=self._on_ai_magic_generate,
            on_text_overlay_text_changed=lambda text: self._on_shared_text_overlay_changed(
                text, source="stitch"
            ),
        )
        self._stitch_tab.start_requested.connect(self._start_stitching)
        self._stitch_tab.cancel_requested.connect(self._cancel)

        ready = QWidget()
        ready_l = QVBoxLayout(ready)
        ready_l.setSpacing(10)
        ready_l.setContentsMargins(12, 12, 12, 12)
        ready_title = QLabel("Готовые видео")
        ready_title.setObjectName("title")
        ready_hint = QLabel(
            "Список сохранённых результатов (SQLite). Клик по превью — открыть файл. "
            "Ctrl+клик или Shift+клик по строке — выделить несколько; затем «Удалить выбранные…». "
            "Файлы на диске при удалении из списка не удаляются."
        )
        ready_hint.setObjectName("hint")
        ready_hint.setWordWrap(True)
        ready_top = QHBoxLayout()
        btn_refresh_ready = QPushButton("Обновить список")
        btn_refresh_ready.setObjectName("secondary")
        btn_refresh_ready.clicked.connect(self._refresh_ready_list)
        btn_remove_selected = QPushButton("Удалить выбранные…")
        btn_remove_selected.setObjectName("danger")
        btn_remove_selected.clicked.connect(self._on_ready_remove_selected)
        ready_top.addWidget(ready_title)
        ready_top.addStretch()
        ready_top.addWidget(btn_remove_selected)
        ready_top.addWidget(btn_refresh_ready)
        ready_l.addLayout(ready_top)
        ready_l.addWidget(ready_hint)
        self._ready_list = QListWidget()
        self._ready_list.setObjectName("readyList")
        self._ready_list.setSpacing(6)
        self._ready_list.setAlternatingRowColors(False)
        self._ready_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._ready_list.setUniformItemSizes(True)
        ready_l.addWidget(self._ready_list, 1)

        uploaded = QWidget()
        uploaded_l = QVBoxLayout(uploaded)
        uploaded_l.setSpacing(8)
        uploaded_l.setContentsMargins(12, 12, 12, 12)

        self._uploaded_all: list[UploadedVideo] = []
        self._uploaded_render_pos = 0
        self._uploaded_render_timer = QTimer(self)
        self._uploaded_render_timer.setInterval(_UPLOADED_RENDER_TICK_MS)
        self._uploaded_render_timer.timeout.connect(self._tick_uploaded_list_render)

        uploaded_top = QHBoxLayout()
        uploaded_top.setSpacing(8)
        self._uploaded_session_filter = QComboBox()
        self._uploaded_session_filter.setObjectName("uploadedSessionFilter")
        self._uploaded_session_filter.setMinimumWidth(360)
        self._uploaded_session_filter.setMaxVisibleItems(16)
        self._uploaded_session_filter.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._uploaded_session_filter.setToolTip(
            "Сессия залива. В скобках: успешно залито / всего уникализировано по данным сессии."
        )
        self._uploaded_session_filter.currentIndexChanged.connect(
            lambda _i: self._refresh_uploaded_list()
        )
        self._btn_uploaded_refresh = QPushButton("Список")
        self._btn_uploaded_refresh.setObjectName("secondary")
        self._btn_uploaded_refresh.setToolTip("Перечитать залитые видео из локальной базы")
        self._btn_uploaded_refresh.clicked.connect(self._refresh_uploaded_list)
        self._btn_uploaded_check = QPushButton("▶  Прочекать")
        self._btn_uploaded_check.setObjectName("uploadedCheckBtn")
        self._btn_uploaded_check.setToolTip(
            "Запросить просмотры, лайки и комментарии через YouTube Data API (для роликов в текущем списке)"
        )
        self._btn_uploaded_check.clicked.connect(self._refresh_uploaded_stats_visible)
        uploaded_top.addWidget(self._uploaded_session_filter, 1)
        uploaded_top.addWidget(self._btn_uploaded_refresh, 0)
        uploaded_top.addWidget(self._btn_uploaded_check, 0)
        uploaded_l.addLayout(uploaded_top)

        self._uploaded_ig_checker_row = QWidget()
        ig_checker_l = QHBoxLayout(self._uploaded_ig_checker_row)
        ig_checker_l.setContentsMargins(0, 0, 0, 0)
        ig_checker_l.setSpacing(8)
        ig_checker_lbl = QLabel("Аккаунт для чека:")
        ig_checker_lbl.setObjectName("uploadedIgCheckerLabel")
        self._uploaded_ig_checker_profile_id = (
            self._settings.value("instagram/stats_checker_profile_id", "", type=str)
            or ""
        ).strip()
        self._uploaded_ig_checker_value = QLabel("— не выбран —")
        self._uploaded_ig_checker_value.setObjectName("uploadedIgCheckerValue")
        self._uploaded_ig_checker_value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._uploaded_ig_checker_value.setToolTip(
            "Антидетект-профиль для чека метрик (instagrapi).\n"
            "Лучше отдельный «читающий» аккаунт — не тот, с которого льёте.\n"
            "Параллельный/агрессивный чек с тем же sessionid убивает вход "
            "в браузере (Exceeded 30 redirects)."
        )
        self._btn_uploaded_ig_checker_pick = QPushButton("Выбрать профиль")
        self._btn_uploaded_ig_checker_pick.setObjectName("secondary")
        self._btn_uploaded_ig_checker_pick.setAutoDefault(False)
        self._btn_uploaded_ig_checker_pick.setDefault(False)
        self._btn_uploaded_ig_checker_pick.setToolTip(
            "Выбрать профиль с Instagram-сессией для чека.\n"
            "Рекомендуется отдельный аккаунт только для статистики."
        )
        self._btn_uploaded_ig_checker_pick.clicked.connect(
            self._pick_uploaded_ig_checker_profile
        )
        ig_checker_l.addWidget(ig_checker_lbl)
        ig_checker_l.addWidget(self._uploaded_ig_checker_value, 1)
        ig_checker_l.addWidget(self._btn_uploaded_ig_checker_pick)
        uploaded_l.addWidget(self._uploaded_ig_checker_row)
        self._uploaded_ig_checker_row.setVisible(self._platform == PLATFORM_INSTAGRAM)
        self._refresh_uploaded_ig_checker_label()

        uploaded_hint = QLabel(
            "История успешных заливов на YouTube"
        )
        uploaded_hint.setObjectName("uploadedSectionHint")
        uploaded_hint.setWordWrap(True)
        uploaded_l.addWidget(uploaded_hint)
        self._uploaded_stats_status = QLabel("")
        self._uploaded_stats_status.setObjectName("uploadedStatsStatus")
        self._uploaded_stats_status.setWordWrap(True)
        uploaded_l.addWidget(self._uploaded_stats_status)

        self._uploaded_sort_mode: str = "views"
        body = QHBoxLayout()
        body.setSpacing(12)

        side = QFrame()
        side.setObjectName("uploadedSidePanel")
        side.setMinimumWidth(180)
        side.setMaximumWidth(300)
        side.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        side_l = QVBoxLayout(side)
        side_l.setSpacing(10)
        side_l.setContentsMargins(12, 12, 12, 12)

        itogo = QLabel("Итого")
        itogo.setObjectName("uploadedSideTitle")
        side_l.addWidget(itogo)

        def _stat_tile(title: str) -> tuple[QFrame, QLabel]:
            fr = QFrame()
            fr.setObjectName("uploadedStatTile")
            vl = QVBoxLayout(fr)
            vl.setContentsMargins(10, 8, 10, 8)
            vl.setSpacing(2)
            t = QLabel(title)
            t.setObjectName("uploadedStatTileTitle")
            val = QLabel("0")
            val.setObjectName("uploadedStatTileValue")
            vl.addWidget(t)
            vl.addWidget(val)
            return fr, val

        gstat = QGridLayout()
        gstat.setSpacing(8)
        t_v, self._uploaded_side_val_videos = _stat_tile("Видео")
        t_views, self._uploaded_side_val_views = _stat_tile("Просмотры")
        t_likes, self._uploaded_side_val_likes = _stat_tile("Лайки")
        t_com, self._uploaded_side_val_comments = _stat_tile("Комментарии")
        gstat.addWidget(t_v, 0, 0)
        gstat.addWidget(t_views, 0, 1)
        gstat.addWidget(t_likes, 1, 0)
        gstat.addWidget(t_com, 1, 1)
        side_l.addLayout(gstat)

        def _metric_row(label_text: str, value_obj_name: str) -> QLabel:
            row_w = QWidget()
            hl = QHBoxLayout(row_w)
            hl.setContentsMargins(0, 2, 0, 2)
            hl.setSpacing(8)
            nm = QLabel(label_text)
            nm.setObjectName("uploadedMetricName")
            hl.addWidget(nm, 1)
            v = QLabel("0")
            v.setObjectName(value_obj_name)
            hl.addWidget(v, 0, Qt.AlignmentFlag.AlignRight)
            side_l.addWidget(row_w)
            return v

        self._uploaded_side_val_zero = _metric_row("С 0 просмотров", "uploadedMetricYellow")
        self._uploaded_side_val_300 = _metric_row("300+ просмотров", "uploadedMetricGreen")
        self._uploaded_side_val_18 = _metric_row("С меткой 18+", "uploadedMetricRed")
        self._uploaded_side_val_ban = _metric_row("Забанено / недоступно", "uploadedMetricRed")

        del_btns_wrap = QWidget()
        del_btns_l = QVBoxLayout(del_btns_wrap)
        del_btns_l.setSpacing(6)
        del_btns_l.setContentsMargins(0, 6, 0, 0)
        self._btn_uploaded_delete_unavailable = QPushButton(
            "Удалить из базы: недоступные"
        )
        self._btn_uploaded_delete_unavailable.setObjectName("secondary")
        self._btn_uploaded_delete_unavailable.setToolTip(
            "Удалить записи с пометкой «недоступно» только среди роликов в текущем списке "
            "(выбранная сессия или все сессии). Запись в YouTube не трогается."
        )
        self._btn_uploaded_delete_unavailable.clicked.connect(
            self._on_uploaded_delete_unavailable_clicked
        )
        self._btn_uploaded_delete_18 = QPushButton("Удалить из базы: 18+")
        self._btn_uploaded_delete_18.setObjectName("secondary")
        self._btn_uploaded_delete_18.setToolTip(
            "Удалить записи, которые считаются 18+ (как в счётчике «С меткой 18+»), "
            "только в текущем списке. Запись в YouTube не трогается."
        )
        self._btn_uploaded_delete_18.clicked.connect(self._on_uploaded_delete_18_clicked)
        del_btns_l.addWidget(self._btn_uploaded_delete_unavailable)
        del_btns_l.addWidget(self._btn_uploaded_delete_18)
        side_l.addWidget(del_btns_wrap)

        self._uploaded_side_avg = QLabel("Среднее: —")
        self._uploaded_side_avg.setObjectName("uploadedSideAvg")
        self._uploaded_side_avg.setWordWrap(True)
        side_l.addWidget(self._uploaded_side_avg)

        side_l.addStretch()

        right = QWidget()
        right_l = QVBoxLayout(right)
        right_l.setSpacing(8)
        right_l.setContentsMargins(0, 0, 0, 0)
        sort_row = FlowLayout(hspacing=8, vspacing=6)
        sort_cap = QLabel("Сортировка:")
        sort_cap.setObjectName("uploadedSortCaption")
        sort_row.addWidget(sort_cap)
        self._btn_uploaded_sort_views = QPushButton("▼  Просмотры")
        self._btn_uploaded_sort_views.setObjectName("uploadedSortActive")
        self._btn_uploaded_sort_views.clicked.connect(
            partial(self._set_uploaded_sort_mode, "views")
        )
        self._btn_uploaded_sort_likes = QPushButton("♥  Лайки")
        self._btn_uploaded_sort_likes.setObjectName("uploadedSortInactive")
        self._btn_uploaded_sort_likes.clicked.connect(
            partial(self._set_uploaded_sort_mode, "likes")
        )
        sort_row.addWidget(self._btn_uploaded_sort_views)
        sort_row.addWidget(self._btn_uploaded_sort_likes)
        right_l.addLayout(sort_row)

        self._uploaded_list = QListWidget()
        self._uploaded_list.setObjectName("uploadedList")
        self._uploaded_list.setSpacing(4)
        self._uploaded_list.setAlternatingRowColors(False)
        self._uploaded_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._uploaded_list.setUniformItemSizes(True)
        self._uploaded_list.verticalScrollBar().valueChanged.connect(
            self._on_uploaded_list_scrolled
        )
        right_l.addWidget(self._uploaded_list, 1)

        body.addWidget(side, 0)
        body.addWidget(right, 1)
        uploaded_l.addLayout(body, 1)

        profiles = QWidget()
        profiles_l = QVBoxLayout(profiles)
        profiles_l.setSpacing(10)
        profiles_l.setContentsMargins(12, 12, 12, 12)
        self._profiles_title = QLabel("Профили антидетекта")
        self._profiles_title.setObjectName("title")
        self._profiles_hint = QLabel(
            "Отметьте квадратиками профили для залива."
        )
        self._profiles_hint.setObjectName("hint")
        self._profiles_hint.setWordWrap(True)

        profiles_header = QHBoxLayout()
        profiles_header.addWidget(self._profiles_title)
        profiles_header.addStretch()

        profiles_search_row = QHBoxLayout()
        profiles_search_row.setSpacing(8)
        self._dolphin_query = QLineEdit()
        self._dolphin_query.setPlaceholderText("Поиск по загруженным профилям…")
        self._btn_profiles_filter_tags = QPushButton("По тэгам")
        self._btn_profiles_filter_tags.setObjectName("secondary")
        self._btn_profiles_filter_tags.setAutoDefault(False)
        self._btn_profiles_filter_tags.setDefault(False)
        self._btn_profiles_filter_tags.setToolTip(
            "Отфильтровать список по выбранным тегам "
            "(все теги загруженных профилей, не только Zaliver)."
        )
        self._btn_profiles_filter_tags.clicked.connect(self._open_profiles_tag_filter_dialog)
        self._btn_profiles_refresh = QPushButton("Обновить")
        self._btn_profiles_refresh.setObjectName("secondary")
        self._btn_profiles_refresh.setAutoDefault(False)
        self._btn_profiles_refresh.setDefault(False)
        self._btn_profiles_refresh.clicked.connect(self._refresh_antydetect_profiles)
        profiles_search_row.addWidget(self._dolphin_query, 1)
        profiles_search_row.addWidget(self._btn_profiles_filter_tags)
        profiles_search_row.addWidget(self._btn_profiles_refresh)
        self._sync_profiles_tag_filter_button()

        profiles_actions_row = FlowLayout(hspacing=8, vspacing=8)
        self._btn_profiles_check_availability = QPushButton("Проверить доступность YouTube")
        self._btn_profiles_check_availability.setObjectName("secondary")
        self._btn_profiles_check_availability.setAutoDefault(False)
        self._btn_profiles_check_availability.setDefault(False)
        self._btn_profiles_check_availability.setToolTip(
            "Только для отмеченных профилей (квадратики): режим Headless из настроек, "
            "число параллельных браузеров — в разделе «Настройки». "
            "YouTube: Studio, создание канала и «Далее» при необходимости, "
            "ожидание URL studio.youtube.com/channel/{id} или channel-appeal. "
            "Instagram: открытие instagram.com и проверка, что аккаунт уже вошёл; "
            "после проверки профиль закрывается."
        )
        self._btn_profiles_check_availability.clicked.connect(
            self._start_profiles_availability_check
        )
        self._btn_profiles_register_accounts = QPushButton("Зарегать акки")
        self._btn_profiles_register_accounts.setObjectName("secondary")
        self._btn_profiles_register_accounts.setAutoDefault(False)
        self._btn_profiles_register_accounts.setDefault(False)
        self._btn_profiles_register_accounts.setToolTip(
            "Только для отмеченных профилей (Instagram): вход в Gmail, "
            "вторая вкладка instagram.com → «Создать новый аккаунт», "
            "заполнение формы; капча — расширение AntiCaptcha в антидетекте "
            "(ключ в Настройки антидетекта), иначе ручное ожидание; "
            "затем код из почты. "
            "Режим Headless из настроек; параллельность — в «Настройках»."
        )
        self._btn_profiles_register_accounts.clicked.connect(
            self._start_profiles_instagram_register
        )
        self._btn_profiles_connect_2fa = QPushButton("Подключить 2FA")
        self._btn_profiles_connect_2fa.setObjectName("secondary")
        self._btn_profiles_connect_2fa.setAutoDefault(False)
        self._btn_profiles_connect_2fa.setDefault(False)
        self._btn_profiles_connect_2fa.setToolTip(
            "Только для отмеченных профилей (Instagram): Accounts Center → "
            "Password and security → Two-factor authentication → "
            "при необходимости код из письма Meta (Gmail) → "
            "Authentication app; секрет сохраняется в inst_2fa, "
            "затем вводится OTP для подтверждения. "
            "Если 2FA уже включена — тоже успешный статус (секрет не перезаписываем). "
            "Режим Headless из настроек; параллельность — в «Настройках»."
        )
        self._btn_profiles_connect_2fa.clicked.connect(
            self._start_profiles_instagram_2fa
        )
        self._btn_profiles_import_accounts = QPushButton("Импортировать данные учёток")
        self._btn_profiles_import_accounts.setObjectName("secondary")
        self._btn_profiles_import_accounts.setAutoDefault(False)
        self._btn_profiles_import_accounts.setDefault(False)
        self._btn_profiles_import_accounts.setToolTip(
            "Загрузить логин, пароль и 2FA из вставленного текста или .txt "
            "в отмеченные профили своего антидетекта "
            "(сопоставление по порядку строк)."
        )
        self._btn_profiles_import_accounts.clicked.connect(
            self._open_profiles_accounts_import_dialog
        )
        self._btn_profiles_warmup = QPushButton("Прогрев")
        self._btn_profiles_warmup.setObjectName("secondary")
        self._btn_profiles_warmup.setAutoDefault(False)
        self._btn_profiles_warmup.setDefault(False)
        self._btn_profiles_warmup.setToolTip(
            "Только для отмеченных профилей: прогрев ленты Shorts (YouTube) "
            "или Reels (Instagram) — просмотр роликов, случайные лайки/подписки. "
            "Режим Headless из настроек; параллельность — в «Настройках»."
        )
        self._btn_profiles_warmup.clicked.connect(self._start_profiles_warmup)
        self._btn_profiles_promote = QPushButton("Продвижение")
        self._btn_profiles_promote.setObjectName("secondary")
        self._btn_profiles_promote.setAutoDefault(False)
        self._btn_profiles_promote.setDefault(False)
        self._btn_profiles_promote.setToolTip(
            "Только для отмеченных профилей. YouTube: Studio → опц. подписка "
            "на каналы → Shorts. Instagram: сессия → открыть каждый залитый "
            "рилс по ссылке → Подписаться/лайк/коммент на странице рилса "
            "(без захода в профиль). Headless и параллельность — в настройках."
        )
        self._btn_profiles_promote.clicked.connect(self._start_profiles_promote)
        self._btn_profiles_cookie_farm = QPushButton("Фарм Cookie")
        self._btn_profiles_cookie_farm.setObjectName("secondary")
        self._btn_profiles_cookie_farm.setAutoDefault(False)
        self._btn_profiles_cookie_farm.setDefault(False)
        self._btn_profiles_cookie_farm.setToolTip(
            "Только для отмеченных профилей: по очереди открывает сайты из списка "
            "и медленно прокручивает страницу заданное время. "
            "Режим Headless из настроек; параллельность — в «Настройках»."
        )
        self._btn_profiles_cookie_farm.clicked.connect(self._start_profiles_cookie_farm)
        self._btn_profiles_clear_zaliver_tags = QPushButton("Очистить теги залива")
        self._btn_profiles_clear_zaliver_tags.setObjectName("secondary")
        self._btn_profiles_clear_zaliver_tags.setAutoDefault(False)
        self._btn_profiles_clear_zaliver_tags.setDefault(False)
        self._btn_profiles_clear_zaliver_tags.setToolTip(
            "С отмеченных профилей снимает служебные теги Zaliver "
            "(ошибки залива, проверки Studio, смены аватарки/названия, прогрева, "
            "продвижения, фарма Cookie, регистрации/2FA Instagram "
            "и заполнения канала и т.д.). Только свой антидетект."
        )
        self._btn_profiles_clear_zaliver_tags.clicked.connect(
            self._start_clear_zaliver_profile_tags
        )
        profiles_actions_row.addWidget(self._btn_profiles_warmup)
        profiles_actions_row.addWidget(self._btn_profiles_promote)
        profiles_actions_row.addWidget(self._btn_profiles_cookie_farm)
        profiles_actions_row.addWidget(self._btn_profiles_check_availability)
        profiles_actions_row.addWidget(self._btn_profiles_register_accounts)
        profiles_actions_row.addWidget(self._btn_profiles_connect_2fa)
        profiles_actions_row.addWidget(self._btn_profiles_import_accounts)
        self._sync_profiles_platform_actions_visibility()

        self._profiles_status = QLabel("")
        self._profiles_status.setObjectName("hint")
        self._profiles_status.setWordWrap(True)

        self._profiles_list = QListWidget()
        self._profiles_list.setObjectName("profilesList")
        self._profiles_list.setSpacing(4)
        self._profiles_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._profiles_list.setMouseTracking(True)
        self._profiles_list.viewport().setMouseTracking(True)
        self._dolphin_query.textChanged.connect(self._schedule_profiles_filter)
        self._dolphin_query.returnPressed.connect(self._refresh_antydetect_profiles)

        self._profiles_interaction = ProfilesListInteraction(
            self._profiles_list,
            self._upload_store,
            on_upload_pause_click=self._ask_reset_upload_cooldown_for_profile,
            on_account_data_click=self._open_profile_account_data_dialog,
            on_gmail_data_click=self._open_profile_gmail_data_dialog,
            on_preview_click=self._open_profile_cdp_preview,
            upload_pause=self._upload_pause_between_uploads(),
        )
        list_sel_row, self._lbl_checked_profiles_count = self._build_profiles_selection_toolbar(
            self,
            self._profiles_interaction,
            on_select_filter=self._select_profiles_checked_filter,
            on_clear=self._clear_profiles_checked_selection,
        )
        list_sel_row.addWidget(self._btn_profiles_clear_zaliver_tags)
        self._profiles_interaction.selection_changed.connect(
            self._on_profiles_checked_selection_changed
        )

        profiles_l.addLayout(profiles_header)
        profiles_l.addLayout(profiles_search_row)
        profiles_l.addLayout(profiles_actions_row)
        profiles_l.addWidget(self._profiles_hint)
        profiles_l.addLayout(list_sel_row)
        profiles_l.addWidget(self._profiles_status)
        profiles_l.addWidget(self._profiles_list, 1)

        self._channel_edit_tab = ChannelEditTabPane(
            platform=self._platform,
            recent_channel_names=self._upload_store.list_recent_channel_name_fields(
                platform=self._platform
            ),
            recent_channel_descriptions=self._upload_store.list_recent_channel_descriptions(
                platform=self._platform
            ),
            recent_link_titles=self._upload_store.list_recent_channel_link_titles(
                platform=self._platform
            ),
            recent_link_urls=self._upload_store.list_recent_channel_link_urls(
                platform=self._platform
            ),
            recent_video_default_titles=self._upload_store.list_recent_video_default_title_fields(
                platform=self._platform
            ),
            ai_generate_fn=self._on_ai_magic_generate,
        )
        self._channel_edit_tab.select_profiles_requested.connect(
            self._start_channel_setup_from_tab
        )

        self._ai_tab = AiTabPane(
            self, settings=self._settings, platform=self._platform
        )

        def _compact_settings_vbox(box: QGroupBox) -> QVBoxLayout:
            box.setObjectName("settingsSection")
            lay = QVBoxLayout(box)
            lay.setSpacing(4)
            lay.setContentsMargins(6, 4, 6, 4)
            return lay

        def _compact_settings_grid(box: QGroupBox) -> QGridLayout:
            box.setObjectName("settingsSection")
            lay = QGridLayout(box)
            lay.setHorizontalSpacing(8)
            lay.setVerticalSpacing(4)
            lay.setContentsMargins(6, 4, 6, 4)
            return lay

        def _settings_save_row(btn: QPushButton) -> QWidget:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(0)
            row.addStretch()
            row.addWidget(btn)
            wrap = QWidget()
            wrap.setLayout(row)
            return wrap

        settings = QWidget()
        settings_outer = QVBoxLayout(settings)
        settings_outer.setSpacing(0)
        settings_outer.setContentsMargins(0, 0, 0, 0)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        settings_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        settings_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        settings_scroll.setMinimumWidth(0)
        settings_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        settings_inner = QWidget()
        settings_inner.setMinimumWidth(0)
        settings_inner.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        settings_l = QVBoxLayout(settings_inner)
        settings_l.setSpacing(6)
        settings_l.setContentsMargins(12, 8, 12, 8)
        settings_title = QLabel("Настройки")
        settings_title.setObjectName("title")
        settings_hint = QLabel(
            "Настройки своего антидетекта (локальный HTTP API) "
            "и параметры обработки видео (GPU, потоки, ffmpeg)."
            if self._platform != PLATFORM_YT_INST
            else "Общие настройки антидетекта и ИИ, плюс разделы YouTube и Instagram "
            "(параметры берутся из настроек соответствующих платформ). "
            "Параметры обработки видео (GPU, потоки, ffmpeg) — общие для уникализации и нарезки."
        )
        settings_hint.setObjectName("hint")
        settings_hint.setWordWrap(True)

        self._gb_stats_username = QGroupBox("Имя пользователя")
        gsu = _compact_settings_vbox(self._gb_stats_username)
        self._stats_server_username = QLineEdit()
        self._btn_save_stats_username = QPushButton("Сохранить")
        self._btn_save_stats_username.setObjectName("secondary")
        self._btn_save_stats_username.setAutoDefault(False)
        self._btn_save_stats_username.setDefault(False)
        self._btn_save_stats_username.clicked.connect(
            self._save_stats_server_username_settings
        )
        gsu.addWidget(self._stats_server_username)
        gsu.addWidget(_settings_save_row(self._btn_save_stats_username))

        self._dolphin_headless = QCheckBox("Headless (без окна браузера)")
        self._dolphin_headless.setChecked(True)
        self._dolphin_headless.setToolTip(
            "Если включено — профиль запускается без окна браузера (headless)."
        )

        self._gb_max_concurrent_browsers = QGroupBox("Параллельные браузеры")
        gmc = _compact_settings_vbox(self._gb_max_concurrent_browsers)
        browsers_hint = QLabel(
            "Максимум одновременно открытых браузеров при заливке, проверке Studio, "
            "редактировании каналов, прогреве, регистрации Instagram и подключении 2FA."
        )
        browsers_hint.setObjectName("hint")
        browsers_hint.setWordWrap(True)
        gmc.addWidget(browsers_hint)
        browsers_row = QHBoxLayout()
        browsers_row.setContentsMargins(0, 0, 0, 0)
        browsers_row.setSpacing(8)
        browsers_row.addWidget(QLabel("Одновременно:"))
        self._max_concurrent_browsers_slider = SmoothSlider(Qt.Orientation.Horizontal)
        self._max_concurrent_browsers_slider.setMinimum(MAX_CONCURRENT_BROWSERS_MIN)
        self._max_concurrent_browsers_slider.setMaximum(MAX_CONCURRENT_BROWSERS_MAX)
        self._max_concurrent_browsers_slider.setValue(
            max_concurrent_browsers_from_settings(self._settings)
        )
        self._max_concurrent_browsers_label = QLabel()
        self._update_max_concurrent_browsers_label(
            self._max_concurrent_browsers_slider.value()
        )
        self._max_concurrent_browsers_slider.valueChanged.connect(
            self._update_max_concurrent_browsers_label
        )
        self._max_concurrent_browsers_slider.valueChanged.connect(
            lambda *_: self._save_max_concurrent_browsers_setting()
        )
        browsers_row.addWidget(self._max_concurrent_browsers_slider, 1)
        browsers_row.addWidget(self._max_concurrent_browsers_label)
        w_browsers_row = QWidget()
        w_browsers_row.setLayout(browsers_row)
        gmc.addWidget(w_browsers_row)

        self._gb_antydetect_local = QGroupBox("Локальный HTTP API")
        gl = _compact_settings_grid(self._gb_antydetect_local)
        self._local_api_base_url = QLineEdit()
        self._local_api_base_url.setPlaceholderText(DEFAULT_LOCAL_API_BASE_URL)
        self._local_api_base_url.setToolTip(
            "Корень HTTP-сервиса (без завершающего слэша), как в OpenAPI: /profiles, /health, …"
        )
        gl.addWidget(QLabel("Базовый URL:"), 0, 0)
        gl.addWidget(self._local_api_base_url, 0, 1)
        self._local_api_token = QLineEdit()
        self._local_api_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._local_api_token.setPlaceholderText("secret (для serve)")
        self._local_api_token.setToolTip(
            "Bearer-токен для режима antidetect serve. "
            "Пусто — без Authorization (десктоп Qt без auth). "
            "По умолчанию у serve: secret."
        )
        gl.addWidget(QLabel("API token:"), 1, 0)
        gl.addWidget(self._local_api_token, 1, 1)

        self._btn_save_antydetect = QPushButton("Сохранить")
        self._btn_save_antydetect.setObjectName("secondary")
        self._btn_save_antydetect.clicked.connect(self._save_antydetect_settings)

        self._settings_status = QLabel("")
        self._settings_status.setObjectName("hint")
        self._settings_status.setWordWrap(True)

        self._antydetect_save_row = QWidget()
        antydetect_save_l = QVBoxLayout(self._antydetect_save_row)
        antydetect_save_l.setContentsMargins(0, 0, 0, 0)
        antydetect_save_l.setSpacing(4)
        antydetect_save_l.addWidget(_settings_save_row(self._btn_save_antydetect))
        antydetect_save_l.addWidget(self._settings_status)

        gb_yt = QGroupBox("YouTube")
        self._gb_youtube_settings = gb_yt
        gy = _compact_settings_grid(gb_yt)
        self._youtube_api_key = QLineEdit()
        self._youtube_api_key.setPlaceholderText("YOUTUBE_API_KEY (YouTube Data API v3)…")
        self._youtube_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._youtube_api_key.setToolTip(
            "Ключ для YouTube Data API v3. Нужен для стабильного получения просмотров/лайков/комментариев.\n"
            "Хранится локально в настройках приложения (QSettings)."
        )
        self._youtube_show_key = QCheckBox("Показать ключ")
        self._youtube_show_key.stateChanged.connect(self._on_youtube_show_key_changed)
        self._youtube_search_oldest = QCheckBox("Искать старый канал")
        self._youtube_search_oldest.setChecked(False)
        self._youtube_search_oldest.setToolTip(
            "Если включено — перед заливом и проверкой Studio ищется самый старый канал "
            "или проверяется, что открыт сохранённый yt_oldest_name.\n"
            "Если выключено — используется текущий открытый канал без переключения."
        )
        self._youtube_search_oldest.stateChanged.connect(
            self._on_youtube_search_oldest_changed
        )
        yt_pause_tip = (
            "Минимальная пауза между успешными заливами с одного профиля YouTube.\n"
            "0 ч 0 мин — браузер не закрывается между роликами на том же профиле;\n"
            "следующее видео с того же профиля можно заливать сразу."
        )
        self._youtube_upload_pause_hours = QSpinBox()
        self._youtube_upload_pause_hours.setRange(0, 168)
        self._youtube_upload_pause_hours.setSingleStep(1)
        self._youtube_upload_pause_hours.setValue(3)
        self._youtube_upload_pause_hours.setSuffix(" ч")
        self._youtube_upload_pause_hours.setToolTip(yt_pause_tip)
        self._youtube_upload_pause_minutes = QSpinBox()
        self._youtube_upload_pause_minutes.setRange(0, 59)
        self._youtube_upload_pause_minutes.setSingleStep(1)
        self._youtube_upload_pause_minutes.setValue(0)
        self._youtube_upload_pause_minutes.setSuffix(" мин")
        self._youtube_upload_pause_minutes.setToolTip(yt_pause_tip)
        yt_pause_row = QHBoxLayout()
        yt_pause_row.setContentsMargins(0, 0, 0, 0)
        yt_pause_row.setSpacing(8)
        yt_pause_row.addWidget(self._youtube_upload_pause_hours)
        yt_pause_row.addWidget(self._youtube_upload_pause_minutes)
        yt_pause_row.addStretch(1)
        yt_pause_wrap = QWidget()
        yt_pause_wrap.setLayout(yt_pause_row)
        self._youtube_pause_label = QLabel("Пауза между видео:")
        self._youtube_pause_label.setToolTip(yt_pause_tip)
        self._youtube_pause_wrap = yt_pause_wrap
        yt_pause_visible = self._platform == PLATFORM_YOUTUBE
        self._youtube_pause_label.setVisible(yt_pause_visible)
        self._youtube_pause_wrap.setVisible(yt_pause_visible)
        self._btn_save_youtube = QPushButton("Сохранить")
        self._btn_save_youtube.setObjectName("secondary")
        self._btn_save_youtube.clicked.connect(self._save_youtube_settings)
        self._youtube_settings_status = QLabel("")
        self._youtube_settings_status.setObjectName("hint")
        self._youtube_settings_status.setWordWrap(True)

        gy.addWidget(QLabel("API key (для статистики):"), 0, 0)
        gy.addWidget(self._youtube_api_key, 0, 1)
        gy.addWidget(self._youtube_show_key, 1, 0, 1, 2)
        gy.addWidget(self._youtube_search_oldest, 2, 0, 1, 2)
        gy.addWidget(self._youtube_pause_label, 3, 0)
        gy.addWidget(self._youtube_pause_wrap, 3, 1)
        gy.addWidget(_settings_save_row(self._btn_save_youtube), 4, 0, 1, 2)
        gy.addWidget(self._youtube_settings_status, 5, 0, 1, 2)
        # В Instagram API-ключ Data API не используется (статистика через сессию профиля).
        # Yt+Inst — оба раздела: YouTube и Instagram.
        gb_yt.setVisible(self._platform != PLATFORM_INSTAGRAM)

        gb_ig = QGroupBox("Instagram")
        self._gb_instagram_settings = gb_ig
        gi = _compact_settings_grid(gb_ig)
        pause_tip = (
            "Минимальная пауза между успешными заливами с одного профиля Instagram.\n"
            "0 ч 0 мин — браузер не закрывается между роликами на том же профиле;\n"
            "появляется настройка «Вкладок на профиль» для параллельного залива.\n"
            "В режиме Yt+Inst используется эта же пауза Instagram "
            "(0 — следующий залив на тот же профиль без закрытия браузера)."
        )
        self._instagram_upload_pause_hours = QSpinBox()
        self._instagram_upload_pause_hours.setRange(0, 168)
        self._instagram_upload_pause_hours.setSingleStep(1)
        self._instagram_upload_pause_hours.setValue(3)
        self._instagram_upload_pause_hours.setSuffix(" ч")
        self._instagram_upload_pause_hours.setToolTip(pause_tip)
        self._instagram_upload_pause_minutes = QSpinBox()
        self._instagram_upload_pause_minutes.setRange(0, 59)
        self._instagram_upload_pause_minutes.setSingleStep(1)
        self._instagram_upload_pause_minutes.setValue(0)
        self._instagram_upload_pause_minutes.setSuffix(" мин")
        self._instagram_upload_pause_minutes.setToolTip(pause_tip)
        pause_row = QHBoxLayout()
        pause_row.setContentsMargins(0, 0, 0, 0)
        pause_row.setSpacing(8)
        pause_row.addWidget(self._instagram_upload_pause_hours)
        pause_row.addWidget(self._instagram_upload_pause_minutes)
        pause_row.addStretch(1)
        pause_wrap = QWidget()
        pause_wrap.setLayout(pause_row)

        tabs_tip = (
            "Сколько вкладок Instagram открывать в одном профиле при паузе 0,\n"
            "чтобы заливать ролики параллельно в одном окне браузера.\n"
            f"Диапазон {INSTAGRAM_TABS_PER_PROFILE_MIN}–"
            f"{INSTAGRAM_TABS_PER_PROFILE_MAX}, по умолчанию "
            f"{DEFAULT_INSTAGRAM_TABS_PER_PROFILE}."
        )
        self._instagram_tabs_per_profile_label = QLabel("Вкладок на профиль:")
        self._instagram_tabs_per_profile_label.setToolTip(tabs_tip)
        self._instagram_tabs_per_profile = QSpinBox()
        self._instagram_tabs_per_profile.setRange(
            INSTAGRAM_TABS_PER_PROFILE_MIN, INSTAGRAM_TABS_PER_PROFILE_MAX
        )
        self._instagram_tabs_per_profile.setSingleStep(1)
        self._instagram_tabs_per_profile.setValue(DEFAULT_INSTAGRAM_TABS_PER_PROFILE)
        self._instagram_tabs_per_profile.setToolTip(tabs_tip)
        self._instagram_tabs_per_profile_label.setVisible(False)
        self._instagram_tabs_per_profile.setVisible(False)
        self._instagram_upload_pause_hours.valueChanged.connect(
            self._sync_instagram_tabs_setting_visibility
        )
        self._instagram_upload_pause_minutes.valueChanged.connect(
            self._sync_instagram_tabs_setting_visibility
        )

        crop_tip = (
            "Как в Instagram (Select Crop): Оригинал, 1:1, 9:16 или 16:9.\n"
            "По умолчанию — Оригинал (без принудительной обрезки)."
        )
        self._instagram_crop_aspect_label = QLabel("Обрезка:")
        self._instagram_crop_aspect_label.setToolTip(crop_tip)
        self._instagram_crop_aspect = QComboBox()
        self._instagram_crop_aspect.addItem("Оригинал", "original")
        self._instagram_crop_aspect.addItem("1:1", "1:1")
        self._instagram_crop_aspect.addItem("9:16", "9:16")
        self._instagram_crop_aspect.addItem("16:9", "16:9")
        self._instagram_crop_aspect.setToolTip(crop_tip)

        self._btn_save_instagram = QPushButton("Сохранить")
        self._btn_save_instagram.setObjectName("secondary")
        self._btn_save_instagram.clicked.connect(self._save_instagram_settings)
        self._instagram_settings_status = QLabel("")
        self._instagram_settings_status.setObjectName("hint")
        self._instagram_settings_status.setWordWrap(True)
        gi.addWidget(QLabel("Пауза между видео:"), 0, 0)
        gi.addWidget(pause_wrap, 0, 1)
        gi.addWidget(self._instagram_crop_aspect_label, 1, 0)
        gi.addWidget(self._instagram_crop_aspect, 1, 1)
        gi.addWidget(self._instagram_tabs_per_profile_label, 2, 0)
        gi.addWidget(self._instagram_tabs_per_profile, 2, 1)
        gi.addWidget(_settings_save_row(self._btn_save_instagram), 3, 0, 1, 2)
        gi.addWidget(self._instagram_settings_status, 4, 0, 1, 2)
        gb_ig.setVisible(
            self._platform in (PLATFORM_INSTAGRAM, PLATFORM_YT_INST)
        )

        gb_ai = QGroupBox("ИИ")
        gai = _compact_settings_grid(gb_ai)
        ai_hint = QLabel(
            "OpenAI-совместимый API (например OpenAI, OpenRouter, локальный сервер). "
            "Базовый URL без завершающего слэша, обычно с суффиксом /v1."
        )
        ai_hint.setObjectName("hint")
        ai_hint.setWordWrap(True)
        self._ai_base_url = QLineEdit()
        self._ai_base_url.setPlaceholderText("https://api.openai.com/v1")
        self._ai_base_url.setToolTip(
            "Базовый URL эндпоинта OpenAI-совместимого сервиса "
            "(например https://api.openai.com/v1 или http://127.0.0.1:1234/v1)."
        )
        self._ai_api_key = QLineEdit()
        self._ai_api_key.setPlaceholderText("API key…")
        self._ai_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._ai_api_key.setToolTip(
            "Ключ API. Хранится локально в настройках приложения (QSettings)."
        )
        self._ai_show_key = QCheckBox("Показать ключ")
        self._ai_show_key.stateChanged.connect(self._on_ai_show_key_changed)
        self._ai_model = QLineEdit()
        self._ai_model.setPlaceholderText("gpt-4o-mini")
        self._ai_model.setToolTip("Название модели, как ожидает выбранный сервис.")
        self._btn_save_ai = QPushButton("Сохранить")
        self._btn_save_ai.setObjectName("secondary")
        self._btn_save_ai.clicked.connect(self._save_ai_settings)
        self._ai_settings_status = QLabel("")
        self._ai_settings_status.setObjectName("hint")
        self._ai_settings_status.setWordWrap(True)

        gai.addWidget(ai_hint, 0, 0, 1, 2)
        gai.addWidget(QLabel("URL эндпоинта:"), 1, 0)
        gai.addWidget(self._ai_base_url, 1, 1)
        gai.addWidget(QLabel("API key:"), 2, 0)
        gai.addWidget(self._ai_api_key, 2, 1)
        gai.addWidget(self._ai_show_key, 3, 0, 1, 2)
        gai.addWidget(QLabel("Модель:"), 4, 0)
        gai.addWidget(self._ai_model, 4, 1)
        gai.addWidget(_settings_save_row(self._btn_save_ai), 5, 0, 1, 2)
        gai.addWidget(self._ai_settings_status, 6, 0, 1, 2)

        self._gb_processing = QGroupBox("Обработка")
        gp = _compact_settings_grid(self._gb_processing)
        proc_hint = QLabel(
            "Общие параметры уникализации и нарезки. Обработка через ffmpeg "
            "(фильтры + кодирование). Несколько роликов — параллельно по файлам; "
            "длинный ролик режется на части. Нужны ffmpeg и ffprobe в PATH. "
            "Результат — MP4 (H.264 + AAC)."
        )
        proc_hint.setObjectName("hint")
        proc_hint.setWordWrap(True)
        gp.addWidget(proc_hint, 0, 0, 1, 2)

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
        gp.addWidget(self._ffmpeg_row, 1, 0, 1, 2)

        self.use_gpu = ToggleSwitch(
            "GPU при обработке кадров (декод, фильтры, кодирование)"
        )
        self.use_gpu.setChecked(
            bool(self._settings.value("use_gpu_enabled", False, type=bool))
        )
        self.use_gpu.toggled.connect(self._save_folder_settings)
        self.use_gpu_finalize = ToggleSwitch(
            "GPU при склейке и mux звука (concat, ускорение, фон/текст)"
        )
        self.use_gpu_finalize.setChecked(
            bool(self._settings.value("use_gpu_finalize_enabled", False, type=bool))
        )
        self.use_gpu_finalize.toggled.connect(self._save_folder_settings)
        gpu_hint = QLabel(
            "Независимо друг от друга. Можно кадры на CPU, а склейку на GPU (NVENC/QSV/AMF)."
        )
        gpu_hint.setObjectName("hint")
        gpu_hint.setWordWrap(True)
        gp.addWidget(self.use_gpu, 2, 0, 1, 2)
        gp.addWidget(self.use_gpu_finalize, 3, 0, 1, 2)
        gp.addWidget(gpu_hint, 4, 0, 1, 2)

        self.slice_fps_mode = QComboBox()
        self.slice_fps_mode.addItem("30 fps", "30")
        self.slice_fps_mode.addItem("60 fps", "60")
        self.slice_fps_mode.setToolTip(
            "Частота кадров итогового ролика при нарезке."
        )
        fps_mode = str(
            self._settings.value("slice/fps_mode", DEFAULT_SLICE_FPS_MODE, type=str)
            or DEFAULT_SLICE_FPS_MODE
        )
        if fps_mode.strip().lower() in ("auto", "авто"):
            fps_mode = DEFAULT_SLICE_FPS_MODE
        fps_idx = self.slice_fps_mode.findData(fps_mode)
        self.slice_fps_mode.setCurrentIndex(fps_idx if fps_idx >= 0 else 0)
        self.slice_fps_mode.currentIndexChanged.connect(
            lambda *_: self._save_folder_settings()
        )
        fps_hint = QLabel(
            "Только для нарезки. 60 fps вдвое медленнее рендера; "
            "для Shorts/Reels обычно достаточно 30."
        )
        fps_hint.setObjectName("hint")
        fps_hint.setWordWrap(True)
        gp.addWidget(QLabel("FPS нарезки:"), 5, 0)
        gp.addWidget(self.slice_fps_mode, 5, 1)
        gp.addWidget(fps_hint, 6, 0, 1, 2)

        self.thread_slider = SmoothSlider(Qt.Orientation.Horizontal)
        self.thread_slider.setMinimum(1)
        # Максимум слайдера — число доступных логических потоков CPU.
        self.thread_slider.setMaximum(_max_worker_slider())
        self.thread_slider.setValue(_default_workers())
        self.thread_label = QLabel()
        self._update_thread_label(self.thread_slider.value())
        self.thread_slider.valueChanged.connect(self._update_thread_label)
        gp.addWidget(QLabel("Потоков процессов:"), 7, 0, Qt.AlignmentFlag.AlignVCenter)
        thr_row = QHBoxLayout()
        thr_row.setSpacing(8)
        thr_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        thr_row.addWidget(self.thread_slider, 1, Qt.AlignmentFlag.AlignVCenter)
        thr_row.addWidget(self.thread_label, 0, Qt.AlignmentFlag.AlignVCenter)
        w_thr = QWidget()
        w_thr.setLayout(thr_row)
        gp.addWidget(w_thr, 7, 1, Qt.AlignmentFlag.AlignVCenter)

        settings_l.addWidget(settings_title)
        settings_l.addWidget(settings_hint)
        settings_l.addWidget(self._gb_stats_username)
        settings_l.addWidget(self._gb_processing)
        settings_l.addWidget(self._dolphin_headless)
        settings_l.addWidget(self._gb_max_concurrent_browsers)
        settings_l.addWidget(self._gb_antydetect_local)
        settings_l.addWidget(self._antydetect_save_row)
        settings_l.addWidget(gb_yt)
        settings_l.addWidget(gb_ig)
        settings_l.addWidget(gb_ai)
        settings_l.addStretch()
        settings_scroll.setWidget(settings_inner)
        settings_outer.addWidget(settings_scroll, 1)

        self._stack = QStackedWidget()
        self._stack.addWidget(home)
        self._stack.addWidget(self._slice_tab)
        self._stack.addWidget(self._stitch_tab)
        self._stack.addWidget(ready)
        self._stack.addWidget(uploaded)
        self._stack.addWidget(profiles)
        self._stack.addWidget(self._channel_edit_tab)
        self._stack.addWidget(self._ai_tab)
        self._stack.addWidget(settings)

        self._nav = QListWidget()
        self._nav.setObjectName("sideNav")
        self._nav.setMinimumWidth(140)
        self._nav.setMaximumWidth(210)
        self._nav.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self._nav.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._nav.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._nav.addItems(
            [
                "Уникализация",
                "Нарезка",
                "Склейка",
                "Готовые видео",
                "Залитые видео",
                "Профили",
                "Редактирование каналов",
                "ИИ",
                "Настройки",
            ]
        )
        # Yt+Inst: уникализация, нарезка, склейка и настройки (YT+IG разделы).
        if self._platform == PLATFORM_YT_INST:
            for i in range(3, self._nav.count()):
                item = self._nav.item(i)
                if item is None:
                    continue
                # 8 = Настройки
                item.setHidden(i != 8)
        self._nav.setCurrentRow(0)
        self._nav.currentRowChanged.connect(self._on_nav_row_changed)

        self._btn_back_modes = QPushButton("← Выбор платформы")
        self._btn_back_modes.setObjectName("sideNavBack")
        self._btn_back_modes.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_back_modes.setToolTip("Вернуться к выбору режима")
        self._btn_back_modes.clicked.connect(self.back_to_modes.emit)
        self._btn_back_modes.setVisible(bool(getattr(self, "_embedded", False)))

        nav_col = QVBoxLayout()
        nav_col.setSpacing(8)
        nav_col.setContentsMargins(0, 0, 0, 0)
        nav_col.addWidget(self._nav, 1)
        nav_col.addWidget(self._btn_back_modes, 0)

        outer = QHBoxLayout(self)
        outer.setSpacing(8)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.addLayout(nav_col, 0)
        outer.addWidget(self._stack, 1)
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

    def _on_nav_row_changed(self, row: int) -> None:
        self._stack.setCurrentIndex(max(0, min(row, self._stack.count() - 1)))
        if row == 3:
            self._refresh_ready_list()
        if row == 4:
            self._refresh_uploaded_list()
        if row == 5:
            self._refresh_antydetect_profiles()
        if row == 6:
            self._sync_channel_edit_tab()

    def _sorted_uploaded_videos(
        self, videos: list[UploadedVideo], mode: str
    ) -> list[UploadedVideo]:
        m = (mode or "views").strip().lower()

        def _tiebreak(v: UploadedVideo) -> tuple[float, int]:
            return (-uploaded_at_sort_ts(v.uploaded_at), -int(v.id))

        if m == "likes":

            def key_l(v: UploadedVideo) -> tuple:
                if v.stats_unavailable or v.like_count is None:
                    return (1, 0, *_tiebreak(v))
                return (0, -int(v.like_count), *_tiebreak(v))

            return sorted(videos, key=key_l)

        def key_v(v: UploadedVideo) -> tuple:
            if v.stats_unavailable or v.view_count is None:
                return (1, 0, *_tiebreak(v))
            return (0, -int(v.view_count), *_tiebreak(v))

        return sorted(videos, key=key_v)

    def _uploaded_session_filter_scope_label(self) -> str:
        if not hasattr(self, "_uploaded_session_filter"):
            return "все сессии"
        try:
            sid = int(self._uploaded_session_filter.currentData() or 0)
        except Exception:
            sid = 0
        if sid > 0:
            return f"только сессия №{sid}"
        return "все сессии"

    def _uploaded_videos_for_current_filter_sorted(self) -> list[UploadedVideo]:
        """Тот же набор роликов, что строится для списка залитых (фильтр сессии + сортировка)."""
        only_session_id = 0
        try:
            if hasattr(self, "_uploaded_session_filter"):
                only_session_id = int(self._uploaded_session_filter.currentData() or 0)
        except Exception:
            only_session_id = 0
        try:
            sessions = self._upload_store.list_sessions(limit=400, platform=self._platform)
        except Exception:
            sessions = []
        ids = [int(s.id) for s in sessions]
        m: dict[int, list[UploadedVideo]] = {}
        try:
            if ids:
                raw = self._upload_store.list_uploaded_videos_for_sessions(
                    ids, platform=self._platform
                )
                m = raw if isinstance(raw, dict) else {}
        except Exception:
            m = {}
        flat: list[UploadedVideo] = []
        if only_session_id > 0:
            flat = list(m.get(int(only_session_id), []) or [])
        else:
            for s in sessions:
                flat.extend(m.get(int(s.id), []) or [])
            flat.sort(
                key=lambda v: (uploaded_at_sort_ts(v.uploaded_at), int(v.id)),
                reverse=True,
            )
        mode = getattr(self, "_uploaded_sort_mode", "views")
        return self._sorted_uploaded_videos(flat, mode)

    def _on_uploaded_delete_unavailable_clicked(self) -> None:
        flat = self._uploaded_videos_for_current_filter_sorted()
        targets = [v for v in flat if v.stats_unavailable]
        if not targets:
            QMessageBox.information(
                self,
                "Zaliver",
                "В текущем списке нет записей с недоступной статистикой.",
            )
            return
        scope = self._uploaded_session_filter_scope_label()
        n = len(targets)
        ans = QMessageBox.question(
            self,
            "Zaliver",
            f"Удалить из локальной базы {n} залитых видео "
            f"с недоступной статистикой?\n\n"
            f"Область: {scope}.\n"
            "Ролики на YouTube не удаляются — только строки в этой программе.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            deleted = self._upload_store.delete_uploaded_videos_by_ids(
                v.id for v in targets
            )
        except Exception as e:
            QMessageBox.warning(self, "Zaliver", f"Не удалось удалить из базы:\n{e!r}")
            return
        QMessageBox.information(
            self,
            "Zaliver",
            f"Удалено записей: {deleted}.",
        )
        self._refresh_uploaded_list()

    def _on_uploaded_delete_18_clicked(self) -> None:
        flat = self._uploaded_videos_for_current_filter_sorted()
        targets = [v for v in flat if _uploaded_counts_as_18_plus_side(v)]
        if not targets:
            QMessageBox.information(
                self,
                "Zaliver",
                "В текущем списке нет записей, попадающих под «18+».",
            )
            return
        scope = self._uploaded_session_filter_scope_label()
        n = len(targets)
        ans = QMessageBox.question(
            self,
            "Zaliver",
            f"Удалить из локальной базы {n} залитых видео "
            f"с меткой 18+ (текст и/или возрастное ограничение по API)?\n\n"
            f"Область: {scope}.\n"
            "Ролики на YouTube не удаляются, а только строки в этой программе.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            deleted = self._upload_store.delete_uploaded_videos_by_ids(
                v.id for v in targets
            )
        except Exception as e:
            QMessageBox.warning(self, "Zaliver", f"Не удалось удалить из базы:\n{e!r}")
            return
        QMessageBox.information(
            self,
            "Zaliver",
            f"Удалено записей: {deleted}.",
        )
        self._refresh_uploaded_list()

    def _set_uploaded_sort_mode(self, mode: str) -> None:
        m = (mode or "views").strip().lower()
        if m not in ("views", "likes"):
            return
        if getattr(self, "_uploaded_sort_mode", "views") == m:
            return
        self._uploaded_sort_mode = m
        self._sync_uploaded_sort_buttons()
        self._refresh_uploaded_list()

    def _sync_uploaded_sort_buttons(self) -> None:
        if not hasattr(self, "_btn_uploaded_sort_views"):
            return
        v_first = self._uploaded_sort_mode == "views"
        self._btn_uploaded_sort_views.setObjectName(
            "uploadedSortActive" if v_first else "uploadedSortInactive"
        )
        self._btn_uploaded_sort_likes.setObjectName(
            "uploadedSortInactive" if v_first else "uploadedSortActive"
        )
        for b in (self._btn_uploaded_sort_views, self._btn_uploaded_sort_likes):
            st = b.style()
            if st is not None:
                st.unpolish(b)
                st.polish(b)

    def _update_uploaded_side_panel(self, videos: list[UploadedVideo]) -> None:
        if not hasattr(self, "_uploaded_side_val_videos"):
            return
        n = len(videos)
        self._uploaded_side_val_videos.setText(str(n))
        counted = [v for v in videos if not v.stats_unavailable]
        total_views = _sum_optional_int([v.view_count for v in counted])
        total_likes = _sum_optional_int([v.like_count for v in counted])
        total_comments = _sum_optional_int([v.comment_count for v in counted])
        self._uploaded_side_val_views.setText(_format_int_compact(total_views))
        self._uploaded_side_val_likes.setText(_format_int_compact(total_likes))
        self._uploaded_side_val_comments.setText(_format_int_compact(total_comments))

        n_zero = sum(
            1
            for v in counted
            if v.view_count is not None and int(v.view_count) == 0
        )
        n_300 = sum(
            1
            for v in counted
            if v.view_count is not None and int(v.view_count) >= 300
        )
        n_18 = sum(1 for v in videos if _uploaded_counts_as_18_plus_side(v))
        n_ban = sum(1 for v in videos if v.stats_unavailable)
        self._uploaded_side_val_zero.setText(str(n_zero))
        self._uploaded_side_val_300.setText(str(n_300))
        self._uploaded_side_val_18.setText(str(n_18))
        self._uploaded_side_val_ban.setText(str(n_ban))

        vc = [int(v.view_count) for v in counted if v.view_count is not None]
        lc = [int(v.like_count) for v in counted if v.like_count is not None]
        cc = [int(v.comment_count) for v in counted if v.comment_count is not None]
        parts: list[str] = []
        if vc:
            parts.append(f"{_format_int_compact(round(sum(vc) / len(vc)))} 👁")
        if lc:
            parts.append(f"{_format_int_compact(round(sum(lc) / len(lc)))} ♥")
        if cc:
            parts.append(f"{_format_int_compact(round(sum(cc) / len(cc)))} 💬")
        self._uploaded_side_avg.setText(
            "Среднее: " + "   ".join(parts) if parts else "Среднее: —"
        )

    def _uploaded_list_row_width(self) -> int:
        vw = self._uploaded_list.viewport().width()
        return max(520, vw - 8) if vw > 80 else 560

    def _uploaded_video_tooltip(self, v: UploadedVideo) -> str:
        updated = (
            _format_stored_datetime(v.stats_updated_at or "")
            if v.stats_updated_at
            else "—"
        )
        sess = (
            _format_stored_datetime(v.session_started_at or "")
            if v.session_started_at
            else "—"
        )
        uploaded_at = _format_stored_datetime(v.uploaded_at or "")
        tip_lines = [
            (v.title or "").strip(),
            f"videoId: {v.video_id}",
            f"profile_id: {v.profile_id or '—'}",
            f"url: {v.url}",
            f"session_id: {v.session_id}",
            f"session_started_at: {v.session_started_at or '—'}",
            f"uploaded_at: {v.uploaded_at}",
            f"stats_updated_at: {v.stats_updated_at or '—'}",
            f"Обновлено (чит.): {updated}",
            f"Сессия: #{v.session_id} ({sess})",
            f"Загружено (чит.): {uploaded_at}",
        ]
        if v.stats_unavailable:
            if v.stats_unavailable_data_api:
                tip_lines.append(
                    "Статистика: YoutubeDataApiError — ответ YouTube Data API "
                    "без данных по этому videoId."
                )
            else:
                tip_lines.append(
                    "Статистика: не удалось получить (блокировка, приват или удалено)."
                )
        elif v.age_restricted is True:
            tip_lines.append(
                "Возрастное ограничение YouTube (Data API): 18+ "
                "(contentDetails.contentRating.ytRating = ytAgeRestricted)."
            )
        return "\n".join(tip_lines)

    def _uploaded_add_list_row(self, v: UploadedVideo) -> None:
        w_hint = self._uploaded_list_row_width()
        row_h = _UPLOADED_ROW_H
        tip = self._brand(self._uploaded_video_tooltip(v))
        it = QListWidgetItem()
        it.setData(Qt.ItemDataRole.UserRole + 1, str(v.video_id))
        it.setData(Qt.ItemDataRole.UserRole + 2, str(v.url))
        it.setToolTip(tip)
        it.setSizeHint(QSize(w_hint, row_h))
        self._uploaded_list.addItem(it)
        prof_cap = _uploaded_row_profile_caption(
            profile_id=v.profile_id,
            profiles=self._profiles_raw,
        )
        row_w = _UploadedVideoRow(
            title=v.title,
            url=v.url,
            video_id=v.video_id,
            uploaded_at_iso=v.uploaded_at or "",
            view_count=v.view_count,
            like_count=v.like_count,
            comment_count=v.comment_count,
            stats_unavailable=v.stats_unavailable,
            stats_unavailable_data_api=v.stats_unavailable_data_api,
            age_restricted=v.age_restricted,
            profile_caption=prof_cap,
            tooltip=tip,
            list_widget=self._uploaded_list,
            parent=self._uploaded_list,
        )
        apply_platform_branding(row_w, self._platform)
        row_w.activated.connect(self._open_uploaded_url)
        self._uploaded_list.setItemWidget(it, row_w)

    def _stop_uploaded_list_render(self) -> None:
        if hasattr(self, "_uploaded_render_timer"):
            self._uploaded_render_timer.stop()

    def _uploaded_append_list_slice(self, count: int) -> None:
        flat = self._uploaded_all
        start = self._uploaded_render_pos
        end = min(start + max(1, int(count)), len(flat))
        if start >= end:
            return
        for v in flat[start:end]:
            self._uploaded_add_list_row(v)
        self._uploaded_render_pos = end

    def _tick_uploaded_list_render(self) -> None:
        if self._uploaded_render_pos >= len(self._uploaded_all):
            self._stop_uploaded_list_render()
            return
        self._uploaded_append_list_slice(_UPLOADED_RENDER_BATCH)
        if self._uploaded_render_pos >= len(self._uploaded_all):
            self._stop_uploaded_list_render()

    def _on_uploaded_list_scrolled(self, value: int) -> None:
        flat = self._uploaded_all
        if self._uploaded_render_pos >= len(flat):
            return
        sb = self._uploaded_list.verticalScrollBar()
        if sb.maximum() <= 0 or value < sb.maximum() - 96:
            return
        self._uploaded_append_list_slice(_UPLOADED_SCROLL_BATCH)

    def _start_uploaded_list_render(self) -> None:
        self._stop_uploaded_list_render()
        self._uploaded_render_pos = 0
        if not self._uploaded_all:
            return
        self._uploaded_render_timer.start()

    def _refresh_uploaded_list(self) -> None:
        if not hasattr(self, "_uploaded_list"):
            return

        self._populate_uploaded_session_filter()
        self._stop_uploaded_list_render()
        self._uploaded_list.clear()
        self._uploaded_all = self._uploaded_videos_for_current_filter_sorted()
        self._update_uploaded_side_panel(self._uploaded_all)

        if not self._uploaded_all:
            w_hint = self._uploaded_list_row_width()
            it = QListWidgetItem()
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            it.setSizeHint(QSize(w_hint, 80))
            self._uploaded_list.addItem(it)
            empty = QLabel("Нет залитых видео для этого фильтра.")
            empty.setObjectName("hint")
            empty.setWordWrap(True)
            self._uploaded_list.setItemWidget(it, empty)
            return

        self._start_uploaded_list_render()

    def _uploaded_merge_video_in_cache(self, v: UploadedVideo) -> None:
        vid = (v.video_id or "").strip()
        if not vid:
            return
        for i, cur in enumerate(self._uploaded_all):
            if (cur.video_id or "").strip() == vid:
                self._uploaded_all[i] = v
                return

    def _uploaded_patch_list_row(self, video_id: str) -> None:
        vid = (video_id or "").strip()
        if not vid:
            return
        v = next(
            (x for x in self._uploaded_all if (x.video_id or "").strip() == vid),
            None,
        )
        if v is None:
            return
        for i in range(self._uploaded_list.count()):
            it = self._uploaded_list.item(i)
            if it is None:
                continue
            if (it.data(Qt.ItemDataRole.UserRole + 1) or "").strip() != vid:
                continue
            row_w = self._uploaded_list.itemWidget(it)
            if isinstance(row_w, _UploadedVideoRow):
                row_w.update_from_video(v)
            break

    def _uploaded_persist_stats_batch(self, successes: object, errors: object) -> None:
        """Сохраняет пакет в БД и обновляет кэш/видимые строки без перестройки списка."""
        touched: set[str] = set()
        succ = successes if isinstance(successes, list) else []
        failures_raw = errors if isinstance(errors, list) else []
        for item in failures_raw:
            ve = ""
            is_api = False
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                ve = str(item[0] or "").strip()
                is_api = bool(item[2])
            else:
                ev = _uploaded_stats_error_video_id(item)
                if ev:
                    ve = ev
                    is_api = "YoutubeDataApiError" in str(item)
            if not ve:
                continue
            try:
                self._upload_store.mark_video_stats_unavailable(
                    video_id=ve,
                    youtube_data_api_error=is_api,
                    platform=self._platform,
                )
            except Exception:
                pass
            for cur in self._uploaded_all:
                if (cur.video_id or "").strip() == ve:
                    self._uploaded_merge_video_in_cache(
                        replace(
                            cur,
                            view_count=None,
                            like_count=None,
                            comment_count=None,
                            stats_unavailable=True,
                            stats_unavailable_data_api=is_api,
                            age_restricted=None,
                        )
                    )
                    break
            touched.add(ve)
        for row in succ:
            if not isinstance(row, (list, tuple)) or len(row) < 4:
                continue
            vid, vc, lc, cc = row[0], row[1], row[2], row[3]
            ar = bool(row[4]) if len(row) >= 5 else False
            ve = str(vid or "").strip()
            if not ve:
                continue
            try:
                self._upload_store.update_video_stats(
                    video_id=ve,
                    view_count=int(vc),
                    like_count=lc if lc is None else int(lc),
                    comment_count=cc if cc is None else int(cc),
                    age_restricted=ar,
                    platform=self._platform,
                )
            except Exception:
                pass
            for cur in self._uploaded_all:
                if (cur.video_id or "").strip() == ve:
                    self._uploaded_merge_video_in_cache(
                        replace(
                            cur,
                            view_count=int(vc),
                            like_count=lc if lc is None else int(lc),
                            comment_count=cc if cc is None else int(cc),
                            stats_unavailable=False,
                            stats_unavailable_data_api=False,
                            age_restricted=ar,
                        )
                    )
                    break
            touched.add(ve)
        for ve in touched:
            self._uploaded_patch_list_row(ve)
        if touched:
            self._update_uploaded_side_panel(self._uploaded_all)

    def _populate_uploaded_session_filter(
        self, *, preferred_session_id: int | None = None
    ) -> None:
        if not hasattr(self, "_uploaded_session_filter"):
            return
        combo: QComboBox = self._uploaded_session_filter
        if preferred_session_id is not None and int(preferred_session_id) > 0:
            prev_id = int(preferred_session_id)
        else:
            prev = combo.currentData()
            try:
                prev_id = int(prev or 0)
            except Exception:
                prev_id = 0

        try:
            sessions = self._upload_store.list_sessions(limit=400, platform=self._platform)
        except Exception:
            sessions = []

        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("Все сессии", 0)
            for s in sessions:
                up = int(s.uploaded_ok or 0)
                if up <= 0:
                    continue
                proc = int(s.processed_videos or 0)
                dt = _format_upload_combo_datetime(s.started_at or "")
                combo.addItem(f"{dt} ({up}/{proc})", int(s.id))

            if prev_id > 0:
                idx = combo.findData(prev_id)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                else:
                    combo.setCurrentIndex(0)
            else:
                combo.setCurrentIndex(0)
        finally:
            combo.blockSignals(False)

    def _uploaded_ig_checker_selected_id(self) -> str:
        return (getattr(self, "_uploaded_ig_checker_profile_id", "") or "").strip()

    def _set_uploaded_ig_checker_profile_id(self, profile_id: str) -> None:
        pid = (profile_id or "").strip()
        self._uploaded_ig_checker_profile_id = pid
        if pid:
            self._settings.setValue("instagram/stats_checker_profile_id", pid)
            try:
                self._settings.sync()
            except Exception:
                pass
        self._refresh_uploaded_ig_checker_label()

    def _refresh_uploaded_ig_checker_label(self) -> None:
        if not hasattr(self, "_uploaded_ig_checker_value"):
            return
        pid = self._uploaded_ig_checker_selected_id()
        if not pid:
            self._uploaded_ig_checker_value.setText("— не выбран —")
            return
        name = ""
        for p in self._profiles_raw or []:
            if not isinstance(p, dict):
                continue
            if _profile_id(p) == pid:
                name = _profile_name(p)
                break
        if name:
            self._uploaded_ig_checker_value.setText(f"{name}  ({pid})")
        else:
            self._uploaded_ig_checker_value.setText(pid)

    def _pick_uploaded_ig_checker_profile(self) -> None:
        if self._platform != PLATFORM_INSTAGRAM:
            return
        profiles = [p for p in (self._profiles_raw or []) if isinstance(p, dict)]
        if not profiles:
            QMessageBox.information(
                self,
                "Профиль для чека",
                "Сначала загрузите список профилей (вкладка «Профили» → «Обновить»).",
            )
            return

        dlg_holder: list[IgCheckerProfilePickDialog | None] = [None]

        def _pause_click(pid: str) -> None:
            dlg = dlg_holder[0]
            self._ask_reset_upload_cooldown_for_profile(
                pid,
                dialog_parent=dlg or self,
                dialog_profiles_interaction=(
                    dlg._interaction
                    if dlg is not None and hasattr(dlg, "_interaction")
                    else None
                ),
            )

        dlg = IgCheckerProfilePickDialog(
            profiles=profiles,
            upload_store=self._upload_store,
            platform=self._platform,
            initially_selected_id=self._uploaded_ig_checker_selected_id(),
            on_upload_pause_click=_pause_click,
            upload_pause=self._upload_pause_between_uploads(),
            parent=self,
        )
        dlg_holder[0] = dlg
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._set_uploaded_ig_checker_profile_id(dlg.selected_profile_id())

    def _populate_uploaded_ig_checker_profiles(self) -> None:
        """Обновить подпись выбранного профиля после загрузки списка."""
        if hasattr(self, "_uploaded_ig_checker_row"):
            self._uploaded_ig_checker_row.setVisible(
                self._platform == PLATFORM_INSTAGRAM
            )
        # Если сохранённый id пропал из списка — оставляем id, но покажем без имени.
        saved = (
            self._settings.value("instagram/stats_checker_profile_id", "", type=str)
            or ""
        ).strip()
        if saved and not self._uploaded_ig_checker_selected_id():
            self._uploaded_ig_checker_profile_id = saved
        self._refresh_uploaded_ig_checker_label()
        if self._platform == PLATFORM_INSTAGRAM and hasattr(
            self, "_btn_uploaded_check"
        ):
            self._btn_uploaded_check.setToolTip(
                "Запросить просмотры, лайки и комментарии через сессию "
                "выбранного профиля (instagrapi, без API-ключа Meta)."
            )

    def _make_instagram_sessionid_provider(self, profile_id: str):
        """Callable для воркера: достать sessionid из браузера выбранного профиля."""
        pid = (profile_id or "").strip()
        token = self._legacy_dolphin_token()
        kind = self._antidetect_kind()
        base_url = self._own_antidetect_base_url_from_settings(kind)
        headless = True
        if hasattr(self, "_dolphin_headless"):
            headless = bool(self._dolphin_headless.isChecked())
        else:
            headless = bool(
                self._settings.value("antydetect/dolphin_headless", True, type=bool)
            )
        try:
            remote_cdp = self._remote_cdp_launch_options_for_kind(kind)
        except Exception:
            remote_cdp = None

        def _provider() -> str:
            from zaliver.antydetect.antic_open import (
                extract_instagram_sessionid_from_local_antidetect_profile,
                extract_instagram_sessionid_from_profile,
                set_log_sink,
            )

            set_log_sink(self._ui_log_line.emit)
            if _is_own_antidetect_kind(kind):
                if not (base_url or "").strip():
                    raise RuntimeError(
                        f"Укажите базовый URL {_own_antidetect_api_label(kind)} "
                        "API в настройках."
                    )
                return extract_instagram_sessionid_from_local_antidetect_profile(
                    pid,
                    base_url=base_url,
                    headless=headless,
                    remote_cdp=remote_cdp,
                )
            return extract_instagram_sessionid_from_profile(
                pid,
                local_token=token or None,
                headless=headless,
            )

        return _provider

    def _refresh_uploaded_stats_visible(self) -> None:
        if not hasattr(self, "_uploaded_list"):
            return
        if self._stats_thread is not None and self._stats_thread.isRunning():
            return
        flat = getattr(self, "_uploaded_all", None) or []
        if not flat:
            flat = self._uploaded_videos_for_current_filter_sorted()
        vids = sorted(
            {(v.video_id or "").strip() for v in flat if (v.video_id or "").strip()}
        )
        if not vids:
            QMessageBox.information(
                self,
                "Zaliver",
                "Нет видео для обновления статистики. "
                "Выберите сессию с роликами или нажмите «Список».",
            )
            return

        self._uploaded_stats_status.setText(
            f"Обновление статистики: 0 / {len(vids)}…"
        )
        self._btn_uploaded_check.setEnabled(False)
        self._stats_thread = QThread()

        if self._platform == PLATFORM_INSTAGRAM:
            pid = self._uploaded_ig_checker_selected_id()
            if not pid:
                self._btn_uploaded_check.setEnabled(True)
                self._stats_thread = None
                QMessageBox.information(
                    self,
                    "Zaliver",
                    "Выберите профиль в «Аккаунт для чека» — "
                    "с его Instagram-сессии пойдут запросы метрик.",
                )
                if hasattr(self, "_uploaded_stats_status"):
                    self._uploaded_stats_status.setText("")
                return
            login, password, twofa = self._instagram_session_credentials(pid)
            worker = UploadedInstagramStatsRefreshWorker(
                vids,
                profile_id=pid,
                username=login,
                password=password,
                twofa_secret=twofa,
                sessionid_provider=self._make_instagram_sessionid_provider(pid),
                proxy=self._instagram_checker_proxy_dsn(pid),
            )
            self._stats_worker = worker
            worker.log_line.connect(self._ui_log_line.emit)
        else:
            key = ""
            if hasattr(self, "_youtube_api_key"):
                key = (self._youtube_api_key.text() or "").strip()
            self._stats_worker = UploadedStatsRefreshWorker(vids, key)

        self._stats_worker.moveToThread(self._stats_thread)
        self._stats_thread.started.connect(self._stats_worker.run)
        self._stats_worker.progress.connect(self._on_uploaded_stats_progress)
        self._stats_worker.batch_done.connect(self._on_uploaded_stats_batch_done)
        self._stats_worker.finished.connect(self._on_uploaded_stats_worker_finished)
        self._stats_thread.finished.connect(self._on_uploaded_stats_thread_finished)
        self._stats_thread.start()

    def _on_uploaded_stats_progress(self, step: int, total: int, video_id: str) -> None:
        if not hasattr(self, "_uploaded_stats_status"):
            return
        t = max(1, int(total))
        s = max(0, min(int(step), t))
        vid = (video_id or "").strip()
        tail = f" — {vid}" if vid else ""
        self._uploaded_stats_status.setText(
            f"Обновление статистики: {s} / {t}{tail}"
        )

    def _on_uploaded_stats_batch_done(self, successes: object, errors: object) -> None:
        self._uploaded_persist_stats_batch(successes, errors)

    def _on_uploaded_stats_worker_finished(self, successes: object, errors: object) -> None:
        succ = successes if isinstance(successes, list) else []
        fails = errors if isinstance(errors, list) else []
        if (
            self._platform == PLATFORM_INSTAGRAM
            and not succ
            and fails
        ):
            sample = ""
            for item in fails:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    sample = str(item[1] or "").strip()
                    if sample:
                        break
            if sample:
                # Одна и та же ошибка сессии на все ролики — показать причину.
                same = True
                for item in fails:
                    if not isinstance(item, (list, tuple)) or len(item) < 2:
                        continue
                    if str(item[1] or "").strip() != sample:
                        same = False
                        break
                if same:
                    QMessageBox.warning(
                        self,
                        "Zaliver",
                        "Не удалось обновить статистику Instagram:\n\n"
                        f"{sample}\n\n"
                        "Частая причина: чекер убил sessionid "
                        "(Exceeded 30 redirects) — Instagram в этом профиле "
                        "перестаёт грузиться.\n\n"
                        "Что делать:\n"
                        "• зайдите в Instagram вручную в антидетект-профиле "
                        "и перелогиньтесь;\n"
                        "• для чека лучше отдельный «читающий» аккаунт, "
                        "не тот, с которого льёте;\n"
                        "• не гоняйте чек по тысячам рилсов подряд.\n\n"
                        "Смотрите лог ([ig-stats]).",
                    )
        if hasattr(self, "_uploaded_stats_status"):
            if succ or fails:
                self._uploaded_stats_status.setText(
                    f"Готово: ок {len(succ)}, ошибок {len(fails)}"
                )
            else:
                self._uploaded_stats_status.setText("")
        t = self._stats_thread
        if t is not None:
            t.quit()

    def _on_uploaded_stats_thread_finished(self) -> None:
        self._stats_thread = None
        if self._stats_worker is not None:
            self._stats_worker.deleteLater()
            self._stats_worker = None
        if hasattr(self, "_btn_uploaded_check"):
            self._btn_uploaded_check.setEnabled(True)
        if hasattr(self, "_uploaded_all") and self._uploaded_all:
            mode = getattr(self, "_uploaded_sort_mode", "views")
            self._uploaded_all = self._sorted_uploaded_videos(self._uploaded_all, mode)
            self._stop_uploaded_list_render()
            self._uploaded_list.clear()
            self._update_uploaded_side_panel(self._uploaded_all)
            self._start_uploaded_list_render()

    def _open_uploaded_url(self, url: str) -> None:
        u = (url or "").strip()
        if not u:
            return
        try:
            QDesktopServices.openUrl(QUrl(u))
        except Exception:
            pass

    def _open_video_path(self, raw: str) -> None:
        if not raw:
            return
        p = Path(str(raw))
        try:
            if not p.is_file():
                QMessageBox.warning(self, "Zaliver", "Файл не найден (возможно, удалён).")
                self._refresh_ready_list()
                return
        except OSError:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.resolve())))

    def _on_ready_remove_requested(self, video_id: int) -> None:
        r = QMessageBox.question(
            self,
            "Zaliver",
            "Убрать эту запись из списка? Файл видео на диске не будет удалён.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        try:
            ok = self._video_store.remove_video_record(int(video_id))
        except (OSError, sqlite3.Error):
            QMessageBox.warning(self, "Zaliver", "Не удалось удалить запись.")
            return
        if not ok:
            QMessageBox.warning(
                self, "Zaliver", "Запись не найдена (список будет обновлён)."
            )
        self._refresh_ready_list()

    def _on_ready_remove_selected(self) -> None:
        items = self._ready_list.selectedItems()
        if not items:
            QMessageBox.information(
                self,
                "Zaliver",
                "Выделите строки (Ctrl+клик или Shift+клик по строкам), "
                "затем снова нажмите «Удалить выбранные…».",
            )
            return
        ids: list[int] = []
        for it in items:
            raw = it.data(Qt.ItemDataRole.UserRole + 1)
            if raw is not None:
                ids.append(int(raw))
        if not ids:
            return
        n = len(ids)
        r = QMessageBox.question(
            self,
            "Zaliver",
            f"Убрать из списка выбранные записи ({n} шт.)? "
            "Файлы видео на диске не будут удалены.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = self._video_store.remove_video_records(ids)
        except (OSError, sqlite3.Error):
            QMessageBox.warning(self, "Zaliver", "Не удалось удалить записи.")
            return
        if removed < n:
            QMessageBox.warning(
                self,
                "Zaliver",
                f"Удалено записей: {removed} из {n} (остальные уже отсутствовали в базе).",
            )
        self._refresh_ready_list()

    def _refresh_ready_list(self) -> None:
        if not hasattr(self, "_ready_list"):
            return
        try:
            self._video_store.prune_missing_files()
        except OSError:
            pass
        self._ready_list.clear()
        try:
            rows = self._video_store.list_videos(500)
        except OSError:
            rows = []
        row_h = _READY_THUMB_H + 28
        vw = self._ready_list.viewport().width()
        w_hint = max(520, vw - 8) if vw > 80 else 560
        for i, v in enumerate(rows, start=1):
            name = Path(v.path).name
            tip = f"{v.path}\nСоздан файл: {v.created_at}\nДобавлено в список: {v.added_at}"
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, v.path)
            it.setData(Qt.ItemDataRole.UserRole + 1, int(v.id))
            it.setToolTip(tip)
            it.setSizeHint(QSize(w_hint, row_h))
            self._ready_list.addItem(it)
            row_w = _ReadyVideoRow(
                v.id,
                i,
                v.path,
                name,
                _format_stored_datetime(v.created_at),
                v.thumb_path,
                tip,
                self._ready_list,
                parent=self._ready_list,
            )
            row_w.activated.connect(self._open_video_path)
            row_w.remove_requested.connect(self._on_ready_remove_requested)
            self._ready_list.setItemWidget(it, row_w)

    def _is_montage_mode(self, mode: str | None = None) -> bool:
        m = (mode or getattr(self, "_active_work_mode", "") or "").strip()
        return m in ("slicing", "stitching")

    def _montage_tab(self, mode: str | None = None):
        m = (mode or getattr(self, "_active_work_mode", "") or "").strip()
        if m == "slicing":
            return getattr(self, "_slice_tab", None)
        if m == "stitching":
            return getattr(self, "_stitch_tab", None)
        return None

    def _montage_work_label(self, mode: str | None = None) -> str:
        m = (mode or getattr(self, "_active_work_mode", "") or "").strip()
        if m == "stitching":
            return "Склейка"
        if m == "slicing":
            return "Нарезка"
        return "Уникализация"

    def _delete_after_upload_enabled(self) -> bool:
        mode = (getattr(self, "_upload_log_mode", "") or "").strip()
        if not mode:
            mode = (getattr(self, "_active_work_mode", "") or "").strip()
        if self._is_montage_mode(mode):
            tab = self._montage_tab(mode)
            return bool(
                tab is not None
                and hasattr(tab, "delete_after_upload")
                and tab.delete_after_upload.isChecked()
            )
        return bool(
            hasattr(self, "delete_after_upload") and self.delete_after_upload.isChecked()
        )

    def _set_processing_upload_throttle(self, enabled: bool) -> None:
        """Пока идёт залив параллельно с обработкой — режем ffmpeg, чтобы браузер не тормозил."""
        for ctrl in (
            getattr(self, "_processor", None),
            getattr(self, "_slice_processor", None),
            getattr(self, "_stitch_processor", None),
        ):
            if ctrl is None:
                continue
            fn = getattr(ctrl, "set_upload_throttle", None)
            if not callable(fn):
                continue
            try:
                fn(bool(enabled))
            except Exception:
                pass
        if enabled:
            try:
                self._append_session_log(
                    "[upload] Обработка приглушена на время залива "
                    "(меньше параллельного ffmpeg) — браузер должен кликать быстрее."
                )
            except Exception:
                pass

    def _streaming_upload_profile_count(self, pending: dict | None = None) -> int:
        src = pending if pending is not None else getattr(self, "_pending_upload", None)
        raw = ""
        if isinstance(src, dict):
            raw = str(src.get("profile_ids") or "")
        return len([p for p in raw.split(",") if p.strip()])

    def _compute_streaming_upload_min_ready(
        self, *, profile_count: int, planned: int
    ) -> int:
        """Запас перед стартом залива и лимит буфера: профили×2, не больше плана."""
        return compute_ready_buffer_limit(
            profile_count=profile_count, planned=planned
        )

    def _release_ready_buffer_slot(self, video_path: str) -> None:
        """Слот буфера свободен — обработка может сделать следующее видео."""
        path = str(video_path or "").strip()
        if not path:
            return
        for ctrl in (
            getattr(self, "_processor", None),
            getattr(self, "_slice_processor", None),
            getattr(self, "_stitch_processor", None),
        ):
            if ctrl is None:
                continue
            fn = getattr(ctrl, "release_ready_buffer_path", None)
            if not callable(fn):
                continue
            try:
                fn(path)
            except Exception:
                pass

    def _enqueue_or_start_streaming_upload(self, video_path: str) -> None:
        """При «по мере готовности»: ждём запас профили×2, затем стартуем; дальше — в очередь."""
        if not getattr(self, "_upload_streaming_active", False):
            return
        path = (video_path or "").strip()
        if not path:
            return
        mgr = getattr(self, "_upload_manager", None)
        if mgr is not None:
            try:
                mgr.enqueue_videos(
                    video_paths=[path],
                    title=getattr(self, "_upload_streaming_title", "") or "",
                    description=getattr(self, "_upload_streaming_description", "") or "",
                )
                try:
                    self._append_session_log(
                        f"[upload] В очередь по мере готовности: {Path(path).name}"
                    )
                except Exception:
                    pass
            except Exception as e:
                try:
                    self._append_session_log(
                        f"[upload] Не удалось добавить в очередь: {Path(path).name} ({e!r})"
                    )
                except Exception:
                    pass
            return
        pending = self._pending_upload
        if pending is None:
            return
        ready_paths = [
            p.strip()
            for p in (self._just_saved_outputs or [])
            if isinstance(p, str) and p.strip()
        ]
        ready_n = len(ready_paths)
        min_ready = max(1, int(getattr(self, "_upload_streaming_min_ready", 1) or 1))
        if ready_n < min_ready:
            try:
                n_prof = self._streaming_upload_profile_count(pending)
                self._append_session_log(
                    f"[upload] Залив по мере готовности: ждём запас "
                    f"{min_ready} видео (профили×2"
                    + (f" = {n_prof}×2" if n_prof > 0 else "")
                    + f"), готово {ready_n}/{min_ready}: {Path(path).name}"
                )
            except Exception:
                pass
            return
        self._upload_log_mode = self._active_work_mode
        if self._start_upload_queue_from_pending(
            pending, ready_paths, streaming=True
        ):
            self._pending_upload = None
            self._set_processing_upload_throttle(True)
            try:
                self._append_session_log(
                    f"[upload] Залив по мере готовности: старт с запасом "
                    f"{ready_n} видео (порог {min_ready})"
                )
            except Exception:
                pass
        else:
            # Старт не удался — откат к обычному режиму (всё, потом залив).
            self._upload_streaming_active = False
            self._upload_log_mode = ""
            self._set_processing_upload_throttle(False)

    def _delete_output_video_after_upload(self, video_path: str) -> None:
        p = Path(str(video_path or "").strip()).expanduser()
        if not str(p):
            return
        try:
            if not p.is_file():
                self._release_ready_buffer_slot(str(p))
                return
            p.unlink()
        except OSError as e:
            try:
                self._ui_log_line.emit(
                    f"[upload] Не удалось удалить файл после залива: {p.name} ({e!r})"
                )
            except Exception:
                pass
            self._release_ready_buffer_slot(str(p))
            return
        try:
            self._video_store.prune_missing_files()
        except Exception:
            pass
        try:
            self._ui_log_line.emit(f"[upload] Удалён после залива: {p.name}")
        except Exception:
            pass
        self._release_ready_buffer_slot(str(p))
        try:
            QTimer.singleShot(0, self._refresh_ready_list)
        except Exception:
            pass

    def _maybe_delete_output_after_upload_success(
        self,
        video_path: str,
        *,
        record_platform: str | None = None,
        yt_inst_upload: bool = False,
    ) -> None:
        """Удалить файл сразу после успеха; для Yt+Inst — после Instagram."""
        path = str(video_path or "").strip()
        if not path:
            return
        plat = (record_platform or "").strip().lower()
        # YouTube в Yt+Inst ещё нужен Instagram (pipeline / pause 0) — не трогаем.
        if yt_inst_upload and plat == PLATFORM_YOUTUBE:
            with self._upload_success_lock:
                self._upload_yt_inst_pending_delete.add(path)
            return
        with self._upload_success_lock:
            self._upload_yt_inst_pending_delete.discard(path)
        if getattr(self, "_upload_delete_after_enabled", False):
            self._delete_output_video_after_upload(path)
        else:
            self._release_ready_buffer_slot(path)

    def _delete_yt_inst_pending_outputs(self, video_paths: list[str]) -> None:
        """Удалить файлы Yt+Inst, когда Instagram закончил (ошибка / отмена)."""
        delete_on = bool(getattr(self, "_upload_delete_after_enabled", False))
        for video_path in video_paths:
            path = str(video_path or "").strip()
            if not path:
                continue
            with self._upload_success_lock:
                pending = path in self._upload_yt_inst_pending_delete
                if pending:
                    self._upload_yt_inst_pending_delete.discard(path)
            if delete_on and pending:
                self._delete_output_video_after_upload(path)
            else:
                self._release_ready_buffer_slot(path)

    def _on_output_saved(self, path: str, include_in_upload: bool = True) -> None:
        if isinstance(path, str) and path.strip():
            p = path.strip()
            if include_in_upload:
                self._just_saved_outputs.append(p)
                self._enqueue_or_start_streaming_upload(p)
            else:
                self._append_log(
                    f"Исключено из залива в YouTube: {Path(p).name}"
                )
            try:
                s = self._upload_session
                if s is not None:
                    self._upload_store.inc_processed(session_id=int(s.id), delta=1)
            except Exception:
                pass

        def work() -> None:
            try:
                self._video_store.upsert_video(path)
            finally:
                self._after_video_saved.emit()

        threading.Thread(target=work, daemon=True).start()

    def _prompt_title_desc_and_profile(self, *, mode: str = "uniquify") -> dict[str, str] | None:
        profiles = self._profiles_raw or []
        if not profiles:
            # Профили ещё не подтянулись — не блокируем уникализацию (раньше return None
            # давал «Старт» без реакции, если пользователь не на вкладке с профилями).
            try:
                work_label = self._montage_work_label(mode)
                self._profiles_status.setText(
                    f"Профили ещё не загружены — запускаю загрузку… "
                    f"{work_label} без залива в YouTube (профили не выбраны)."
                )
            except Exception:
                pass
            self._refresh_antydetect_profiles()
            return {"title": "", "description": "", "profile_ids": "", "publish_before_checks": False, "keep_studio_title": False, "upload_as_ready": False, "schedule_publish": False, "schedule_times_iso": [], "schedule_warmup_shorts": False, "schedule_warmup_shorts_recommendations": False, "schedule_warmup_search_query": "", "schedule_warmup_hashtag": ""}

        dlg = QDialog(self)
        if mode == "slicing":
            dlg_title = "Загрузка в YouTube после нарезки"
        elif mode == "stitching":
            dlg_title = "Загрузка в YouTube после склейки"
        else:
            dlg_title = "Загрузка в YouTube после уникализации"
        dlg.setWindowTitle(dlg_title)
        dlg.setModal(True)
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            dlg.setMinimumSize(
                QSize(
                    min(980, max(560, geo.width() - 48)),
                    min(780, max(420, geo.height() - 48)),
                )
            )
            dlg.resize(min(1100, geo.width() - 24), min(860, geo.height() - 24))
        else:
            dlg.setMinimumSize(QSize(980, 780))
            dlg.resize(1100, 860)

        grid = QGridLayout(dlg)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        recent_upload_titles = self._upload_store.list_recent_upload_titles(
            platform=self._platform
        )
        title_edit = QPlainTextEdit()
        title_edit.setPlaceholderText(
            "Название видео (обязательное для загрузки в YouTube). "
            "Можно использовать переменные: {date}, {profile}, {video}, {index}… "
            "Enter — новая строка."
        )
        title_edit.setMinimumHeight(56)
        title_edit.setMaximumHeight(96)
        title_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        if recent_upload_titles:
            title_edit.setPlainText(recent_upload_titles[0])

        desc_edit = QPlainTextEdit()
        desc_edit.setPlaceholderText("Описание (необязательно)…")
        desc_edit.setMinimumHeight(44)
        desc_edit.setMaximumHeight(72)
        desc_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        btn_desc_wand = make_magic_wand_button(
            tooltip="Сгенерировать описание через ИИ (промпт «Описание видео»)"
        )
        btn_desc_wand.clicked.connect(
            lambda _checked=False: self._on_ai_magic_generate(
                default_prompt_id="builtin_video_description",
                window_title="Генерация описания",
                apply_text=lambda text: desc_edit.setPlainText(text),
                parent=dlg,
            )
        )
        desc_row, _desc_recent = field_with_recent_picker(
            desc_edit,
            recent=[],
            tooltip="Недавние описания видео",
            side_extras=[btn_desc_wand],
        )

        publish_before_checks_cb = QCheckBox("Опубликовать до проверок")
        publish_before_checks_cb.setChecked(False)
        publish_before_checks_cb.setToolTip(
            "Если включено — сразу после названия проходим мастер до «Открытый доступ» "
            "пока идёт загрузка; «Опубликовать» нажимается после 100% загрузки "
            "или сообщения «Загрузка завершена… обработка скоро начнётся», "
            "без ожидания проверок YouTube."
        )

        keep_studio_title_cb = QCheckBox("Название из настроек/названия файлов")
        keep_studio_title_cb.setChecked(False)
        keep_studio_title_cb.setToolTip(
            "Если включено — в YouTube Studio не очищаем поле «Название»: "
            "остаётся значение из настроек канала или имени файла."
        )

        upload_as_ready_cb = QCheckBox("Заливать по мере готовности")
        upload_as_ready_cb.setChecked(False)
        upload_as_ready_cb.setToolTip(
            "Если включено: залив стартует после запаса готовых видео "
            "(число выбранных профилей × 2). Например, 5 профилей — после 10 роликов. "
            "Если всего видео меньше этого запаса — ждём, пока обработаются все. "
            "Дальше обработка не копит больше этого запаса: пока ролики заливаются "
            "и удаляются, слот в буфере освобождается и делается следующее видео.\n"
            "Если выключено: сначала обрабатываются все видео, затем начинается залив."
        )

        btn_title_hints = make_variables_hint_button(parent=dlg, field=title_edit)
        btn_title_wand = make_magic_wand_button(
            tooltip="Сгенерировать название через ИИ (промпт «Название видео»)"
        )
        title_row, _title_recent = field_with_recent_picker(
            title_edit,
            recent=recent_upload_titles,
            tooltip="Недавние названия видео",
            side_extras=[btn_title_hints, btn_title_wand],
        )

        def _apply_ai_title(text: str) -> None:
            value = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            title_edit.setPlainText(value)

        btn_title_wand.clicked.connect(
            lambda _checked=False: self._on_ai_magic_generate(
                default_prompt_id="builtin_video_title",
                window_title="Генерация названия",
                apply_text=_apply_ai_title,
                parent=dlg,
            )
        )

        def _sync_keep_studio_title_ui(checked: bool) -> None:
            title_edit.setEnabled(not checked)
            btn_title_hints.setEnabled(not checked)
            btn_title_wand.setEnabled(not checked)
            _title_recent.setEnabled(
                (not checked) and recent_picker_has_items(_title_recent)
            )
            if checked:
                title_edit.setPlaceholderText(
                    "Название не вводится — берётся из Studio (настройки канала или имя файла)…"
                )
            else:
                title_edit.setPlaceholderText(
                    "Название видео (обязательное для загрузки в YouTube). "
                    "Можно использовать переменные: {date}, {profile}, {video}, {index}… "
                    "Enter — новая строка."
                )

        keep_studio_title_cb.toggled.connect(_sync_keep_studio_title_ui)

        schedule_publish_cb = QCheckBox("Опубликовать в отложку")
        schedule_publish_cb.setChecked(False)
        schedule_publish_cb.setToolTip(
            "На экране «Открытый доступ» выбирается отложенная публикация (Москва). "
            "На каждый профиль подряд загружается по одному видео на каждое указанное время."
        )

        schedule_warmup_group = QGroupBox("Прогрев во время отложки")
        schedule_warmup_layout = QVBoxLayout(schedule_warmup_group)
        schedule_warmup_layout.setContentsMargins(12, 8, 8, 8)
        schedule_warmup_layout.setSpacing(6)

        schedule_warmup_cb = QCheckBox("Прогрев Shorts во второй вкладке")
        schedule_warmup_cb.setChecked(False)
        schedule_warmup_cb.setToolTip(
            "Пока на одной вкладке профиля идёт отложенная заливка в Studio, "
            "на соседней вкладке того же профиля крутится лента YouTube Shorts. "
            "Прогрев останавливается после отложки всех видео этого профиля."
        )
        schedule_warmup_layout.addWidget(schedule_warmup_cb)

        schedule_warmup_recommend_cb = QCheckBox("Рекомендации Shorts")
        schedule_warmup_recommend_cb.setChecked(False)
        schedule_warmup_recommend_cb.setToolTip(
            "Открыть ленту рекомендаций Shorts. Если снять галочку — "
            "можно указать поисковый запрос или хэштег."
        )
        schedule_warmup_layout.addWidget(schedule_warmup_recommend_cb)

        schedule_warmup_hashtag_cb = QCheckBox("Хэштег")
        schedule_warmup_hashtag_cb.setChecked(False)
        schedule_warmup_hashtag_cb.setToolTip(
            "Прогрев Shorts со страницы хэштега. Символ # можно не указывать."
        )
        schedule_warmup_layout.addWidget(schedule_warmup_hashtag_cb)

        schedule_warmup_hashtag_row = QWidget()
        schedule_warmup_hashtag_row_l = QHBoxLayout(schedule_warmup_hashtag_row)
        schedule_warmup_hashtag_row_l.setContentsMargins(24, 0, 0, 0)
        schedule_warmup_hashtag_row_l.setSpacing(8)
        schedule_warmup_hashtag_lbl = QLabel("Хэштег:")
        schedule_warmup_hashtag_edit = QLineEdit()
        schedule_warmup_hashtag_edit.setPlaceholderText("хэштег или #хэштег")
        schedule_warmup_hashtag_row_l.addWidget(schedule_warmup_hashtag_lbl)
        schedule_warmup_hashtag_row_l.addWidget(schedule_warmup_hashtag_edit, 1)
        schedule_warmup_layout.addWidget(schedule_warmup_hashtag_row)

        schedule_warmup_search_row = QWidget()
        schedule_warmup_search_row_l = QHBoxLayout(schedule_warmup_search_row)
        schedule_warmup_search_row_l.setContentsMargins(24, 0, 0, 0)
        schedule_warmup_search_row_l.setSpacing(8)
        schedule_warmup_search_lbl = QLabel("Поисковый запрос:")
        schedule_warmup_search_edit = QLineEdit()
        schedule_warmup_search_edit.setPlaceholderText("Текст для поиска Shorts на YouTube")
        schedule_warmup_search_row_l.addWidget(schedule_warmup_search_lbl)
        schedule_warmup_search_row_l.addWidget(schedule_warmup_search_edit, 1)
        schedule_warmup_layout.addWidget(schedule_warmup_search_row)

        schedule_times_widget = QWidget()
        schedule_times_layout = QVBoxLayout(schedule_times_widget)
        schedule_times_layout.setContentsMargins(24, 0, 0, 0)
        schedule_times_layout.setSpacing(6)
        msk_tz = QTimeZone(b"Europe/Moscow")
        now_msk = datetime.now(tz=MSK)
        default_base = (now_msk + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )

        def _default_schedule_qdt(offset_hours: int = 0) -> QDateTime:
            dt = default_base + timedelta(hours=offset_hours)
            qdt = QDateTime(dt.year, dt.month, dt.day, dt.hour, dt.minute)
            qdt.setTimeZone(msk_tz)
            return qdt

        schedule_time_edits: list[QDateTimeEdit] = []
        schedule_time_rows: list[QWidget] = []
        schedule_time_rows_container = QWidget()
        schedule_time_rows_layout = QVBoxLayout(schedule_time_rows_container)
        schedule_time_rows_layout.setContentsMargins(0, 0, 0, 0)
        schedule_time_rows_layout.setSpacing(6)

        schedule_time_scroll = QScrollArea()
        schedule_time_scroll.setWidgetResizable(True)
        schedule_time_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        schedule_time_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        schedule_time_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        schedule_time_scroll.setWidget(schedule_time_rows_container)
        schedule_time_scroll.setMinimumHeight(40)
        schedule_time_scroll.setMaximumHeight(220)
        schedule_time_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        schedule_times_layout.addWidget(schedule_time_scroll)

        def _make_schedule_time_row(index: int, *, visible: bool) -> QDateTimeEdit:
            row = QWidget()
            row_l = QHBoxLayout(row)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.setSpacing(8)
            lbl = QLabel(f"Время {index} (МСК):")
            edit = QDateTimeEdit()
            edit.setDisplayFormat("dd.MM.yyyy HH:mm")
            edit.setCalendarPopup(True)
            edit.setTimeZone(msk_tz)
            edit.setDateTime(_default_schedule_qdt(5 * (index - 1)))
            row_l.addWidget(lbl)
            row_l.addWidget(edit, 1)
            schedule_time_rows_layout.addWidget(row)
            schedule_time_edits.append(edit)
            schedule_time_rows.append(row)
            row.setVisible(visible)
            return edit

        _make_schedule_time_row(1, visible=True)

        schedule_slots_visible = 1
        schedule_btns = QHBoxLayout()
        btn_add_schedule_time = QPushButton("Добавить время")
        btn_add_schedule_time.setObjectName("secondary")
        btn_remove_schedule_time = QPushButton("Убрать время")
        btn_remove_schedule_time.setObjectName("secondary")
        schedule_btns.addWidget(btn_add_schedule_time)
        schedule_btns.addWidget(btn_remove_schedule_time)
        schedule_btns.addStretch()
        schedule_times_layout.addLayout(schedule_btns)
        schedule_hint = QLabel(
            "Интервал между временами — не менее 5 часов. "
            "Сначала все видео на одном профиле (по одному на каждое время), затем следующий профиль."
        )
        schedule_hint.setObjectName("hint")
        schedule_hint.setWordWrap(True)
        schedule_times_layout.addWidget(schedule_hint)

        def _sync_schedule_time_buttons() -> None:
            btn_remove_schedule_time.setEnabled(schedule_slots_visible > 1)

        def _sync_schedule_times_visibility(checked: bool) -> None:
            schedule_times_widget.setVisible(checked)
            schedule_warmup_group.setVisible(checked)

        def _sync_schedule_warmup_options() -> None:
            warmup_on = schedule_warmup_cb.isChecked()
            schedule_warmup_recommend_cb.setEnabled(warmup_on)
            schedule_warmup_hashtag_cb.setEnabled(warmup_on)
            use_hashtag = warmup_on and schedule_warmup_hashtag_cb.isChecked()
            use_reco = (
                warmup_on
                and schedule_warmup_recommend_cb.isChecked()
                and not use_hashtag
            )
            schedule_warmup_hashtag_row.setVisible(use_hashtag)
            schedule_warmup_search_row.setVisible(
                warmup_on and not use_reco and not use_hashtag
            )

        def _on_schedule_warmup_recommend_toggled(checked: bool) -> None:
            if checked and schedule_warmup_hashtag_cb.isChecked():
                schedule_warmup_hashtag_cb.blockSignals(True)
                schedule_warmup_hashtag_cb.setChecked(False)
                schedule_warmup_hashtag_cb.blockSignals(False)
            _sync_schedule_warmup_options()

        def _on_schedule_warmup_hashtag_toggled(checked: bool) -> None:
            if checked and schedule_warmup_recommend_cb.isChecked():
                schedule_warmup_recommend_cb.blockSignals(True)
                schedule_warmup_recommend_cb.setChecked(False)
                schedule_warmup_recommend_cb.blockSignals(False)
            _sync_schedule_warmup_options()

        def _add_schedule_time_slot() -> None:
            nonlocal schedule_slots_visible
            schedule_slots_visible += 1
            prev = schedule_time_edits[-1].dateTime()
            edit = _make_schedule_time_row(schedule_slots_visible, visible=True)
            edit.setDateTime(prev.addSecs(5 * 3600))
            _sync_schedule_time_buttons()

        def _remove_schedule_time_slot() -> None:
            nonlocal schedule_slots_visible
            if schedule_slots_visible <= 1:
                return
            row = schedule_time_rows.pop()
            schedule_time_edits.pop()
            schedule_time_rows_layout.removeWidget(row)
            row.deleteLater()
            schedule_slots_visible -= 1
            _sync_schedule_time_buttons()

        btn_add_schedule_time.clicked.connect(_add_schedule_time_slot)
        btn_remove_schedule_time.clicked.connect(_remove_schedule_time_slot)
        schedule_publish_cb.toggled.connect(_sync_schedule_times_visibility)
        schedule_warmup_cb.toggled.connect(_sync_schedule_warmup_options)
        schedule_warmup_recommend_cb.toggled.connect(
            _on_schedule_warmup_recommend_toggled
        )
        schedule_warmup_hashtag_cb.toggled.connect(
            _on_schedule_warmup_hashtag_toggled
        )
        _sync_schedule_times_visibility(False)
        schedule_warmup_group.setVisible(False)
        _sync_schedule_warmup_options()
        _sync_schedule_time_buttons()

        def _collect_schedule_times_msk() -> list[datetime]:
            out: list[datetime] = []
            for i in range(schedule_slots_visible):
                qdt = schedule_time_edits[i].dateTime()
                py = qdt.toPyDateTime()
                if py.tzinfo is None:
                    py = py.replace(tzinfo=MSK)
                else:
                    py = py.astimezone(MSK)
                out.append(py)
            return out

        lw = QListWidget()
        lw.setObjectName("uploadProfilesList")
        lw.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        lw.setSpacing(4)
        lw.setMinimumHeight(420)
        lw.setMouseTracking(True)

        ids: list[str] = []
        profile_rows: list[tuple[str, dict[str, object]]] = []
        for p in profiles:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or p.get("browserProfileId") or p.get("profile_id") or "").strip()
            if not pid:
                continue
            ids.append(pid)
            profile_rows.append((pid, p))

        preselect: set[str] = set()
        if self._profiles_interaction is not None:
            preselect = set(self._profiles_interaction.checked_profile_ids)

        last_upload_map = self._upload_store.last_uploaded_at_by_profiles(
            ids, platform=self._platform
        )
        dlg_profiles = [p for _pid, p in profile_rows]
        total_dlg_profiles = len(dlg_profiles)

        dlg_tag_filter: list[frozenset[str]] = [frozenset()]
        dlg_tag_exclude: list[frozenset[str]] = [frozenset()]
        dlg_filter_timer = QTimer(dlg)
        dlg_filter_timer.setSingleShot(True)

        def _dlg_profiles_matched(q_raw: str) -> list[dict[str, object]]:
            tokens = profile_search_tokens(q_raw)
            tag_filter = dlg_tag_filter[0]
            tag_exclude = dlg_tag_exclude[0]
            matched: list[tuple[int, dict[str, object]]] = []
            for i, p in enumerate(dlg_profiles):
                if not isinstance(p, dict):
                    continue
                if not profile_matches_search(p, tokens):
                    continue
                if not profile_matches_tag_filter(p, tag_filter, tag_exclude):
                    continue
                matched.append((i, p))
            matched.sort(key=lambda ip: profile_search_rank(ip[1], tokens, q_raw, ip[0]))
            return [p for _i, p in matched]

        def _dlg_upload_pause_click(pid: str) -> None:
            self._ask_reset_upload_cooldown_for_profile(
                pid,
                dialog_parent=dlg,
                dialog_profiles_interaction=dlg_interaction,
            )

        dlg_interaction = ProfilesListInteraction(
            lw,
            self._upload_store,
            on_upload_pause_click=_dlg_upload_pause_click,
            upload_pause=self._upload_pause_between_uploads(),
        )
        dlg_interaction.populate(dlg_profiles, last_upload_map, preserve_checked=preselect)

        def _apply_dlg_profiles_filter() -> None:
            visible = _dlg_profiles_matched(dlg_query.text())
            pids = [_profile_id(p) for p in visible]
            pids = [x for x in pids if x]
            filtered_last = {k: last_upload_map[k] for k in pids if k in last_upload_map}
            dlg_interaction.populate(visible, filtered_last, prune_checked_to_existing=False)
            _update_dlg_upload_profile_count()

        def _schedule_dlg_profiles_filter() -> None:
            dlg_filter_timer.start(150)

        dlg_search_row, dlg_query = self._make_dlg_profiles_search_row(
            dlg,
            dlg_profiles,
            dlg_tag_filter,
            dlg_tag_exclude,
            on_changed=_apply_dlg_profiles_filter,
        )
        dlg_filter_timer.timeout.connect(_apply_dlg_profiles_filter)
        dlg_query.textChanged.connect(_schedule_dlg_profiles_filter)

        def _dlg_select_filter(mode: str) -> None:
            visible = _dlg_profiles_matched(dlg_query.text())
            by_id = self._profiles_by_id_map(visible)
            pids = list(by_id.keys())
            filtered_last = {k: last_upload_map[k] for k in pids if k in last_upload_map}
            dlg_interaction.select_checked_by_filter(mode, by_id, filtered_last)

        n_inputs = len(self._selected_input_files or [])
        try:
            copies_n = max(1, int(self.copies_per_file.value()))
        except Exception:
            copies_n = 1

        def _planned_videos_count() -> int:
            tab = self._montage_tab(mode) if self._is_montage_mode(mode) else None
            if tab is not None and hasattr(tab, "copies_per_track"):
                try:
                    return max(1, int(tab.copies_per_track.value()))
                except Exception:
                    return 1
            try:
                copies = max(1, int(self.copies_per_file.value()))
            except Exception:
                copies = copies_n
            return max(0, n_inputs) * copies

        dlg_profile_count_lbl = QLabel("")
        dlg_profile_count_lbl.setObjectName("hint")
        dlg_profile_count_lbl.setWordWrap(True)

        dlg_raise_videos_btn = QPushButton("")
        dlg_raise_videos_btn.setObjectName("secondary")
        dlg_raise_videos_btn.setVisible(False)

        dlg_raise_videos_schedule_btn = QPushButton("")
        dlg_raise_videos_schedule_btn.setObjectName("secondary")
        dlg_raise_videos_schedule_btn.setVisible(False)

        def _raise_videos_to_profile_count(profile_count: int) -> None:
            target = max(0, int(profile_count))
            if target <= 0:
                return
            tab = self._montage_tab(mode) if self._is_montage_mode(mode) else None
            if tab is not None and hasattr(tab, "copies_per_track"):
                tab.copies_per_track.setValue(target)
                tab.save_settings()
            elif n_inputs > 0:
                need_copies = (target + n_inputs - 1) // n_inputs
                self.copies_per_file.setValue(max(1, need_copies))

        def _on_raise_videos_clicked() -> None:
            _raise_videos_to_profile_count(dlg_interaction.checked_count())
            _update_dlg_upload_profile_count()

        def _on_raise_videos_schedule_clicked() -> None:
            n = dlg_interaction.checked_count()
            target = n * schedule_slots_visible
            _raise_videos_to_profile_count(target)
            _update_dlg_upload_profile_count()

        dlg_raise_videos_btn.clicked.connect(_on_raise_videos_clicked)
        dlg_raise_videos_schedule_btn.clicked.connect(_on_raise_videos_schedule_clicked)

        if mode == "slicing":
            planned_label = "Будет нарезано видео"
            only_label = "(только нарезка)."
        elif mode == "stitching":
            planned_label = "Будет склеено видео"
            only_label = "(только склейка)."
        else:
            planned_label = "Будет уникализировано видео"
            only_label = "(только уникализация)."

        def _update_dlg_upload_profile_count() -> None:
            n = dlg_interaction.checked_count()
            shown = dlg_interaction.lw.count()
            q = dlg_query.text().strip()
            pv = _planned_videos_count()
            lines = [f"{planned_label}: {pv}"]
            if q or dlg_tag_filter[0] or dlg_tag_exclude[0]:
                lines.append(f"Показано профилей: {shown} из {total_dlg_profiles}")
            if n <= 0:
                lines.append(
                    f"Выбрано профилей для залива: 0 — без залива в YouTube {only_label}"
                )
            else:
                lines.append(f"Выбрано профилей для залива: {n}")
            dlg_profile_count_lbl.setText("\n".join(lines))
            can_raise_base = n > 0 and (
                self._is_montage_mode(mode) or n_inputs > 0
            )
            can_raise_profiles = can_raise_base and n > pv
            schedule_multi = (
                schedule_publish_cb.isChecked() and schedule_slots_visible > 1
            )
            required_schedule = n * schedule_slots_visible if schedule_multi else 0
            can_raise_schedule = (
                can_raise_base and schedule_multi and required_schedule > pv
            )

            if can_raise_profiles:
                dlg_raise_videos_btn.setText(f"Увеличить число видео до {n}")
                dlg_raise_videos_btn.setVisible(True)
            else:
                dlg_raise_videos_btn.setVisible(False)

            if can_raise_schedule:
                dlg_raise_videos_schedule_btn.setText(
                    f"Увеличить число видео до {required_schedule}"
                )
                dlg_raise_videos_schedule_btn.setVisible(True)
            else:
                dlg_raise_videos_schedule_btn.setVisible(False)

        dlg_interaction.selection_changed.connect(_update_dlg_upload_profile_count)
        schedule_publish_cb.toggled.connect(lambda _c: _update_dlg_upload_profile_count())
        btn_add_schedule_time.clicked.connect(
            lambda: _update_dlg_upload_profile_count()
        )
        btn_remove_schedule_time.clicked.connect(
            lambda: _update_dlg_upload_profile_count()
        )
        _update_dlg_upload_profile_count()

        if not ids:
            QMessageBox.warning(
                self,
                "Zaliver",
                "В загруженных профилях не найдено ни одного валидного ID.",
            )
            return None

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Старт")
        btn_dlg_cancel = btns.button(QDialogButtonBox.StandardButton.Cancel)
        btn_dlg_cancel.setText("Отмена")
        btn_dlg_cancel.setObjectName("danger")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)

        is_ig_upload = self._platform == PLATFORM_INSTAGRAM
        desc_label = QLabel("Описание:")
        grid.addWidget(
            QLabel("Название:"),
            0,
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
        )
        grid.addWidget(title_row, 0, 1)
        grid.addWidget(
            desc_label,
            1,
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
        )
        grid.addWidget(desc_row, 1, 1)
        grid.addWidget(publish_before_checks_cb, 2, 1)
        grid.addWidget(keep_studio_title_cb, 3, 1)
        grid.addWidget(upload_as_ready_cb, 4, 1)
        grid.addWidget(schedule_publish_cb, 5, 1)
        grid.addWidget(schedule_warmup_group, 6, 1)
        grid.addWidget(schedule_times_widget, 7, 1)
        if is_ig_upload:
            # YouTube-only: описание, проверки Studio, название из настроек, отложка.
            for w in (
                desc_label,
                desc_row,
                publish_before_checks_cb,
                keep_studio_title_cb,
                schedule_publish_cb,
                schedule_warmup_group,
                schedule_times_widget,
            ):
                w.setVisible(False)
            title_edit.setPlaceholderText(
                "Подпись к Reels (необязательно). "
                "Можно использовать переменные: {date}, {profile}, {video}, {index}… "
                "Enter — новая строка."
            )
        elif self._platform == PLATFORM_YT_INST:
            title_edit.setPlaceholderText(
                "Название YouTube / подпись Instagram. "
                "Переменные: {date}, {profile}, {video}, {index}… "
                "Enter — новая строка."
            )
        profiles_col = QWidget()
        profiles_col_l = QVBoxLayout(profiles_col)
        profiles_col_l.setContentsMargins(0, 0, 0, 0)
        profiles_col_l.setSpacing(8)
        profiles_col_l.addWidget(dlg_profile_count_lbl)
        profiles_col_l.addWidget(dlg_raise_videos_btn)
        profiles_col_l.addWidget(dlg_raise_videos_schedule_btn)
        profiles_col_l.addLayout(dlg_search_row)
        dlg_sel_row, _dlg_checked_lbl = self._build_profiles_selection_toolbar(
            dlg,
            dlg_interaction,
            on_select_filter=_dlg_select_filter,
            on_clear=dlg_interaction.clear_checked_selection,
        )
        profiles_col_l.addLayout(dlg_sel_row)
        profiles_col_l.addWidget(lw, 1)

        grid.addWidget(QLabel("Профили:"), 8, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(profiles_col, 8, 1)
        grid.addWidget(btns, 9, 0, 1, 2)
        grid.setRowStretch(8, 1)

        title_edit.setFocus()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None

        keep_studio_title = (
            False if is_ig_upload else keep_studio_title_cb.isChecked()
        )
        schedule_publish = (
            False if is_ig_upload else schedule_publish_cb.isChecked()
        )
        schedule_warmup_shorts = schedule_publish and schedule_warmup_cb.isChecked()
        schedule_warmup_hashtag_raw = (
            schedule_warmup_hashtag_edit.text().strip()
            if schedule_warmup_shorts and schedule_warmup_hashtag_cb.isChecked()
            else ""
        )
        schedule_warmup_hashtag = schedule_warmup_hashtag_raw
        while schedule_warmup_hashtag.startswith("#"):
            schedule_warmup_hashtag = schedule_warmup_hashtag[1:].lstrip()
        schedule_warmup_hashtag = "".join(schedule_warmup_hashtag.split())
        schedule_warmup_shorts_recommendations = (
            schedule_warmup_recommend_cb.isChecked()
            if schedule_warmup_shorts and not schedule_warmup_hashtag
            else False
        )
        schedule_warmup_search_query = (
            schedule_warmup_search_edit.text().strip()
            if (
                schedule_warmup_shorts
                and not schedule_warmup_shorts_recommendations
                and not schedule_warmup_hashtag
            )
            else ""
        )
        if (
            schedule_warmup_shorts
            and schedule_warmup_hashtag_cb.isChecked()
            and not schedule_warmup_hashtag
        ):
            QMessageBox.warning(
                self,
                "Zaliver",
                "Укажите хэштег для прогрева Shorts.",
            )
            return None
        if (
            schedule_warmup_shorts
            and not schedule_warmup_shorts_recommendations
            and not schedule_warmup_search_query
            and not schedule_warmup_hashtag
        ):
            QMessageBox.warning(
                self,
                "Zaliver",
                "Укажите поисковый запрос или хэштег для прогрева Shorts "
                "либо включите «Рекомендации Shorts».",
            )
            return None
        schedule_times_iso: list[str] = []
        if schedule_publish:
            schedule_times_msk = _collect_schedule_times_msk()
            sched_err = validate_schedule_times(schedule_times_msk)
            if sched_err:
                QMessageBox.warning(self, "Zaliver", sched_err)
                return None
            schedule_times_iso = [t.isoformat() for t in sorted(schedule_times_msk)]
        title = (title_edit.toPlainText() or "").strip()
        if title and not keep_studio_title:
            if not is_ig_upload:
                show_youtube_title_warnings(self, [title])
            self._upload_store.remember_upload_title(title, platform=self._platform)
        description = (
            "" if is_ig_upload else (desc_edit.toPlainText() or "").strip()
        )
        publish_before_checks = (
            True if is_ig_upload else publish_before_checks_cb.isChecked()
        )
        upload_as_ready = bool(upload_as_ready_cb.isChecked())
        try:
            self._settings.setValue("upload_as_ready", upload_as_ready)
        except Exception:
            pass
        picked = dlg_interaction.batch_profile_ids()

        # Если профили не выбраны, считаем, что пользователь хочет только уникализировать видео,
        # без загрузки в YouTube. В этом случае title не обязателен.
        if not picked:
            return {
                "title": title,
                "description": description,
                "profile_ids": "",
                "publish_before_checks": publish_before_checks,
                "keep_studio_title": keep_studio_title,
                "upload_as_ready": upload_as_ready,
                "schedule_publish": schedule_publish,
                "schedule_times_iso": schedule_times_iso,
                "schedule_warmup_shorts": schedule_warmup_shorts,
                "schedule_warmup_shorts_recommendations": schedule_warmup_shorts_recommendations,
                "schedule_warmup_search_query": schedule_warmup_search_query,
                "schedule_warmup_hashtag": schedule_warmup_hashtag,
            }
        if not is_ig_upload and not keep_studio_title and not title:
            QMessageBox.warning(self, "Zaliver", "Название видео обязательно для загрузки в YouTube.")
            return None

        return {
            "title": title,
            "description": description,
            "profile_ids": ",".join(picked),
            "publish_before_checks": publish_before_checks,
            "keep_studio_title": keep_studio_title,
            "upload_as_ready": upload_as_ready,
            "schedule_publish": schedule_publish,
            "schedule_times_iso": schedule_times_iso,
            "schedule_warmup_shorts": schedule_warmup_shorts,
            "schedule_warmup_shorts_recommendations": schedule_warmup_shorts_recommendations,
            "schedule_warmup_search_query": schedule_warmup_search_query,
            "schedule_warmup_hashtag": schedule_warmup_hashtag,
        }

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._sync_ffmpeg_install_row()

    def _sync_ffmpeg_install_row(self) -> None:
        needs = needs_ffmpeg_install_prompt()
        if not needs:
            self._ffmpeg_row.setVisible(False)
            return
        self._ffmpeg_row.setVisible(True)
        if sys.platform == "darwin":
            btn = f"Установить {MACOS_BREW_FFMPEG_FORMULA}"
            self.btn_install_ffmpeg.setText(btn)
            if macos_ffmpeg_needs_full_install():
                hint = (
                    "Найден обычный ffmpeg без фильтра drawtext (текст на видео не заработает). "
                    f"Нажмите «{btn}» — Homebrew поставит полную сборку "
                    f"(brew install {MACOS_BREW_FFMPEG_FORMULA})."
                )
            else:
                hint = (
                    "ffmpeg/ffprobe не найдены — без них обработка недоступна. "
                    f"Кнопка справа: Homebrew (brew install {MACOS_BREW_FFMPEG_FORMULA})."
                )
        else:
            btn = "Установить ffmpeg"
            self.btn_install_ffmpeg.setText(btn)
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
        if not needs_ffmpeg_install_prompt():
            self._sync_ffmpeg_install_row()
            return

        dlg = QProgressDialog(self)
        dlg_title = (
            f"Установка {MACOS_BREW_FFMPEG_FORMULA}"
            if sys.platform == "darwin"
            else "Установка ffmpeg"
        )
        dlg.setWindowTitle(dlg_title)
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
        try:
            mf = self._settings.value("background_music_files", [], type=list) or []
        except Exception:
            mf = []
        raw_music = [str(x) for x in mf if str(x).strip()]
        pruned: list[str] = []
        for p in raw_music:
            try:
                pp = Path(p)
                if pp.is_file():
                    pruned.append(str(pp.resolve()))
            except OSError:
                continue
        self._background_music_files = pruned
        if len(pruned) != len(raw_music):
            self._settings.setValue("background_music_files", list(pruned))
            try:
                self._settings.sync()
            except Exception:
                pass
        if hasattr(self, "delete_after_upload"):
            self.delete_after_upload.setChecked(
                bool(self._settings.value("delete_after_upload", False, type=bool))
            )
        if hasattr(self, "background_music"):
            self.background_music.setChecked(
                bool(self._settings.value("background_music_enabled", False, type=bool))
            )
        if hasattr(self, "use_gpu"):
            self.use_gpu.setChecked(
                bool(self._settings.value("use_gpu_enabled", False, type=bool))
            )
        if hasattr(self, "use_gpu_finalize"):
            self.use_gpu_finalize.setChecked(
                bool(self._settings.value("use_gpu_finalize_enabled", False, type=bool))
            )
        if hasattr(self, "slice_fps_mode"):
            fps_mode = str(
                self._settings.value("slice/fps_mode", DEFAULT_SLICE_FPS_MODE, type=str)
                or DEFAULT_SLICE_FPS_MODE
            )
            if fps_mode.strip().lower() in ("auto", "авто"):
                fps_mode = DEFAULT_SLICE_FPS_MODE
            idx = self.slice_fps_mode.findData(fps_mode)
            self.slice_fps_mode.blockSignals(True)
            self.slice_fps_mode.setCurrentIndex(idx if idx >= 0 else 0)
            self.slice_fps_mode.blockSignals(False)
        if hasattr(self, "background_music_mix"):
            self.background_music_mix.setChecked(
                bool(self._settings.value("background_music_mix_with_source", False, type=bool))
            )
        if hasattr(self, "background_music_volume"):
            try:
                vv_lo = int(
                    self._settings.value(
                        "background_music_volume_pct_min",
                        self._settings.value("background_music_volume_pct", 35),
                        type=int,
                    )
                )
            except Exception:
                vv_lo = 35
            try:
                vv_hi = int(
                    self._settings.value(
                        "background_music_volume_pct_max",
                        vv_lo,
                        type=int,
                    )
                )
            except Exception:
                vv_hi = vv_lo
            vv_lo = max(0, min(100, vv_lo))
            vv_hi = max(0, min(100, vv_hi))
            self.background_music_volume.blockSignals(True)
            self.background_music_volume.setValues(vv_lo, vv_hi)
            self.background_music_volume.blockSignals(False)
        self._sync_music_list_widget()
        self._update_music_mix_controls()
        if hasattr(self, "text_overlay_enabled"):
            self.text_overlay_enabled.setChecked(
                bool(self._settings.value("text_overlay_enabled", True, type=bool))
            )
            shared_text = self._settings.value("text_overlay_text", "", type=str) or ""
            if str(shared_text).strip() in {
                "GAME IN BIO",
                "5.000.000$ GIVEAWAY IN BIO",
            }:
                shared_text = ""
                try:
                    self._settings.setValue("text_overlay_text", "")
                except Exception:
                    pass
            self._set_uniquify_text_overlay_text(str(shared_text))
            self.refresh_text_overlay_recent()
            if hasattr(self, "_slice_tab"):
                self._slice_tab.set_text_overlay_text(str(shared_text))
            if hasattr(self, "_stitch_tab"):
                self._stitch_tab.set_text_overlay_text(str(shared_text))
            self.text_overlay_from_middle.setChecked(
                bool(
                    self._settings.value("text_overlay_from_middle", True, type=bool)
                )
            )
            try:
                fs = int(self._settings.value("text_overlay_font_size", 95, type=int))
            except Exception:
                fs = 95
            self.text_overlay_font_size.setValue(max(12, min(240, fs)))
            orient = self._settings.value("text_overlay_orientation", "vertical", type=str) or "vertical"
            idx = self.text_overlay_orientation.findData(
                "horizontal" if orient == "horizontal" else "vertical"
            )
            if idx >= 0:
                self.text_overlay_orientation.setCurrentIndex(idx)
            self._text_overlay_glow_color = (
                self._settings.value("text_overlay_glow_color", "#00FFFF", type=str) or "#00FFFF"
            )
            self._text_overlay_text_color = (
                self._settings.value("text_overlay_text_color", "#FFFFFF", type=str) or "#FFFFFF"
            )
            self.text_overlay_glow_enabled.setChecked(
                bool(self._settings.value("text_overlay_glow_enabled", True, type=bool))
            )
            try:
                ls = int(self._settings.value("text_overlay_letter_spacing", 0, type=int))
            except Exception:
                ls = 0
            self.text_overlay_letter_spacing.setValue(max(-20, min(80, ls)))
            self._text_overlay_font_path = (
                self._settings.value("text_overlay_font_path", "", type=str) or ""
            ).strip()
            self._populate_text_overlay_font_combo()
            self.text_overlay_font_bold.setChecked(
                bool(self._settings.value("text_overlay_font_bold", True, type=bool))
            )
            try:
                ax = float(self._settings.value("text_overlay_anchor_x", 0.5, type=float))
                ay = float(self._settings.value("text_overlay_anchor_y", 0.15, type=float))
            except Exception:
                ax, ay = 0.5, 0.15
            try:
                waf_lo = float(
                    self._settings.value(
                        "text_overlay_wave_amp_frac_min",
                        self._settings.value(
                            "text_overlay_wave_amp_frac", NEON_WAVE_AMP_FRAC
                        ),
                        type=float,
                    )
                )
            except Exception:
                waf_lo = NEON_WAVE_AMP_FRAC
            try:
                waf_hi = float(
                    self._settings.value(
                        "text_overlay_wave_amp_frac_max",
                        waf_lo,
                        type=float,
                    )
                )
            except Exception:
                waf_hi = waf_lo
            try:
                wfs_lo = float(
                    self._settings.value(
                        "text_overlay_wave_frame_speed_min",
                        self._settings.value(
                            "text_overlay_wave_frame_speed", NEON_WAVE_FRAME_SPEED
                        ),
                        type=float,
                    )
                )
            except Exception:
                wfs_lo = NEON_WAVE_FRAME_SPEED
            try:
                wfs_hi = float(
                    self._settings.value(
                        "text_overlay_wave_frame_speed_max",
                        wfs_lo,
                        type=float,
                    )
                )
            except Exception:
                wfs_hi = wfs_lo
            waf_lo = max(0.0, min(0.35, waf_lo))
            waf_hi = max(0.0, min(0.35, waf_hi))
            wfs_lo = max(0.0, min(0.25, wfs_lo))
            wfs_hi = max(0.0, min(0.25, wfs_hi))
            self.text_overlay_wave_amp.blockSignals(True)
            self.text_overlay_wave_amp.setValues(
                int(round(waf_lo * 100)), int(round(waf_hi * 100))
            )
            self.text_overlay_wave_amp.blockSignals(False)
            self.text_overlay_wave_speed.blockSignals(True)
            self.text_overlay_wave_speed.setValues(
                int(round(wfs_lo * 100)), int(round(wfs_hi * 100))
            )
            self.text_overlay_wave_speed.blockSignals(False)
            self._sync_text_overlay_color_btn(
                self.text_overlay_glow_btn, self._text_overlay_glow_color
            )
            self._sync_text_overlay_color_btn(
                self.text_overlay_text_btn, self._text_overlay_text_color
            )
            self._sync_text_overlay_preview(ax, ay)
            self._update_text_overlay_controls()
        self._load_fx_enable_settings()

    def _load_fx_enable_settings(self) -> None:
        if not hasattr(self, "fx_brightness_enabled"):
            return
        pairs = [
            ("fx_brightness_enabled", self.fx_brightness_enabled),
            ("fx_contrast_enabled", self.fx_contrast_enabled),
            ("fx_saturation_enabled", self.fx_saturation_enabled),
            ("fx_scale_enabled", self.fx_scale_enabled),
            ("fx_noise_enabled", self.fx_noise_enabled),
            ("playback_speed_enabled", self.audio_speed),
        ]
        self._fx_loading = True
        try:
            for key, cb in pairs:
                cb.setChecked(bool(self._settings.value(key, True, type=bool)))
        finally:
            self._fx_loading = False
        self._sync_fx_enable_slider_states()

    def _save_folder_settings(self) -> None:
        self._settings.setValue("output_folder", self.output_dir_edit.text().strip())
        if hasattr(self, "delete_after_upload"):
            self._settings.setValue(
                "delete_after_upload", bool(self.delete_after_upload.isChecked())
            )
        self._settings.setValue("input_files", list(self._selected_input_files))
        self._settings.setValue("background_music_files", list(self._background_music_files))
        if hasattr(self, "background_music"):
            self._settings.setValue(
                "background_music_enabled", bool(self.background_music.isChecked())
            )
        if hasattr(self, "use_gpu"):
            self._settings.setValue("use_gpu_enabled", bool(self.use_gpu.isChecked()))
        if hasattr(self, "use_gpu_finalize"):
            self._settings.setValue(
                "use_gpu_finalize_enabled", bool(self.use_gpu_finalize.isChecked())
            )
        if hasattr(self, "slice_fps_mode"):
            self._settings.setValue(
                "slice/fps_mode",
                str(self.slice_fps_mode.currentData() or DEFAULT_SLICE_FPS_MODE),
            )
        if hasattr(self, "background_music_mix"):
            self._settings.setValue(
                "background_music_mix_with_source",
                bool(self.background_music_mix.isChecked()),
            )
        if hasattr(self, "background_music_volume"):
            self._settings.setValue(
                "background_music_volume_pct",
                int(round(self.background_music_volume.lowValue())),
            )
            self._settings.setValue(
                "background_music_volume_pct_min",
                int(round(self.background_music_volume.lowValue())),
            )
            self._settings.setValue(
                "background_music_volume_pct_max",
                int(round(self.background_music_volume.highValue())),
            )
        if hasattr(self, "text_overlay_enabled"):
            self._settings.setValue(
                "text_overlay_enabled", bool(self.text_overlay_enabled.isChecked())
            )
            self._settings.setValue(
                "text_overlay_text", self.text_overlay_edit.toPlainText()
            )
            self._settings.setValue(
                "text_overlay_from_middle",
                bool(self.text_overlay_from_middle.isChecked()),
            )
            self._settings.setValue(
                "text_overlay_font_size", int(self.text_overlay_font_size.value())
            )
            orient = self.text_overlay_orientation.currentData()
            self._settings.setValue(
                "text_overlay_orientation",
                orient if isinstance(orient, str) else "vertical",
            )
            self._settings.setValue("text_overlay_glow_color", self._text_overlay_glow_color)
            self._settings.setValue("text_overlay_text_color", self._text_overlay_text_color)
            self._settings.setValue(
                "text_overlay_glow_enabled", bool(self.text_overlay_glow_enabled.isChecked())
            )
            self._settings.setValue(
                "text_overlay_letter_spacing", int(self.text_overlay_letter_spacing.value())
            )
            self._settings.setValue("text_overlay_font_path", self._text_overlay_font_path)
            self._settings.setValue(
                "text_overlay_font_bold", bool(self.text_overlay_font_bold.isChecked())
            )
            ax, ay = self.text_overlay_preview.anchor()
            self._settings.setValue("text_overlay_anchor_x", float(ax))
            self._settings.setValue("text_overlay_anchor_y", float(ay))
            waf_lo, waf_hi, wfs_lo, wfs_hi = self._text_overlay_wave_values()
            self._settings.setValue("text_overlay_wave_amp_frac", float(waf_lo))
            self._settings.setValue("text_overlay_wave_amp_frac_min", float(waf_lo))
            self._settings.setValue("text_overlay_wave_amp_frac_max", float(waf_hi))
            self._settings.setValue("text_overlay_wave_frame_speed", float(wfs_lo))
            self._settings.setValue("text_overlay_wave_frame_speed_min", float(wfs_lo))
            self._settings.setValue("text_overlay_wave_frame_speed_max", float(wfs_hi))
        if hasattr(self, "fx_brightness_enabled"):
            self._settings.setValue(
                "fx_brightness_enabled", bool(self.fx_brightness_enabled.isChecked())
            )
            self._settings.setValue(
                "fx_contrast_enabled", bool(self.fx_contrast_enabled.isChecked())
            )
            self._settings.setValue(
                "fx_saturation_enabled", bool(self.fx_saturation_enabled.isChecked())
            )
            self._settings.setValue(
                "fx_scale_enabled", bool(self.fx_scale_enabled.isChecked())
            )
            self._settings.setValue(
                "fx_noise_enabled", bool(self.fx_noise_enabled.isChecked())
            )
            self._settings.setValue(
                "playback_speed_enabled", bool(self.audio_speed.isChecked())
            )
            self._settings.setValue("fx_crop_jitter_enabled", False)
            self._settings.setValue("fx_seed_enabled", False)
            self._settings.setValue("audio_chorus_enabled", False)

    def _antidetect_kind(self) -> str:
        """Режим антидетекта из настроек: local | remote (логика без UI-выбора)."""
        stored = (
            self._settings.value("antydetect/default_browser", "local", type=str) or "local"
        )
        return _normalize_antidetect_kind(str(stored))

    def _legacy_dolphin_token(self) -> str:
        """Токен Dolphin больше не настраивается в UI (legacy из QSettings)."""
        return (
            self._settings.value("antydetect/dolphin_token", "", type=str) or ""
        ).strip()

    def _update_profiles_section_header(self) -> None:
        if not hasattr(self, "_profiles_title"):
            return
        kind = self._antidetect_kind()
        if kind == "remote":
            self._profiles_title.setText("Профили (удалённый антидетект)")
        else:
            self._profiles_title.setText("Профили (локальный антидетект)")
        pause_short = format_upload_pause_short(self._upload_pause_between_uploads())
        self._profiles_hint.setText(
            f"Отметьте квадратиками профили для залива; «Пауза {pause_short}» — можно ли снова загружать "
            "(клик по оранжевой подписи сбрасывает паузу)."
        )
        if hasattr(self, "_dolphin_query"):
            self._dolphin_query.setPlaceholderText(
                "Поиск по загруженным профилям (имя, ID, движок)…"
            )
        self._sync_profiles_tab_action_buttons()

    def _sync_profiles_tab_action_buttons(self) -> None:
        kind = self._antidetect_kind()
        own = _is_own_antidetect_kind(kind)
        busy = (
            self._profiles_availability_running
            or self._profiles_register_running
            or self._profiles_2fa_running
            or self._profiles_channel_setup_running
            or self._profiles_warmup_running
            or self._profiles_promote_running
            or self._profiles_cookie_farm_running
            or self._profiles_tags_clear_running
            or self._profiles_refresh_running
        )
        if hasattr(self, "_btn_profiles_clear_zaliver_tags"):
            self._btn_profiles_clear_zaliver_tags.setEnabled(own and not busy)
        if hasattr(self, "_btn_profiles_check_availability"):
            self._btn_profiles_check_availability.setEnabled(not busy)
        if hasattr(self, "_btn_profiles_register_accounts"):
            self._btn_profiles_register_accounts.setEnabled(not busy)
        if hasattr(self, "_btn_profiles_connect_2fa"):
            self._btn_profiles_connect_2fa.setEnabled(not busy)
        if hasattr(self, "_btn_profiles_warmup"):
            self._btn_profiles_warmup.setEnabled(not busy)
        if hasattr(self, "_btn_profiles_promote"):
            self._btn_profiles_promote.setEnabled(not busy)
        if hasattr(self, "_btn_profiles_cookie_farm"):
            self._btn_profiles_cookie_farm.setEnabled(not busy)
        if hasattr(self, "_btn_profiles_refresh"):
            self._btn_profiles_refresh.setEnabled(not busy)
        if hasattr(self, "_btn_profiles_import_accounts"):
            self._btn_profiles_import_accounts.setEnabled(own and not busy)
        self._sync_profiles_platform_actions_visibility()

    def _sync_profiles_platform_actions_visibility(self) -> None:
        is_ig = self._platform == PLATFORM_INSTAGRAM
        if hasattr(self, "_btn_profiles_register_accounts"):
            self._btn_profiles_register_accounts.setVisible(is_ig)
        if hasattr(self, "_btn_profiles_connect_2fa"):
            self._btn_profiles_connect_2fa.setVisible(is_ig)
        # Продвижение: Shorts (YT) или Reels (IG).
        if hasattr(self, "_btn_profiles_promote"):
            self._btn_profiles_promote.setVisible(True)
        # Прогрев: Shorts (YT) или Reels (IG).
        if hasattr(self, "_btn_profiles_warmup"):
            self._btn_profiles_warmup.setVisible(True)
        if hasattr(self, "_uploaded_ig_checker_row"):
            self._uploaded_ig_checker_row.setVisible(is_ig)
            if is_ig:
                self._populate_uploaded_ig_checker_profiles()

    def _load_antydetect_settings(self) -> None:
        if not hasattr(self, "_local_api_base_url"):
            return
        if hasattr(self, "_dolphin_headless"):
            headless = self._settings.value(
                "antydetect/dolphin_headless", True, type=bool
            )
            self._dolphin_headless.setChecked(bool(headless))
        # Старый режим dolphin больше не поддерживается в UI.
        if (
            self._settings.value("antydetect/default_browser", "", type=str) or ""
        ).strip().lower() == "dolphin":
            self._settings.setValue("antydetect/default_browser", "local")
        if self._settings.contains("antydetect/local_api_base_url"):
            url = (self._settings.value("antydetect/local_api_base_url", "", type=str) or "").strip()
        else:
            url = DEFAULT_LOCAL_API_BASE_URL
        self._local_api_base_url.setText(url)
        if hasattr(self, "_local_api_token"):
            tok = (
                self._settings.value("antydetect/local_api_token", "", type=str) or ""
            ).strip()
            self._local_api_token.setText(tok)
            from zaliver.antydetect.local_antidetect_api import set_default_local_api_token

            set_default_local_api_token(tok)
        if hasattr(self, "_max_concurrent_browsers_slider"):
            self._max_concurrent_browsers_slider.blockSignals(True)
            self._max_concurrent_browsers_slider.setValue(
                max_concurrent_browsers_from_settings(self._settings)
            )
            self._max_concurrent_browsers_slider.blockSignals(False)
            self._update_max_concurrent_browsers_label(
                self._max_concurrent_browsers_slider.value()
            )

    def _save_antydetect_settings(self) -> None:
        if hasattr(self, "_dolphin_headless"):
            self._settings.setValue(
                "antydetect/dolphin_headless",
                bool(self._dolphin_headless.isChecked()),
            )
        if hasattr(self, "_local_api_base_url"):
            self._settings.setValue(
                "antydetect/local_api_base_url",
                (self._local_api_base_url.text() or "").strip(),
            )
        if hasattr(self, "_local_api_token"):
            tok = (self._local_api_token.text() or "").strip()
            self._settings.setValue("antydetect/local_api_token", tok)
            from zaliver.antydetect.local_antidetect_api import set_default_local_api_token

            set_default_local_api_token(tok)
        self._save_max_concurrent_browsers_setting()
        try:
            self._settings.sync()
        except Exception:
            pass
        if hasattr(self, "_settings_status"):
            self._settings_status.setText("Сохранено.")
        self._update_profiles_section_header()

    def _update_max_concurrent_browsers_label(self, value: int) -> None:
        if hasattr(self, "_max_concurrent_browsers_label"):
            self._max_concurrent_browsers_label.setText(
                _max_concurrent_browsers_label_text(value)
            )

    def _save_max_concurrent_browsers_setting(self) -> None:
        if not hasattr(self, "_max_concurrent_browsers_slider"):
            return
        self._settings.setValue(
            "antydetect/max_concurrent_browsers",
            self._max_concurrent_browsers(),
        )
        try:
            self._settings.sync()
        except Exception:
            pass

    def _max_concurrent_browsers(self) -> int:
        if hasattr(self, "_max_concurrent_browsers_slider"):
            return clamp_max_concurrent_browsers(
                self._max_concurrent_browsers_slider.value()
            )
        return max_concurrent_browsers_from_settings(self._settings)

    def _settings_for(self, platform: str) -> PlatformSettings:
        """Настройки конкретной платформы (Yt+Inst читает youtube / instagram отдельно)."""
        store = getattr(self._settings, "store", self._settings)
        return PlatformSettings(store, platform)

    def _load_youtube_settings(self) -> None:
        if not hasattr(self, "_youtube_api_key"):
            return
        yt = self._settings_for(PLATFORM_YOUTUBE)
        key = (yt.value("youtube/api_key", "", type=str) or "").strip()
        self._youtube_api_key.setText(key)
        if key:
            os.environ["YOUTUBE_API_KEY"] = key
        if hasattr(self, "_youtube_search_oldest"):
            search_oldest = yt.value(
                "youtube/search_oldest_channel", False, type=bool
            )
            self._youtube_search_oldest.blockSignals(True)
            self._youtube_search_oldest.setChecked(bool(search_oldest))
            self._youtube_search_oldest.blockSignals(False)
        if hasattr(self, "_stats_server_username"):
            gu = (
                yt.value("stats_server/username", "", type=str) or ""
            ).strip()
            self._stats_server_username.setText(gu)
        self._load_youtube_upload_pause_widgets(yt)

    def _upload_pause_between_uploads(
        self, platform: str | None = None
    ) -> timedelta:
        """
        Пауза между заливами из настроек платформы.
        YouTube и Instagram — свои значения; Yt+Inst — пауза Instagram.
        """
        plat = normalize_platform(platform or self._platform)
        if plat == PLATFORM_YT_INST:
            plat = PLATFORM_INSTAGRAM
        return upload_pause_from_settings(self._settings_for(plat))

    def _load_youtube_upload_pause_widgets(self, yt: PlatformSettings) -> None:
        if not hasattr(self, "_youtube_upload_pause_hours"):
            return
        if not hasattr(self, "_youtube_upload_pause_minutes"):
            return
        pause = upload_pause_from_settings(yt)
        total_mins = max(0, int(round(pause.total_seconds() / 60.0)))
        hours, mins = divmod(total_mins, 60)
        hours = max(0, min(168, hours))
        mins = max(0, min(59, mins))
        self._youtube_upload_pause_hours.blockSignals(True)
        self._youtube_upload_pause_minutes.blockSignals(True)
        self._youtube_upload_pause_hours.setValue(hours)
        self._youtube_upload_pause_minutes.setValue(mins)
        self._youtube_upload_pause_hours.blockSignals(False)
        self._youtube_upload_pause_minutes.blockSignals(False)

    def _load_instagram_settings(self) -> None:
        if not hasattr(self, "_instagram_upload_pause_hours"):
            return
        if not hasattr(self, "_instagram_upload_pause_minutes"):
            return
        pause = self._upload_pause_between_uploads(PLATFORM_INSTAGRAM)
        total_mins = max(0, int(round(pause.total_seconds() / 60.0)))
        hours, mins = divmod(total_mins, 60)
        hours = max(0, min(168, hours))
        mins = max(0, min(59, mins))
        self._instagram_upload_pause_hours.blockSignals(True)
        self._instagram_upload_pause_minutes.blockSignals(True)
        self._instagram_upload_pause_hours.setValue(hours)
        self._instagram_upload_pause_minutes.setValue(mins)
        self._instagram_upload_pause_hours.blockSignals(False)
        self._instagram_upload_pause_minutes.blockSignals(False)
        if hasattr(self, "_instagram_tabs_per_profile"):
            tabs_n = instagram_tabs_per_profile_from_settings(
                self._settings_for(PLATFORM_INSTAGRAM)
            )
            self._instagram_tabs_per_profile.blockSignals(True)
            self._instagram_tabs_per_profile.setValue(tabs_n)
            self._instagram_tabs_per_profile.blockSignals(False)
        if hasattr(self, "_instagram_crop_aspect"):
            crop = instagram_crop_aspect_from_settings(
                self._settings_for(PLATFORM_INSTAGRAM)
            )
            idx = self._instagram_crop_aspect.findData(crop)
            self._instagram_crop_aspect.blockSignals(True)
            self._instagram_crop_aspect.setCurrentIndex(idx if idx >= 0 else 0)
            self._instagram_crop_aspect.blockSignals(False)
        self._sync_instagram_tabs_setting_visibility()

    def _sync_instagram_tabs_setting_visibility(self, *_args) -> None:
        """Показывать «Вкладок на профиль» только при паузе 0 ч 0 мин."""
        if not hasattr(self, "_instagram_tabs_per_profile"):
            return
        hours = 0
        mins = 0
        if hasattr(self, "_instagram_upload_pause_hours"):
            hours = int(self._instagram_upload_pause_hours.value())
        if hasattr(self, "_instagram_upload_pause_minutes"):
            mins = int(self._instagram_upload_pause_minutes.value())
        show = hours == 0 and mins == 0
        self._instagram_tabs_per_profile.setVisible(show)
        if hasattr(self, "_instagram_tabs_per_profile_label"):
            self._instagram_tabs_per_profile_label.setVisible(show)

    def _instagram_tabs_per_profile_value(self) -> int:
        # Instagram / Yt+Inst: из UI настроек; иначе — из namespace Instagram.
        if self._platform in (PLATFORM_INSTAGRAM, PLATFORM_YT_INST) and hasattr(
            self, "_instagram_tabs_per_profile"
        ):
            return clamp_instagram_tabs_per_profile(
                self._instagram_tabs_per_profile.value()
            )
        return instagram_tabs_per_profile_from_settings(
            self._settings_for(PLATFORM_INSTAGRAM)
        )

    def _instagram_crop_aspect_value(self) -> str:
        if self._platform in (PLATFORM_INSTAGRAM, PLATFORM_YT_INST) and hasattr(
            self, "_instagram_crop_aspect"
        ):
            raw = self._instagram_crop_aspect.currentData()
            return normalize_instagram_crop_aspect(
                raw if raw is not None else DEFAULT_INSTAGRAM_CROP_ASPECT
            )
        return instagram_crop_aspect_from_settings(
            self._settings_for(PLATFORM_INSTAGRAM)
        )

    def _save_instagram_settings(self) -> None:
        if not hasattr(self, "_instagram_upload_pause_hours"):
            return
        if not hasattr(self, "_instagram_upload_pause_minutes"):
            return
        hours = max(0, min(168, int(self._instagram_upload_pause_hours.value())))
        mins = max(0, min(59, int(self._instagram_upload_pause_minutes.value())))
        total_mins = hours * 60 + mins
        ig = self._settings_for(PLATFORM_INSTAGRAM)
        ig.setValue("upload_pause_minutes", total_mins)
        # Совместимость со старым ключом (целые часы).
        ig.setValue("upload_pause_hours", hours)
        tabs_n = self._instagram_tabs_per_profile_value()
        ig.setValue(SETTINGS_KEY_INSTAGRAM_TABS_PER_PROFILE, tabs_n)
        crop = self._instagram_crop_aspect_value()
        ig.setValue(SETTINGS_KEY_INSTAGRAM_CROP_ASPECT, crop)
        try:
            ig.sync()
        except Exception:
            pass
        pause = timedelta(minutes=total_mins)
        if self._profiles_interaction is not None:
            self._profiles_interaction.set_upload_pause(pause)
        self._update_profiles_section_header()
        self._sync_upload_pause_selection_labels()
        self._sync_instagram_tabs_setting_visibility()
        if self._profiles_raw is not None:
            self._apply_profiles_filter()
        if hasattr(self, "_instagram_settings_status"):
            short = format_upload_pause_short(pause)
            extra = ""
            if total_mins <= 0:
                extra = f" Вкладок на профиль: {tabs_n}."
            self._instagram_settings_status.setText(
                f"Пауза между видео сохранена: {short}.{extra} Обрезка: {crop}."
            )

    def _sync_upload_pause_selection_labels(self) -> None:
        """Обновить подписи «Выделить…» / фильтр доступных под текущую паузу."""
        pause = self._upload_pause_between_uploads()
        short = format_upload_pause_short(pause)
        human = format_upload_pause_human(pause)
        btn = getattr(self, "_profiles_select_btn", None)
        if btn is not None:
            btn.setToolTip(
                f"Отметить профили по условию (пауза {short}, ошибки, "
                "данные учётки, старейший канал)"
            )
        act = getattr(self, "_profiles_select_avail_action", None)
        if act is not None:
            act.setText(f"Доступные (пауза {short} прошла)")
            act.setToolTip(
                "Профили, с которых снова можно заливать: прошли "
                f"{human} после последнего залива или заливов ещё не было"
            )

    def _stats_server_username_stripped(self) -> str:
        if not hasattr(self, "_stats_server_username"):
            return (
                self._settings.value("stats_server/username", "", type=str) or ""
            ).strip()
        return (self._stats_server_username.text() or "").strip()

    def _persist_stats_server_username_to_settings(self, username: str) -> None:
        gu = (username or "").strip()
        if gu:
            self._settings.setValue("stats_server/username", gu)
        else:
            try:
                self._settings.remove("stats_server/username")
            except Exception:
                self._settings.setValue("stats_server/username", "")
        try:
            self._settings.sync()
        except Exception:
            pass
        if hasattr(self, "_stats_server_username"):
            self._stats_server_username.setText(gu)

    def _save_stats_server_username_settings(self) -> None:
        self._persist_stats_server_username_to_settings(
            self._stats_server_username_stripped()
        )

    def _prompt_stats_server_username_if_empty(self) -> bool:
        if self._stats_server_username_stripped():
            return True
        dlg = QDialog(self)
        dlg.setWindowTitle("Имя пользователя")
        dlg.setModal(True)
        v = QVBoxLayout(dlg)
        edit = QLineEdit()
        edit.setText(
            (self._settings.value("stats_server/username", "", type=str) or "").strip()
        )
        v.addWidget(edit)

        row = QHBoxLayout()
        row.addStretch()
        btn_cancel = QPushButton("Отмена")
        btn_cancel.setObjectName("danger")
        btn_next = QPushButton("Далее")
        btn_next.setDefault(True)
        btn_next.setAutoDefault(True)

        def on_next() -> None:
            t = (edit.text() or "").strip()
            if not t:
                return
            self._persist_stats_server_username_to_settings(t)
            dlg.accept()

        btn_next.clicked.connect(on_next)
        btn_cancel.clicked.connect(dlg.reject)
        edit.returnPressed.connect(on_next)
        row.addWidget(btn_cancel)
        row.addWidget(btn_next)
        v.addLayout(row)

        edit.setFocus()
        return dlg.exec() == QDialog.DialogCode.Accepted

    def _save_youtube_settings(self) -> None:
        if not hasattr(self, "_youtube_api_key"):
            return
        yt = self._settings_for(PLATFORM_YOUTUBE)
        key = (self._youtube_api_key.text() or "").strip()
        key_msg = ""
        if key:
            yt.setValue("youtube/api_key", key)
            os.environ["YOUTUBE_API_KEY"] = key
            key_msg = "Ключ YouTube Data API сохранён."
        else:
            try:
                yt.remove("youtube/api_key")
            except Exception:
                yt.setValue("youtube/api_key", "")
            os.environ.pop("YOUTUBE_API_KEY", None)
            key_msg = "Ключ API очищен."

        pause_msg = ""
        if (
            self._platform == PLATFORM_YOUTUBE
            and hasattr(self, "_youtube_upload_pause_hours")
            and hasattr(self, "_youtube_upload_pause_minutes")
        ):
            hours = max(0, min(168, int(self._youtube_upload_pause_hours.value())))
            mins = max(0, min(59, int(self._youtube_upload_pause_minutes.value())))
            total_mins = hours * 60 + mins
            yt.setValue("upload_pause_minutes", total_mins)
            yt.setValue("upload_pause_hours", hours)
            pause = timedelta(minutes=total_mins)
            if self._profiles_interaction is not None:
                self._profiles_interaction.set_upload_pause(pause)
            self._update_profiles_section_header()
            self._sync_upload_pause_selection_labels()
            if self._profiles_raw is not None:
                self._apply_profiles_filter()
            pause_msg = f" Пауза между видео: {format_upload_pause_short(pause)}."

        try:
            yt.sync()
        except Exception:
            pass
        if hasattr(self, "_youtube_settings_status"):
            self._youtube_settings_status.setText(f"{key_msg}{pause_msg}".strip())

    def _on_youtube_show_key_changed(self, _state: int) -> None:
        if not hasattr(self, "_youtube_api_key") or not hasattr(self, "_youtube_show_key"):
            return
        show = bool(self._youtube_show_key.isChecked())
        self._youtube_api_key.setEchoMode(
            QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        )

    def _load_ai_settings(self) -> None:
        if not hasattr(self, "_ai_base_url"):
            return
        self._ai_base_url.setText(
            (self._settings.value("ai/base_url", "", type=str) or "").strip()
        )
        self._ai_api_key.setText(
            (self._settings.value("ai/api_key", "", type=str) or "").strip()
        )
        self._ai_model.setText(
            (self._settings.value("ai/model", "", type=str) or "").strip()
        )

    def _save_ai_settings(self) -> None:
        if not hasattr(self, "_ai_base_url"):
            return
        base_url = (self._ai_base_url.text() or "").strip().rstrip("/")
        api_key = (self._ai_api_key.text() or "").strip()
        model = (self._ai_model.text() or "").strip()

        if base_url:
            self._settings.setValue("ai/base_url", base_url)
            self._ai_base_url.setText(base_url)
        else:
            try:
                self._settings.remove("ai/base_url")
            except Exception:
                self._settings.setValue("ai/base_url", "")

        if api_key:
            self._settings.setValue("ai/api_key", api_key)
        else:
            try:
                self._settings.remove("ai/api_key")
            except Exception:
                self._settings.setValue("ai/api_key", "")

        if model:
            self._settings.setValue("ai/model", model)
        else:
            try:
                self._settings.remove("ai/model")
            except Exception:
                self._settings.setValue("ai/model", "")

        try:
            self._settings.sync()
        except Exception:
            pass
        if hasattr(self, "_ai_settings_status"):
            self._ai_settings_status.setText("Настройки ИИ сохранены.")

    def _on_ai_show_key_changed(self, _state: int) -> None:
        if not hasattr(self, "_ai_api_key") or not hasattr(self, "_ai_show_key"):
            return
        show = bool(self._ai_show_key.isChecked())
        self._ai_api_key.setEchoMode(
            QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        )

    def _on_ai_magic_generate(
        self,
        *,
        default_prompt_id: str,
        window_title: str,
        apply_text: Callable[[str], None],
        parent: QWidget | None = None,
        ask_reply_lines: bool = False,
        default_reply_lines: int = 1,
    ) -> None:
        """Диалог выбора промпта → генерация → вставка результата в поле."""
        parent_w = parent or self
        base_url = (self._settings.value("ai/base_url", "", type=str) or "").strip()
        api_key = (self._settings.value("ai/api_key", "", type=str) or "").strip()
        model = (self._settings.value("ai/model", "", type=str) or "").strip()
        if not base_url or not api_key or not model:
            QMessageBox.warning(
                parent_w,
                "ИИ",
                "Заполните URL эндпоинта, API key и модель в разделе «Настройки» → «ИИ».",
            )
            return

        prompts: list[tuple[str, str, str]] = []
        if hasattr(self, "_ai_tab"):
            prompts = self._ai_tab.prompts()
        if not prompts:
            QMessageBox.warning(
                parent_w,
                "ИИ",
                "Нет промптов. Добавьте их во вкладке «ИИ».",
            )
            return

        dlg = AiGenerateDialog(
            prompts=prompts,
            default_prompt_id=default_prompt_id,
            base_url=base_url,
            api_key=api_key,
            model=model,
            window_title=window_title,
            ask_reply_lines=ask_reply_lines,
            default_reply_lines=default_reply_lines,
            parent=parent_w,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            apply_text(dlg.result_text())
        except RuntimeError:
            pass

    def _youtube_search_oldest_channel(self) -> bool:
        if hasattr(self, "_youtube_search_oldest") and self._platform != PLATFORM_INSTAGRAM:
            return bool(self._youtube_search_oldest.isChecked())
        return bool(
            self._settings_for(PLATFORM_YOUTUBE).value(
                "youtube/search_oldest_channel", False, type=bool
            )
        )

    def _on_youtube_search_oldest_changed(self, _state: int) -> None:
        if not hasattr(self, "_youtube_search_oldest"):
            return
        self._settings_for(PLATFORM_YOUTUBE).setValue(
            "youtube/search_oldest_channel",
            bool(self._youtube_search_oldest.isChecked()),
        )
        try:
            self._settings_for(PLATFORM_YOUTUBE).sync()
        except Exception:
            pass

    @staticmethod
    def _profile_search_blob(profile: dict[str, object]) -> str:
        parts: list[str] = []

        def add(v: object) -> None:
            if v is None:
                return
            if isinstance(v, str):
                s = v.strip()
                if s:
                    parts.append(s)
                return
            if isinstance(v, (int, float, bool)):
                parts.append(str(v))
                return
            if isinstance(v, dict):
                for vv in v.values():
                    add(vv)
                return
            if isinstance(v, list):
                for vv in v:
                    add(vv)

        add(profile.get("id"))
        add(profile.get("browserProfileId"))
        add(profile.get("profile_id"))
        add(profile.get("name"))
        add(profile.get("mainWebsite"))
        add(profile.get("description"))
        add(profile.get("tags"))
        add(profile.get("proxy"))
        add(profile.get("notes"))
        add(profile.get("note"))
        add(profile.get("status"))
        add(profile.get("statusId"))
        return " ".join(parts).lower()

    def _profiles_visible_matched(self) -> list[dict[str, object]]:
        raw = self._profiles_raw or []
        q_raw = (self._dolphin_query.text() if hasattr(self, "_dolphin_query") else "") or ""
        tokens = profile_search_tokens(q_raw)
        tag_filter = getattr(self, "_profiles_tag_filter", frozenset()) or frozenset()
        tag_exclude = getattr(self, "_profiles_tag_exclude", frozenset()) or frozenset()
        matched: list[tuple[int, dict[str, object]]] = []
        for i, p in enumerate(raw):
            if not isinstance(p, dict):
                continue
            if not profile_matches_search(p, tokens):
                continue
            if not profile_matches_tag_filter(p, tag_filter, tag_exclude):
                continue
            matched.append((i, p))
        matched.sort(key=lambda ip: profile_search_rank(ip[1], tokens, q_raw, ip[0]))
        return [p for _i, p in matched]

    def _profiles_filter_active(self) -> bool:
        q = (self._dolphin_query.text() if hasattr(self, "_dolphin_query") else "") or ""
        if q.strip():
            return True
        if getattr(self, "_profiles_tag_filter", frozenset()):
            return True
        return bool(getattr(self, "_profiles_tag_exclude", frozenset()))

    def _sync_profiles_tag_filter_button(self) -> None:
        if not hasattr(self, "_btn_profiles_filter_tags"):
            return
        self._sync_tag_filter_button(
            self._btn_profiles_filter_tags,
            getattr(self, "_profiles_tag_filter", frozenset()) or frozenset(),
            getattr(self, "_profiles_tag_exclude", frozenset()) or frozenset(),
        )

    @staticmethod
    def _sync_tag_filter_button(
        btn: QPushButton,
        tag_filter: frozenset[str],
        tag_exclude: frozenset[str] | None = None,
    ) -> None:
        exclude = tag_exclude or frozenset()
        n_in = len(tag_filter)
        n_ex = len(exclude)
        if n_in or n_ex:
            if n_in and n_ex:
                btn.setText(f"По тэгам ({n_in}/−{n_ex})")
            elif n_ex:
                btn.setText(f"По тэгам (−{n_ex})")
            else:
                btn.setText(f"По тэгам ({n_in})")
            parts: list[str] = []
            if n_in:
                parts.append(f"включить: {n_in}")
            if n_ex:
                parts.append(f"исключить: {n_ex}")
            btn.setToolTip(
                "Активен фильтр по тегам ("
                + ", ".join(parts)
                + "). Нажмите, чтобы изменить или сбросить."
            )
        else:
            btn.setText("По тэгам")
            btn.setToolTip(
                "Отфильтровать список по выбранным тегам "
                "(можно включить и исключить теги)."
            )

    def _make_dlg_profiles_search_row(
        self,
        parent: QWidget,
        profiles: list[dict[str, object]],
        tag_filter_box: list[frozenset[str]],
        tag_exclude_box: list[frozenset[str]] | None = None,
        *,
        on_changed: Callable[[], None],
    ) -> tuple[QHBoxLayout, QLineEdit]:
        """Строка поиска + «По тэгам» для диалогов выбора профилей."""
        if not tag_filter_box:
            tag_filter_box.append(frozenset())
        if tag_exclude_box is None:
            tag_exclude_box = [frozenset()]
        if not tag_exclude_box:
            tag_exclude_box.append(frozenset())

        query = QLineEdit()
        query.setPlaceholderText("Поиск по профилям (имя, ID, теги)…")

        btn = QPushButton("По тэгам")
        btn.setObjectName("secondary")
        btn.setAutoDefault(False)
        btn.setDefault(False)
        self._sync_tag_filter_button(btn, tag_filter_box[0], tag_exclude_box[0])

        def _open_tags() -> None:
            dlg = ProfileTagsFilterDialog(
                tags=collect_all_tags_from_profiles(profiles),
                initially_checked=tag_filter_box[0],
                initially_excluded=tag_exclude_box[0],
                parent=parent,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            tag_filter_box[0] = frozenset(dlg.selected_tags())
            tag_exclude_box[0] = frozenset(dlg.excluded_tags())
            self._sync_tag_filter_button(btn, tag_filter_box[0], tag_exclude_box[0])
            on_changed()

        btn.clicked.connect(_open_tags)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(query, 1)
        row.addWidget(btn)
        return row, query

    def _open_profiles_tag_filter_dialog(self) -> None:
        raw = self._profiles_raw
        if raw is None:
            QMessageBox.information(
                self,
                "Фильтр по тегам",
                "Сначала загрузите профили (кнопка «Обновить»).",
            )
            return
        tags = collect_all_tags_from_profiles(raw)
        dlg = ProfileTagsFilterDialog(
            tags=tags,
            initially_checked=self._profiles_tag_filter,
            initially_excluded=self._profiles_tag_exclude,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._profiles_tag_filter = frozenset(dlg.selected_tags())
        self._profiles_tag_exclude = frozenset(dlg.excluded_tags())
        self._sync_profiles_tag_filter_button()
        self._apply_profiles_filter()

    def _profiles_by_id_map(self, profiles: list[dict[str, object]]) -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        for p in profiles:
            pid = _profile_id(p)
            if pid:
                out[pid] = p
        return out

    def _build_profiles_selection_toolbar(
        self,
        parent: QWidget,
        interaction: ProfilesListInteraction,
        *,
        on_select_filter: Callable[[str], None],
        on_clear: Callable[[], None] | None = None,
    ) -> tuple[FlowLayout, QLabel]:
        """Строка «Выделено» + «Выделить…» + «Снять выделение» для списка профилей."""
        row = FlowLayout(hspacing=8, vspacing=6)
        lbl = QLabel("Выделено: 0")
        lbl.setObjectName("hint")
        lbl.setToolTip("Число профилей, отмеченных для залива")

        def _sync_count() -> None:
            lbl.setText(f"Выделено: {interaction.checked_count()}")

        btn_clear = QPushButton("Снять выделение")
        btn_clear.setObjectName("secondary")
        btn_clear.setAutoDefault(False)
        btn_clear.setDefault(False)
        btn_clear.clicked.connect(on_clear or interaction.clear_checked_selection)

        btn_select = QPushButton("Выделить…")
        btn_select.setObjectName("secondary")
        btn_select.setAutoDefault(False)
        btn_select.setDefault(False)
        pause_short = format_upload_pause_short(self._upload_pause_between_uploads())
        pause_human = format_upload_pause_human(self._upload_pause_between_uploads())
        btn_select.setToolTip(
            f"Отметить профили по условию (пауза {pause_short}, ошибки, данные учётки, старейший канал)"
        )
        select_menu = QMenu(parent)
        act_all = select_menu.addAction("Все видимые")
        act_all.setToolTip("Отметить все профили в списке")
        act_all.triggered.connect(lambda: on_select_filter("all"))
        act_avail = select_menu.addAction(f"Доступные (пауза {pause_short} прошла)")
        act_avail.setToolTip(
            "Профили, с которых снова можно заливать: прошли "
            f"{pause_human} после последнего залива "
            "или заливов ещё не было"
        )
        act_avail.triggered.connect(lambda: on_select_filter("available"))
        # Главная вкладка «Профили» — запомним для обновления после смены паузы.
        if interaction is self._profiles_interaction:
            self._profiles_select_btn = btn_select
            self._profiles_select_avail_action = act_avail
        act_clean = select_menu.addAction("Без ошибок в статусах")
        act_clean.setToolTip(
            "Прокси активен, нет тегов/флагов с «ошибка», профиль не помечен после сбоев залива"
        )
        act_clean.triggered.connect(lambda: on_select_filter("no_errors"))
        act_errors = select_menu.addAction("С ошибками в статусах")
        act_errors.setToolTip(
            "Прокси неактивен, есть теги/флаги с «ошибка» или профиль помечен после сбоев залива"
        )
        act_errors.triggered.connect(lambda: on_select_filter("with_errors"))
        select_menu.addSeparator()
        act_no_account = select_menu.addAction("Без данных в учётке")
        act_no_account.setToolTip(
            "Профили без логина, пароля и 2FA YouTube в custom_data (свой антидетект)"
        )
        act_no_account.triggered.connect(lambda: on_select_filter("no_account_data"))
        act_no_oldest = select_menu.addAction("Без определённого старейшего канала")
        act_no_oldest.setToolTip(
            "Профили, для которых ещё не сохранён yt_oldest_name после проверки каналов"
        )
        act_no_oldest.triggered.connect(lambda: on_select_filter("no_oldest_channel"))
        btn_select.setMenu(select_menu)

        row.addWidget(lbl)
        row.addWidget(btn_select)
        row.addWidget(btn_clear)

        interaction.selection_changed.connect(_sync_count)
        _sync_count()
        return row, lbl

    def _prompt_profiles_selection_dialog(
        self,
        *,
        window_title: str,
        ok_text: str = "Применить",
        count_label_prefix: str = "Выбрано профилей",
        preselect: set[str] | None = None,
    ) -> list[str] | None:
        """Диалог выбора профилей (как при заливе): поиск, фильтры, чекбоксы."""
        profiles = self._profiles_raw or []
        if not profiles:
            return None

        dlg = QDialog(self)
        dlg.setWindowTitle(window_title)
        dlg.setModal(True)
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            dlg.setMinimumSize(
                QSize(
                    min(720, max(480, geo.width() - 48)),
                    min(620, max(360, geo.height() - 48)),
                )
            )
            dlg.resize(min(860, geo.width() - 24), min(720, geo.height() - 24))
        else:
            dlg.setMinimumSize(QSize(720, 620))
            dlg.resize(860, 720)

        layout = QVBoxLayout(dlg)
        layout.setSpacing(10)

        lw = QListWidget()
        lw.setObjectName("uploadProfilesList")
        lw.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        lw.setSpacing(4)
        lw.setMinimumHeight(420)
        lw.setMouseTracking(True)

        ids: list[str] = []
        dlg_profiles: list[dict[str, object]] = []
        for p in profiles:
            if not isinstance(p, dict):
                continue
            pid = str(
                p.get("id") or p.get("browserProfileId") or p.get("profile_id") or ""
            ).strip()
            if not pid:
                continue
            ids.append(pid)
            dlg_profiles.append(p)

        if not ids:
            QMessageBox.warning(
                self,
                window_title,
                "В загруженных профилях не найдено ни одного валидного ID.",
            )
            return None

        last_upload_map = self._upload_store.last_uploaded_at_by_profiles(
            ids, platform=self._platform
        )
        total_dlg_profiles = len(dlg_profiles)

        dlg_tag_filter: list[frozenset[str]] = [frozenset()]
        dlg_tag_exclude: list[frozenset[str]] = [frozenset()]
        dlg_filter_timer = QTimer(dlg)
        dlg_filter_timer.setSingleShot(True)

        def _dlg_profiles_matched(q_raw: str) -> list[dict[str, object]]:
            tokens = profile_search_tokens(q_raw)
            tag_filter = dlg_tag_filter[0]
            tag_exclude = dlg_tag_exclude[0]
            matched: list[tuple[int, dict[str, object]]] = []
            for i, p in enumerate(dlg_profiles):
                if not isinstance(p, dict):
                    continue
                if not profile_matches_search(p, tokens):
                    continue
                if not profile_matches_tag_filter(p, tag_filter, tag_exclude):
                    continue
                matched.append((i, p))
            matched.sort(key=lambda ip: profile_search_rank(ip[1], tokens, q_raw, ip[0]))
            return [p for _i, p in matched]

        def _dlg_upload_pause_click(pid: str) -> None:
            self._ask_reset_upload_cooldown_for_profile(
                pid,
                dialog_parent=dlg,
                dialog_profiles_interaction=dlg_interaction,
            )

        dlg_interaction = ProfilesListInteraction(
            lw,
            self._upload_store,
            on_upload_pause_click=_dlg_upload_pause_click,
            on_account_data_click=self._open_profile_account_data_dialog,
            on_preview_click=self._open_profile_cdp_preview,
            upload_pause=self._upload_pause_between_uploads(),
        )
        dlg_interaction.populate(
            dlg_profiles,
            last_upload_map,
            preserve_checked=preselect or set(),
        )

        def _apply_dlg_profiles_filter() -> None:
            visible = _dlg_profiles_matched(dlg_query.text())
            pids = [_profile_id(p) for p in visible]
            pids = [x for x in pids if x]
            filtered_last = {k: last_upload_map[k] for k in pids if k in last_upload_map}
            dlg_interaction.populate(visible, filtered_last, prune_checked_to_existing=False)
            _update_dlg_profile_count()

        def _schedule_dlg_profiles_filter() -> None:
            dlg_filter_timer.start(150)

        dlg_search_row, dlg_query = self._make_dlg_profiles_search_row(
            dlg,
            dlg_profiles,
            dlg_tag_filter,
            dlg_tag_exclude,
            on_changed=_apply_dlg_profiles_filter,
        )
        dlg_filter_timer.timeout.connect(_apply_dlg_profiles_filter)
        dlg_query.textChanged.connect(_schedule_dlg_profiles_filter)

        def _dlg_select_filter(mode: str) -> None:
            visible = _dlg_profiles_matched(dlg_query.text())
            by_id = self._profiles_by_id_map(visible)
            pids = list(by_id.keys())
            filtered_last = {k: last_upload_map[k] for k in pids if k in last_upload_map}
            dlg_interaction.select_checked_by_filter(mode, by_id, filtered_last)

        dlg_profile_count_lbl = QLabel("")
        dlg_profile_count_lbl.setObjectName("hint")
        dlg_profile_count_lbl.setWordWrap(True)

        def _update_dlg_profile_count() -> None:
            n = dlg_interaction.checked_count()
            shown = dlg_interaction.lw.count()
            q = dlg_query.text().strip()
            lines = [f"{count_label_prefix}: {n}"]
            if q or dlg_tag_filter[0] or dlg_tag_exclude[0]:
                lines.append(f"Показано профилей: {shown} из {total_dlg_profiles}")
            dlg_profile_count_lbl.setText("\n".join(lines))

        dlg_interaction.selection_changed.connect(_update_dlg_profile_count)
        _update_dlg_profile_count()

        dlg_sel_row, _dlg_checked_lbl = self._build_profiles_selection_toolbar(
            dlg,
            dlg_interaction,
            on_select_filter=_dlg_select_filter,
            on_clear=dlg_interaction.clear_checked_selection,
        )

        layout.addWidget(dlg_profile_count_lbl)
        layout.addLayout(dlg_search_row)
        layout.addLayout(dlg_sel_row)
        layout.addWidget(lw, 1)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QDialogButtonBox.StandardButton.Ok).setText(ok_text)
        btn_dlg_cancel = btns.button(QDialogButtonBox.StandardButton.Cancel)
        btn_dlg_cancel.setText("Отмена")
        btn_dlg_cancel.setObjectName("danger")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None

        profile_ids = dlg_interaction.batch_profile_ids()
        if not profile_ids:
            QMessageBox.warning(
                self,
                window_title,
                "Отметьте хотя бы один профиль.",
            )
            return None
        return profile_ids

    def _refresh_profiles_list_view(self) -> int:
        if self._profiles_interaction is None:
            return 0
        visible = self._profiles_visible_matched()
        pids = [_profile_id(p) for p in visible]
        pids = [x for x in pids if x]
        last_upload_map = self._upload_store.last_uploaded_at_by_profiles(
            pids, platform=self._platform
        )
        kind = (
            self._antidetect_kind()
        )
        show_account = _is_own_antidetect_kind(kind if isinstance(kind, str) else "")
        show_preview = isinstance(kind, str) and kind.strip() == "remote"
        is_ig = self._platform == PLATFORM_INSTAGRAM
        account_btn_text = "Данные Insta" if is_ig else "Данные учетки"
        account_tip = (
            "Логин, пароль и 2FA Instagram (custom_data локального антидетекта)"
            if is_ig
            else "Логин, пароль и 2FA YouTube (custom_data локального антидетекта)"
        )
        self._profiles_interaction.populate(
            visible,
            last_upload_map,
            prune_checked_to_existing=False,
            show_account_data_button=show_account,
            show_preview_button=show_preview,
            account_data_button_text=account_btn_text,
            account_data_tooltip=account_tip,
            show_gmail_data_button=show_account and is_ig,
            gmail_data_tooltip=(
                "Логин, пароль и 2FA Gmail (стартовые значения из yt_*; "
                "сохранение в gmail_login / gmail_password / gmail_2fa)"
                if is_ig
                else None
            ),
        )
        self._profiles_list_render_gen += 1
        return len(visible)

    def _schedule_profiles_filter(self) -> None:
        if not hasattr(self, "_profiles_list"):
            return
        if self._profiles_raw is None:
            return
        self._profiles_filter_timer.start(150)

    def _apply_profiles_filter(self) -> None:
        if not hasattr(self, "_profiles_list"):
            return
        raw = self._profiles_raw
        if raw is None:
            return

        shown = self._refresh_profiles_list_view()
        total = len(raw)
        n_checked = (
            self._profiles_interaction.checked_count()
            if self._profiles_interaction
            else 0
        )
        if self._profiles_filter_active():
            base = f"Фильтр: показано {shown} из {total}"
        else:
            base = f"Загружено профилей: {total}"
        if n_checked:
            self._profiles_status.setText(f"{base}. Отмечено: {n_checked}")
        else:
            self._profiles_status.setText(base)

    def _clear_profiles_checked_selection(self) -> None:
        if self._profiles_interaction is not None:
            self._profiles_interaction.clear_checked_selection()

    def _select_profiles_checked_filter(self, mode: str) -> None:
        if self._profiles_interaction is None or self._profiles_raw is None:
            return
        visible = self._profiles_visible_matched()
        by_id = self._profiles_by_id_map(visible)
        pids = list(by_id.keys())
        last_upload_map = self._upload_store.last_uploaded_at_by_profiles(
            pids, platform=self._platform
        )
        self._profiles_interaction.select_checked_by_filter(mode, by_id, last_upload_map)

    def _on_profiles_checked_selection_changed(self) -> None:
        if hasattr(self, "_lbl_checked_profiles_count") and self._profiles_interaction:
            n = self._profiles_interaction.checked_count()
            self._lbl_checked_profiles_count.setText(f"Выделено: {n}")
        if self._profiles_raw is not None:
            self._apply_profiles_filter_status_line_only()

    def _apply_profiles_filter_status_line_only(self) -> None:
        raw = self._profiles_raw or []
        shown = self._profiles_list.count()
        total = len(raw)
        n_checked = (
            self._profiles_interaction.checked_count()
            if self._profiles_interaction
            else 0
        )
        if self._profiles_filter_active():
            base = f"Фильтр: показано {shown} из {total}"
        else:
            base = f"Загружено профилей: {total}"
        if n_checked:
            self._profiles_status.setText(f"{base}. Отмечено: {n_checked}")
        else:
            self._profiles_status.setText(base)

    def _refresh_antydetect_profiles(self) -> None:
        if not hasattr(self, "_profiles_list"):
            return
        self._profiles_filter_timer.stop()
        self._save_antydetect_settings()

        token = self._legacy_dolphin_token()

        kind = self._antidetect_kind()
        base_url = self._own_antidetect_base_url_from_settings(kind)

        self._profiles_refresh_running = True
        self._sync_profiles_tab_action_buttons()
        self._profiles_status.setText("Загрузка профилей…")

        t = threading.Thread(
            target=self._profiles_worker,
            kwargs={"kind": kind, "token": token, "base_url": base_url},
            daemon=True,
        )
        t.start()

    def _profiles_worker(self, *, kind: str, token: str, base_url: str) -> None:
        del token  # Dolphin JWT больше не используется
        try:
            k = _normalize_antidetect_kind(kind)
            u = (base_url or "").strip()
            if not u:
                self._profiles_load_failed.emit(
                    f"Укажите базовый URL {_own_antidetect_api_label(k)} API в настройках "
                    "и сохраните."
                )
                return
            api = LocalAntidetectHttpAPI(u)
            try:
                raw = api.list_profiles()
            finally:
                api.close()
            profiles = [normalize_local_profile_for_ui(p) for p in raw]
            self._profiles_loaded.emit(profiles)
        except LocalAntidetectError as e:
            self._profiles_load_failed.emit(
                f"Проверьте, что сервис {_own_antidetect_api_label(kind)} антидетекта доступен "
                f"и базовый URL верен.\n{e}"
            )
        except Exception as e:
            self._profiles_load_failed.emit(repr(e))

    def _on_profiles_loaded(self, profiles_obj: object) -> None:
        self._profiles_refresh_running = False
        self._sync_profiles_tab_action_buttons()
        profiles = profiles_obj if isinstance(profiles_obj, list) else []
        cleaned: list[dict[str, object]] = [p for p in profiles if isinstance(p, dict)]
        self._profiles_raw = cleaned
        if self._profiles_interaction is not None:
            existing = {_profile_id(p) for p in cleaned}
            existing.discard("")
            self._profiles_interaction.checked_profile_ids.intersection_update(existing)
        self._apply_profiles_filter()
        self._populate_uploaded_ig_checker_profiles()

    def _start_profiles_availability_check(self) -> None:
        if self._profiles_availability_running:
            QMessageBox.information(
                self,
                "Проверка доступности",
                "Проверка уже выполняется. Дождитесь завершения.",
            )
            return
        if self._profiles_raw is None:
            QMessageBox.warning(
                self,
                "Проверка доступности",
                "Сначала загрузите список профилей (кнопка «Обновить»).",
            )
            return
        profile_ids = self._collect_checked_profile_ids()
        if not profile_ids:
            target = (
                "вход в Instagram"
                if self._platform == PLATFORM_INSTAGRAM
                else "YouTube Studio"
            )
            QMessageBox.warning(
                self,
                "Проверка доступности",
                f"Отметьте квадратиками профили, для которых нужно проверить {target}.",
            )
            return

        token = self._legacy_dolphin_token()
        kind = self._antidetect_kind()
        base_url = self._own_antidetect_base_url_from_settings(kind)

        headless = True
        if hasattr(self, "_dolphin_headless"):
            headless = bool(self._dolphin_headless.isChecked())
        else:
            headless = bool(
                self._settings.value("antydetect/dolphin_headless", True, type=bool)
            )

        try:
            remote_cdp = self._remote_cdp_launch_options_for_kind(kind)
        except LocalAntidetectError as e:
            QMessageBox.warning(self, "Проверка доступности", str(e))
            return

        self._profiles_availability_running = True
        self._sync_profiles_tab_action_buttons()
        check_label = (
            "Instagram"
            if self._platform == PLATFORM_INSTAGRAM
            else "Studio"
        )
        self._profiles_status.setText(
            f"Проверка доступности {check_label}: 0 / {len(profile_ids)}…"
        )
        headless_label = "headless" if headless else "с окном браузера"
        max_concurrent = self._max_concurrent_browsers()

        try:
            self._append_log(
                f"[availability] Старт проверки {len(profile_ids)} профилей "
                f"({headless_label}, до {max_concurrent} параллельно)…"
            )

            threading.Thread(
                target=self._profiles_availability_worker,
                kwargs={
                    "profile_ids": profile_ids,
                    "kind": kind,
                    "token": token,
                    "base_url": base_url,
                    "headless": headless,
                    "remote_cdp": remote_cdp,
                    "max_concurrent": max_concurrent,
                },
                daemon=True,
            ).start()
        except Exception as e:
            self._profiles_availability_running = False
            self._sync_profiles_tab_action_buttons()
            self._append_log(f"[availability] Не удалось запустить проверку: {e!r}")
            QMessageBox.critical(
                self,
                "Проверка доступности",
                f"Не удалось запустить проверку:\n{e}",
            )

    def _profiles_availability_worker(
        self,
        *,
        profile_ids: list[str],
        kind: str,
        token: str,
        base_url: str,
        headless: bool,
        remote_cdp: RemoteCdpLaunchOptions | None = None,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_BROWSERS,
    ) -> None:
        from zaliver.antydetect.antic_open import (
            check_instagram_availability_in_local_antidetect_profile,
            check_instagram_availability_in_profile,
            check_studio_availability_in_local_antidetect_profile,
            check_studio_availability_in_profile,
            set_log_sink,
        )
        from zaliver.youtube_upload.multi_availability_checker import (
            MultiProfileAvailabilityChecker,
        )
        from zaliver.antydetect.profile_tags import (
            INSTAGRAM_AVAILABILITY_ERROR_TAG,
            INSTAGRAM_AVAILABILITY_SUCCESS_TAG,
            STUDIO_AVAILABILITY_ERROR_TAG,
            STUDIO_AVAILABILITY_SUCCESS_TAG,
        )

        set_log_sink(self._ui_log_line.emit)
        kind_s = (kind or "").strip()
        base_u = (base_url or "").strip() or DEFAULT_LOCAL_API_BASE_URL
        is_instagram = self._platform == PLATFORM_INSTAGRAM
        success_tag = (
            INSTAGRAM_AVAILABILITY_SUCCESS_TAG
            if is_instagram
            else STUDIO_AVAILABILITY_SUCCESS_TAG
        )
        error_tag = (
            INSTAGRAM_AVAILABILITY_ERROR_TAG
            if is_instagram
            else STUDIO_AVAILABILITY_ERROR_TAG
        )

        def _check_one(pid: str) -> None:
            if is_instagram:
                sess_login, sess_pwd, sess_2fa = self._instagram_session_credentials(
                    pid
                )
                creds = self._profile_login_credentials(pid)
                if _is_own_antidetect_kind(kind_s):
                    u = (base_url or "").strip()
                    if not u:
                        raise LocalAntidetectError(
                            f"Укажите базовый URL {_own_antidetect_api_label(kind_s)} API в настройках."
                        )
                    check_instagram_availability_in_local_antidetect_profile(
                        pid,
                        base_url=u,
                        headless=headless,
                        remote_cdp=remote_cdp,
                        session_login=sess_login,
                        session_password=sess_pwd,
                        session_twofa=sess_2fa,
                        login_credentials=creds,
                    )
                else:
                    check_instagram_availability_in_profile(
                        pid,
                        local_token=token or None,
                        headless=headless,
                        session_login=sess_login,
                        session_password=sess_pwd,
                        session_twofa=sess_2fa,
                        login_credentials=creds,
                    )
                return

            creds = self._profile_login_credentials(pid)
            yt_oldest = self._profile_yt_oldest_name(pid) or None
            search_oldest = self._youtube_search_oldest_channel()
            if _is_own_antidetect_kind(kind_s):
                u = (base_url or "").strip()
                if not u:
                    raise LocalAntidetectError(
                        f"Укажите базовый URL {_own_antidetect_api_label(kind_s)} API в настройках."
                    )
                check_studio_availability_in_local_antidetect_profile(
                    pid,
                    base_url=u,
                    headless=headless,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    search_oldest_channel=search_oldest,
                    remote_cdp=remote_cdp,
                )
            else:
                check_studio_availability_in_profile(
                    pid,
                    local_token=token or None,
                    headless=headless,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    search_oldest_channel=search_oldest,
                )

        def _on_profile_done(pid: str, ok: bool, err: str) -> None:
            if not _is_own_antidetect_kind(kind_s):
                if not ok:
                    self._ui_log_line.emit(
                        f"[availability] profile={pid}: теги проверки доступности "
                        "доступны только для своего антидетекта."
                    )
                return
            self._apply_zaliver_profile_tags_from_worker(
                profile_id=pid,
                kind=kind_s,
                base_url=base_u,
                updates=[(ok, success_tag, error_tag)],
                log_prefix="availability",
            )

        def _on_progress(done: int, total: int, profile_id: str) -> None:
            self._studio_availability_progress.emit(done, total, profile_id)

        mgr = MultiProfileAvailabilityChecker(
            profile_ids=profile_ids,
            check_one=_check_one,
            on_profile_done=_on_profile_done,
            on_progress=_on_progress,
            log_sink=self._ui_log_line.emit,
            max_concurrent=max_concurrent,
        )
        try:
            ok_n, fail_n, failed_ids = mgr.run()
            self._last_availability_failed_ids = list(failed_ids)
            self._studio_availability_finished.emit(ok_n, fail_n)
        except Exception as e:
            self._ui_log_line.emit(f"[availability] Критическая ошибка воркера: {e!r}")
            self._last_availability_failed_ids = list(profile_ids)
            self._studio_availability_finished.emit(0, len(profile_ids))

    def _start_profiles_instagram_register(self) -> None:
        if self._platform != PLATFORM_INSTAGRAM:
            QMessageBox.information(
                self,
                "Регистрация Instagram",
                "Регистрация доступна только в режиме Instagram.",
            )
            return
        if self._profiles_register_running:
            QMessageBox.information(
                self,
                "Регистрация Instagram",
                "Регистрация уже выполняется. Дождитесь завершения.",
            )
            return
        if self._profiles_raw is None:
            QMessageBox.warning(
                self,
                "Регистрация Instagram",
                "Сначала загрузите список профилей (кнопка «Обновить»).",
            )
            return
        profile_ids = self._collect_checked_profile_ids()
        if not profile_ids:
            QMessageBox.warning(
                self,
                "Регистрация Instagram",
                "Отметьте квадратиками профили для регистрации аккаунтов Instagram.",
            )
            return

        token = self._legacy_dolphin_token()
        kind = self._antidetect_kind()
        base_url = self._own_antidetect_base_url_from_settings(kind)

        headless = True
        if hasattr(self, "_dolphin_headless"):
            headless = bool(self._dolphin_headless.isChecked())
        else:
            headless = bool(
                self._settings.value("antydetect/dolphin_headless", True, type=bool)
            )

        try:
            remote_cdp = self._remote_cdp_launch_options_for_kind(kind)
        except LocalAntidetectError as e:
            QMessageBox.warning(self, "Регистрация Instagram", str(e))
            return

        self._profiles_register_running = True
        self._sync_profiles_tab_action_buttons()
        self._profiles_status.setText(
            f"Регистрация Instagram: 0 / {len(profile_ids)}…"
        )
        headless_label = "headless" if headless else "с окном браузера"
        max_concurrent = self._max_concurrent_browsers()

        try:
            self._append_log(
                f"[ig-register] Старт регистрации {len(profile_ids)} профилей "
                f"({headless_label}, до {max_concurrent} параллельно)…"
            )
            threading.Thread(
                target=self._profiles_instagram_register_worker,
                kwargs={
                    "profile_ids": profile_ids,
                    "kind": kind,
                    "token": token,
                    "base_url": base_url,
                    "headless": headless,
                    "remote_cdp": remote_cdp,
                    "max_concurrent": max_concurrent,
                },
                daemon=True,
            ).start()
        except Exception as e:
            self._profiles_register_running = False
            self._sync_profiles_tab_action_buttons()
            self._append_log(f"[ig-register] Не удалось запустить: {e!r}")
            QMessageBox.critical(
                self,
                "Регистрация Instagram",
                f"Не удалось запустить регистрацию:\n{e}",
            )

    def _profiles_instagram_register_worker(
        self,
        *,
        profile_ids: list[str],
        kind: str,
        token: str,
        base_url: str,
        headless: bool,
        remote_cdp: RemoteCdpLaunchOptions | None = None,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_BROWSERS,
    ) -> None:
        from zaliver.antydetect.antic_open import (
            register_instagram_account_in_local_antidetect_profile,
            register_instagram_account_in_profile,
            set_log_sink,
        )
        from zaliver.youtube_upload.multi_availability_checker import (
            MultiProfileAvailabilityChecker,
        )
        from zaliver.antydetect.profile_tags import (
            IG_REGISTER_ERROR_TAG,
            IG_REGISTER_SMS_ERROR_TAG,
            IG_REGISTER_SUCCESS_TAG,
            apply_ig_register_result_tag,
        )
        from zaliver.instagram_upload.register import InstagramSmsCaptchaError

        set_log_sink(self._ui_log_line.emit)
        kind_s = (kind or "").strip()
        base_u = (base_url or "").strip() or DEFAULT_LOCAL_API_BASE_URL

        def _check_one(pid: str) -> None:
            creds = self._profile_login_credentials(pid)

            def _on_manual_captcha() -> None:
                self._manual_captcha_needed.emit(pid)

            if _is_own_antidetect_kind(kind_s):
                u = (base_url or "").strip()
                if not u:
                    raise LocalAntidetectError(
                        f"Укажите базовый URL {_own_antidetect_api_label(kind_s)} API в настройках."
                    )
                register_instagram_account_in_local_antidetect_profile(
                    pid,
                    base_url=u,
                    headless=headless,
                    login_credentials=creds,
                    remote_cdp=remote_cdp,
                    on_manual_captcha=_on_manual_captcha,
                )
            else:
                register_instagram_account_in_profile(
                    pid,
                    local_token=token or None,
                    headless=headless,
                    login_credentials=creds,
                    on_manual_captcha=_on_manual_captcha,
                )

        def _on_profile_done(pid: str, ok: bool, err: str) -> None:
            if not _is_own_antidetect_kind(kind_s):
                if not ok:
                    self._ui_log_line.emit(
                        f"[ig-register] profile={pid}: теги регистрации "
                        "доступны только для своего антидетекта."
                    )
                return
            sms = (not ok) and (
                InstagramSmsCaptchaError.matches(err)
                or IG_REGISTER_SMS_ERROR_TAG in (err or "")
            )
            try:
                api = LocalAntidetectHttpAPI(base_u)
                try:
                    tag = apply_ig_register_result_tag(
                        api,
                        pid,
                        success=ok,
                        sms_captcha=sms,
                    )
                    self._ui_log_line.emit(
                        f"[ig-register] profile={pid} tag_set={tag!r}"
                    )
                finally:
                    api.close()
                error_tag = (
                    IG_REGISTER_SMS_ERROR_TAG if sms else IG_REGISTER_ERROR_TAG
                )
                from zaliver.antydetect.profile_tags import IG_REGISTER_RESULT_TAGS

                self._profile_zaliver_tags_cache_update.emit(
                    pid,
                    [
                        {
                            "success": ok,
                            "success_tag": IG_REGISTER_SUCCESS_TAG,
                            "error_tag": error_tag,
                            "strip_tags": list(IG_REGISTER_RESULT_TAGS),
                        }
                    ],
                )
            except Exception as te:
                self._ui_log_line.emit(
                    f"[ig-register] profile={pid} tag_set_failed err={te!r}"
                )

        def _on_progress(done: int, total: int, profile_id: str) -> None:
            self._instagram_register_progress.emit(done, total, profile_id)

        mgr = MultiProfileAvailabilityChecker(
            profile_ids=profile_ids,
            check_one=_check_one,
            on_profile_done=_on_profile_done,
            on_progress=_on_progress,
            log_sink=self._ui_log_line.emit,
            max_concurrent=max_concurrent,
        )
        try:
            ok_n, fail_n, failed_ids = mgr.run()
            self._last_register_failed_ids = list(failed_ids)
            self._instagram_register_finished.emit(ok_n, fail_n)
        except Exception as e:
            self._ui_log_line.emit(f"[ig-register] Критическая ошибка воркера: {e!r}")
            self._last_register_failed_ids = list(profile_ids)
            self._instagram_register_finished.emit(0, len(profile_ids))

    def _start_profiles_instagram_2fa(self) -> None:
        if self._platform != PLATFORM_INSTAGRAM:
            QMessageBox.information(
                self,
                "Подключение 2FA Instagram",
                "Подключение 2FA доступно только в режиме Instagram.",
            )
            return
        if self._profiles_2fa_running:
            QMessageBox.information(
                self,
                "Подключение 2FA Instagram",
                "Подключение 2FA уже выполняется. Дождитесь завершения.",
            )
            return
        if self._profiles_raw is None:
            QMessageBox.warning(
                self,
                "Подключение 2FA Instagram",
                "Сначала загрузите список профилей (кнопка «Обновить»).",
            )
            return
        profile_ids = self._collect_checked_profile_ids()
        if not profile_ids:
            QMessageBox.warning(
                self,
                "Подключение 2FA Instagram",
                "Отметьте квадратиками профили для подключения 2FA Instagram.",
            )
            return

        token = self._legacy_dolphin_token()
        kind = self._antidetect_kind()
        base_url = self._own_antidetect_base_url_from_settings(kind)

        headless = True
        if hasattr(self, "_dolphin_headless"):
            headless = bool(self._dolphin_headless.isChecked())
        else:
            headless = bool(
                self._settings.value("antydetect/dolphin_headless", True, type=bool)
            )

        try:
            remote_cdp = self._remote_cdp_launch_options_for_kind(kind)
        except LocalAntidetectError as e:
            QMessageBox.warning(self, "Подключение 2FA Instagram", str(e))
            return

        self._profiles_2fa_running = True
        self._sync_profiles_tab_action_buttons()
        self._profiles_status.setText(
            f"Подключение 2FA Instagram: 0 / {len(profile_ids)}…"
        )
        headless_label = "headless" if headless else "с окном браузера"
        max_concurrent = self._max_concurrent_browsers()

        try:
            self._append_log(
                f"[ig-2fa] Старт подключения 2FA для {len(profile_ids)} профилей "
                f"({headless_label}, до {max_concurrent} параллельно)…"
            )
            threading.Thread(
                target=self._profiles_instagram_2fa_worker,
                kwargs={
                    "profile_ids": profile_ids,
                    "kind": kind,
                    "token": token,
                    "base_url": base_url,
                    "headless": headless,
                    "remote_cdp": remote_cdp,
                    "max_concurrent": max_concurrent,
                },
                daemon=True,
            ).start()
        except Exception as e:
            self._profiles_2fa_running = False
            self._sync_profiles_tab_action_buttons()
            self._append_log(f"[ig-2fa] Не удалось запустить: {e!r}")
            QMessageBox.critical(
                self,
                "Подключение 2FA Instagram",
                f"Не удалось запустить подключение 2FA:\n{e}",
            )

    def _profiles_instagram_2fa_worker(
        self,
        *,
        profile_ids: list[str],
        kind: str,
        token: str,
        base_url: str,
        headless: bool,
        remote_cdp: RemoteCdpLaunchOptions | None = None,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_BROWSERS,
    ) -> None:
        from zaliver.antydetect.antic_open import (
            set_log_sink,
            setup_instagram_2fa_in_local_antidetect_profile,
            setup_instagram_2fa_in_profile,
        )
        from zaliver.youtube_upload.multi_availability_checker import (
            MultiProfileAvailabilityChecker,
        )
        from zaliver.antydetect.profile_tags import (
            IG_2FA_ERROR_TAG,
            IG_2FA_RESULT_TAGS,
            IG_2FA_SUCCESS_TAG,
            apply_mutually_exclusive_profile_tag,
        )

        set_log_sink(self._ui_log_line.emit)
        kind_s = (kind or "").strip()
        base_u = (base_url or "").strip() or DEFAULT_LOCAL_API_BASE_URL

        def _check_one(pid: str) -> None:
            creds = self._profile_login_credentials(pid)
            sess_login, sess_pwd, sess_2fa = self._instagram_session_credentials(pid)
            if _is_own_antidetect_kind(kind_s):
                u = (base_url or "").strip()
                if not u:
                    raise LocalAntidetectError(
                        f"Укажите базовый URL {_own_antidetect_api_label(kind_s)} API в настройках."
                    )
                setup_instagram_2fa_in_local_antidetect_profile(
                    pid,
                    base_url=u,
                    headless=headless,
                    remote_cdp=remote_cdp,
                    login_credentials=creds,
                    session_login=sess_login,
                    session_password=sess_pwd,
                    session_twofa=sess_2fa,
                    keep_open_on_error=False,
                )
            else:
                setup_instagram_2fa_in_profile(
                    pid,
                    local_token=token or None,
                    headless=headless,
                    login_credentials=creds,
                    session_login=sess_login,
                    session_password=sess_pwd,
                    session_twofa=sess_2fa,
                    keep_open_on_error=False,
                )

        def _on_profile_done(pid: str, ok: bool, err: str) -> None:
            if not _is_own_antidetect_kind(kind_s):
                if not ok:
                    self._ui_log_line.emit(
                        f"[ig-2fa] profile={pid}: теги 2FA "
                        "доступны только для своего антидетекта."
                    )
                return
            try:
                api = LocalAntidetectHttpAPI(base_u)
                try:
                    apply_mutually_exclusive_profile_tag(
                        api,
                        pid,
                        success=ok,
                        success_tag=IG_2FA_SUCCESS_TAG,
                        error_tag=IG_2FA_ERROR_TAG,
                    )
                    tag = IG_2FA_SUCCESS_TAG if ok else IG_2FA_ERROR_TAG
                    self._ui_log_line.emit(
                        f"[ig-2fa] profile={pid} tag_set={tag!r}"
                    )
                finally:
                    api.close()
                self._profile_zaliver_tags_cache_update.emit(
                    pid,
                    [
                        {
                            "success": ok,
                            "success_tag": IG_2FA_SUCCESS_TAG,
                            "error_tag": IG_2FA_ERROR_TAG,
                            "strip_tags": list(IG_2FA_RESULT_TAGS),
                        }
                    ],
                )
            except Exception as te:
                self._ui_log_line.emit(
                    f"[ig-2fa] profile={pid} tag_set_failed err={te!r}"
                )

        def _on_progress(done: int, total: int, profile_id: str) -> None:
            self._instagram_2fa_progress.emit(done, total, profile_id)

        mgr = MultiProfileAvailabilityChecker(
            profile_ids=profile_ids,
            check_one=_check_one,
            on_profile_done=_on_profile_done,
            on_progress=_on_progress,
            log_sink=self._ui_log_line.emit,
            max_concurrent=max_concurrent,
        )
        try:
            ok_n, fail_n, failed_ids = mgr.run()
            self._last_2fa_failed_ids = list(failed_ids)
            self._instagram_2fa_finished.emit(ok_n, fail_n)
        except Exception as e:
            self._ui_log_line.emit(f"[ig-2fa] Критическая ошибка воркера: {e!r}")
            self._last_2fa_failed_ids = list(profile_ids)
            self._instagram_2fa_finished.emit(0, len(profile_ids))

    def _profiles_channel_setup_dialog_title(self) -> str:
        return "Редактирование канала"

    def _sync_channel_edit_tab(self) -> None:
        if not hasattr(self, "_channel_edit_tab"):
            return
        self._channel_edit_tab.load_recent_values(
            channel_names=self._upload_store.list_recent_channel_name_fields(
                platform=self._platform
            ),
            descriptions=self._upload_store.list_recent_channel_descriptions(
                platform=self._platform
            ),
            link_titles=self._upload_store.list_recent_channel_link_titles(
                platform=self._platform
            ),
            link_urls=self._upload_store.list_recent_channel_link_urls(
                platform=self._platform
            ),
            video_titles=self._upload_store.list_recent_video_default_title_fields(
                platform=self._platform
            ),
        )
        self._channel_edit_tab.set_running(self._profiles_channel_setup_running)

    def _start_channel_setup_from_tab(self) -> None:
        title = self._profiles_channel_setup_dialog_title()
        if self._profiles_channel_setup_running:
            QMessageBox.information(
                self,
                title,
                "Настройка канала уже выполняется. Дождитесь завершения.",
            )
            return
        if not self._profiles_raw:
            QMessageBox.information(
                self,
                title,
                "Сначала загрузите список профилей (вкладка «Профили» → «Обновить»).",
            )
            return

        tab = self._channel_edit_tab
        form_err = tab.validate_form()
        if form_err:
            QMessageBox.warning(self, title, form_err)
            return

        preselect: set[str] = set()
        if self._profiles_interaction is not None:
            preselect = set(self._profiles_interaction.checked_profile_ids)

        profile_ids = self._prompt_profiles_selection_dialog(
            window_title=title,
            ok_text=(
                "Применить"
                if self._platform == PLATFORM_INSTAGRAM
                else "Применить в Studio"
            ),
            count_label_prefix="Выбрано профилей для редактирования",
            preselect=preselect,
        )
        if not profile_ids:
            return

        by_id = self._profiles_by_id_map(self._profiles_raw)
        selected_profiles = [by_id[pid] for pid in profile_ids if pid in by_id]
        if not selected_profiles:
            QMessageBox.warning(
                self,
                title,
                "Не удалось найти выбранные профили в загруженном списке.",
            )
            return

        tab.set_selected_profiles(selected_profiles)

        description = tab.channel_description()
        description_lines = tab.channel_description_lines()
        channel_links = tab.channel_links()
        link_title = tab.channel_link_title()
        link_url = tab.channel_link_url()
        if description.strip():
            self._upload_store.remember_channel_description(
                tab.channel_description_field_text(), platform=self._platform
            )
        for lt, lu in channel_links:
            self._upload_store.remember_channel_link_title(
                lt, platform=self._platform
            )
            self._upload_store.remember_channel_link_url(
                lu, platform=self._platform
            )
        video_default_titles = tab.video_default_titles_for_remember()
        if video_default_titles:
            for vt in video_default_titles:
                self._upload_store.remember_video_default_title(
                    vt, platform=self._platform
                )
        video_titles_field = tab.video_default_titles_field_text()
        if video_titles_field.strip():
            self._upload_store.remember_video_default_title_field(
                video_titles_field, platform=self._platform
            )
        channel_names = tab.channel_names_for_remember()
        if channel_names:
            self._upload_store.remember_channel_names(
                channel_names, platform=self._platform
            )
        channel_names_field = tab.channel_names_field_text()
        if channel_names_field.strip():
            self._upload_store.remember_channel_name_field(
                channel_names_field, platform=self._platform
            )
        assignments = tab.profile_assignments()
        has_text_fill = tab.has_channel_text_fill()
        has_video_title_fill = tab.has_video_default_title()
        has_customization = tab.has_profile_customization()
        change_language = tab.change_language_before_edit()
        is_ig = self._platform == PLATFORM_INSTAGRAM

        if has_video_title_fill and not is_ig:
            show_youtube_title_warnings(
                self,
                tab.video_default_titles_for_remember(),
                window_title="Редактирование канала",
            )

        if is_ig:
            has_ig_bio = bool(description_lines) or any(
                str(a.get("channel_description") or "").strip() for a in assignments
            )
            has_ig_avatar = any(bool(a.get("avatar_png")) for a in assignments)
            has_ig_username = any(
                bool(str(a.get("channel_name") or "").strip())
                and not bool(a.get("skip_name_change"))
                for a in assignments
            )
            if (
                not has_ig_bio
                and not has_ig_avatar
                and not has_ig_username
                and not change_language
            ):
                QMessageBox.warning(
                    self,
                    title,
                    "Для Instagram укажите юзернейм, фото, bio и/или отметьте "
                    "«Поменять язык».",
                )
                return
            has_video_title_fill = False
            has_text_fill = has_ig_bio

        if (
            not has_text_fill
            and not has_video_title_fill
            and not has_customization
            and not change_language
        ):
            return

        confirm_msg = tab.confirm_message_for_profiles(len(selected_profiles))
        answer = QMessageBox.question(
            self,
            title,
            confirm_msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        kind = self._antidetect_kind()
        kind_s = _normalize_antidetect_kind(kind if isinstance(kind, str) else "")
        if (
            has_customization
            and not is_ig
            and not _is_own_antidetect_kind(kind_s)
        ):
            QMessageBox.information(
                self,
                title,
                "Аватарки и названия доступны только для своего антидетекта "
                "(локальный или удалённый API).",
            )
            return

        token = self._legacy_dolphin_token()
        base_url = self._own_antidetect_base_url_from_settings(kind_s)
        need_base = _is_own_antidetect_kind(kind_s) and (
            has_customization or is_ig
        )
        if need_base and not (base_url or "").strip():
            QMessageBox.warning(
                self,
                title,
                f"Укажите базовый URL {_own_antidetect_api_label(kind_s)} "
                "API в настройках и сохраните.",
            )
            return

        headless = False

        try:
            remote_cdp = self._remote_cdp_launch_options_for_kind(kind_s)
        except LocalAntidetectError as e:
            QMessageBox.warning(self, title, str(e))
            return

        assignment_ids = {
            str(a.get("profile_id") or "").strip() for a in assignments
        }
        # Instagram: bio/аватар/юзернейм из assignments, либо общая смена языка / bio.
        work_profile_ids = [
            pid
            for pid in profile_ids
            if has_text_fill or change_language or pid in assignment_ids
        ]
        if not work_profile_ids:
            return

        self._profiles_channel_setup_running = True
        self._sync_profiles_tab_action_buttons()
        self._channel_edit_tab.set_running(True)
        setup_label = "Instagram" if is_ig else "Studio"
        status_line = (
            f"Редактирование профиля ({setup_label}): 0 / {len(work_profile_ids)}…"
        )
        self._profiles_status.setText(status_line)
        self._channel_edit_tab.set_status(status_line)
        max_concurrent = self._max_concurrent_browsers()
        self._append_log(
            f"[channel_setup] Старт для {len(work_profile_ids)} профилей "
            f"(с окном браузера, до {max_concurrent} параллельно"
            + (", со сменой языка" if change_language else "")
            + ")…"
        )

        threading.Thread(
            target=self._profiles_channel_setup_worker,
            kwargs={
                "profile_ids": work_profile_ids,
                "kind": kind_s,
                "token": token,
                "base_url": base_url,
                "headless": headless,
                "description": description,
                "description_lines": description_lines,
                "link_title": link_title,
                "link_url": link_url,
                "channel_links": channel_links,
                "assignments": assignments,
                "has_text_fill": has_text_fill,
                "change_language": change_language,
                "remote_cdp": remote_cdp,
                "max_concurrent": max_concurrent,
            },
            daemon=True,
        ).start()

    def _profiles_channel_setup_worker(
        self,
        *,
        profile_ids: list[str],
        kind: str,
        token: str,
        base_url: str,
        headless: bool,
        description: str,
        description_lines: list[str],
        link_title: str,
        link_url: str,
        channel_links: list[tuple[str, str]] | None = None,
        assignments: list[dict[str, object]],
        has_text_fill: bool,
        change_language: bool = False,
        remote_cdp: RemoteCdpLaunchOptions | None = None,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_BROWSERS,
    ) -> None:
        from zaliver.antydetect.antic_open import (
            set_log_sink,
            setup_channel_in_local_antidetect_profile,
            setup_channel_in_profile,
            setup_instagram_profile_in_local_antidetect_profile,
            setup_instagram_profile_in_profile,
        )
        from zaliver.youtube_upload.multi_availability_checker import (
            MultiProfileAvailabilityChecker,
        )

        set_log_sink(self._ui_log_line.emit)
        kind_s = (kind or "").strip()
        base_u = (base_url or "").strip() or DEFAULT_LOCAL_API_BASE_URL
        by_id: dict[str, dict[str, object]] = {}
        for item in assignments:
            pid = str(item.get("profile_id") or "").strip()
            if pid:
                by_id[pid] = item
        profile_index = {pid: i for i, pid in enumerate(profile_ids)}

        def _profile_name_for(pid: str) -> str:
            item = by_id.get(pid)
            if item:
                name = str(item.get("profile_name") or "").strip()
                if name:
                    return name
            for p in self._profiles_raw or []:
                if isinstance(p, dict) and _profile_id(p) == pid:
                    return _profile_name(p)
            return pid

        def _expand_channel_field(
            text: str | None,
            pid: str,
            *,
            limit_title: bool = False,
        ) -> str:
            raw = (text or "").strip()
            if not raw:
                return ""
            ctx = TitleVariableContext(
                profile_name=_profile_name_for(pid),
                video_path="",
                index=profile_index.get(pid, 0) + 1,
            )
            if limit_title:
                return expand_and_limit_title(raw, ctx).title
            return expand_title_variables(raw, ctx)

        def _description_for_profile(pid: str) -> str:
            item = by_id.get(pid)
            if item:
                row_desc = str(item.get("channel_description") or "").strip()
                if row_desc:
                    return _expand_channel_field(row_desc, pid)
            if description_lines:
                line = description_lines[
                    profile_index.get(pid, 0) % len(description_lines)
                ]
                return _expand_channel_field(line, pid)
            return _expand_channel_field(description, pid)

        def _link_for_profile(pid: str) -> tuple[str, str] | None:
            """Одна ссылка на профиль: i-я строка → i-й аккаунт (с зацикливанием)."""
            idx = profile_index.get(pid, 0)
            if channel_links:
                lt_raw, lu_raw = channel_links[idx % len(channel_links)]
            else:
                lt_raw, lu_raw = link_title, link_url
            lt = _expand_channel_field(lt_raw, pid)
            lu = _expand_channel_field(lu_raw, pid)
            if lt and lu:
                return (lt, lu)
            return None

        def _setup_one(pid: str) -> None:
            is_ig = self._platform == PLATFORM_INSTAGRAM
            item = by_id.get(pid)
            png = item.get("avatar_png") if item else None
            avatar_path: Path | None = None
            if isinstance(png, (bytes, bytearray)) and png:
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                    tf.write(bytes(png))
                    avatar_path = Path(tf.name)

            try:
                if is_ig:
                    profile_description = (
                        _description_for_profile(pid) if has_text_fill else ""
                    )
                    channel_name = (
                        _expand_channel_field(
                            str(item.get("channel_name") or ""), pid
                        )
                        or None
                        if item
                        else None
                    )
                    skip_name_change = (
                        bool(item.get("skip_name_change")) if item else False
                    )
                    ig_username = (
                        None if skip_name_change else (channel_name or None)
                    )
                    ig_login, ig_password, ig_twofa = (
                        self._instagram_session_credentials(pid)
                    )
                    if _is_own_antidetect_kind(kind_s):
                        u = (base_url or "").strip()
                        if not u:
                            raise LocalAntidetectError(
                                f"Укажите базовый URL {_own_antidetect_api_label(kind_s)} "
                                "API в настройках."
                            )
                        setup_instagram_profile_in_local_antidetect_profile(
                            pid,
                            description=profile_description or None,
                            avatar_path=avatar_path,
                            username=ig_username,
                            change_language=change_language,
                            base_url=u,
                            headless=headless,
                            remote_cdp=remote_cdp,
                            session_login=ig_login,
                            session_password=ig_password,
                            session_twofa=ig_twofa,
                        )
                    else:
                        setup_instagram_profile_in_profile(
                            pid,
                            description=profile_description or None,
                            avatar_path=avatar_path,
                            username=ig_username,
                            change_language=change_language,
                            local_token=token or None,
                            headless=headless,
                            session_login=ig_login,
                            session_password=ig_password,
                            session_twofa=ig_twofa,
                        )
                    return

                creds = self._profile_login_credentials(pid)
                yt_oldest = self._profile_yt_oldest_name(pid) or None
                search_oldest = self._youtube_search_oldest_channel()

                channel_name = (
                    _expand_channel_field(str(item.get("channel_name") or ""), pid)
                    or None
                    if item
                    else None
                )
                skip_name_change = bool(item.get("skip_name_change")) if item else False
                video_default_title = (
                    _expand_channel_field(
                        str(item.get("video_default_title") or ""),
                        pid,
                        limit_title=True,
                    )
                    or None
                    if item
                    else None
                )
                has_video_title_fill = bool(video_default_title)
                profile_description = (
                    _description_for_profile(pid) if has_text_fill else ""
                )
                profile_link = _link_for_profile(pid) if has_text_fill else None
                profile_links = [profile_link] if profile_link else None

                if _is_own_antidetect_kind(kind_s):
                    u = (base_url or "").strip()
                    if not u:
                        raise LocalAntidetectError(
                            f"Укажите базовый URL {_own_antidetect_api_label(kind_s)} "
                            "API в настройках."
                        )
                    setup_channel_in_local_antidetect_profile(
                        pid,
                        description=profile_description or None if has_text_fill else None,
                        link_title=profile_link[0] if profile_link else None,
                        link_url=profile_link[1] if profile_link else None,
                        channel_links=profile_links,
                        video_default_title=(
                            video_default_title if has_video_title_fill else None
                        ),
                        avatar_path=avatar_path,
                        channel_name=channel_name,
                        skip_name_change=skip_name_change,
                        change_language=change_language,
                        base_url=u,
                        headless=headless,
                        login_credentials=creds,
                        yt_oldest_name=yt_oldest,
                        search_oldest_channel=search_oldest,
                        remote_cdp=remote_cdp,
                    )
                else:
                    setup_channel_in_profile(
                        pid,
                        description=profile_description or None if has_text_fill else None,
                        link_title=profile_link[0] if profile_link else None,
                        link_url=profile_link[1] if profile_link else None,
                        channel_links=profile_links,
                        video_default_title=(
                            video_default_title if has_video_title_fill else None
                        ),
                        avatar_path=avatar_path,
                        channel_name=channel_name,
                        skip_name_change=skip_name_change,
                        change_language=change_language,
                        local_token=token or None,
                        headless=headless,
                        login_credentials=creds,
                        yt_oldest_name=yt_oldest,
                        search_oldest_channel=search_oldest,
                    )
            finally:
                if avatar_path is not None:
                    try:
                        avatar_path.unlink(missing_ok=True)
                    except OSError:
                        pass

        def _on_progress(done: int, total: int, profile_id: str) -> None:
            self._studio_channel_setup_progress.emit(done, total, profile_id)

        def _on_profile_done(pid: str, ok: bool, err: str) -> None:
            if not _is_own_antidetect_kind(kind_s):
                return
            from zaliver.antydetect.profile_tags import (
                AVATAR_CHANGE_ERROR_TAG,
                AVATAR_CHANGE_SUCCESS_TAG,
                DESCRIPTION_FILL_ERROR_TAG,
                DESCRIPTION_FILL_SUCCESS_TAG,
                IG_AVATAR_CHANGE_ERROR_TAG,
                IG_AVATAR_CHANGE_SUCCESS_TAG,
                IG_DESCRIPTION_FILL_ERROR_TAG,
                IG_DESCRIPTION_FILL_SUCCESS_TAG,
                IG_LANGUAGE_CHANGE_ERROR_TAG,
                IG_LANGUAGE_CHANGE_SUCCESS_TAG,
                IG_NAME_CHANGE_ERROR_TAG,
                IG_NAME_CHANGE_SUCCESS_TAG,
                LANGUAGE_CHANGE_ERROR_TAG,
                LANGUAGE_CHANGE_SUCCESS_TAG,
                LINK_FILL_ERROR_TAG,
                LINK_FILL_SUCCESS_TAG,
                NAME_CHANGE_ERROR_TAG,
                NAME_CHANGE_SUCCESS_TAG,
                VIDEO_TITLE_CHANGE_ERROR_TAG,
                VIDEO_TITLE_CHANGE_SUCCESS_TAG,
            )

            is_ig = self._platform == PLATFORM_INSTAGRAM
            updates: list[tuple[bool, str, str]] = []
            if change_language:
                if is_ig:
                    updates.append(
                        (
                            ok,
                            IG_LANGUAGE_CHANGE_SUCCESS_TAG,
                            IG_LANGUAGE_CHANGE_ERROR_TAG,
                        )
                    )
                else:
                    updates.append(
                        (ok, LANGUAGE_CHANGE_SUCCESS_TAG, LANGUAGE_CHANGE_ERROR_TAG)
                    )
            if has_text_fill:
                if _description_for_profile(pid):
                    if is_ig:
                        updates.append(
                            (
                                ok,
                                IG_DESCRIPTION_FILL_SUCCESS_TAG,
                                IG_DESCRIPTION_FILL_ERROR_TAG,
                            )
                        )
                    else:
                        updates.append(
                            (
                                ok,
                                DESCRIPTION_FILL_SUCCESS_TAG,
                                DESCRIPTION_FILL_ERROR_TAG,
                            )
                        )
                if not is_ig and _link_for_profile(pid):
                    updates.append((ok, LINK_FILL_SUCCESS_TAG, LINK_FILL_ERROR_TAG))

            item = by_id.get(pid)
            if item:
                has_avatar = bool(item.get("avatar_png"))
                has_name = bool(
                    str(item.get("channel_name") or "").strip()
                ) and not bool(item.get("skip_name_change"))
                has_video_title = not is_ig and bool(
                    str(item.get("video_default_title") or "").strip()
                )
                if has_avatar:
                    if is_ig:
                        updates.append(
                            (
                                ok,
                                IG_AVATAR_CHANGE_SUCCESS_TAG,
                                IG_AVATAR_CHANGE_ERROR_TAG,
                            )
                        )
                    else:
                        updates.append(
                            (ok, AVATAR_CHANGE_SUCCESS_TAG, AVATAR_CHANGE_ERROR_TAG)
                        )
                if has_name:
                    if is_ig:
                        updates.append(
                            (ok, IG_NAME_CHANGE_SUCCESS_TAG, IG_NAME_CHANGE_ERROR_TAG)
                        )
                    else:
                        updates.append(
                            (ok, NAME_CHANGE_SUCCESS_TAG, NAME_CHANGE_ERROR_TAG)
                        )
                if has_video_title:
                    updates.append(
                        (
                            ok,
                            VIDEO_TITLE_CHANGE_SUCCESS_TAG,
                            VIDEO_TITLE_CHANGE_ERROR_TAG,
                        )
                    )

            if updates:
                self._apply_zaliver_profile_tags_from_worker(
                    profile_id=pid,
                    kind=kind_s,
                    base_url=base_u,
                    updates=updates,
                    log_prefix="channel_setup",
                )

        mgr = MultiProfileAvailabilityChecker(
            profile_ids=profile_ids,
            check_one=_setup_one,
            on_profile_done=_on_profile_done,
            on_progress=_on_progress,
            log_sink=self._ui_log_line.emit,
            max_concurrent=max_concurrent,
        )
        ok_n, fail_n, failed_ids = mgr.run()
        self._last_channel_setup_failed_ids = list(failed_ids)
        self._studio_channel_setup_finished.emit(ok_n, fail_n)

    def _prompt_shorts_warmup_settings(self) -> ShortsWarmupSettings | None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Прогрев YouTube")
        dlg.setModal(True)
        dlg.setMinimumWidth(420)
        v = QVBoxLayout(dlg)

        hint = QLabel(
            "Для каждого отмеченного профиля: авторизация, выбор канала и "
            "просмотр указанного числа Shorts. На каждом ролике с заданной "
            "вероятностью ставится лайк и/или оформляется подписка."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        v.addWidget(hint)

        form = QFormLayout()
        count_spin = QSpinBox()
        count_spin.setRange(1, 9999)
        count_spin.setValue(10)
        form.addRow("Количество просмотренных Shorts:", count_spin)

        like_spin = QDoubleSpinBox()
        like_spin.setRange(0.0, 100.0)
        like_spin.setDecimals(1)
        like_spin.setSingleStep(1.0)
        like_spin.setSuffix(" %")
        like_spin.setValue(10.0)
        form.addRow("Вероятность лайка:", like_spin)

        subscribe_spin = QDoubleSpinBox()
        subscribe_spin.setRange(0.0, 100.0)
        subscribe_spin.setDecimals(1)
        subscribe_spin.setSingleStep(1.0)
        subscribe_spin.setSuffix(" %")
        subscribe_spin.setValue(10.0)
        form.addRow("Вероятность подписки:", subscribe_spin)

        watch_range_row = QHBoxLayout()
        watch_min_spin = QSpinBox()
        watch_min_spin.setRange(1, 9999)
        watch_min_spin.setValue(5)
        watch_min_spin.setSuffix(" с")
        watch_max_spin = QSpinBox()
        watch_max_spin.setRange(1, 9999)
        watch_max_spin.setValue(25)
        watch_max_spin.setSuffix(" с")
        watch_range_row.addWidget(watch_min_spin)
        watch_range_row.addWidget(QLabel("—"))
        watch_range_row.addWidget(watch_max_spin)
        watch_range_row.addStretch()
        watch_range_w = QWidget()
        watch_range_w.setLayout(watch_range_row)
        watch_range_lbl = QLabel("Длительность просмотра Short:")
        form.addRow(watch_range_lbl, watch_range_w)

        watch_full_cb = QCheckBox("Смотреть каждый Short до конца")
        watch_full_cb.setToolTip(
            "Как при прогреве во время отложки: дождаться конца ролика "
            "(прогресс в логе в процентах), затем листать дальше. "
            "Если снять галочку — случайное время в указанном диапазоне."
        )
        form.addRow("", watch_full_cb)

        def _sync_watch_mode(full_watch: bool) -> None:
            watch_range_lbl.setVisible(not full_watch)
            watch_range_w.setVisible(not full_watch)

        watch_full_cb.toggled.connect(_sync_watch_mode)
        _sync_watch_mode(watch_full_cb.isChecked())

        shorts_recommend_cb = QCheckBox("Рекомендации Shorts")
        shorts_recommend_cb.setChecked(True)
        shorts_recommend_cb.setToolTip(
            "Открыть ленту рекомендаций Shorts. Если снять галочку — "
            "можно указать поисковый запрос или хэштег."
        )
        form.addRow("", shorts_recommend_cb)

        hashtag_cb = QCheckBox("Хэштег")
        hashtag_cb.setChecked(False)
        hashtag_cb.setToolTip(
            "Прогрев Shorts и горизонтальных видео со страницы хэштега "
            "(youtube.com/hashtag/…). Символ # в начале можно не указывать."
        )
        form.addRow("", hashtag_cb)

        hashtag_row = QWidget()
        hashtag_row_l = QHBoxLayout(hashtag_row)
        hashtag_row_l.setContentsMargins(0, 0, 0, 0)
        hashtag_row_l.setSpacing(8)
        hashtag_lbl = QLabel("Хэштег:")
        hashtag_edit = QLineEdit()
        hashtag_edit.setPlaceholderText("хэштег или #хэштег")
        hashtag_row_l.addWidget(hashtag_lbl)
        hashtag_row_l.addWidget(hashtag_edit, 1)
        form.addRow("", hashtag_row)

        shorts_search_row = QWidget()
        shorts_search_row_l = QHBoxLayout(shorts_search_row)
        shorts_search_row_l.setContentsMargins(0, 0, 0, 0)
        shorts_search_row_l.setSpacing(8)
        shorts_search_lbl = QLabel("Поисковый запрос:")
        shorts_search_edit = QLineEdit()
        shorts_search_edit.setPlaceholderText("Текст для поиска Shorts на YouTube")
        shorts_search_row_l.addWidget(shorts_search_lbl)
        shorts_search_row_l.addWidget(shorts_search_edit, 1)
        form.addRow("", shorts_search_row)

        def _sync_shorts_source_fields() -> None:
            use_hashtag = hashtag_cb.isChecked()
            use_reco = shorts_recommend_cb.isChecked() and not use_hashtag
            hashtag_row.setVisible(use_hashtag)
            shorts_search_row.setVisible(not use_reco and not use_hashtag)
            horiz_on = watch_horizontal_cb.isChecked()
            search_label.setVisible(horiz_on and not use_hashtag)
            search_edit.setVisible(horiz_on and not use_hashtag)
            hashtag_horizontal_hint.setVisible(horiz_on and use_hashtag)

        def _on_recommend_toggled(checked: bool) -> None:
            if checked and hashtag_cb.isChecked():
                hashtag_cb.blockSignals(True)
                hashtag_cb.setChecked(False)
                hashtag_cb.blockSignals(False)
            _sync_shorts_source_fields()

        def _on_hashtag_toggled(checked: bool) -> None:
            if checked and shorts_recommend_cb.isChecked():
                shorts_recommend_cb.blockSignals(True)
                shorts_recommend_cb.setChecked(False)
                shorts_recommend_cb.blockSignals(False)
            _sync_shorts_source_fields()

        shorts_recommend_cb.toggled.connect(_on_recommend_toggled)
        hashtag_cb.toggled.connect(_on_hashtag_toggled)

        v.addLayout(form)

        horizontal_group = QGroupBox("Горизонтальные видео")
        horizontal_form = QFormLayout(horizontal_group)
        watch_horizontal_cb = QCheckBox("Смотреть после Shorts")
        horizontal_form.addRow(watch_horizontal_cb)

        search_label = QLabel("Поисковый запрос:")
        search_edit = QLineEdit()
        search_edit.setPlaceholderText("Текст для поиска на главной YouTube")
        horizontal_form.addRow(search_label, search_edit)

        hashtag_horizontal_hint = QLabel(
            "При включённом хэштеге горизонтальные видео берутся "
            "со страницы хэштега (вкладка «Все»)."
        )
        hashtag_horizontal_hint.setObjectName("hint")
        hashtag_horizontal_hint.setWordWrap(True)
        horizontal_form.addRow(hashtag_horizontal_hint)

        horizontal_count_spin = QSpinBox()
        horizontal_count_spin.setRange(1, 999)
        horizontal_count_spin.setValue(3)
        horizontal_count_label = QLabel("Количество просмотренных видео:")
        horizontal_form.addRow(horizontal_count_label, horizontal_count_spin)

        horizontal_watch_hint = QLabel("Время просмотра: 3–5 мин на каждое видео (случайно)")
        horizontal_watch_hint.setObjectName("hint")
        horizontal_watch_hint.setWordWrap(True)

        def _sync_horizontal_fields(checked: bool) -> None:
            for w in (
                horizontal_count_label,
                horizontal_count_spin,
                horizontal_watch_hint,
            ):
                w.setVisible(checked)
            _sync_shorts_source_fields()

        watch_horizontal_cb.toggled.connect(_sync_horizontal_fields)
        _sync_horizontal_fields(False)
        _sync_shorts_source_fields()
        horizontal_form.addRow(horizontal_watch_hint)
        v.addWidget(horizontal_group)

        row = QHBoxLayout()
        row.addStretch()
        btn_cancel = QPushButton("Отмена")
        btn_cancel.setObjectName("danger")
        btn_start = QPushButton("Старт")
        btn_start.setDefault(True)
        btn_start.setAutoDefault(True)
        btn_cancel.clicked.connect(dlg.reject)
        row.addWidget(btn_cancel)
        row.addWidget(btn_start)
        v.addLayout(row)

        def _normalize_hashtag_input(raw: str) -> str:
            tag = (raw or "").strip()
            while tag.startswith("#"):
                tag = tag[1:].lstrip()
            return "".join(tag.split())

        def _try_accept() -> None:
            if (
                not watch_full_cb.isChecked()
                and watch_min_spin.value() > watch_max_spin.value()
            ):
                QMessageBox.warning(
                    dlg,
                    "Прогрев YouTube",
                    "Минимальная длительность просмотра Short не может быть "
                    "больше максимальной.",
                )
                return
            use_hashtag = hashtag_cb.isChecked()
            tag = _normalize_hashtag_input(hashtag_edit.text()) if use_hashtag else ""
            if use_hashtag and not tag:
                QMessageBox.warning(
                    dlg,
                    "Прогрев YouTube",
                    "Укажите хэштег для прогрева.",
                )
                return
            use_shorts_search = (
                not shorts_recommend_cb.isChecked()
                and not use_hashtag
                and not shorts_search_edit.text().strip()
            )
            if use_shorts_search:
                QMessageBox.warning(
                    dlg,
                    "Прогрев YouTube",
                    "Укажите поисковый запрос или хэштег для прогрева Shorts "
                    "либо включите «Рекомендации Shorts».",
                )
                return
            if (
                watch_horizontal_cb.isChecked()
                and not use_hashtag
                and not search_edit.text().strip()
            ):
                QMessageBox.warning(
                    dlg,
                    "Прогрев YouTube",
                    "Укажите текст для поиска горизонтальных видео "
                    "или включите прогрев по хэштегу.",
                )
                return
            dlg.accept()

        btn_start.clicked.connect(_try_accept)

        count_spin.setFocus()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        use_hashtag = hashtag_cb.isChecked()
        tag = _normalize_hashtag_input(hashtag_edit.text()) if use_hashtag else ""
        return ShortsWarmupSettings(
            shorts_count=count_spin.value(),
            like_probability_pct=like_spin.value(),
            subscribe_probability_pct=subscribe_spin.value(),
            shorts_watch_min_s=watch_min_spin.value(),
            shorts_watch_max_s=watch_max_spin.value(),
            watch_full_video=watch_full_cb.isChecked(),
            shorts_recommendations=(
                shorts_recommend_cb.isChecked() and not use_hashtag
            ),
            shorts_search_query=(
                ""
                if use_hashtag or shorts_recommend_cb.isChecked()
                else (shorts_search_edit.text() or "").strip()
            ),
            hashtag=tag,
            watch_horizontal_videos=watch_horizontal_cb.isChecked(),
            horizontal_search_query=(
                ""
                if use_hashtag
                else (search_edit.text() or "").strip()
            ),
            horizontal_videos_count=horizontal_count_spin.value(),
        )

    def _prompt_reels_warmup_settings(self) -> ReelsWarmupSettings | None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Прогрев Instagram Reels")
        dlg.setModal(True)
        dlg.setMinimumWidth(420)
        v = QVBoxLayout(dlg)

        hint = QLabel(
            "Для каждого отмеченного профиля: главная Instagram, при необходимости "
            "вход в аккаунт, затем лента /reels/ или поиск по ключевому слову. "
            "На каждом рилсе с заданной вероятностью ставится лайк и/или подписка."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        v.addWidget(hint)

        form = QFormLayout()
        count_spin = QSpinBox()
        count_spin.setRange(1, 9999)
        count_spin.setValue(15)
        form.addRow("Количество просмотренных Reels:", count_spin)

        like_spin = QDoubleSpinBox()
        like_spin.setRange(0.0, 100.0)
        like_spin.setDecimals(1)
        like_spin.setSingleStep(1.0)
        like_spin.setSuffix(" %")
        like_spin.setValue(35.0)
        form.addRow("Вероятность лайка:", like_spin)

        follow_spin = QDoubleSpinBox()
        follow_spin.setRange(0.0, 100.0)
        follow_spin.setDecimals(1)
        follow_spin.setSingleStep(1.0)
        follow_spin.setSuffix(" %")
        follow_spin.setValue(10.0)
        form.addRow("Вероятность подписки:", follow_spin)

        watch_range_row = QHBoxLayout()
        watch_min_spin = QSpinBox()
        watch_min_spin.setRange(1, 9999)
        watch_min_spin.setValue(4)
        watch_min_spin.setSuffix(" с")
        watch_max_spin = QSpinBox()
        watch_max_spin.setRange(1, 9999)
        watch_max_spin.setValue(12)
        watch_max_spin.setSuffix(" с")
        watch_range_row.addWidget(watch_min_spin)
        watch_range_row.addWidget(QLabel("—"))
        watch_range_row.addWidget(watch_max_spin)
        watch_range_row.addStretch()
        watch_range_w = QWidget()
        watch_range_w.setLayout(watch_range_row)
        watch_range_lbl = QLabel("Длительность просмотра Reel:")
        form.addRow(watch_range_lbl, watch_range_w)

        watch_full_cb = QCheckBox("Смотреть каждый Reel до конца")
        watch_full_cb.setChecked(True)
        watch_full_cb.setToolTip(
            "Дождаться конца ролика, затем листать дальше. "
            "Если снять галочку — случайное время в указанном диапазоне."
        )
        form.addRow("", watch_full_cb)

        def _sync_watch_mode(full_watch: bool) -> None:
            watch_range_lbl.setVisible(not full_watch)
            watch_range_w.setVisible(not full_watch)

        watch_full_cb.toggled.connect(_sync_watch_mode)
        _sync_watch_mode(watch_full_cb.isChecked())

        reels_recommend_cb = QCheckBox("Рекомендации Reels")
        reels_recommend_cb.setChecked(True)
        reels_recommend_cb.setToolTip(
            "Открыть ленту рекомендаций /reels/. Если снять галочку — "
            "укажите запрос: открывается /explore/search/keyword/, "
            "первый рилс в сетке, далее листание вправо."
        )
        form.addRow("", reels_recommend_cb)

        reels_search_row = QWidget()
        reels_search_row_l = QHBoxLayout(reels_search_row)
        reels_search_row_l.setContentsMargins(0, 0, 0, 0)
        reels_search_row_l.setSpacing(8)
        reels_search_lbl = QLabel("Поисковый запрос:")
        reels_search_edit = QLineEdit()
        reels_search_edit.setPlaceholderText("#luxurylifestyle или luxury life")
        reels_search_row_l.addWidget(reels_search_lbl)
        reels_search_row_l.addWidget(reels_search_edit, 1)
        form.addRow("", reels_search_row)

        def _sync_reels_source_fields(checked: bool) -> None:
            reels_search_row.setVisible(not checked)

        reels_recommend_cb.toggled.connect(_sync_reels_source_fields)
        _sync_reels_source_fields(reels_recommend_cb.isChecked())

        v.addLayout(form)

        row = QHBoxLayout()
        row.addStretch()
        btn_cancel = QPushButton("Отмена")
        btn_cancel.setObjectName("danger")
        btn_start = QPushButton("Старт")
        btn_start.setDefault(True)
        btn_start.setAutoDefault(True)
        btn_cancel.clicked.connect(dlg.reject)
        row.addWidget(btn_cancel)
        row.addWidget(btn_start)
        v.addLayout(row)

        def _try_accept() -> None:
            if (
                not watch_full_cb.isChecked()
                and watch_min_spin.value() > watch_max_spin.value()
            ):
                QMessageBox.warning(
                    dlg,
                    "Прогрев Instagram Reels",
                    "Минимальная длительность просмотра не может быть "
                    "больше максимальной.",
                )
                return
            if (
                not reels_recommend_cb.isChecked()
                and not reels_search_edit.text().strip()
            ):
                QMessageBox.warning(
                    dlg,
                    "Прогрев Instagram Reels",
                    "Укажите поисковый запрос для прогрева Reels "
                    "или включите «Рекомендации Reels».",
                )
                return
            dlg.accept()

        btn_start.clicked.connect(_try_accept)

        count_spin.setFocus()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return ReelsWarmupSettings(
            reels_count=count_spin.value(),
            like_probability_pct=like_spin.value(),
            follow_probability_pct=follow_spin.value(),
            watch_min_s=watch_min_spin.value(),
            watch_max_s=watch_max_spin.value(),
            watch_full=watch_full_cb.isChecked(),
            reels_recommendations=reels_recommend_cb.isChecked(),
            reels_search_query=(reels_search_edit.text() or "").strip(),
        )

    def _start_profiles_warmup(self) -> None:
        is_ig = self._platform == PLATFORM_INSTAGRAM
        title = "Прогрев Reels" if is_ig else "Прогрев Shorts"
        if self._profiles_warmup_running:
            QMessageBox.information(
                self,
                title,
                "Прогрев уже выполняется. Дождитесь завершения.",
            )
            return
        if self._profiles_raw is None:
            QMessageBox.warning(
                self,
                title,
                "Сначала загрузите список профилей (кнопка «Обновить»).",
            )
            return
        profile_ids = self._collect_checked_profile_ids()
        if not profile_ids:
            QMessageBox.warning(
                self,
                title,
                "Отметьте квадратиками профили, для которых нужен прогрев.",
            )
            return

        if is_ig:
            warmup_settings: ShortsWarmupSettings | ReelsWarmupSettings | None = (
                self._prompt_reels_warmup_settings()
            )
        else:
            warmup_settings = self._prompt_shorts_warmup_settings()
        if warmup_settings is None:
            return

        token = self._legacy_dolphin_token()
        kind = self._antidetect_kind()
        base_url = self._own_antidetect_base_url_from_settings(kind)

        headless = True
        if hasattr(self, "_dolphin_headless"):
            headless = bool(self._dolphin_headless.isChecked())
        else:
            headless = bool(
                self._settings.value("antydetect/dolphin_headless", True, type=bool)
            )

        try:
            remote_cdp = self._remote_cdp_launch_options_for_kind(kind)
        except LocalAntidetectError as e:
            QMessageBox.warning(self, title, str(e))
            return

        self._profiles_warmup_running = True
        self._sync_profiles_tab_action_buttons()
        self._profiles_status.setText(f"{title}: 0 / {len(profile_ids)}…")
        headless_label = "headless" if headless else "с окном браузера"
        max_concurrent = self._max_concurrent_browsers()

        if isinstance(warmup_settings, ReelsWarmupSettings):
            watch_note = (
                "до конца каждого ролика"
                if warmup_settings.watch_full
                else (
                    f"{warmup_settings.watch_min_s}–"
                    f"{warmup_settings.watch_max_s} с"
                )
            )
            self._append_log(
                f"[warmup] Старт для {len(profile_ids)} профилей "
                f"(Reels: {warmup_settings.reels_count}, "
                f"просмотр {watch_note}, "
                f"лайк {warmup_settings.like_probability_pct:g}%, "
                f"подписка {warmup_settings.follow_probability_pct:g}%"
                + (
                    ", Reels: рекомендации"
                    if warmup_settings.reels_recommendations
                    else f", Reels: поиск «{warmup_settings.reels_search_query}»"
                )
                + f", {headless_label}, до {max_concurrent} параллельно)…"
            )
            worker = self._profiles_reels_warmup_worker
        else:
            watch_note = (
                "до конца каждого ролика (прогресс в %)"
                if warmup_settings.watch_full_video
                else (
                    f"{warmup_settings.shorts_watch_min_s}–"
                    f"{warmup_settings.shorts_watch_max_s} с"
                )
            )
            self._append_log(
                f"[warmup] Старт для {len(profile_ids)} профилей "
                f"(Shorts: {warmup_settings.shorts_count}, "
                f"просмотр {watch_note}, "
                f"лайк {warmup_settings.like_probability_pct:g}%, "
                f"подписка {warmup_settings.subscribe_probability_pct:g}%"
                + (
                    f", Shorts: хэштег «#{warmup_settings.hashtag}»"
                    if (warmup_settings.hashtag or "").strip()
                    else (
                        ", Shorts: рекомендации"
                        if warmup_settings.shorts_recommendations
                        else f", Shorts: поиск «{warmup_settings.shorts_search_query}»"
                    )
                )
                + (
                    (
                        f", горизонтальные: {warmup_settings.horizontal_videos_count}, "
                        f"хэштег «#{warmup_settings.hashtag}»"
                        if (warmup_settings.hashtag or "").strip()
                        else (
                            f", горизонтальные: {warmup_settings.horizontal_videos_count}, "
                            f"поиск «{warmup_settings.horizontal_search_query}»"
                        )
                    )
                    if warmup_settings.watch_horizontal_videos
                    else ""
                )
                + f", {headless_label}, до {max_concurrent} параллельно)…"
            )
            worker = self._profiles_warmup_worker

        threading.Thread(
            target=worker,
            kwargs={
                "profile_ids": profile_ids,
                "kind": kind,
                "token": token,
                "base_url": base_url,
                "headless": headless,
                "warmup_settings": warmup_settings,
                "remote_cdp": remote_cdp,
                "max_concurrent": max_concurrent,
            },
            daemon=True,
        ).start()

    def _profiles_warmup_worker(
        self,
        *,
        profile_ids: list[str],
        kind: str,
        token: str,
        base_url: str,
        headless: bool,
        warmup_settings: ShortsWarmupSettings,
        remote_cdp: RemoteCdpLaunchOptions | None = None,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_BROWSERS,
    ) -> None:
        from zaliver.antydetect.antic_open import (
            set_log_sink,
            warmup_youtube_shorts_in_local_antidetect_profile,
            warmup_youtube_shorts_in_profile,
        )
        from zaliver.antydetect.local_antidetect_api import LocalAntidetectError
        from zaliver.youtube_upload.multi_availability_checker import (
            MultiProfileAvailabilityChecker,
        )

        set_log_sink(self._ui_log_line.emit)
        kind_s = (kind or "").strip()

        def _warmup_one(pid: str) -> None:
            creds = self._profile_login_credentials(pid)
            yt_oldest = self._profile_yt_oldest_name(pid) or None
            search_oldest = self._youtube_search_oldest_channel()
            warmup_kw = {
                "shorts_count": warmup_settings.shorts_count,
                "like_probability_pct": warmup_settings.like_probability_pct,
                "subscribe_probability_pct": warmup_settings.subscribe_probability_pct,
                "shorts_watch_min_s": warmup_settings.shorts_watch_min_s,
                "shorts_watch_max_s": warmup_settings.shorts_watch_max_s,
                "watch_full_video": warmup_settings.watch_full_video,
                "shorts_recommendations": warmup_settings.shorts_recommendations,
                "search_query": (
                    warmup_settings.shorts_search_query or None
                    if not warmup_settings.shorts_recommendations
                    and not (warmup_settings.hashtag or "").strip()
                    else None
                ),
                "hashtag": (warmup_settings.hashtag or "").strip() or None,
                "watch_horizontal_videos": warmup_settings.watch_horizontal_videos,
                "horizontal_search_query": warmup_settings.horizontal_search_query or None,
                "horizontal_videos_count": warmup_settings.horizontal_videos_count,
                "search_oldest_channel": search_oldest,
            }
            if _is_own_antidetect_kind(kind_s):
                u = (base_url or "").strip()
                if not u:
                    raise LocalAntidetectError(
                        f"Укажите базовый URL {_own_antidetect_api_label(kind_s)} API в настройках."
                    )
                warmup_youtube_shorts_in_local_antidetect_profile(
                    pid,
                    base_url=u,
                    headless=headless,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    remote_cdp=remote_cdp,
                    **warmup_kw,
                )
            else:
                warmup_youtube_shorts_in_profile(
                    pid,
                    local_token=token or None,
                    headless=headless,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    **warmup_kw,
                )

        def _on_progress(done: int, total: int, profile_id: str) -> None:
            self._studio_warmup_progress.emit(done, total, profile_id)

        def _on_profile_done(pid: str, ok: bool, err: str) -> None:
            if not _is_own_antidetect_kind(kind_s):
                return
            from zaliver.antydetect.profile_tags import (
                WARMUP_ERROR_TAG,
                WARMUP_SUCCESS_TAG,
            )

            self._apply_zaliver_profile_tags_from_worker(
                profile_id=pid,
                kind=kind_s,
                base_url=base_url,
                updates=[(ok, WARMUP_SUCCESS_TAG, WARMUP_ERROR_TAG)],
                log_prefix="warmup",
            )

        mgr = MultiProfileAvailabilityChecker(
            profile_ids=profile_ids,
            check_one=_warmup_one,
            on_profile_done=_on_profile_done,
            on_progress=_on_progress,
            log_sink=self._ui_log_line.emit,
            max_concurrent=max_concurrent,
        )
        ok_n, fail_n, failed_ids = mgr.run()
        self._last_warmup_failed_ids = list(failed_ids)
        self._studio_warmup_finished.emit(ok_n, fail_n)

    def _profiles_reels_warmup_worker(
        self,
        *,
        profile_ids: list[str],
        kind: str,
        token: str,
        base_url: str,
        headless: bool,
        warmup_settings: ReelsWarmupSettings,
        remote_cdp: RemoteCdpLaunchOptions | None = None,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_BROWSERS,
    ) -> None:
        from zaliver.antydetect.antic_open import (
            set_log_sink,
            warmup_instagram_reels_in_local_antidetect_profile,
            warmup_instagram_reels_in_profile,
        )
        from zaliver.antydetect.local_antidetect_api import LocalAntidetectError
        from zaliver.youtube_upload.multi_availability_checker import (
            MultiProfileAvailabilityChecker,
        )

        set_log_sink(self._ui_log_line.emit)
        kind_s = (kind or "").strip()

        def _warmup_one(pid: str) -> None:
            login, password, twofa = self._instagram_session_credentials(pid)
            warmup_kw = {
                "session_login": login,
                "session_password": password,
                "session_twofa": twofa,
                "reels_count": warmup_settings.reels_count,
                "like_probability_pct": warmup_settings.like_probability_pct,
                "follow_probability_pct": warmup_settings.follow_probability_pct,
                "watch_min_s": float(warmup_settings.watch_min_s),
                "watch_max_s": float(warmup_settings.watch_max_s),
                "watch_full": warmup_settings.watch_full,
                "reels_recommendations": warmup_settings.reels_recommendations,
                "search_query": warmup_settings.reels_search_query,
            }
            if _is_own_antidetect_kind(kind_s):
                u = (base_url or "").strip()
                if not u:
                    raise LocalAntidetectError(
                        f"Укажите базовый URL {_own_antidetect_api_label(kind_s)} API в настройках."
                    )
                warmup_instagram_reels_in_local_antidetect_profile(
                    pid,
                    base_url=u,
                    headless=headless,
                    remote_cdp=remote_cdp,
                    **warmup_kw,
                )
            else:
                warmup_instagram_reels_in_profile(
                    pid,
                    local_token=token or None,
                    headless=headless,
                    **warmup_kw,
                )

        def _on_progress(done: int, total: int, profile_id: str) -> None:
            self._studio_warmup_progress.emit(done, total, profile_id)

        def _on_profile_done(pid: str, ok: bool, err: str) -> None:
            if not _is_own_antidetect_kind(kind_s):
                return
            from zaliver.antydetect.profile_tags import (
                IG_WARMUP_ERROR_TAG,
                IG_WARMUP_SUCCESS_TAG,
            )

            self._apply_zaliver_profile_tags_from_worker(
                profile_id=pid,
                kind=kind_s,
                base_url=base_url,
                updates=[(ok, IG_WARMUP_SUCCESS_TAG, IG_WARMUP_ERROR_TAG)],
                log_prefix="ig-warmup",
            )

        mgr = MultiProfileAvailabilityChecker(
            profile_ids=profile_ids,
            check_one=_warmup_one,
            on_profile_done=_on_profile_done,
            on_progress=_on_progress,
            log_sink=self._ui_log_line.emit,
            max_concurrent=max_concurrent,
        )
        ok_n, fail_n, failed_ids = mgr.run()
        self._last_warmup_failed_ids = list(failed_ids)
        self._studio_warmup_finished.emit(ok_n, fail_n)

    def _prompt_profiles_promote_settings(self) -> ProfilePromoteSettings | None:
        dlg = ProfilePromoteDialog(
            parent=self,
            recent_comments=self._upload_store.list_recent_promote_comment_fields(
                platform=self._platform
            ),
            ai_generate_fn=self._on_ai_magic_generate,
            platform=self._platform,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        settings = dlg.settings()
        if settings.enable_comments:
            try:
                self._upload_store.remember_promote_comment_field(
                    dlg.comments_field_text(), platform=self._platform
                )
            except Exception:
                pass
        return settings

    def _start_profiles_promote(self) -> None:
        if self._profiles_promote_running:
            QMessageBox.information(
                self,
                "Продвижение",
                "Продвижение уже выполняется. Дождитесь завершения.",
            )
            return
        if self._profiles_raw is None:
            QMessageBox.warning(
                self,
                "Продвижение",
                "Сначала загрузите список профилей (кнопка «Обновить»).",
            )
            return
        profile_ids = self._collect_checked_profile_ids()
        if not profile_ids:
            QMessageBox.warning(
                self,
                "Продвижение",
                "Отметьте квадратиками профили для продвижения.",
            )
            return

        promote_settings = self._prompt_profiles_promote_settings()
        if promote_settings is None:
            return

        targets: list = []
        is_ig = self._platform == PLATFORM_INSTAGRAM
        from zaliver.youtube_upload.studio import PromotionTargetVideo

        if is_ig:
            # Instagram: все залитые рилсы из БД (не только пересечение
            # с текущим фильтром списка профилей).
            videos = self._upload_store.list_promotable_videos(
                platform=self._platform,
                profile_ids=None,
                require_positive_views=False,
                one_per_profile=True,
                include_missing_profile_id=True,
            )
            if not videos:
                QMessageBox.warning(
                    self,
                    "Продвижение",
                    "Нет подходящих рилсов в базе: нужны ролики с video_id "
                    "(не помеченные как недоступные). "
                    "Сначала залейте рилсы и при необходимости обновите статистику.",
                )
                return
            targets = [
                PromotionTargetVideo(
                    profile_id=v.profile_id,
                    video_id=v.video_id,
                    url=v.url,
                    title=v.title,
                )
                for v in videos
            ]
        elif promote_settings.subscribe_to_channels:
            visible = self._profiles_visible_matched()
            visible_ids = [
                pid for p in visible if (pid := _profile_id(p))
            ]
            if not visible_ids:
                QMessageBox.warning(
                    self,
                    "Продвижение",
                    "В списке нет видимых профилей для подбора видео.",
                )
                return
            videos = self._upload_store.list_promotable_videos_for_profiles(
                visible_ids, platform=self._platform
            )
            if not videos:
                QMessageBox.warning(
                    self,
                    "Продвижение",
                    "Нет подходящих видео для подписки: нужны ролики с просмотрами "
                    "у видимых профилей (не заблокированные и не в отложке). "
                    "Сначала залейте и прочекайте статистику, либо снимите "
                    "«Подписаться на каналы».",
                )
                return
            targets = [
                PromotionTargetVideo(
                    profile_id=v.profile_id,
                    video_id=v.video_id,
                    url=v.url,
                    title=v.title,
                )
                for v in videos
            ]

        token = self._legacy_dolphin_token()
        kind = self._antidetect_kind()
        base_url = self._own_antidetect_base_url_from_settings(kind)

        headless = True
        if hasattr(self, "_dolphin_headless"):
            headless = bool(self._dolphin_headless.isChecked())
        else:
            headless = bool(
                self._settings.value("antydetect/dolphin_headless", True, type=bool)
            )

        try:
            remote_cdp = self._remote_cdp_launch_options_for_kind(kind)
        except LocalAntidetectError as e:
            QMessageBox.warning(self, "Продвижение", str(e))
            return

        self._profiles_promote_running = True
        self._sync_profiles_tab_action_buttons()
        self._profiles_status.setText(
            f"Продвижение: 0 / {len(profile_ids)}…"
        )
        headless_label = "headless" if headless else "с окном браузера"
        max_concurrent = self._max_concurrent_browsers()
        if is_ig:
            sub_note = (
                "с подпиской на рилсе, "
                if promote_settings.subscribe_to_channels
                else "без подписки, "
            )
            feed_label = f"рилсов по ссылкам до {promote_settings.shorts_count}"
            targets_note = f", целей {len(targets)}"
        elif promote_settings.subscribe_to_channels:
            sub_note = f"подписка на {len(targets)} каналов, "
            feed_label = f"Shorts: {promote_settings.shorts_count}"
            targets_note = ""
        else:
            sub_note = ""
            feed_label = f"Shorts: {promote_settings.shorts_count}"
            targets_note = ""
        comments_note = (
            f", комментарии {promote_settings.comment_probability_pct:g}%"
            if promote_settings.enable_comments
            else ""
        )
        self._append_log(
            f"[promote] Старт для {len(profile_ids)} профилей "
            f"({sub_note}{feed_label}{targets_note}, "
            f"лайк {promote_settings.like_probability_pct:g}%"
            f"{comments_note}, {headless_label}, "
            f"до {max_concurrent} параллельно)…"
        )

        threading.Thread(
            target=self._profiles_promote_worker,
            kwargs={
                "profile_ids": profile_ids,
                "kind": kind,
                "token": token,
                "base_url": base_url,
                "headless": headless,
                "videos": targets,
                "promote_settings": promote_settings,
                "remote_cdp": remote_cdp,
                "max_concurrent": max_concurrent,
            },
            daemon=True,
        ).start()

    def _profiles_promote_worker(
        self,
        *,
        profile_ids: list[str],
        kind: str,
        token: str,
        base_url: str,
        headless: bool,
        videos: list,
        promote_settings: ProfilePromoteSettings,
        remote_cdp: RemoteCdpLaunchOptions | None = None,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_BROWSERS,
    ) -> None:
        from zaliver.antydetect.antic_open import (
            promote_instagram_reels_in_local_antidetect_profile,
            promote_instagram_reels_in_profile,
            promote_youtube_videos_in_local_antidetect_profile,
            promote_youtube_videos_in_profile,
            set_log_sink,
        )
        from zaliver.antydetect.local_antidetect_api import LocalAntidetectError
        from zaliver.youtube_upload.multi_availability_checker import (
            MultiProfileAvailabilityChecker,
        )

        set_log_sink(self._ui_log_line.emit)
        kind_s = (kind or "").strip()
        is_ig = self._platform == PLATFORM_INSTAGRAM
        promote_kw = {
            "subscribe_to_channels": promote_settings.subscribe_to_channels,
            "shorts_count": promote_settings.shorts_count,
            "like_probability_pct": promote_settings.like_probability_pct,
            "shorts_watch_min_s": promote_settings.shorts_watch_min_s,
            "shorts_watch_max_s": promote_settings.shorts_watch_max_s,
            "watch_full_video": promote_settings.watch_full_video,
            "enable_comments": promote_settings.enable_comments,
            "comments": list(promote_settings.comments),
            "comment_probability_pct": promote_settings.comment_probability_pct,
        }
        if not is_ig:
            promote_kw["subscribe_probability_pct"] = 0.0

        def _promote_one(pid: str) -> None:
            if is_ig:
                login, password, twofa = self._instagram_session_credentials(pid)
                ig_kw = {
                    **promote_kw,
                    "session_login": login,
                    "session_password": password,
                    "session_twofa": twofa,
                }
                if _is_own_antidetect_kind(kind_s):
                    u = (base_url or "").strip()
                    if not u:
                        raise LocalAntidetectError(
                            f"Укажите базовый URL {_own_antidetect_api_label(kind_s)} API в настройках."
                        )
                    promote_instagram_reels_in_local_antidetect_profile(
                        pid,
                        base_url=u,
                        videos=videos,
                        headless=headless,
                        remote_cdp=remote_cdp,
                        **ig_kw,
                    )
                else:
                    promote_instagram_reels_in_profile(
                        pid,
                        videos=videos,
                        local_token=token or None,
                        headless=headless,
                        **ig_kw,
                    )
                return

            creds = self._profile_login_credentials(pid)
            yt_oldest = self._profile_yt_oldest_name(pid) or None
            search_oldest = self._youtube_search_oldest_channel()
            if _is_own_antidetect_kind(kind_s):
                u = (base_url or "").strip()
                if not u:
                    raise LocalAntidetectError(
                        f"Укажите базовый URL {_own_antidetect_api_label(kind_s)} API в настройках."
                    )
                promote_youtube_videos_in_local_antidetect_profile(
                    pid,
                    base_url=u,
                    videos=videos,
                    headless=headless,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    search_oldest_channel=search_oldest,
                    remote_cdp=remote_cdp,
                    **promote_kw,
                )
            else:
                promote_youtube_videos_in_profile(
                    pid,
                    videos=videos,
                    local_token=token or None,
                    headless=headless,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    search_oldest_channel=search_oldest,
                    **promote_kw,
                )

        def _on_progress(done: int, total: int, profile_id: str) -> None:
            self._studio_promote_progress.emit(done, total, profile_id)

        def _on_profile_done(pid: str, ok: bool, err: str) -> None:
            if not _is_own_antidetect_kind(kind_s):
                return
            from zaliver.antydetect.profile_tags import (
                IG_PROMOTE_ERROR_TAG,
                IG_PROMOTE_SUCCESS_TAG,
                PROMOTE_ERROR_TAG,
                PROMOTE_SUCCESS_TAG,
            )

            if is_ig:
                success_tag, error_tag = IG_PROMOTE_SUCCESS_TAG, IG_PROMOTE_ERROR_TAG
            else:
                success_tag, error_tag = PROMOTE_SUCCESS_TAG, PROMOTE_ERROR_TAG
            self._apply_zaliver_profile_tags_from_worker(
                profile_id=pid,
                kind=kind_s,
                base_url=base_url,
                updates=[(ok, success_tag, error_tag)],
                log_prefix="promote",
            )

        mgr = MultiProfileAvailabilityChecker(
            profile_ids=profile_ids,
            check_one=_promote_one,
            on_profile_done=_on_profile_done,
            on_progress=_on_progress,
            log_sink=self._ui_log_line.emit,
            max_concurrent=max_concurrent,
        )
        ok_n, fail_n, failed_ids = mgr.run()
        self._last_promote_failed_ids = list(failed_ids)
        self._studio_promote_finished.emit(ok_n, fail_n)

    def _start_profiles_cookie_farm(self) -> None:
        if self._profiles_cookie_farm_running:
            QMessageBox.information(
                self,
                "Фарм Cookie",
                "Фарм Cookie уже выполняется. Дождитесь завершения.",
            )
            return
        if self._profiles_raw is None:
            QMessageBox.warning(
                self,
                "Фарм Cookie",
                "Сначала загрузите список профилей (кнопка «Обновить»).",
            )
            return
        profile_ids = self._collect_checked_profile_ids()
        if not profile_ids:
            QMessageBox.warning(
                self,
                "Фарм Cookie",
                "Отметьте квадратиками профили, для которых нужен фарм Cookie.",
            )
            return

        dlg = ProfileCookieFarmDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        farm_settings = dlg.settings()

        token = self._legacy_dolphin_token()
        kind = self._antidetect_kind()
        base_url = self._own_antidetect_base_url_from_settings(kind)

        headless = True
        if hasattr(self, "_dolphin_headless"):
            headless = bool(self._dolphin_headless.isChecked())
        else:
            headless = bool(
                self._settings.value("antydetect/dolphin_headless", True, type=bool)
            )

        try:
            remote_cdp = self._remote_cdp_launch_options_for_kind(kind)
        except LocalAntidetectError as e:
            QMessageBox.warning(self, "Фарм Cookie", str(e))
            return

        self._profiles_cookie_farm_running = True
        self._sync_profiles_tab_action_buttons()
        self._profiles_status.setText(
            f"Фарм Cookie: 0 / {len(profile_ids)}…"
        )
        headless_label = "headless" if headless else "с окном браузера"
        max_concurrent = self._max_concurrent_browsers()
        if farm_settings.use_preset_domains:
            preset_labels = {"intl": "международный список", "ru": "RU список"}
            source_label = preset_labels.get(
                (farm_settings.preset_kind or "").strip().lower(),
                "заготовленный список",
            )
        else:
            source_label = f"свой список ({len(farm_settings.domains)} доменов)"
        self._append_log(
            f"[cookie_farm] Старт для {len(profile_ids)} профилей "
            f"({farm_settings.sites_count} сайтов, "
            f"{farm_settings.watch_min_s}–{farm_settings.watch_max_s} с на сайт, "
            f"источник: {source_label}, {headless_label}, "
            f"до {max_concurrent} параллельно)…"
        )

        threading.Thread(
            target=self._profiles_cookie_farm_worker,
            kwargs={
                "profile_ids": profile_ids,
                "kind": kind,
                "token": token,
                "base_url": base_url,
                "headless": headless,
                "farm_settings": farm_settings,
                "remote_cdp": remote_cdp,
                "max_concurrent": max_concurrent,
            },
            daemon=True,
        ).start()

    def _profiles_cookie_farm_worker(
        self,
        *,
        profile_ids: list[str],
        kind: str,
        token: str,
        base_url: str,
        headless: bool,
        farm_settings,
        remote_cdp: RemoteCdpLaunchOptions | None = None,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_BROWSERS,
    ) -> None:
        from zaliver.antydetect.antic_open import (
            farm_cookies_in_local_antidetect_profile,
            farm_cookies_in_profile,
            set_log_sink,
        )
        from zaliver.antydetect.cookie_farm import set_log_sink as set_cookie_farm_log_sink
        from zaliver.antydetect.local_antidetect_api import LocalAntidetectError
        from zaliver.youtube_upload.multi_availability_checker import (
            MultiProfileAvailabilityChecker,
        )

        set_log_sink(self._ui_log_line.emit)
        set_cookie_farm_log_sink(self._ui_log_line.emit)
        kind_s = (kind or "").strip()

        farm_kw = {
            "domains": list(farm_settings.domains),
            "sites_count": farm_settings.sites_count,
            "watch_min_s": float(farm_settings.watch_min_s),
            "watch_max_s": float(farm_settings.watch_max_s),
        }

        def _farm_one(pid: str) -> None:
            if _is_own_antidetect_kind(kind_s):
                u = (base_url or "").strip()
                if not u:
                    raise LocalAntidetectError(
                        f"Укажите базовый URL {_own_antidetect_api_label(kind_s)} API в настройках."
                    )
                farm_cookies_in_local_antidetect_profile(
                    pid,
                    base_url=u,
                    headless=headless,
                    remote_cdp=remote_cdp,
                    **farm_kw,
                )
            else:
                farm_cookies_in_profile(
                    pid,
                    local_token=token or None,
                    headless=headless,
                    **farm_kw,
                )

        def _on_progress(done: int, total: int, profile_id: str) -> None:
            self._studio_cookie_farm_progress.emit(done, total, profile_id)

        def _on_profile_done(pid: str, ok: bool, err: str) -> None:
            if not _is_own_antidetect_kind(kind_s):
                return
            from zaliver.antydetect.profile_tags import (
                COOKIE_FARM_ERROR_TAG,
                COOKIE_FARM_SUCCESS_TAG,
            )

            self._apply_zaliver_profile_tags_from_worker(
                profile_id=pid,
                kind=kind_s,
                base_url=base_url,
                updates=[(ok, COOKIE_FARM_SUCCESS_TAG, COOKIE_FARM_ERROR_TAG)],
                log_prefix="cookie_farm",
            )

        mgr = MultiProfileAvailabilityChecker(
            profile_ids=profile_ids,
            check_one=_farm_one,
            on_profile_done=_on_profile_done,
            on_progress=_on_progress,
            log_sink=self._ui_log_line.emit,
            max_concurrent=max_concurrent,
        )
        ok_n, fail_n, failed_ids = mgr.run()
        self._last_cookie_farm_failed_ids = list(failed_ids)
        self._studio_cookie_farm_finished.emit(ok_n, fail_n)

    def _collect_checked_profile_ids(self) -> list[str]:
        if self._profiles_interaction is None:
            return []
        return self._profiles_interaction.batch_profile_ids()

    def _own_antidetect_base_url_from_settings(self, kind: str | None = None) -> str:
        if kind is None:
            kind = self._antidetect_kind()
        k = _normalize_antidetect_kind(kind if isinstance(kind, str) else None)
        if k == "local":
            base_url = (self._local_api_base_url.text() or "").strip()
            if not base_url:
                base_url = (
                    self._settings.value("antydetect/local_api_base_url", "", type=str) or ""
                ).strip()
            if not base_url:
                base_url = DEFAULT_LOCAL_API_BASE_URL
            return base_url
        if k == "remote":
            return (
                self._settings.value("antydetect/remote_api_base_url", "", type=str) or ""
            ).strip()
        return ""

    def _remote_cdp_launch_options_for_kind(self, kind: str) -> RemoteCdpLaunchOptions | None:
        if (kind or "").strip() != "remote":
            return None
        host = (
            self._settings.value("antydetect/remote_cdp_public_host", "", type=str) or ""
        ).strip()
        if not host:
            raise LocalAntidetectError(
                "Для удалённого антидетекта в настройках нужен CDP public host "
                "(ключ antydetect/remote_cdp_public_host)."
            )
        return RemoteCdpLaunchOptions(cdp_public_host=host)

    def _local_antidetect_base_url_from_settings(self) -> str:
        return self._own_antidetect_base_url_from_settings("local")

    def _profile_login_credentials(self, profile_id: str):
        from zaliver.youtube_upload.google_login import (
            credentials_from_custom_data,
            gmail_or_yt_credentials_from_custom_data,
        )

        pid = (profile_id or "").strip()
        for p in self._profiles_raw or []:
            if _profile_id(p) != pid:
                continue
            cd = p.get("custom_data")
            if not isinstance(cd, dict):
                return None
            if self._platform == PLATFORM_INSTAGRAM:
                return gmail_or_yt_credentials_from_custom_data(cd)
            return credentials_from_custom_data(cd)
        return None

    def _instagram_session_credentials(self, profile_id: str) -> tuple[str, str, str]:
        """Логин/пароль/2FA для re-login Instagram (не регистрация)."""
        from zaliver.instagram_upload.instagram_availability import (
            session_login_from_custom_data,
            session_password_from_custom_data,
            session_twofa_from_custom_data,
        )

        pid = (profile_id or "").strip()
        for p in self._profiles_raw or []:
            if _profile_id(p) != pid:
                continue
            cd = p.get("custom_data")
            if not isinstance(cd, dict):
                return "", "", ""
            return (
                session_login_from_custom_data(cd),
                session_password_from_custom_data(cd),
                session_twofa_from_custom_data(cd),
            )
        return "", "", ""

    def _instagram_checker_proxy_dsn(self, profile_id: str) -> str:
        """Прокси выбранного профиля антидетекта → DSN для instagrapi.

        Для своего антидетекта всегда читаем get_profile: в списке/кэше
        легко остаться без proxy_username/password → 407 Proxy Authentication.
        """
        from zaliver.antydetect.proxy_dsn import proxy_dsn_from_profile

        pid = (profile_id or "").strip()
        if not pid:
            return ""

        kind = self._antidetect_kind()

        if _is_own_antidetect_kind(kind):
            base_url = self._own_antidetect_base_url_from_settings(kind)
            if (base_url or "").strip():
                try:
                    api = LocalAntidetectHttpAPI(base_url)
                    try:
                        raw = api.get_profile(pid)
                    finally:
                        api.close()
                    if isinstance(raw, dict):
                        dsn = proxy_dsn_from_profile(raw) or proxy_dsn_from_profile(
                            normalize_local_profile_for_ui(raw)
                        )
                        if dsn:
                            return dsn
                except Exception:
                    pass

        for p in self._profiles_raw or []:
            if not isinstance(p, dict) or _profile_id(p) != pid:
                continue
            return proxy_dsn_from_profile(p)
        return ""

    def _profile_yt_oldest_name(self, profile_id: str) -> str:
        from zaliver.youtube_upload.google_login import oldest_name_from_custom_data

        pid = (profile_id or "").strip()
        for p in self._profiles_raw or []:
            if _profile_id(p) != pid:
                continue
            cd = p.get("custom_data")
            if isinstance(cd, dict):
                return oldest_name_from_custom_data(cd)
        return ""

    def _open_profiles_accounts_import_dialog(self) -> None:
        kind = self._antidetect_kind()
        if not _is_own_antidetect_kind(kind if isinstance(kind, str) else ""):
            QMessageBox.information(
                self,
                "Импорт данных учёток",
                "Импорт доступен только для своего антидетекта "
                "(локальный или удалённый API).",
            )
            return
        if not self._profiles_raw:
            QMessageBox.information(
                self,
                "Импорт данных учёток",
                "Сначала загрузите список профилей (кнопка «Обновить»).",
            )
            return
        if self._profiles_interaction is None:
            return
        profile_ids = self._profiles_interaction.batch_profile_ids()
        if not profile_ids:
            QMessageBox.warning(
                self,
                "Импорт данных учёток",
                "Отметьте квадратиками профили, в которые нужно загрузить учётки.",
            )
            return
        by_id = self._profiles_by_id_map(self._profiles_raw)
        selected_profiles = [by_id[pid] for pid in profile_ids if pid in by_id]
        if not selected_profiles:
            QMessageBox.warning(
                self,
                "Импорт данных учёток",
                "Не удалось найти отмеченные профили в загруженном списке.",
            )
            return

        dlg = ProfileAccountsImportDialog(
            selected_profiles=selected_profiles,
            all_profiles=list(self._profiles_raw or []),
            platform=self._platform,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        payloads = dlg.save_payloads()
        if not payloads:
            return
        rename_to_email = dlg.rename_profiles_to_email()

        base_url = self._own_antidetect_base_url_from_settings()
        if not (base_url or "").strip():
            QMessageBox.warning(
                self,
                "Импорт данных учёток",
                f"Укажите базовый URL {_own_antidetect_api_label(kind if isinstance(kind, str) else '')} "
                "API в настройках и сохраните.",
            )
            return
        saved = 0
        renamed = 0
        errors: list[str] = []
        try:
            api = LocalAntidetectHttpAPI(base_url)
            try:
                for pid, payload in payloads:
                    try:
                        updated = api.merge_profile_custom_data(pid, payload)
                        cd_upd = updated.get("custom_data")
                        new_name = ""
                        if rename_to_email:
                            new_name = str(payload.get(YT_LOGIN_KEY) or "").strip()
                            if new_name:
                                try:
                                    updated = api.update_profile_name(pid, new_name)
                                    renamed += 1
                                except LocalAntidetectError as e:
                                    errors.append(f"{pid} (имя): {e}")
                        if self._profiles_raw is not None:
                            for i, p in enumerate(self._profiles_raw):
                                if _profile_id(p) != pid:
                                    continue
                                merged = dict(p)
                                if isinstance(cd_upd, dict):
                                    merged["custom_data"] = dict(cd_upd)
                                else:
                                    merged["custom_data"] = dict(payload)
                                if new_name:
                                    merged["name"] = new_name
                                self._profiles_raw[i] = merged
                                break
                        saved += 1
                    except LocalAntidetectError as e:
                        errors.append(f"{pid}: {e}")
            finally:
                api.close()
        except LocalAntidetectError as e:
            QMessageBox.warning(
                self,
                "Импорт данных учёток",
                f"Не удалось подключиться к API антидетекта:\n{e}",
            )
            return

        self._apply_profiles_filter()
        msg = f"Сохранено профилей: {saved} из {len(payloads)}."
        if rename_to_email and renamed:
            msg += f"\nПереименовано: {renamed}."
        if errors:
            msg += "\n\nОшибки:\n" + "\n".join(errors[:8])
            if len(errors) > 8:
                msg += f"\n… и ещё {len(errors) - 8}."
            QMessageBox.warning(self, "Импорт данных учёток", msg)
        else:
            QMessageBox.information(self, "Импорт данных учёток", msg)

    def _open_profile_cdp_preview(self, profile_id: str) -> None:
        pid = (profile_id or "").strip()
        if not pid:
            return
        kind = self._antidetect_kind()
        if not isinstance(kind, str) or kind.strip() != "remote":
            QMessageBox.information(
                self,
                "Просмотр",
                "Просмотр доступен только при выбранном «Свой (удалённый API)» в настройках.",
            )
            return

        existing = self._profile_cdp_previews.get(pid)
        if existing is not None:
            try:
                if existing.isVisible():
                    existing.raise_()
                    existing.activateWindow()
                    return
            except RuntimeError:
                existing = None
            self._profile_cdp_previews.pop(pid, None)

        self._save_antydetect_settings()
        base_url = self._own_antidetect_base_url_from_settings("remote")
        if not (base_url or "").strip():
            QMessageBox.warning(
                self,
                "Просмотр",
                "Укажите базовый URL удалённого API в настройках и сохраните.",
            )
            return

        name = pid
        for p in self._profiles_raw or []:
            if _profile_id(p) == pid:
                name = _profile_name(p)
                break

        try:
            api = LocalAntidetectHttpAPI(base_url)
            try:
                ws_url, session_id, user_msg = api.resolve_running_cdp_ws_url_for_profile(pid)
            finally:
                api.close()
        except LocalAntidetectError as e:
            QMessageBox.warning(self, "Просмотр", str(e))
            return

        if not (ws_url or "").strip():
            QMessageBox.information(
                self,
                "Просмотр",
                (user_msg or "").strip() or "Профиль не запущен.",
            )
            return

        dlg = ProfileCdpPreviewDialog(
            profile_id=pid,
            profile_name=name,
            base_url=base_url,
            cdp_ws_url=ws_url,
            session_id=session_id or "",
            parent=self,
        )
        self._profile_cdp_previews[pid] = dlg
        dlg.destroyed.connect(lambda _obj=None, p=pid: self._profile_cdp_previews.pop(p, None))
        dlg.show()

    def _open_profile_account_data_dialog(self, profile_id: str) -> None:
        pid = (profile_id or "").strip()
        if not pid:
            return
        section = (
            SECTION_INSTAGRAM
            if self._platform == PLATFORM_INSTAGRAM
            else SECTION_YOUTUBE
        )
        self._show_profile_account_data_dialog(pid, section=section)

    def _open_profile_gmail_data_dialog(self, profile_id: str) -> None:
        pid = (profile_id or "").strip()
        if not pid:
            return
        self._show_profile_account_data_dialog(pid, section=SECTION_GMAIL)

    def _show_profile_account_data_dialog(
        self, profile_id: str, *, section: str = SECTION_YOUTUBE
    ) -> None:
        pid = (profile_id or "").strip()
        if not pid:
            return

        profile: dict[str, object] | None = None
        for p in self._profiles_raw or []:
            if _profile_id(p) == pid:
                profile = p
                break
        name = _profile_name(profile) if profile else pid
        custom_data: dict[str, object] = {}
        if profile is not None:
            cd = profile.get("custom_data")
            if isinstance(cd, dict):
                custom_data = dict(cd)

        base_url = self._own_antidetect_base_url_from_settings()
        load_error: str | None = None
        try:
            api = LocalAntidetectHttpAPI(base_url)
            try:
                fresh = api.get_profile(pid)
                cd_fresh = fresh.get("custom_data")
                if isinstance(cd_fresh, dict):
                    custom_data = dict(cd_fresh)
            finally:
                api.close()
        except LocalAntidetectError as e:
            load_error = str(e)

        dlg_titles = {
            SECTION_INSTAGRAM: "Данные Insta",
            SECTION_GMAIL: "Данные Gmail",
            SECTION_YOUTUBE: "Данные учетки",
        }
        dlg_title = dlg_titles.get(section, "Данные учетки")

        dlg = ProfileAccountDataDialog(
            profile_name=name,
            profile_id=pid,
            custom_data=custom_data,
            section=section,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        payload = dlg.account_data_payload()
        if section == SECTION_GMAIL and not payload:
            QMessageBox.information(
                self,
                dlg_title,
                "Изменений нет — gmail_* в custom_data не обновлялись.",
            )
            return
        login_key = YT_LOGIN_KEY
        if section == SECTION_INSTAGRAM:
            login_key = INST_LOGIN_KEY
        elif section == SECTION_GMAIL:
            login_key = GMAIL_LOGIN_KEY
        login = str(payload.get(login_key) or "").strip()
        if login and section == SECTION_YOUTUBE:
            from zaliver.ui.account_import_parser import (
                find_profiles_with_login,
                format_profile_login_conflict,
            )

            dupes = find_profiles_with_login(
                login, list(self._profiles_raw or []), exclude_profile_id=pid
            )
            if dupes:
                owners = "\n".join(
                    f"• {format_profile_login_conflict(p)}" for p in dupes
                )
                answer = QMessageBox.warning(
                    self,
                    dlg_title,
                    f"Почта {login} уже указана в другом профиле:\n{owners}\n\n"
                    "Всё равно сохранить?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
        try:
            api = LocalAntidetectHttpAPI(base_url)
            try:
                updated = api.merge_profile_custom_data(pid, payload)
            finally:
                api.close()
        except LocalAntidetectError as e:
            QMessageBox.warning(
                self,
                dlg_title,
                f"Не удалось сохранить:\n{e}",
            )
            return

        cd_upd = updated.get("custom_data")
        if self._profiles_raw is not None:
            for i, p in enumerate(self._profiles_raw):
                if _profile_id(p) != pid:
                    continue
                merged = dict(p)
                if isinstance(cd_upd, dict):
                    merged["custom_data"] = dict(cd_upd)
                else:
                    merged["custom_data"] = dict(payload)
                self._profiles_raw[i] = merged
                break

        msg = "Сохранено в custom_data профиля."
        if load_error:
            msg = (
                "Сохранено в custom_data профиля.\n\n"
                f"При открытии не удалось обновить данные с API: {load_error}"
            )
        QMessageBox.information(self, dlg_title, msg)

    def _start_clear_zaliver_profile_tags(self) -> None:
        if self._profiles_tags_clear_running:
            QMessageBox.information(
                self,
                "Очистка тегов",
                "Очистка уже выполняется. Дождитесь завершения.",
            )
            return
        kind = self._antidetect_kind()
        if not _is_own_antidetect_kind(kind if isinstance(kind, str) else ""):
            QMessageBox.warning(
                self,
                "Очистка тегов",
                "Снятие тегов Zaliver доступно только для своего антидетекта. "
                "Выберите «Свой (локальный API)» или «Свой (удалённый API)» в настройках.",
            )
            return
        if self._profiles_raw is None:
            QMessageBox.warning(
                self,
                "Очистка тегов",
                "Сначала загрузите список профилей (кнопка «Обновить»).",
            )
            return
        profile_ids = self._collect_checked_profile_ids()
        if not profile_ids:
            QMessageBox.warning(
                self,
                "Очистка тегов",
                "Отметьте профили квадратиками, с которых нужно снять теги залива.",
            )
            return
        from zaliver.ui.profile_tags_clear_dialog import (
            ProfileTagsClearDialog,
            collect_zaliver_tags_for_profiles,
        )

        by_id = self._profiles_by_id_map(self._profiles_raw)
        tags_on_profiles = collect_zaliver_tags_for_profiles(by_id, profile_ids)
        if not tags_on_profiles:
            QMessageBox.information(
                self,
                "Очистка тегов",
                "У отмеченных профилей нет служебных тегов Zaliver для очистки.",
            )
            return

        dlg = ProfileTagsClearDialog(
            profile_count=len(profile_ids),
            tags=tags_on_profiles,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        tags_to_clear = tuple(dlg.selected_tags())
        if not tags_to_clear:
            return

        base_url = self._own_antidetect_base_url_from_settings()
        if not (base_url or "").strip():
            QMessageBox.warning(
                self,
                "Очистка тегов",
                f"Укажите базовый URL {_own_antidetect_api_label(kind if isinstance(kind, str) else '')} "
                "API в настройках и сохраните.",
            )
            return
        self._profiles_tags_clear_running = True
        self._sync_profiles_tab_action_buttons()
        self._profiles_status.setText(
            f"Очистка тегов залива: 0 / {len(profile_ids)}…"
        )
        self._append_log(
            f"[tags] Старт очистки {len(tags_to_clear)} тегов "
            f"у {len(profile_ids)} профилей…"
        )
        threading.Thread(
            target=self._clear_zaliver_profile_tags_worker,
            kwargs={
                "profile_ids": profile_ids,
                "base_url": base_url,
                "tags_to_clear": tags_to_clear,
            },
            daemon=True,
        ).start()

    def _clear_zaliver_profile_tags_worker(
        self,
        *,
        profile_ids: list[str],
        base_url: str,
        tags_to_clear: tuple[str, ...],
    ) -> None:
        from zaliver.antydetect.profile_tags import clear_zaliver_tags_on_profile

        tags = tuple(t for t in tags_to_clear if (t or "").strip())
        api = LocalAntidetectHttpAPI((base_url or "").strip())
        total = len(profile_ids)
        removed_total = 0
        try:
            for i, pid in enumerate(profile_ids, start=1):
                self._zaliver_profile_tags_clear_progress.emit(i, total, pid)
                n = clear_zaliver_tags_on_profile(api, pid, tags=tags)
                removed_total += n
                if n > 0:
                    self._ui_log_line.emit(
                        f"[tags] profile={pid}: снято тегов {n} "
                        f"из {len(tags)}"
                    )
                try:
                    self._upload_store.reset_profile_upload_errors(profile_id=pid)
                except Exception:
                    pass
        finally:
            api.close()
        self._zaliver_profile_tags_clear_finished.emit(len(profile_ids), removed_total)

    def _on_zaliver_profile_tags_clear_progress(
        self, current: int, total: int, profile_id: str
    ) -> None:
        pid = (profile_id or "").strip()
        self._profiles_status.setText(
            f"Очистка тегов залива: {current} / {total}"
            + (f" — профиль {pid}" if pid else "…")
        )

    def _on_zaliver_profile_tags_clear_finished(
        self, profile_count: int, removed_total: int
    ) -> None:
        self._profiles_tags_clear_running = False
        self._sync_profiles_tab_action_buttons()
        n = int(profile_count)
        r = int(removed_total)
        self._profiles_status.setText(
            f"Очистка тегов завершена: профилей {n}, снято тегов {r}."
        )
        self._append_log(
            f"[tags] Итог: профилей {n}, снято тегов {r}."
        )
        self._refresh_antydetect_profiles()
        QMessageBox.information(
            self,
            "Очистка тегов залива",
            f"Обработано профилей: {n}.\nСнято тегов (успешных DELETE): {r}.",
        )

    def _on_studio_availability_progress(self, current: int, total: int, profile_id: str) -> None:
        pid = (profile_id or "").strip()
        check_label = "Instagram" if self._platform == PLATFORM_INSTAGRAM else "Studio"
        self._profiles_status.setText(
            f"Проверка доступности {check_label}: {current} / {total}"
            + (f" — профиль {pid}" if pid else "…")
        )

    def _on_studio_availability_finished(self, ok_n: int, fail_n: int) -> None:
        self._profiles_availability_running = False
        self._sync_profiles_tab_action_buttons()
        total = int(ok_n) + int(fail_n)
        self._profiles_status.setText(
            f"Проверка доступности завершена: успешно {ok_n}, с ошибкой {fail_n} "
            f"(всего {total})."
        )
        self._append_log(
            f"[availability] Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}."
        )
        if int(fail_n) > 0:
            failed = getattr(self, "_last_availability_failed_ids", None) or []
            if failed:
                self._append_log(
                    "[availability] Недоступные профили (ID): " + ", ".join(failed)
                )
        kind = self._antidetect_kind()
        if _is_own_antidetect_kind((kind or "").strip()) and total > 0:
            self._refresh_profiles_list_after_zaliver_tags()
        QMessageBox.information(
            self,
            "Проверка доступности",
            f"Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}.",
        )

    def _on_instagram_register_progress(
        self, current: int, total: int, profile_id: str
    ) -> None:
        pid = (profile_id or "").strip()
        self._profiles_status.setText(
            f"Регистрация Instagram: {current} / {total}"
            + (f" — профиль {pid}" if pid else "…")
        )

    def _raise_zaliver_window(self) -> None:
        """Поднять Zaliver поверх других окон (AppShell или MainWindow)."""
        win = self.window()
        try:
            if win.isMinimized():
                win.showNormal()
            else:
                win.show()
            win.raise_()
            win.activateWindow()
        except Exception:
            pass
        app = QApplication.instance()
        if app is not None:
            try:
                app.alert(win, 3000)
            except Exception:
                pass

    def _profile_display_name(self, profile_id: str) -> str:
        pid = (profile_id or "").strip()
        if not pid:
            return ""
        for p in self._profiles_raw or []:
            if not isinstance(p, dict):
                continue
            if _profile_id(p) == pid:
                return _profile_name(p)
        return ""

    def _focus_profiles_tab_and_profile(self, profile_id: str) -> None:
        """Открыть вкладку «Профили» и выделить нужный профиль."""
        pid = (profile_id or "").strip()
        try:
            # 0 Уникализация, 1 Нарезка, 2 Склейка, 3 Готовые, 4 Залитые, 5 Профили
            self._nav.setCurrentRow(5)
        except Exception:
            pass
        inter = getattr(self, "_profiles_interaction", None)
        if inter is None or not pid:
            return
        ok = inter.focus_profile(pid, check=True, attention=True)
        if not ok:
            self._append_log(
                f"[ig-register] Профиль {pid} не найден в списке "
                "(возможно, скрыт фильтром) — сбросьте поиск/фильтр тегов."
            )
            self._append_log(
                f"[ig-register] Нужна ручная капча для профиля {pid}."
            )
        else:
            self._append_log(
                f"[ig-register] Выделен профиль {pid} — пройдите капчу в браузере."
            )

    def _on_manual_captcha_needed(self, profile_id: str) -> None:
        """Если расширение не решило капчу — системное уведомление Windows."""
        pid = (profile_id or "").strip()
        if not pid:
            return
        QTimer.singleShot(0, lambda p=pid: self._show_manual_captcha_notification(p))

    def _show_manual_captcha_notification(self, profile_id: str) -> None:
        pid = (profile_id or "").strip()
        if not pid:
            return
        name = self._profile_display_name(pid)
        self._append_log(
            f"[ig-register] Нужна ручная капча для профиля {pid}"
            + (f" ({name})" if name else "")
            + "."
        )
        self._pending_captcha_notify_profile_id = pid

        # Мигание кнопки на панели задач (Windows alert).
        try:
            win = self.window()
            app = QApplication.instance()
            if app is not None:
                app.alert(win, 0)  # 0 = пока пользователь не среагирует
        except Exception:
            pass

        title = "Zaliver — нужна капча"
        if name:
            body = f"Пройдите капчу вручную.\nПрофиль: {name} ({pid})"
        else:
            body = f"Пройдите капчу вручную.\nПрофиль: {pid}"

        shown = False
        try:
            win = self.window()
            notifier = getattr(win, "desktop_notifier", None)
            if notifier is None:
                from zaliver.ui.desktop_notify import DesktopNotifier

                if not hasattr(self, "_fallback_desktop_notifier"):
                    self._fallback_desktop_notifier = DesktopNotifier(self)
                notifier = self._fallback_desktop_notifier

            try:
                notifier.message_clicked.disconnect(
                    self._on_desktop_captcha_notification_clicked
                )
            except Exception:
                pass
            notifier.message_clicked.connect(
                self._on_desktop_captcha_notification_clicked,
                Qt.ConnectionType.QueuedConnection,
            )

            show = getattr(win, "show_desktop_notification", None)
            if callable(show):
                shown = bool(show(title, body, msecs=30000))
            else:
                shown = bool(notifier.notify(title, body, msecs=30000))
        except Exception as e:
            self._append_log(f"[ig-register] Уведомление: {e!r}")
            shown = False

        if not shown:
            # Fallback: без tray — просто поднять окно и выделить профиль.
            self._append_log(
                "[ig-register] Системные уведомления недоступны — "
                "открываю вкладку «Профили»."
            )
            try:
                self._raise_zaliver_window()
                self._focus_profiles_tab_and_profile(pid)
            except Exception:
                pass

    def _on_desktop_captcha_notification_clicked(self) -> None:
        pid = (getattr(self, "_pending_captcha_notify_profile_id", "") or "").strip()
        try:
            self._raise_zaliver_window()
            if pid:
                self._focus_profiles_tab_and_profile(pid)
        except Exception as e:
            self._append_log(
                f"[ig-register] Не удалось открыть профиль из уведомления: {e!r}"
            )

    def _on_instagram_register_finished(self, ok_n: int, fail_n: int) -> None:
        self._profiles_register_running = False
        self._sync_profiles_tab_action_buttons()
        total = int(ok_n) + int(fail_n)
        self._profiles_status.setText(
            f"Регистрация Instagram завершена: успешно {ok_n}, с ошибкой {fail_n} "
            f"(всего {total})."
        )
        self._append_log(
            f"[ig-register] Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}."
        )
        if int(fail_n) > 0:
            failed = getattr(self, "_last_register_failed_ids", None) or []
            if failed:
                self._append_log(
                    "[ig-register] Профили с ошибкой (ID): " + ", ".join(failed)
                )
        kind = self._antidetect_kind()
        if _is_own_antidetect_kind((kind or "").strip()) and total > 0:
            self._refresh_profiles_list_after_zaliver_tags()
        QMessageBox.information(
            self,
            "Регистрация Instagram",
            f"Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}.\n"
            "Успех = форма отправлена и аккаунт подтверждён кодом из почты.",
        )

    def _on_instagram_2fa_progress(
        self, current: int, total: int, profile_id: str
    ) -> None:
        pid = (profile_id or "").strip()
        self._profiles_status.setText(
            f"Подключение 2FA Instagram: {current} / {total}"
            + (f" — профиль {pid}" if pid else "…")
        )

    def _on_instagram_2fa_finished(self, ok_n: int, fail_n: int) -> None:
        self._profiles_2fa_running = False
        self._sync_profiles_tab_action_buttons()
        total = int(ok_n) + int(fail_n)
        self._profiles_status.setText(
            f"Подключение 2FA Instagram завершено: успешно {ok_n}, с ошибкой {fail_n} "
            f"(всего {total})."
        )
        self._append_log(
            f"[ig-2fa] Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}."
        )
        if int(fail_n) > 0:
            failed = getattr(self, "_last_2fa_failed_ids", None) or []
            if failed:
                self._append_log(
                    "[ig-2fa] Профили с ошибкой (ID): " + ", ".join(failed)
                )
        kind = self._antidetect_kind()
        if _is_own_antidetect_kind((kind or "").strip()) and total > 0:
            self._refresh_profiles_list_after_zaliver_tags()
        QMessageBox.information(
            self,
            "Подключение 2FA Instagram",
            f"Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}.\n"
            "Успех = 2FA подключена (или уже была включена). "
            "Новый секрет сохраняется в inst_2fa.",
        )

    def _on_studio_channel_setup_progress(
        self, current: int, total: int, profile_id: str
    ) -> None:
        pid = (profile_id or "").strip()
        status_line = (
            f"Настройка канала в Studio: {current} / {total}"
            + (f" — профиль {pid}" if pid else "…")
        )
        self._profiles_status.setText(status_line)
        if hasattr(self, "_channel_edit_tab"):
            self._channel_edit_tab.set_status(status_line)

    def _on_studio_channel_setup_finished(self, ok_n: int, fail_n: int) -> None:
        self._profiles_channel_setup_running = False
        self._sync_profiles_tab_action_buttons()
        total = int(ok_n) + int(fail_n)
        title = self._profiles_channel_setup_dialog_title()
        status_line = (
            f"Настройка канала завершена: успешно {ok_n}, с ошибкой {fail_n} "
            f"(всего {total})."
        )
        self._profiles_status.setText(status_line)
        if hasattr(self, "_channel_edit_tab"):
            self._channel_edit_tab.set_running(False)
            self._channel_edit_tab.set_status(status_line)
        self._append_log(
            f"[channel_setup] Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}."
        )
        if int(fail_n) > 0:
            failed = getattr(self, "_last_channel_setup_failed_ids", None) or []
            if failed:
                self._append_log(
                    "[channel_setup] Ошибки (ID): " + ", ".join(failed)
                )
        self._refresh_profiles_list_after_zaliver_tags()
        QMessageBox.information(
            self,
            title,
            f"Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}.",
        )

    def _on_studio_warmup_progress(
        self, current: int, total: int, profile_id: str
    ) -> None:
        pid = (profile_id or "").strip()
        label = (
            "Прогрев Reels"
            if self._platform == PLATFORM_INSTAGRAM
            else "Прогрев Shorts"
        )
        self._profiles_status.setText(
            f"{label}: {current} / {total}"
            + (f" — профиль {pid}" if pid else "…")
        )

    def _on_studio_warmup_finished(self, ok_n: int, fail_n: int) -> None:
        self._profiles_warmup_running = False
        self._sync_profiles_tab_action_buttons()
        total = int(ok_n) + int(fail_n)
        label = (
            "Прогрев Reels"
            if self._platform == PLATFORM_INSTAGRAM
            else "Прогрев Shorts"
        )
        self._profiles_status.setText(
            f"{label} завершён: успешно {ok_n}, с ошибкой {fail_n} "
            f"(всего {total})."
        )
        self._append_log(
            f"[warmup] Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}."
        )
        if int(fail_n) > 0:
            failed = getattr(self, "_last_warmup_failed_ids", None) or []
            if failed:
                self._append_log(
                    "[warmup] Ошибки (ID): " + ", ".join(failed)
                )
        self._refresh_profiles_list_after_zaliver_tags()
        QMessageBox.information(
            self,
            label,
            f"Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}.",
        )

    def _on_studio_promote_progress(
        self, current: int, total: int, profile_id: str
    ) -> None:
        pid = (profile_id or "").strip()
        self._profiles_status.setText(
            f"Продвижение: {current} / {total}"
            + (f" — профиль {pid}" if pid else "…")
        )

    def _on_studio_promote_finished(self, ok_n: int, fail_n: int) -> None:
        self._profiles_promote_running = False
        self._sync_profiles_tab_action_buttons()
        total = int(ok_n) + int(fail_n)
        self._profiles_status.setText(
            f"Продвижение завершено: успешно {ok_n}, с ошибкой {fail_n} "
            f"(всего {total})."
        )
        self._append_log(
            f"[promote] Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}."
        )
        if int(fail_n) > 0:
            failed = getattr(self, "_last_promote_failed_ids", None) or []
            if failed:
                self._append_log(
                    "[promote] Ошибки (ID): " + ", ".join(failed)
                )
        self._refresh_profiles_list_after_zaliver_tags()
        QMessageBox.information(
            self,
            "Продвижение",
            f"Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}.",
        )

    def _on_studio_cookie_farm_progress(
        self, current: int, total: int, profile_id: str
    ) -> None:
        pid = (profile_id or "").strip()
        self._profiles_status.setText(
            f"Фарм Cookie: {current} / {total}"
            + (f" — профиль {pid}" if pid else "…")
        )

    def _on_studio_cookie_farm_finished(self, ok_n: int, fail_n: int) -> None:
        self._profiles_cookie_farm_running = False
        self._sync_profiles_tab_action_buttons()
        total = int(ok_n) + int(fail_n)
        self._profiles_status.setText(
            f"Фарм Cookie завершён: успешно {ok_n}, с ошибкой {fail_n} "
            f"(всего {total})."
        )
        self._append_log(
            f"[cookie_farm] Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}."
        )
        if int(fail_n) > 0:
            failed = getattr(self, "_last_cookie_farm_failed_ids", None) or []
            if failed:
                self._append_log(
                    "[cookie_farm] Ошибки (ID): " + ", ".join(failed)
                )
        kind = self._antidetect_kind()
        if _is_own_antidetect_kind((kind or "").strip()) and total > 0:
            self._refresh_profiles_list_after_zaliver_tags()
        QMessageBox.information(
            self,
            "Фарм Cookie",
            f"Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}.",
        )

    def _on_profiles_load_failed(self, message: str) -> None:
        self._profiles_refresh_running = False
        self._sync_profiles_tab_action_buttons()
        self._profiles_raw = None
        if self._profiles_interaction is not None:
            self._profiles_interaction.clear_checked_selection()
            self._profiles_interaction.checked_profile_ids.clear()
        if hasattr(self, "_profiles_list"):
            self._profiles_list.blockSignals(True)
            try:
                self._profiles_list.clear()
            finally:
                self._profiles_list.blockSignals(False)
            self._profiles_list_render_gen += 1
        if hasattr(self, "_lbl_checked_profiles_count"):
            self._lbl_checked_profiles_count.setText("Выделено: 0")
        self._profiles_status.setText(f"Не удалось загрузить список профилей.\n{message}")

    def _ask_reset_upload_cooldown_for_profile(
        self,
        profile_id: str,
        *,
        dialog_parent: QWidget | None = None,
        dialog_profile_list: QListWidget | None = None,
        dialog_profiles_interaction: ProfilesListInteraction | None = None,
    ) -> None:
        pid = (profile_id or "").strip()
        if not pid:
            return
        parent = dialog_parent or self
        pause = self._upload_pause_between_uploads()
        pause_short = format_upload_pause_short(pause)
        ans = QMessageBox.question(
            parent,
            f"Пауза {pause_short}",
            "Обновить время паузы с последнего залива? После подтверждения с этим профилем снова можно будет загружать видео.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        n = self._upload_store.reset_latest_upload_time_for_profile(
            profile_id=pid, platform=self._platform, pause=pause
        )
        if n <= 0:
            QMessageBox.information(
                parent,
                "Zaliver",
                "Нет сохранённых заливов для этого профиля в базе — обновлять нечего.",
            )
            return
        QMessageBox.information(
            parent,
            "Zaliver",
            "Пауза обновлена: с этого профиля снова можно загружать видео.",
        )
        new_map = self._upload_store.last_uploaded_at_by_profiles(
            [pid], platform=self._platform
        )
        new_iso = new_map.get(pid)
        if dialog_profiles_interaction is not None:
            dialog_profiles_interaction.update_upload_cooldown_for_profile(pid, new_iso)
        elif self._profiles_interaction is not None:
            self._profiles_interaction.update_upload_cooldown_for_profile(pid, new_iso)
        elif hasattr(self, "_profiles_list"):
            self._apply_profiles_filter()

    def _dolphin_google_worker(
        self,
        *,
        profile_id: str,
        token: str,
        kind: str,
        base_url: str,
        upload_video_path: str | None = None,
        upload_title: str | None = None,
        upload_description: str | None = None,
        remote_cdp: RemoteCdpLaunchOptions | None = None,
    ) -> None:
        try:
            # Логи антидетекта могут быть критичны для диагностики.
            # Пишем первую строку напрямую в UI, ещё до импортов (импорт может упасть).
            try:
                self._ui_log_line.emit(
                    f"[antydetect] worker start: profile_id={profile_id!r}, kind={kind!r}"
                )
            except Exception:
                pass
            from zaliver.antydetect.antic_open import (
                open_google_in_local_antidetect_profile,
                open_google_in_profile,
                set_log_sink,
            )

            set_log_sink(self._ui_log_line.emit)
            try:
                self._ui_log_line.emit("[antydetect] log sink установлен.")
            except Exception:
                pass

            headless = True
            if hasattr(self, "_dolphin_headless"):
                headless = bool(self._dolphin_headless.isChecked())
            else:
                headless = bool(
                    self._settings.value("antydetect/dolphin_headless", True, type=bool)
                )

            creds = self._profile_login_credentials(profile_id)
            yt_oldest = self._profile_yt_oldest_name(profile_id) or None
            search_oldest = self._youtube_search_oldest_channel()
            if _is_own_antidetect_kind(kind):
                u = (base_url or "").strip()
                if not u:
                    raise LocalAntidetectError(
                        f"Сначала укажите базовый URL {_own_antidetect_api_label(kind)} API в настройках."
                    )
                res = open_google_in_local_antidetect_profile(
                    profile_id,
                    base_url=u,
                    headless=headless,
                    video_path=upload_video_path,
                    title=upload_title,
                    description=upload_description,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    search_oldest_channel=search_oldest,
                    remote_cdp=remote_cdp,
                )
            else:
                res = open_google_in_profile(
                    profile_id,
                    local_token=token or None,
                    headless=headless,
                    video_path=upload_video_path,
                    title=upload_title,
                    description=upload_description,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    search_oldest_channel=search_oldest,
                )
            try:
                vid = ""
                url = ""
                if isinstance(res, dict):
                    vid = str(res.get("video_id") or "").strip()
                    url = str(res.get("url") or "").strip()
                if not vid and url:
                    try:
                        from zaliver.youtube_parsing.video_stats import extract_video_id

                        vid = extract_video_id(url)
                    except Exception:
                        pass
                if vid:
                    sid = int(self._upload_session.id) if self._upload_session is not None else 0
                    if sid <= 0:
                        raise RuntimeError("upload_session is not set (sid=0)")
                    self._upload_store.add_uploaded_video(
                        session_id=sid,
                        title=upload_title or "",
                        description=upload_description or "",
                        url=url,
                        video_id=vid,
                        profile_id=profile_id,
                        platform=self._platform,
                    )
                    try:
                        self._upload_store.inc_uploaded_ok(
                            session_id=sid, delta=1
                        )
                    except Exception:
                        pass
                    try:
                        self._ui_log_line.emit(
                            f"[uploaded] сохранено: videoId={vid!r}, session_id={sid}"
                        )
                    except Exception:
                        pass
                    try:
                        QTimer.singleShot(0, self._refresh_uploaded_list)
                    except Exception:
                        pass
                else:
                    try:
                        self._ui_log_line.emit(
                            f"[uploaded] не удалось сохранить: пустой videoId (url={url!r}, res={res!r})"
                        )
                    except Exception:
                        pass
            except Exception:
                try:
                    import traceback

                    self._ui_log_line.emit(
                        "[uploaded] ошибка сохранения в базу:\n" + traceback.format_exc()
                    )
                except Exception:
                    pass
            self._dolphin_google_ready.emit(profile_id)
        except Exception as e:
            try:
                import traceback

                self._ui_log_line.emit(
                    "[antydetect] worker error:\n" + traceback.format_exc()
                )
            except Exception:
                pass
            self._dolphin_google_failed.emit(profile_id, str(e))

    def _on_dolphin_google_ready(self, _profile_id: str) -> None:
        if self._profiles_raw is not None:
            self._apply_profiles_filter()
        self._upload_session_upload_done = True
        self._maybe_finish_upload_session(status="done")

    def _on_dolphin_google_failed(self, profile_id: str, message: str) -> None:
        if self._profiles_raw is not None:
            self._apply_profiles_filter()
        self._upload_session_upload_done = True
        self._maybe_finish_upload_session(status="upload_failed")
        kind = self._antidetect_kind()
        if _is_own_antidetect_kind(kind if isinstance(kind, str) else ""):
            hint = (
                f"Нужны доступный API {_own_antidetect_api_label(kind if isinstance(kind, str) else '')} "
                "антидетекта, Playwright и сессия Studio в профиле. "
            )
        else:
            hint = (
                "Нужны Dolphin, Local API, playwright и сессия Studio в профиле. "
            )
        self._profiles_status.setText(
            f"Профиль {profile_id}: не удалось открыть YouTube Studio / загрузку. "
            f"{hint}{message}"
        )

    def _input_files_dialog_filter(self) -> str:
        return "Видео (*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts);;Все файлы (*)"

    def _normalize_input_path_key(self, p: str) -> str:
        try:
            return os.path.normcase(str(Path(p).resolve()))
        except OSError:
            return os.path.normcase(os.path.normpath(str(p)))

    def _merge_unique_input_paths(
        self, existing: list[str], new_files: list[str]
    ) -> list[str]:
        seen = {self._normalize_input_path_key(p) for p in existing}
        merged = list(existing)
        for f in new_files:
            raw = str(f).strip()
            if not raw:
                continue
            try:
                p = str(Path(raw).resolve())
            except OSError:
                p = raw
            key = self._normalize_input_path_key(p)
            if key in seen:
                continue
            seen.add(key)
            merged.append(p)
        return merged

    def _input_files_start_dir(self) -> str:
        if self._selected_input_files:
            return str(Path(self._selected_input_files[0]).parent)
        return str(Path.home())

    def _browse_input_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите видеофайлы для обработки (можно несколько)",
            self._input_files_start_dir(),
            self._input_files_dialog_filter(),
        )
        if files:
            self._selected_input_files = self._merge_unique_input_paths([], files)
            self._sync_input_files_hint()
            self._save_folder_settings()

    def _add_input_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Добавить видеофайлы к списку",
            self._input_files_start_dir(),
            self._input_files_dialog_filter(),
        )
        if files:
            self._selected_input_files = self._merge_unique_input_paths(
                self._selected_input_files, files
            )
            self._sync_input_files_hint()
            self._save_folder_settings()

    def _clear_input_files(self) -> None:
        if not self._selected_input_files:
            return
        self._selected_input_files = []
        self._sync_input_files_hint()
        self._save_folder_settings()

    def _sync_input_files_hint(self) -> None:
        if not hasattr(self, "_input_files_hint"):
            return
        n = len(self._selected_input_files or [])
        has_files = n > 0
        if hasattr(self, "_btn_add_input_files"):
            self._btn_add_input_files.setVisible(has_files)
        if hasattr(self, "_btn_clear_input_files"):
            self._btn_clear_input_files.setVisible(has_files)
        if n <= 0:
            self._input_files_hint.setText("Не выбрано — нажмите «Выбрать файлы…»")
            self._input_files_hint.setToolTip("")
            if hasattr(self, "text_overlay_preview"):
                self._schedule_text_overlay_preview_sync()
            return
        names = [Path(p).name for p in self._selected_input_files]
        preview = ", ".join(names[:4])
        if n > 4:
            preview = f"{preview} и ещё {n - 4}"
        self._input_files_hint.setText(f"Выбрано: {n} ({preview})")
        self._input_files_hint.setToolTip("\n".join(names))
        if hasattr(self, "text_overlay_preview"):
            self._schedule_text_overlay_preview_sync()

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

    def _sync_music_hint(self) -> None:
        if not hasattr(self, "_music_hint"):
            return
        n = len(self._background_music_files)
        if n <= 0:
            self._music_hint.setText(
                "Пул треков пуст. Добавьте несколько файлов — для каждого выходного видео "
                "будет выбран случайный трек и случайное место на шкале времени (длина = длина ролика)."
            )
        else:
            self._music_hint.setText(
                f"В пуле треков: {n}. Для каждого выходного MP4 — случайный файл и отрезок под длительность ролика. "
                "Включите «Смешивать с аудио исходника», чтобы музыка шла поверх звука видео (ползунок громкости)."
            )

    def _on_music_volume_slider_changed(self, *_args) -> None:
        self._save_folder_settings()

    def _update_music_mix_controls(self, _checked: bool = False) -> None:
        if not hasattr(self, "background_music_mix"):
            return
        music_on = bool(self.background_music.isChecked())
        if hasattr(self, "_music_settings_panel"):
            self._music_settings_panel.setVisible(music_on)
        self.background_music_mix.setEnabled(music_on)
        mix_on = music_on and self.background_music_mix.isChecked()
        self.background_music_volume.setEnabled(mix_on)

    def _sync_music_list_widget(self) -> None:
        if not hasattr(self, "_music_list"):
            return
        self._music_list.clear()
        for p in self._background_music_files:
            it = QListWidgetItem(Path(p).name)
            it.setToolTip(p)
            it.setData(Qt.ItemDataRole.UserRole, p)
            self._music_list.addItem(it)
        has_files = bool(self._background_music_files)
        if hasattr(self, "btn_add_music"):
            self.btn_add_music.setText(
                "Добавить еще файлы…" if has_files else "Добавить треки…"
            )
        if hasattr(self, "btn_clear_music"):
            self.btn_clear_music.setVisible(has_files)
        self._sync_music_hint()

    def _browse_background_music(self) -> None:
        start = str(Path.home())
        if self._background_music_files:
            start = str(Path(self._background_music_files[0]).parent)
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Аудио для фона (можно несколько)",
            start,
            "Аудио (*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus);;Все файлы (*)",
        )
        if not files:
            return
        self._background_music_files = self._merge_unique_music_paths(
            self._background_music_files, files
        )
        self._sync_music_list_widget()
        self._save_folder_settings()

    def _merge_unique_music_paths(
        self, existing: list[str], new_files: list[str]
    ) -> list[str]:
        seen = {self._normalize_music_path_key(p) for p in existing}
        merged = list(existing)
        for f in new_files:
            raw = str(f).strip()
            if not raw:
                continue
            try:
                p = str(Path(raw).resolve())
            except OSError:
                p = raw
            key = self._normalize_music_path_key(p)
            if key in seen:
                continue
            seen.add(key)
            merged.append(p)
        return merged

    def _clear_background_music(self) -> None:
        if not self._background_music_files:
            return
        self._background_music_files = []
        self._sync_music_list_widget()
        self._save_folder_settings()

    def _normalize_music_path_key(self, p: str) -> str:
        """Ключ для сравнения путей (Windows: регистр и слэши)."""
        try:
            return os.path.normcase(str(Path(p).resolve()))
        except OSError:
            return os.path.normcase(os.path.normpath(str(p)))

    def _remove_selected_music(self) -> None:
        if not hasattr(self, "_music_list"):
            return
        items = list(self._music_list.selectedItems())
        # Клик по кнопке иногда снимает выделение до слота — остаётся текущая строка.
        if not items:
            cur = self._music_list.currentItem()
            if cur is not None:
                items = [cur]
        if not items:
            return
        drop_keys: set[str] = set()
        for it in items:
            raw = it.data(Qt.ItemDataRole.UserRole)
            if raw is None:
                continue
            drop_keys.add(self._normalize_music_path_key(str(raw)))
        if not drop_keys:
            return
        self._background_music_files = [
            p
            for p in self._background_music_files
            if self._normalize_music_path_key(p) not in drop_keys
        ]
        self._sync_music_list_widget()
        self._save_folder_settings()

    def _text_overlay_wave_values(self) -> tuple[float, float, float, float]:
        """(amp_lo, amp_hi, speed_lo, speed_hi) в долях 0..1."""
        amp_lo = max(0.0, min(0.35, float(self.text_overlay_wave_amp.lowValue()) / 100.0))
        amp_hi = max(0.0, min(0.35, float(self.text_overlay_wave_amp.highValue()) / 100.0))
        speed_lo = max(
            0.0, min(0.25, float(self.text_overlay_wave_speed.lowValue()) / 100.0)
        )
        speed_hi = max(
            0.0, min(0.25, float(self.text_overlay_wave_speed.highValue()) / 100.0)
        )
        if amp_hi < amp_lo:
            amp_lo, amp_hi = amp_hi, amp_lo
        if speed_hi < speed_lo:
            speed_lo, speed_hi = speed_hi, speed_lo
        return amp_lo, amp_hi, speed_lo, speed_hi

    def _sync_text_overlay_wave_labels(self) -> None:
        return

    def _on_text_overlay_wave_changed(self, *_args) -> None:
        self._schedule_text_overlay_preview_sync()
        self._save_folder_settings()

    def _text_overlay_settings(self) -> TextOverlaySettings:
        orient = self.text_overlay_orientation.currentData()
        ax, ay = self.text_overlay_preview.anchor()
        waf_lo, waf_hi, wfs_lo, wfs_hi = self._text_overlay_wave_values()
        # Для превью/базового значения — середина диапазона.
        waf = (waf_lo + waf_hi) * 0.5
        wfs = (wfs_lo + wfs_hi) * 0.5
        return TextOverlaySettings(
            enabled=bool(self.text_overlay_enabled.isChecked()),
            text=self.text_overlay_edit.toPlainText(),
            font_size=int(self.text_overlay_font_size.value()),
            glow_enabled=bool(self.text_overlay_glow_enabled.isChecked()),
            glow_color=self._text_overlay_glow_color,
            text_color=self._text_overlay_text_color,
            letter_spacing=int(self.text_overlay_letter_spacing.value()),
            custom_font_path=self._text_overlay_font_path,
            font_bold=bool(self.text_overlay_font_bold.isChecked()),
            preview_orientation=orient if isinstance(orient, str) else "vertical",
            anchor_x=float(ax),
            anchor_y=float(ay),
            wave_amp_frac=float(waf),
            wave_char_phase=float(NEON_WAVE_CHAR_PHASE),
            wave_frame_speed=float(wfs),
            from_middle=bool(self.text_overlay_from_middle.isChecked()),
        )

    def _text_overlay_options_dict(self) -> dict:
        d = self._text_overlay_settings().to_dict()
        waf_lo, waf_hi, wfs_lo, wfs_hi = self._text_overlay_wave_values()
        d["wave_amp_frac_min"] = float(waf_lo)
        d["wave_amp_frac_max"] = float(waf_hi)
        d["wave_frame_speed_min"] = float(wfs_lo)
        d["wave_frame_speed_max"] = float(wfs_hi)
        return d

    def _apply_text_overlay_options(self, raw: dict) -> None:
        """Применить настройки текста (в т.ч. из импорта JSON)."""
        from zaliver.ui.text_overlay_io import normalize_text_overlay_export_dict

        d = normalize_text_overlay_export_dict(raw if isinstance(raw, dict) else {})
        self.text_overlay_enabled.setChecked(bool(d.get("enabled", True)))
        self._set_uniquify_text_overlay_text(str(d.get("text") or ""))
        self.text_overlay_from_middle.setChecked(bool(d.get("from_middle", True)))
        try:
            fs = int(d.get("font_size", 95))
        except (TypeError, ValueError):
            fs = 95
        self.text_overlay_font_size.setValue(max(12, min(240, fs)))
        orient = (
            "horizontal"
            if str(d.get("preview_orientation") or "").lower() == "horizontal"
            else "vertical"
        )
        idx = self.text_overlay_orientation.findData(orient)
        if idx >= 0:
            self.text_overlay_orientation.setCurrentIndex(idx)
        self._text_overlay_glow_color = str(d.get("glow_color") or "#00FFFF")
        self._text_overlay_text_color = str(d.get("text_color") or "#FFFFFF")
        self.text_overlay_glow_enabled.setChecked(bool(d.get("glow_enabled", True)))
        try:
            ls = int(d.get("letter_spacing", 0))
        except (TypeError, ValueError):
            ls = 0
        self.text_overlay_letter_spacing.setValue(max(-20, min(80, ls)))
        self._text_overlay_font_path = str(d.get("custom_font_path") or "").strip()
        self._populate_text_overlay_font_combo()
        self.text_overlay_font_bold.setChecked(bool(d.get("font_bold", True)))
        waf_lo = float(d.get("wave_amp_frac_min", d.get("wave_amp_frac", NEON_WAVE_AMP_FRAC)))
        waf_hi = float(d.get("wave_amp_frac_max", waf_lo))
        wfs_lo = float(
            d.get("wave_frame_speed_min", d.get("wave_frame_speed", NEON_WAVE_FRAME_SPEED))
        )
        wfs_hi = float(d.get("wave_frame_speed_max", wfs_lo))
        self.text_overlay_wave_amp.blockSignals(True)
        self.text_overlay_wave_amp.setValues(
            int(round(waf_lo * 100)), int(round(waf_hi * 100))
        )
        self.text_overlay_wave_amp.blockSignals(False)
        self.text_overlay_wave_speed.blockSignals(True)
        self.text_overlay_wave_speed.setValues(
            int(round(wfs_lo * 100)), int(round(wfs_hi * 100))
        )
        self.text_overlay_wave_speed.blockSignals(False)
        try:
            ax = float(d.get("anchor_x", 0.5))
            ay = float(d.get("anchor_y", 0.15))
        except (TypeError, ValueError):
            ax, ay = 0.5, 0.15
        self._sync_text_overlay_color_btn(
            self.text_overlay_glow_btn, self._text_overlay_glow_color
        )
        self._sync_text_overlay_color_btn(
            self.text_overlay_text_btn, self._text_overlay_text_color
        )
        self._sync_text_overlay_preview(ax, ay)
        self._update_text_overlay_controls()
        self._save_folder_settings()
        # Синхронизировать общий текст с нарезкой/склейкой.
        shared = self.text_overlay_edit.toPlainText()
        if hasattr(self, "_slice_tab"):
            self._slice_tab.set_text_overlay_text(shared)
        if hasattr(self, "_stitch_tab"):
            self._stitch_tab.set_text_overlay_text(shared)

    def _text_overlay_preview_videos(self) -> list[str]:
        """Выбранные исходники в том же порядке, что в списке файлов."""
        out: list[str] = []
        seen: set[str] = set()
        for p in self._selected_input_files or []:
            raw = str(p).strip()
            if not raw:
                continue
            try:
                path = Path(raw)
                if not path.is_file():
                    continue
                resolved = str(path.resolve())
                key = os.path.normcase(resolved)
            except OSError:
                if not Path(raw).is_file():
                    continue
                resolved = raw
                key = os.path.normcase(os.path.normpath(raw))
            if key in seen:
                continue
            seen.add(key)
            out.append(resolved)
        return out

    def _clamp_text_overlay_preview_index(self) -> None:
        videos = self._text_overlay_preview_videos()
        n = len(videos)
        if n <= 0:
            self._text_overlay_preview_index = 0
            return
        self._text_overlay_preview_index = int(self._text_overlay_preview_index) % n

    def _update_text_overlay_preview_nav(self) -> None:
        videos = self._text_overlay_preview_videos()
        n = len(videos)
        self._clamp_text_overlay_preview_index()
        idx = int(self._text_overlay_preview_index)
        can_cycle = n > 1
        if hasattr(self, "_btn_text_preview_prev"):
            self._btn_text_preview_prev.setEnabled(can_cycle)
            self._btn_text_preview_next.setEnabled(can_cycle)
            self._btn_text_preview_prev.setVisible(True)
            self._btn_text_preview_next.setVisible(True)
        if hasattr(self, "_text_overlay_preview_meta"):
            if n <= 0:
                self._text_overlay_preview_meta.setText(
                    "Нет исходников — выберите видео во вкладке «Исходники»"
                )
            else:
                name = Path(videos[idx]).name
                self._text_overlay_preview_meta.setText(f"{idx + 1} / {n} · {name}")

    def _text_overlay_preview_prev(self) -> None:
        videos = self._text_overlay_preview_videos()
        n = len(videos)
        if n <= 0:
            self._update_text_overlay_preview_nav()
            return
        if n == 1:
            self._text_overlay_preview_index = 0
            self._apply_text_overlay_preview_video(force=True)
            return
        self._text_overlay_preview_index = (int(self._text_overlay_preview_index) - 1) % n
        self._apply_text_overlay_preview_video(force=True)

    def _text_overlay_preview_next(self) -> None:
        videos = self._text_overlay_preview_videos()
        n = len(videos)
        if n <= 0:
            self._update_text_overlay_preview_nav()
            return
        if n == 1:
            self._text_overlay_preview_index = 0
            self._apply_text_overlay_preview_video(force=True)
            return
        self._text_overlay_preview_index = (int(self._text_overlay_preview_index) + 1) % n
        self._apply_text_overlay_preview_video(force=True)

    def _apply_text_overlay_preview_video(self, *, force: bool = False) -> None:
        """Показать кадр текущего исходника по индексу (стрелки листают файлы по кругу)."""
        if not hasattr(self, "text_overlay_preview"):
            return
        videos = self._text_overlay_preview_videos()
        self._update_text_overlay_preview_nav()
        current = None
        if videos:
            self._clamp_text_overlay_preview_index()
            current = videos[int(self._text_overlay_preview_index)]
        self.text_overlay_preview.set_background_video(current, force=force)
        overlay_on = bool(self.text_overlay_enabled.isChecked())
        self.text_overlay_preview.set_text_visible(overlay_on)

    def _sync_text_overlay_color_btn(self, btn: QPushButton, hex_color: str) -> None:
        c = QColor(hex_color)
        fg = "#0f1117" if c.lightness() > 140 else "#f8fafc"
        btn.setStyleSheet(
            f"background-color: {c.name()}; color: {fg}; font-weight: 700;"
        )

    def _sync_text_overlay_preview(
        self, anchor_x: float | None = None, anchor_y: float | None = None
    ) -> None:
        if not hasattr(self, "text_overlay_preview"):
            return
        self._apply_text_overlay_preview_video(force=False)
        preview = self.text_overlay_preview
        overlay_on = bool(self.text_overlay_enabled.isChecked())
        if not overlay_on:
            return
        orient = self.text_overlay_orientation.currentData()
        preview.blockSignals(True)
        preview.set_orientation(orient if isinstance(orient, str) else "vertical")
        preview.set_font_size(int(self.text_overlay_font_size.value()))
        preview.set_glow_enabled(bool(self.text_overlay_glow_enabled.isChecked()))
        preview.set_glow_color(self._text_overlay_glow_color)
        preview.set_text_color(self._text_overlay_text_color)
        preview.set_letter_spacing(int(self.text_overlay_letter_spacing.value()))
        preview.set_font_path(self._text_overlay_font_path)
        preview.set_font_bold(bool(self.text_overlay_font_bold.isChecked()))
        waf_lo, waf_hi, wfs_lo, wfs_hi = self._text_overlay_wave_values()
        preview.set_wave_settings((waf_lo + waf_hi) * 0.5, (wfs_lo + wfs_hi) * 0.5)
        preview.set_text(self.text_overlay_edit.toPlainText())
        if anchor_x is not None and anchor_y is not None:
            preview.set_anchor(anchor_x, anchor_y)
        preview.blockSignals(False)

    def _schedule_text_overlay_preview_sync(self) -> None:
        if not hasattr(self, "_text_overlay_preview_timer"):
            self._text_overlay_preview_timer = QTimer(self)
            self._text_overlay_preview_timer.setSingleShot(True)
            self._text_overlay_preview_timer.timeout.connect(self._sync_text_overlay_preview)
        self._text_overlay_preview_timer.start(40)

    def _update_text_overlay_controls(self, _checked: bool = False) -> None:
        if not hasattr(self, "text_overlay_enabled"):
            return
        on = bool(self.text_overlay_enabled.isChecked())
        self._text_overlay_panel.setVisible(on)
        self._text_overlay_panel.setEnabled(on)
        glow_on = bool(self.text_overlay_glow_enabled.isChecked())
        self.text_overlay_glow_btn.setEnabled(on and glow_on)
        if on:
            self._sync_text_overlay_preview()

    def _populate_text_overlay_font_combo(self) -> None:
        if not hasattr(self, "text_overlay_font_combo"):
            return
        combo = self.text_overlay_font_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("По умолчанию (Montserrat Bold)", "")
        for label, path in list_bundled_overlay_fonts():
            combo.addItem(label, path)
        custom = (self._text_overlay_font_path or "").strip()
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
            idx = combo.findData(self._text_overlay_font_path)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        if idx >= 0:
            data = combo.itemData(idx)
            self._text_overlay_font_path = str(data) if data else ""
        combo.blockSignals(False)

    def _on_text_overlay_glow_enabled_changed(self, _checked: bool) -> None:
        self._update_text_overlay_controls()
        self._sync_text_overlay_preview()
        self._save_folder_settings()

    def _on_text_overlay_letter_spacing_changed(self, _value: int) -> None:
        self._sync_text_overlay_preview()
        self._save_folder_settings()

    def _on_text_overlay_font_bold_changed(self, _checked: bool) -> None:
        self._sync_text_overlay_preview()
        self._save_folder_settings()

    def _on_text_overlay_font_changed(self, _index: int) -> None:
        data = self.text_overlay_font_combo.currentData()
        self._text_overlay_font_path = str(data) if data else ""
        self._sync_text_overlay_preview()
        self._save_folder_settings()

    def _pick_text_overlay_font_file(self) -> None:
        start = (
            str(Path(self._text_overlay_font_path).parent)
            if self._text_overlay_font_path
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
        self._text_overlay_font_path = path
        self._populate_text_overlay_font_combo()
        self._sync_text_overlay_preview()
        self._save_folder_settings()

    def _on_text_overlay_content_changed(self) -> None:
        self._schedule_text_overlay_preview_sync()
        if getattr(self, "_syncing_text_overlay", False):
            return
        self._save_folder_settings()
        text = self.text_overlay_edit.toPlainText() if hasattr(self, "text_overlay_edit") else ""
        self._on_shared_text_overlay_changed(text, source="uniquify")

    def _recent_text_overlay_texts(self) -> list[str]:
        store = getattr(self, "_upload_store", None)
        if store is None:
            return []
        try:
            return list(
                store.list_recent_text_overlay_texts(platform=self._platform) or []
            )
        except Exception:
            return []

    def refresh_text_overlay_recent(self) -> None:
        picker = getattr(self, "_text_overlay_recent_picker", None)
        if picker is not None:
            fill_recent_values_picker(picker, self._recent_text_overlay_texts())
        if hasattr(self, "_slice_tab"):
            self._slice_tab.refresh_text_overlay_recent()
        if hasattr(self, "_stitch_tab"):
            self._stitch_tab.refresh_text_overlay_recent()

    def _set_uniquify_text_overlay_text(self, text: str) -> None:
        if not hasattr(self, "text_overlay_edit"):
            return
        value = text if text is not None else ""
        if self.text_overlay_edit.toPlainText() == value:
            return
        self._syncing_text_overlay = True
        try:
            self.text_overlay_edit.setPlainText(value)
        finally:
            self._syncing_text_overlay = False

    def _on_shared_text_overlay_changed(
        self, text: str, *, source: str | None = None
    ) -> None:
        """Синхронизировать текст наложения между уникализацией / нарезкой / склейкой."""
        value = text if text is not None else ""
        try:
            self._settings.setValue("text_overlay_text", value)
        except Exception:
            pass
        if source != "uniquify":
            self._set_uniquify_text_overlay_text(value)
        if source != "slice" and hasattr(self, "_slice_tab"):
            self._slice_tab.set_text_overlay_text(value)
        if source != "stitch" and hasattr(self, "_stitch_tab"):
            self._stitch_tab.set_text_overlay_text(value)

    def _apply_ai_text_overlay(self, text: str) -> None:
        value = text if text is not None else ""
        self._set_uniquify_text_overlay_text(value)
        self._on_text_overlay_content_changed()

    def _remember_shared_text_overlay_text(self, text: str | None = None) -> None:
        value = text
        if value is None and hasattr(self, "text_overlay_edit"):
            value = self.text_overlay_edit.toPlainText()
        value = value if value is not None else ""
        if not str(value).strip():
            return
        try:
            self._upload_store.remember_text_overlay_text(
                value, platform=self._platform
            )
        except Exception:
            pass
        self.refresh_text_overlay_recent()

    def _on_text_overlay_font_size_changed(self, _value: int) -> None:
        self._schedule_text_overlay_preview_sync()
        self._save_folder_settings()

    def _on_text_overlay_orientation_changed(self, _index: int) -> None:
        self._sync_text_overlay_preview()
        self._save_folder_settings()

    def _center_text_overlay_horizontally(self) -> None:
        if not hasattr(self, "text_overlay_preview"):
            return
        _ax, ay = self.text_overlay_preview.anchor()
        self.text_overlay_preview.set_anchor(0.5, ay)
        self._save_folder_settings()

    def _center_text_overlay_vertically(self) -> None:
        if not hasattr(self, "text_overlay_preview"):
            return
        ax, _ay = self.text_overlay_preview.anchor()
        self.text_overlay_preview.set_anchor(ax, 0.5)
        self._save_folder_settings()

    def _on_text_overlay_position_changed(self, ax: float, ay: float) -> None:
        self._save_folder_settings()

    def _pick_text_overlay_glow_color(self) -> None:
        initial = QColor(self._text_overlay_glow_color)
        picked = QColorDialog.getColor(initial, self, "Цвет неонового свечения")
        if not picked.isValid():
            return
        self._text_overlay_glow_color = picked.name().upper()
        self._sync_text_overlay_color_btn(
            self.text_overlay_glow_btn, self._text_overlay_glow_color
        )
        self._sync_text_overlay_preview()
        self._save_folder_settings()

    def _pick_text_overlay_text_color(self) -> None:
        initial = QColor(self._text_overlay_text_color)
        picked = QColorDialog.getColor(initial, self, "Цвет текста")
        if not picked.isValid():
            return
        self._text_overlay_text_color = picked.name().upper()
        self._sync_text_overlay_color_btn(
            self.text_overlay_text_btn, self._text_overlay_text_color
        )
        self._sync_text_overlay_preview()
        self._save_folder_settings()

    def _on_fx_enable_toggled(self, _checked: bool = False) -> None:
        self._sync_fx_enable_slider_states()
        if getattr(self, "_fx_loading", False):
            return
        self._save_folder_settings()

    def _sync_fx_enable_slider_states(self) -> None:
        """Галочки всегда активны; слайдеры — по включению эффекта."""
        if not hasattr(self, "fx_brightness_enabled"):
            return
        pairs = [
            (self.fx_brightness_enabled, self.rb_brightness),
            (self.fx_contrast_enabled, self.rb_contrast),
            (self.fx_saturation_enabled, self.rb_saturation),
            (self.fx_scale_enabled, self.rb_scale_pct),
            (self.fx_noise_enabled, self.rb_noise),
            (self.audio_speed, self.audio_speed_range),
        ]
        for cb, w in pairs:
            w.setEnabled(bool(cb.isChecked()))
        for cb in getattr(self, "_fx_enable_checks", []):
            cb.setEnabled(True)

    def _processing_run_options(self, *, for_slicing: bool = False) -> dict:
        """Общие параметры обработки из раздела «Настройки»."""
        out: dict = {
            "num_workers": int(self.thread_slider.value()),
            "use_gpu": bool(self.use_gpu.isChecked()),
            "use_gpu_finalize": bool(self.use_gpu_finalize.isChecked()),
        }
        if for_slicing:
            out["slice_fps_mode"] = str(
                self.slice_fps_mode.currentData() or DEFAULT_SLICE_FPS_MODE
            )
        return out

    def _build_options(self) -> dict:
        bounds = RandomUniquifyBounds(
            brightness_min=float(self.rb_brightness.lowValue()),
            brightness_max=float(self.rb_brightness.highValue()),
            contrast_min=float(self.rb_contrast.lowValue()),
            contrast_max=float(self.rb_contrast.highValue()),
            saturation_min=float(self.rb_saturation.lowValue()),
            saturation_max=float(self.rb_saturation.highValue()),
            crop_jitter_min=0,
            crop_jitter_max=0,
            scale_pct_min=float(self.rb_scale_pct.lowValue()),
            scale_pct_max=float(self.rb_scale_pct.highValue()),
            noise_sigma_min=float(self.rb_noise.lowValue()),
            noise_sigma_max=float(self.rb_noise.highValue()),
            seed_min=0,
            seed_max=0,
            playback_speed_min=float(self.audio_speed_range.lowValue()),
            playback_speed_max=float(self.audio_speed_range.highValue()),
            audio_chorus_prob=0.0,
            audio_chorus_prob_min=0.0,
            audio_chorus_prob_max=0.0,
        ).to_dict()
        st = UniquifySettings(
            brightness_delta=float(self.rb_brightness.lowValue()),
            contrast=float(self.rb_contrast.lowValue()),
            saturation_scale=float(self.rb_saturation.lowValue()),
            crop_jitter_px=0,
            scale_pct=float(self.rb_scale_pct.lowValue()),
            noise_sigma=float(self.rb_noise.lowValue()),
            seed_base=0,
            playback_speed_factor=float(self.audio_speed_range.lowValue()),
            audio_chorus=False,
        )
        return {
            "input_dir": "",
            "output_dir": self.output_dir_edit.text().strip(),
            "input_files": list(self._selected_input_files),
            **self._processing_run_options(),
            "settings": st.to_dict(),
            "manual_bounds": bounds,
            "randomize_uniquify": True,
            "copies_per_file": int(self.copies_per_file.value()),
            "one_copy_no_effects": bool(self.one_copy_no_effects.isChecked()),
            "brightness_enabled": bool(self.fx_brightness_enabled.isChecked()),
            "contrast_enabled": bool(self.fx_contrast_enabled.isChecked()),
            "saturation_enabled": bool(self.fx_saturation_enabled.isChecked()),
            "crop_jitter_enabled": False,
            "scale_enabled": bool(self.fx_scale_enabled.isChecked()),
            "noise_enabled": bool(self.fx_noise_enabled.isChecked()),
            "seed_enabled": False,
            "playback_speed_enabled": bool(self.audio_speed.isChecked()),
            "audio_chorus_enabled": False,
            "background_music_enabled": bool(self.background_music.isChecked()),
            "background_music_mix_with_source": bool(self.background_music_mix.isChecked()),
            "background_music_volume_pct": int(
                round(self.background_music_volume.lowValue())
            ),
            "background_music_volume_pct_min": int(
                round(self.background_music_volume.lowValue())
            ),
            "background_music_volume_pct_max": int(
                round(self.background_music_volume.highValue())
            ),
            "background_music_files": [
                p for p in self._background_music_files if Path(p).is_file()
            ],
            "random_bounds": bounds,
            "text_overlay": self._text_overlay_options_dict(),
        }

    def _start(self) -> None:
        self._active_work_mode = "uniquify"
        self._save_folder_settings()
        if not self._prompt_stats_server_username_if_empty():
            return
        pending = self._prompt_title_desc_and_profile()
        if pending is None:
            return
        self._pending_upload = pending
        self._just_saved_outputs = []
        self._upload_streaming_active = False
        self._upload_streaming_title = ""
        self._upload_streaming_description = ""

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
        toc = opts.get("text_overlay") or {}
        if bool(toc.get("enabled")) and not str(toc.get("text") or "").strip():
            QMessageBox.warning(
                self,
                "Zaliver",
                "Включён текст на видео, но поле текста пустое.\n"
                "Введите текст или выключите опцию.",
            )
            return
        self._remember_shared_text_overlay_text(str(toc.get("text") or ""))
        if bool(opts.get("background_music_enabled")):
            if not (opts.get("background_music_files") or []):
                QMessageBox.warning(
                    self,
                    "Zaliver",
                    "Включена фоновая музыка, но список треков пуст или файлы недоступны.\n"
                    "Добавьте аудиофайлы или выключите опцию.",
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

        raw_prof = (pending.get("profile_ids") or "").strip()
        opts["youtube_upload_after_processing"] = bool(raw_prof)
        self._progress_hold_youtube = bool(raw_prof)
        self._upload_cancel_profile_ids = []
        self._upload_cancel_kind = ""
        self._upload_cancel_dolphin_token = ""
        self._upload_streaming_active = bool(
            raw_prof and pending.get("upload_as_ready")
        )
        if self._upload_streaming_active:
            opts["num_workers"] = 2

        # Upload session starts only on "Start".
        try:
            planned = len(list(opts.get("input_files") or [])) * max(
                1, int(opts.get("copies_per_file") or 1)
            )
        except Exception:
            planned = 0
        n_prof = self._streaming_upload_profile_count(pending)
        self._upload_streaming_min_ready = self._compute_streaming_upload_min_ready(
            profile_count=n_prof, planned=planned
        )
        if self._upload_streaming_active:
            opts["upload_ready_buffer_limit"] = int(self._upload_streaming_min_ready)
        try:
            self._upload_session = self._upload_store.start_session(
            planned_videos=planned, platform=self._platform
        )
        except Exception:
            self._upload_session = None
        self._upload_session_processing_done = False
        self._upload_session_upload_done = False
        self._upload_session_upload_expected = bool(raw_prof)

        self.log.clear()
        self.progress.setRange(0, 1)
        self.progress.setValueImmediate(0)
        self.progress_label.setText("Подготовка…")
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)

        if self._upload_streaming_active:
            min_ready = int(self._upload_streaming_min_ready)
            self._append_log(
                "Залив по мере готовности: обработка в 2 потока, "
                f"залив — после запаса {min_ready} готовых видео "
                f"({n_prof} профилей×2"
                + (
                    f", всего запланировано {planned}"
                    if planned > 0 and planned < n_prof * 2
                    else ""
                )
                + "). Буфер: максимум столько же сделанных, но ещё не залитых; "
                "после залива/удаления слот освобождается и обрабатывается следующее."
            )

        self._work_thread = QThread()
        self._processor = ProcessingController()
        self._processor.moveToThread(self._work_thread)
        self._work_thread.started.connect(partial(self._processor.run, opts))
        self._processor.progress.connect(self._on_progress)
        self._processor.finished.connect(self._on_finished)
        self._processor.output_saved.connect(self._on_output_saved)
        self._processor.log_line.connect(self._append_log)
        self._processor.finished.connect(self._work_thread.quit)
        self._processor.finished.connect(self._processor.deleteLater)
        self._work_thread.finished.connect(self._thread_cleanup)
        self._work_thread.start()

    def _pending_upload_for_slicing(self) -> dict[str, str] | None:
        """Окно названия/описания/профилей — как при уникализации (можно без залива)."""
        return self._prompt_title_desc_and_profile(mode="slicing")

    def _start_slicing(self) -> None:
        self._active_work_mode = "slicing"
        self._slice_tab.save_settings()
        if not self._prompt_stats_server_username_if_empty():
            return
        pending = self._pending_upload_for_slicing()
        if pending is None:
            return
        self._pending_upload = pending
        self._just_saved_outputs = []
        self._upload_streaming_active = False
        self._upload_streaming_title = ""
        self._upload_streaming_description = ""

        opts = self._slice_tab.build_options()
        opts.update(self._processing_run_options(for_slicing=True))
        if not opts["output_dir"]:
            QMessageBox.warning(self, "Zaliver", "Укажите выходную папку.")
            return
        if not opts.get("clip_files"):
            QMessageBox.warning(
                self,
                "Zaliver",
                "Выберите хотя бы один видеоклип (кнопка «Выбрать клипы…»).",
            )
            return
        if not opts.get("music_files"):
            QMessageBox.warning(
                self,
                "Zaliver",
                "Добавьте хотя бы один аудиотрек для нарезки.",
            )
            return
        toc = opts.get("text_overlay") or {}
        if bool(toc.get("enabled")) and not str(toc.get("text") or "").strip():
            QMessageBox.warning(
                self,
                "Zaliver",
                "Включён текст на видео, но поле текста пустое.\n"
                "Введите текст или выключите опцию.",
            )
            return
        self._remember_shared_text_overlay_text(str(toc.get("text") or ""))
        scene_err = self._slice_tab.validate_scene_options()
        if scene_err:
            QMessageBox.warning(self, "Zaliver", scene_err)
            return
        if self._work_thread and self._work_thread.isRunning():
            return

        raw_prof = (pending.get("profile_ids") or "").strip()
        opts["youtube_upload_after_processing"] = bool(raw_prof)
        self._progress_hold_youtube = bool(raw_prof)
        self._upload_cancel_profile_ids = []
        self._upload_cancel_kind = ""
        self._upload_cancel_dolphin_token = ""
        self._upload_streaming_active = bool(
            raw_prof and pending.get("upload_as_ready")
        )
        if self._upload_streaming_active:
            opts["num_workers"] = 2

        try:
            planned = len(list(opts.get("music_files") or [])) * max(
                1, int(opts.get("copies_per_track") or 1)
            )
        except Exception:
            planned = 0
        n_prof = self._streaming_upload_profile_count(pending)
        self._upload_streaming_min_ready = self._compute_streaming_upload_min_ready(
            profile_count=n_prof, planned=planned
        )
        if self._upload_streaming_active:
            opts["upload_ready_buffer_limit"] = int(self._upload_streaming_min_ready)
        try:
            self._upload_session = self._upload_store.start_session(
            planned_videos=planned, platform=self._platform
        )
        except Exception:
            self._upload_session = None
        self._upload_session_processing_done = False
        self._upload_session_upload_done = False
        self._upload_session_upload_expected = bool(raw_prof)

        self._slice_tab.log.clear()
        self._slice_tab.progress.setRange(0, 1)
        self._slice_tab.progress.setValueImmediate(0)
        self._slice_tab.progress_label.setText("Подготовка…")
        self._slice_tab.set_running(running=True)

        if self._upload_streaming_active:
            min_ready = int(self._upload_streaming_min_ready)
            self._append_slice_log(
                "Залив по мере готовности: нарезка в 2 потока, "
                f"залив — после запаса {min_ready} готовых видео "
                f"({n_prof} профилей×2"
                + (
                    f", всего запланировано {planned}"
                    if planned > 0 and planned < n_prof * 2
                    else ""
                )
                + "). Буфер: максимум столько же сделанных, но ещё не залитых; "
                "после залива/удаления слот освобождается и обрабатывается следующее."
            )

        self._work_thread = QThread()
        self._slice_processor = SlicingController()
        self._slice_processor.moveToThread(self._work_thread)
        self._work_thread.started.connect(partial(self._slice_processor.run, opts))
        self._slice_processor.progress.connect(self._on_slice_progress)
        self._slice_processor.finished.connect(self._on_finished)
        self._slice_processor.output_saved.connect(self._on_output_saved)
        self._slice_processor.log_line.connect(self._append_slice_log)
        self._slice_processor.finished.connect(self._work_thread.quit)
        self._slice_processor.finished.connect(self._slice_processor.deleteLater)
        self._work_thread.finished.connect(self._thread_cleanup)
        self._work_thread.start()

    def _pending_upload_for_stitching(self) -> dict[str, str] | None:
        return self._prompt_title_desc_and_profile(mode="stitching")

    def _start_stitching(self) -> None:
        self._active_work_mode = "stitching"
        self._stitch_tab.save_settings()
        if not self._prompt_stats_server_username_if_empty():
            return
        pending = self._pending_upload_for_stitching()
        if pending is None:
            return
        self._pending_upload = pending
        self._just_saved_outputs = []
        self._upload_streaming_active = False
        self._upload_streaming_title = ""
        self._upload_streaming_description = ""

        opts = self._stitch_tab.build_options()
        opts.update(self._processing_run_options(for_slicing=True))
        if not opts["output_dir"]:
            QMessageBox.warning(self, "Zaliver", "Укажите выходную папку.")
            return
        toc = opts.get("text_overlay") or {}
        if bool(toc.get("enabled")) and not str(toc.get("text") or "").strip():
            QMessageBox.warning(
                self,
                "Zaliver",
                "Включён текст на видео, но поле текста пустое.\n"
                "Введите текст или выключите опцию.",
            )
            return
        self._remember_shared_text_overlay_text(str(toc.get("text") or ""))
        part_err = self._stitch_tab.validate_part_options()
        if part_err:
            QMessageBox.warning(self, "Zaliver", part_err)
            return
        if self._work_thread and self._work_thread.isRunning():
            return

        raw_prof = (pending.get("profile_ids") or "").strip()
        opts["youtube_upload_after_processing"] = bool(raw_prof)
        self._progress_hold_youtube = bool(raw_prof)
        self._upload_cancel_profile_ids = []
        self._upload_cancel_kind = ""
        self._upload_cancel_dolphin_token = ""
        self._upload_streaming_active = bool(
            raw_prof and pending.get("upload_as_ready")
        )
        if self._upload_streaming_active:
            opts["num_workers"] = 2

        try:
            planned = max(1, int(opts.get("copies_per_track") or 1))
        except Exception:
            planned = 0
        n_prof = self._streaming_upload_profile_count(pending)
        self._upload_streaming_min_ready = self._compute_streaming_upload_min_ready(
            profile_count=n_prof, planned=planned
        )
        if self._upload_streaming_active:
            opts["upload_ready_buffer_limit"] = int(self._upload_streaming_min_ready)
        try:
            self._upload_session = self._upload_store.start_session(
                planned_videos=planned, platform=self._platform
            )
        except Exception:
            self._upload_session = None
        self._upload_session_processing_done = False
        self._upload_session_upload_done = False
        self._upload_session_upload_expected = bool(raw_prof)

        self._stitch_tab.log.clear()
        self._stitch_tab.progress.setRange(0, 1)
        self._stitch_tab.progress.setValueImmediate(0)
        self._stitch_tab.progress_label.setText("Подготовка…")
        self._stitch_tab.set_running(running=True)

        if self._upload_streaming_active:
            min_ready = int(self._upload_streaming_min_ready)
            self._append_stitch_log(
                "Залив по мере готовности: склейка в 2 потока, "
                f"залив — после запаса {min_ready} готовых видео "
                f"({n_prof} профилей×2"
                + (
                    f", всего запланировано {planned}"
                    if planned > 0 and planned < n_prof * 2
                    else ""
                )
                + "). Буфер: максимум столько же сделанных, но ещё не залитых; "
                "после залива/удаления слот освобождается и обрабатывается следующее."
            )

        self._work_thread = QThread()
        self._stitch_processor = StitchingController()
        self._stitch_processor.moveToThread(self._work_thread)
        self._work_thread.started.connect(partial(self._stitch_processor.run, opts))
        self._stitch_processor.progress.connect(self._on_stitch_progress)
        self._stitch_processor.finished.connect(self._on_finished)
        self._stitch_processor.output_saved.connect(self._on_output_saved)
        self._stitch_processor.log_line.connect(self._append_stitch_log)
        self._stitch_processor.finished.connect(self._work_thread.quit)
        self._stitch_processor.finished.connect(self._stitch_processor.deleteLater)
        self._work_thread.finished.connect(self._thread_cleanup)
        self._work_thread.start()

    def _thread_cleanup(self) -> None:
        self._work_thread = None
        self._processor = None
        self._slice_processor = None
        self._stitch_processor = None

    def _cancel(self) -> None:
        if self._processor is not None:
            self._processor.cancel()
        if self._slice_processor is not None:
            self._slice_processor.cancel()
        if self._stitch_processor is not None:
            self._stitch_processor.cancel()
        mgr = getattr(self, "_upload_manager", None)
        try:
            if mgr is not None:
                try:
                    self._ui_log_line.emit(
                        "[upload] Отмена: останавливаем очередь заливов "
                        "(локальный антик: HTTP stop_session для активных сессий; "
                        "Dolphin: stop_profile по списку профилей)."
                    )
                except Exception:
                    pass
                mgr.stop(reason="user")
        except Exception:
            pass
        self._stop_upload_antidetect_profiles()

    def _stop_upload_antidetect_profiles(self) -> None:
        kind_u = (getattr(self, "_upload_cancel_kind", "") or "").strip()
        ids = [p for p in getattr(self, "_upload_cancel_profile_ids", []) if str(p).strip()]
        try:
            from zaliver.antydetect.antic_open import close_instagram_keep_open_hub

            for pid in ids:
                try:
                    close_instagram_keep_open_hub(str(pid).strip())
                except Exception:
                    pass
        except Exception:
            pass
        if _is_own_antidetect_kind(kind_u):
            try:
                from zaliver.antydetect.local_active_sessions import (
                    stop_all_registered_local_sessions_sync,
                )

                for line in stop_all_registered_local_sessions_sync():
                    try:
                        self._ui_log_line.emit(line)
                    except Exception:
                        pass
            except Exception as e:
                try:
                    self._ui_log_line.emit(f"[upload] [STOP] local antidetect batch err={e!r}")
                except Exception:
                    pass
        elif ids:
            threading.Thread(target=self._stop_dolphin_profiles_for_cancel, daemon=True).start()

    def _stop_dolphin_profiles_for_cancel(self) -> None:
        token = (getattr(self, "_upload_cancel_dolphin_token", "") or "").strip()
        ids = [p.strip() for p in getattr(self, "_upload_cancel_profile_ids", []) if str(p).strip()]
        if not ids:
            return
        try:
            api = DolphinAntyLocalAPI()
            try:
                if token:
                    api.login_with_token(token)
                for pid in ids:
                    try:
                        api.stop_profile(pid)
                    except Exception as e:
                        try:
                            self._ui_log_line.emit(
                                f"[upload] [STOP] Dolphin stop_profile failed profile={pid!r} err={e!r}"
                            )
                        except Exception:
                            pass
            finally:
                api.close()
        except Exception as e:
            try:
                self._ui_log_line.emit(f"[upload] [STOP] Dolphin batch stop failed: {e!r}")
            except Exception:
                pass

    def _release_youtube_progress_hold_if_any(self) -> None:
        if not getattr(self, "_progress_hold_youtube", False):
            return
        self._progress_hold_youtube = False
        mx = max(1, int(self.progress.maximum()))
        self.progress.setRange(0, mx)
        self.progress.setValueImmediate(mx)

    def _finalize_idle_toolbar(self) -> None:
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        if hasattr(self, "_slice_tab"):
            self._slice_tab.set_idle()
        if hasattr(self, "_stitch_tab"):
            self._stitch_tab.set_idle()

    def _sync_toolbar_for_upload_phase(self) -> None:
        """Во время залива на YouTube: отмена доступна, старт выключен."""
        self.btn_cancel.setEnabled(True)
        self.btn_start.setEnabled(False)
        tab = self._montage_tab()
        if tab is not None:
            tab.set_busy()
            tab.progress_label.setText(self._brand("YouTube: загрузка…"))

    def _finish_montage_tab_after_upload(self, status: str) -> None:
        tab = self._montage_tab()
        if tab is None:
            return
        tab.set_idle()
        mx = max(1, int(tab.progress.maximum()))
        tab.progress.setRange(0, mx)
        tab.progress.setValueImmediate(mx)
        if status == "cancelled":
            tab.progress_label.setText(self._brand("Загрузка на YouTube отменена."))
        elif status == "timeout":
            tab.progress_label.setText(
                self._brand("Загрузка на YouTube остановлена по таймауту.")
            )
        elif status == "upload_failed":
            tab.progress_label.setText(
                self._brand("Готово (ошибки загрузки на YouTube).")
            )
        else:
            tab.progress_label.setText("Готово")

    def _finish_slice_tab_after_upload(self, status: str) -> None:
        self._finish_montage_tab_after_upload(status)

    def _on_slice_progress(self, cur: int, total: int, msg: str) -> None:
        if not hasattr(self, "_slice_tab"):
            return
        self._slice_tab.progress.setRange(0, max(1, total))
        self._slice_tab.progress.setValue(cur)
        if msg:
            self._slice_tab.progress_label.setText(msg)

    def _on_stitch_progress(self, cur: int, total: int, msg: str) -> None:
        if not hasattr(self, "_stitch_tab"):
            return
        self._stitch_tab.progress.setRange(0, max(1, total))
        self._stitch_tab.progress.setValue(cur)
        if msg:
            self._stitch_tab.progress_label.setText(msg)

    def _on_youtube_upload_phase_finished(self, status: str) -> None:
        upload_mode = (
            (getattr(self, "_upload_log_mode", "") or "").strip() or self._active_work_mode
        )
        self._upload_log_mode = ""
        self._upload_manager = None
        self._upload_streaming_active = False
        self._upload_streaming_title = ""
        self._upload_streaming_description = ""
        self._upload_streaming_min_ready = 1
        self._set_processing_upload_throttle(False)
        self._upload_cancel_profile_ids = []
        self._upload_cancel_kind = ""
        self._upload_cancel_dolphin_token = ""
        self._release_youtube_progress_hold_if_any()
        mx = max(1, int(self.progress.maximum()))
        self.progress.setRange(0, mx)
        self.progress.setValueImmediate(mx)
        self._finalize_idle_toolbar()
        self._finish_slice_tab_after_upload(status)
        if status == "cancelled":
            if self._is_montage_mode(upload_mode):
                self._append_montage_log(
                    "YouTube: загрузка отменена пользователем.", mode=upload_mode
                )
            else:
                self.progress_label.setText("Загрузка на YouTube отменена.")
                self._append_log("YouTube: загрузка отменена пользователем.")
            QMessageBox.information(self, "Zaliver", "Загрузка на YouTube отменена.")
        elif status == "timeout":
            timeout_msg = (
                "Очередь заливов не завершилась в отведённое время "
                "(долгие паузы между профилями или большая очередь). "
                "Часть видео могла не залиться — см. лог."
            )
            if self._is_montage_mode(upload_mode):
                self._append_montage_log(f"YouTube: {timeout_msg}", mode=upload_mode)
            else:
                self.progress_label.setText("Загрузка на YouTube остановлена по таймауту.")
                self._append_log(f"YouTube: {timeout_msg}")
            QMessageBox.warning(self, "Zaliver", timeout_msg)
        elif status == "upload_failed":
            if self._is_montage_mode(upload_mode):
                self._append_montage_log(
                    "YouTube: очередь завершена, залив не удался (см. лог выше).",
                    mode=upload_mode,
                )
            else:
                self.progress_label.setText("Готово (есть ошибки загрузки на YouTube).")
                self._append_log(
                    "YouTube: очередь завершена, часть загрузок завершилась с ошибками."
                )
        else:
            if self._is_montage_mode(upload_mode):
                self._append_montage_log(
                    "YouTube: очередь загрузок завершена.", mode=upload_mode
                )
            else:
                self.progress_label.setText("Готово")
                self._append_log("YouTube: очередь загрузок завершена.")

    def _on_progress(self, cur: int, total: int, msg: str) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(cur)
        if msg:
            self.progress_label.setText(msg)

    def _start_upload_queue_from_pending(
        self,
        pending: dict,
        video_paths: list[str],
        *,
        streaming: bool = False,
    ) -> bool:
        """Start MultiProfileUploader. Returns False if upload was skipped."""
        token = self._legacy_dolphin_token()
        kind = self._antidetect_kind()
        base_url = self._own_antidetect_base_url_from_settings(kind)

        try:
            remote_cdp = self._remote_cdp_launch_options_for_kind(kind)
        except LocalAntidetectError as e:
            self._append_session_log(
                self._brand(f"YouTube: заливка пропущена — {e}")
            )
            if not streaming:
                self._upload_log_mode = ""
                self._upload_session_upload_expected = False
                self._upload_session_upload_done = True
                self._maybe_finish_upload_session(status="upload_failed")
                self._release_youtube_progress_hold_if_any()
                self._finalize_idle_toolbar()
            QMessageBox.warning(self, "Zaliver", str(e))
            return False

        from zaliver.core import AntidetectLaunchConfig, build_upload_queue_request

        headless = True
        if hasattr(self, "_dolphin_headless"):
            headless = bool(self._dolphin_headless.isChecked())
        else:
            headless = bool(
                self._settings.value("antydetect/dolphin_headless", True, type=bool)
            )
        is_instagram_upload = self._platform == PLATFORM_INSTAGRAM
        is_yt_inst_upload = self._platform == PLATFORM_YT_INST
        # Пауза 0: keep-open для Instagram, Yt+Inst и YouTube.
        ig_keep_browser_open = (
            self._upload_pause_between_uploads().total_seconds() <= 0
        )
        upload_req = build_upload_queue_request(
            platform=self._platform,
            pending=pending,
            video_paths=video_paths,
            antidetect=AntidetectLaunchConfig(
                kind=kind,
                token=token,
                base_url=base_url or "",
                headless=headless,
                remote_cdp=remote_cdp,
            ),
            streaming=streaming,
            max_concurrent_browsers=int(self._max_concurrent_browsers()),
            instagram_tabs_per_profile=int(self._instagram_tabs_per_profile_value()),
            keep_browser_open=ig_keep_browser_open,
            delete_after_upload=self._delete_after_upload_enabled(),
        )
        profile_ids = list(upload_req.profile_ids)
        if not profile_ids:
            self._append_session_log(
                self._brand("YouTube: профили не выбраны — заливка пропущена.")
            )
            if not streaming:
                self._upload_log_mode = ""
                self._upload_session_upload_expected = False
                self._upload_session_upload_done = True
                self._maybe_finish_upload_session(status="done")
                self._release_youtube_progress_hold_if_any()
                self._finalize_idle_toolbar()
            return False

        from zaliver.youtube_upload.multi_uploader import (
            MultiProfileUploader,
            ScheduledUploadItem,
            VideoTask,
        )
        from zaliver.youtube_upload.studio import _studio_canonical_watch_url

        self._clear_previous_upload_result_tags(
            profile_ids=profile_ids,
            kind=kind,
            base_url=base_url,
            for_instagram=is_instagram_upload,
            for_both=is_yt_inst_upload,
        )

        if is_yt_inst_upload:
            upload_platform_label = "Yt+Inst"
        elif is_instagram_upload:
            upload_platform_label = "Instagram Reels"
        else:
            upload_platform_label = "YouTube"
        stream_note = " (по мере готовности)" if streaming else ""
        ids_preview = ",".join(profile_ids)
        self._append_session_log(
            f"{upload_platform_label}: многопоточная заливка стартует{stream_note}. "
            f"Видео={len(video_paths)}, профили={len(profile_ids)} [{ids_preview}]…"
        )
        if is_yt_inst_upload and ig_keep_browser_open:
            self._append_session_log(
                "Yt+Inst: пауза Instagram = 0 — браузер не закрывается, "
                "если следующий залив на тот же профиль."
            )
        elif (
            not is_instagram_upload
            and not is_yt_inst_upload
            and ig_keep_browser_open
        ):
            self._append_session_log(
                "YouTube: пауза = 0 — браузер не закрывается, "
                "если следующий залив на тот же профиль."
            )
        self._upload_delete_after_enabled = upload_req.delete_after_upload
        with self._upload_success_lock:
            self._upload_yt_inst_pending_delete.clear()
        self._sync_toolbar_for_upload_phase()
        self._upload_cancel_kind = (kind or "").strip()
        self._upload_cancel_dolphin_token = token
        self._upload_cancel_profile_ids = list(profile_ids)
        publish_before_checks = upload_req.publish_before_checks
        keep_studio_title = upload_req.keep_studio_title
        schedule_times = list(upload_req.schedule_times)
        schedule_batch_size = len(schedule_times)
        schedule_warmup_shorts = upload_req.schedule_warmup_shorts
        schedule_warmup_shorts_recommendations = (
            upload_req.schedule_warmup_shorts_recommendations
        )
        schedule_warmup_search_query = upload_req.schedule_warmup_search_query
        schedule_warmup_hashtag = upload_req.schedule_warmup_hashtag
        if is_instagram_upload and pending.get("schedule_publish"):
            self._append_session_log(
                "Instagram Reels: отложка Studio не поддерживается — публикуем сразу."
            )

        # Пауза 0 → режим keep_browser_open; решение «оставить/закрыть» — в менеджере.
        max_browsers = upload_req.max_concurrent_browsers
        ig_tabs_n = upload_req.instagram_tabs_per_profile
        ig_tabs_per_profile: dict[str, int] | None = None
        if (
            is_instagram_upload
            and ig_keep_browser_open
            and ig_tabs_n > 1
            and len(profile_ids) <= max_browsers
        ):
            ig_tabs_per_profile = compute_instagram_tabs_per_profile(
                profile_ids,
                ig_tabs_n,
                max_concurrent_browsers=max_browsers,
            )
            if max(ig_tabs_per_profile.values(), default=1) <= 1:
                ig_tabs_per_profile = None
            else:
                tabs_fmt = ", ".join(
                    f"{pid}×{n}" for pid, n in ig_tabs_per_profile.items()
                )
                total_slots = sum(ig_tabs_per_profile.values())
                self._append_session_log(
                    "Instagram Reels: multi-tab — пауза 0, "
                    f"вкладок на профиль={ig_tabs_n}, "
                    f"профилей ≤ лимита окон ({max_browsers}). "
                    f"Вкладки: {tabs_fmt} (всего слотов={total_slots})."
                )
        ig_crop_aspect = (
            self._instagram_crop_aspect_value()
            if (is_instagram_upload or is_yt_inst_upload)
            else DEFAULT_INSTAGRAM_CROP_ASPECT
        )
        if is_instagram_upload or is_yt_inst_upload:
            self._append_session_log(
                f"Instagram: обрезка при заливе — {ig_crop_aspect}."
            )
        mgr_holder: dict[str, MultiProfileUploader | None] = {"mgr": None}

        upload_var_index = {"n": 0}
        upload_var_index_lock = threading.Lock()

        def _next_upload_var_index() -> int:
            with upload_var_index_lock:
                upload_var_index["n"] += 1
                return upload_var_index["n"]

        def _profile_display_name(profile_id: str) -> str:
            for p in self._profiles_raw or []:
                if not isinstance(p, dict):
                    continue
                if _profile_id(p) == profile_id:
                    return _profile_name(p)
            return profile_id

        def _upload_one(profile_id: str, task: VideoTask, tab_index: int = 0) -> None:
            from zaliver.antydetect.antic_open import (
                open_google_in_local_antidetect_profile,
                open_google_in_profile,
                set_log_sink,
                upload_instagram_reel_in_local_antidetect_profile,
                upload_instagram_reel_in_profile,
                upload_youtube_and_instagram_in_local_antidetect_profile,
                upload_youtube_and_instagram_in_profile,
            )

            set_log_sink(self._ui_log_line.emit)
            headless = True
            if hasattr(self, "_dolphin_headless"):
                headless = bool(self._dolphin_headless.isChecked())
            else:
                headless = bool(
                    self._settings.value(
                        "antydetect/dolphin_headless", True, type=bool
                    )
                )

            guser = self._stats_server_username_stripped()
            var_ctx = TitleVariableContext(
                profile_name=_profile_display_name(profile_id),
                video_path=task.video_path,
                index=_next_upload_var_index(),
            )

            def _record_one(
                *,
                video_path: str,
                title: str,
                description: str,
                one_res,
                schedule_publish_at: datetime | None = None,
                record_platform: str | None = None,
            ) -> None:
                plat = (record_platform or self._platform or "").strip() or PLATFORM_YOUTUBE
                if plat == PLATFORM_YT_INST:
                    plat = PLATFORM_YOUTUBE
                is_ig_rec = plat == PLATFORM_INSTAGRAM
                vid = ""
                url = ""
                if isinstance(one_res, dict):
                    vid = str(one_res.get("video_id") or "").strip()
                    url = str(one_res.get("url") or "").strip()
                if not vid and url:
                    if is_ig_rec:
                        for marker in ("/reel/", "/p/"):
                            if marker in url:
                                part = url.split(marker, 1)[1]
                                vid = part.split("/", 1)[0].split("?", 1)[0].strip()
                                break
                    else:
                        try:
                            from zaliver.youtube_parsing.video_stats import (
                                extract_video_id,
                            )

                            vid = extract_video_id(url)
                        except Exception:
                            pass
                if not vid:
                    raise RuntimeError(f"Empty video_id (res={one_res!r})")
                if not url:
                    if is_ig_rec:
                        url = f"https://www.instagram.com/reel/{vid}/"
                    else:
                        url = _studio_canonical_watch_url(vid)
                if not url:
                    raise RuntimeError(f"Empty url (res={one_res!r})")

                sid = int(self._upload_session.id) if self._upload_session is not None else 0
                if sid <= 0:
                    raise RuntimeError("upload_session is not set (sid=0)")

                stored_title = title or ""
                if keep_studio_title and not stored_title and not is_ig_rec:
                    stored_title = Path(video_path).stem

                self._upload_store.add_uploaded_video(
                    session_id=sid,
                    title=stored_title,
                    description=description or "",
                    url=url,
                    video_id=vid,
                    profile_id=profile_id,
                    platform=plat,
                )
                try:
                    self._upload_store.inc_uploaded_ok(session_id=sid, delta=1)
                except Exception:
                    pass
                try:
                    stats_notified = bool(
                        isinstance(one_res, dict) and one_res.get("stats_notified")
                    )
                    if guser and not stats_notified:
                        scheduled_unix = None
                        if not is_ig_rec:
                            sched_dt = parse_msk_datetime(schedule_publish_at)
                            if sched_dt is not None:
                                scheduled_unix = int(sched_dt.timestamp())
                        ok = notify_uploaded_video(
                            video_id=vid,
                            username=guser,
                            profile_id=profile_id,
                            scheduled=scheduled_unix,
                            platform=plat,
                        )
                        try:
                            if ok:
                                self._ui_log_line.emit(
                                    f"[stats_server] уведомление отправлено: videoId={vid}"
                                )
                            else:
                                self._ui_log_line.emit(
                                    f"[stats_server] сервер не принял уведомление: videoId={vid}"
                                )
                        except Exception:
                            pass
                    elif not guser:
                        try:
                            self._ui_log_line.emit(
                                "[stats_server] username не задан — уведомление пропущено."
                            )
                        except Exception:
                            pass
                except Exception as e:
                    try:
                        self._ui_log_line.emit(
                            f"[stats_server] ошибка уведомления: {e!r}"
                        )
                    except Exception:
                        pass
                try:
                    QTimer.singleShot(0, self._refresh_uploaded_list)
                except Exception:
                    pass
                self._maybe_delete_output_after_upload_success(
                    video_path,
                    record_platform=plat,
                    yt_inst_upload=is_yt_inst_upload,
                )

            def _confirm_instagram_result(res, *, multi_tab: bool = False) -> dict | None:
                ig_vid = ""
                ig_url = ""
                candidates: list[dict] = []
                if isinstance(res, dict):
                    ig_vid = str(res.get("video_id") or "").strip()
                    ig_url = str(res.get("url") or "").strip()
                    raw_cands = res.get("candidate_reels")
                    if isinstance(raw_cands, list):
                        for item in raw_cands:
                            if not isinstance(item, dict):
                                continue
                            c_vid = str(item.get("video_id") or "").strip()
                            c_url = str(item.get("url") or "").strip()
                            if c_vid or c_url:
                                candidates.append(
                                    {"video_id": c_vid, "url": c_url}
                                )
                if not candidates and (ig_vid or ig_url):
                    candidates = [{"video_id": ig_vid, "url": ig_url}]

                chosen = None
                skipped: list[str] = []
                for cand in candidates:
                    c_vid = str(cand.get("video_id") or "").strip()
                    c_url = str(cand.get("url") or "").strip()
                    if self._upload_store.has_uploaded_video(
                        video_id=c_vid,
                        url=c_url,
                        platform=PLATFORM_INSTAGRAM,
                    ):
                        skipped.append(c_vid or c_url)
                        continue
                    chosen = cand
                    break
                if chosen is None:
                    detail = (
                        f"проверено={len(candidates)}, already={skipped!r}"
                        if multi_tab
                        else f"video_id={ig_vid!r}, url={ig_url!r}"
                    )
                    raise RuntimeError(
                        "Instagram Reels: "
                        + (
                            "все первые ролики в профиле уже есть в базе залитых"
                            if multi_tab and len(candidates) > 1
                            else "первое видео в профиле уже есть в базе залитых"
                        )
                        + f" ({detail}) — заливка не подтверждена."
                    )
                ig_vid = str(chosen.get("video_id") or "").strip()
                ig_url = str(chosen.get("url") or "").strip()
                if multi_tab and skipped:
                    try:
                        self._ui_log_line.emit(
                            "[upload] Instagram multi-tab: пропущены уже "
                            f"известные Reels {skipped!r}, берём "
                            f"video_id={ig_vid!r}"
                        )
                    except Exception:
                        pass
                if isinstance(res, dict):
                    out = dict(res)
                    out["video_id"] = ig_vid
                    out["url"] = ig_url
                    return out
                return {"video_id": ig_vid, "url": ig_url}

            if is_yt_inst_upload:
                creds = self._profile_login_credentials(profile_id)
                yt_oldest = self._profile_yt_oldest_name(profile_id) or None
                search_oldest = self._youtube_search_oldest_channel()
                sess_login, sess_pwd, sess_2fa = self._instagram_session_credentials(
                    profile_id
                )
                task_scheduled = (
                    task.schedule_publish_at is not None or task.scheduled_batch
                )
                title_result = expand_and_limit_title(task.title, var_ctx)
                resolved_title = title_result.title
                if title_result.truncated:
                    try:
                        self._ui_log_line.emit(
                            "[upload] Название обрезано до 100 символов "
                            f"(было {title_result.original_length})."
                        )
                    except Exception:
                        pass
                resolved_description = expand_title_variables(
                    task.description, var_ctx
                )
                resolved_scheduled_batch = None
                if task.scheduled_batch:
                    resolved_scheduled_batch = []
                    for item in task.scheduled_batch:
                        item_ctx = TitleVariableContext(
                            profile_name=_profile_display_name(profile_id),
                            video_path=item.video_path,
                            index=_next_upload_var_index(),
                        )
                        item_title_result = expand_and_limit_title(
                            item.title, item_ctx
                        )
                        if item_title_result.truncated:
                            try:
                                self._ui_log_line.emit(
                                    "[upload] Название обрезано до 100 символов "
                                    f"(было {item_title_result.original_length})."
                                )
                            except Exception:
                                pass
                        resolved_scheduled_batch.append(
                            ScheduledUploadItem(
                                video_path=item.video_path,
                                title=item_title_result.title,
                                description=expand_title_variables(
                                    item.description, item_ctx
                                ),
                                schedule_publish_at=item.schedule_publish_at,
                            )
                        )
                warmup_kw = {}
                if schedule_warmup_shorts and task_scheduled:
                    warmup_kw = dict(
                        warmup_during_schedule=True,
                        warmup_shorts_recommendations=schedule_warmup_shorts_recommendations,
                        warmup_search_query=schedule_warmup_search_query or None,
                        warmup_hashtag=schedule_warmup_hashtag or None,
                        warmup_shorts_batch_count=5,
                        warmup_like_probability_pct=10.0,
                        warmup_subscribe_probability_pct=10.0,
                        warmup_shorts_watch_min_s=5.0,
                        warmup_shorts_watch_max_s=25.0,
                    )
                keep_open = bool(ig_keep_browser_open) and (
                    (mgr_holder.get("mgr").should_keep_browser_open(profile_id))
                    if mgr_holder.get("mgr") is not None
                    else True
                )

                def _record_yt_inst_youtube(yt_part: dict) -> None:
                    batch_results = []
                    raw_batch = yt_part.get("batch_results")
                    if isinstance(raw_batch, list):
                        batch_results = raw_batch
                    if batch_results and task.scheduled_batch:
                        items_for_record = (
                            resolved_scheduled_batch or task.scheduled_batch
                        )
                        if len(batch_results) != len(items_for_record):
                            raise RuntimeError(
                                "scheduled_batch size mismatch: "
                                f"{len(batch_results)} results vs "
                                f"{len(items_for_record)} tasks"
                            )
                        for item, item_res in zip(items_for_record, batch_results):
                            _record_one(
                                video_path=item.video_path,
                                title=item.title,
                                description=item.description,
                                one_res=item_res,
                                schedule_publish_at=item.schedule_publish_at,
                                record_platform=PLATFORM_YOUTUBE,
                            )
                    else:
                        _record_one(
                            video_path=task.video_path,
                            title=resolved_title,
                            description=resolved_description,
                            one_res=yt_part,
                            schedule_publish_at=task.schedule_publish_at,
                            record_platform=PLATFORM_YOUTUBE,
                        )
                    try:
                        self._set_previous_upload_result_tag(
                            profile_id=profile_id,
                            success=True,
                            kind=kind,
                            base_url=base_url,
                            for_instagram=False,
                        )
                    except Exception:
                        pass
                    try:
                        self._ui_log_line.emit(
                            "[upload] Yt+Inst: YouTube сохранён в залитые "
                            "(уведомление отправлено)."
                        )
                    except Exception:
                        pass

                def _record_yt_inst_instagram(ig_part) -> None:
                    try:
                        ig_batch = []
                        if isinstance(ig_part, dict):
                            raw_ig_batch = ig_part.get("batch_results")
                            if isinstance(raw_ig_batch, list):
                                ig_batch = raw_ig_batch
                        if ig_batch and task.scheduled_batch:
                            items_for_record = (
                                resolved_scheduled_batch or task.scheduled_batch
                            )
                            for item, item_res in zip(items_for_record, ig_batch):
                                confirmed = _confirm_instagram_result(item_res)
                                _record_one(
                                    video_path=item.video_path,
                                    title=item.title,
                                    description=item.description,
                                    one_res=confirmed,
                                    record_platform=PLATFORM_INSTAGRAM,
                                )
                        else:
                            confirmed = _confirm_instagram_result(ig_part)
                            _record_one(
                                video_path=task.video_path,
                                title=resolved_title,
                                description=resolved_description,
                                one_res=confirmed,
                                record_platform=PLATFORM_INSTAGRAM,
                            )
                        try:
                            self._set_previous_upload_result_tag(
                                profile_id=profile_id,
                                success=True,
                                kind=kind,
                                base_url=base_url,
                                for_instagram=True,
                            )
                        except Exception:
                            pass
                        try:
                            self._ui_log_line.emit(
                                "[upload] Yt+Inst: Instagram сохранён в залитые."
                            )
                        except Exception:
                            pass
                    except Exception as e:
                        try:
                            self._ui_log_line.emit(
                                f"[upload] Yt+Inst: запись Instagram не удалась: {e!r}"
                            )
                        except Exception:
                            pass
                        try:
                            self._set_previous_upload_result_tag(
                                profile_id=profile_id,
                                success=False,
                                kind=kind,
                                base_url=base_url,
                                for_instagram=True,
                            )
                        except Exception:
                            pass

                def _on_yt_inst_ig_error(err: BaseException) -> None:
                    try:
                        self._ui_log_line.emit(
                            f"[upload] Yt+Inst: Instagram ошибка (pipeline) — "
                            f"{type(err).__name__}: {err}"
                        )
                    except Exception:
                        pass
                    try:
                        self._set_previous_upload_result_tag(
                            profile_id=profile_id,
                            success=False,
                            kind=kind,
                            base_url=base_url,
                            for_instagram=True,
                        )
                    except Exception:
                        pass
                    # YouTube уже залит — файл больше не нужен Instagram.
                    paths_to_drop = [str(task.video_path or "").strip()]
                    if task.scheduled_batch:
                        for item in task.scheduled_batch:
                            paths_to_drop.append(
                                str(getattr(item, "video_path", "") or "").strip()
                            )
                    self._delete_yt_inst_pending_outputs(paths_to_drop)

                combined_kw = dict(
                    headless=headless,
                    video_path=task.video_path,
                    title=resolved_title,
                    description=resolved_description,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    search_oldest_channel=search_oldest,
                    publish_before_checks=publish_before_checks,
                    keep_studio_title=keep_studio_title,
                    schedule_publish_at=task.schedule_publish_at,
                    scheduled_batch=resolved_scheduled_batch,
                    stats_server_username=guser or None,
                    session_login=sess_login,
                    session_password=sess_pwd,
                    session_twofa=sess_2fa,
                    keep_browser_open=keep_open,
                    on_youtube_success=_record_yt_inst_youtube,
                    on_instagram_success=_record_yt_inst_instagram,
                    on_instagram_error=_on_yt_inst_ig_error,
                    crop_aspect=ig_crop_aspect,
                    **warmup_kw,
                )
                if _is_own_antidetect_kind(kind):
                    res = upload_youtube_and_instagram_in_local_antidetect_profile(
                        profile_id,
                        base_url=(base_url or "").strip(),
                        remote_cdp=remote_cdp,
                        **combined_kw,
                    )
                else:
                    res = upload_youtube_and_instagram_in_profile(
                        profile_id,
                        local_token=token or None,
                        **combined_kw,
                    )

                yt_part = res.get("youtube") if isinstance(res, dict) else None
                ig_part = res.get("instagram") if isinstance(res, dict) else None
                ig_pending = bool(
                    isinstance(res, dict) and res.get("instagram_pending")
                )
                yt_err_s = (
                    str(res.get("youtube_error") or "").strip()
                    if isinstance(res, dict)
                    else ""
                )
                ig_err_s = (
                    str(res.get("instagram_error") or "").strip()
                    if isinstance(res, dict)
                    else ""
                )

                yt_ok = isinstance(yt_part, dict)
                ig_ok = isinstance(ig_part, dict) or (
                    # Уже записан через on_instagram_success в pipeline
                    # при wait_for_instagram — ig_part в res; при pending — ещё нет.
                    False
                )
                # YouTube уже записан в on_youtube_success; IG — в callback или ниже.
                if isinstance(ig_part, dict) and not ig_pending:
                    # Двойная запись не нужна, если callback уже сработал при wait.
                    # При wait_for_instagram callback уже вызван из pipeline —
                    # ig_ok отмечаем по наличию результата без повторной записи.
                    ig_ok = True

                if not yt_ok:
                    try:
                        self._set_previous_upload_result_tag(
                            profile_id=profile_id,
                            success=False,
                            kind=kind,
                            base_url=base_url,
                            for_instagram=False,
                        )
                    except Exception:
                        pass

                if not yt_ok and not ig_ok and not ig_pending:
                    parts = []
                    if yt_err_s:
                        parts.append(f"YouTube: {yt_err_s}")
                    if ig_err_s:
                        parts.append(f"Instagram: {ig_err_s}")
                    detail = "; ".join(parts) if parts else "нет результата"
                    raise RuntimeError(f"Yt+Inst: обе площадки не залиты ({detail})")
                if yt_ok and ig_pending:
                    try:
                        self._ui_log_line.emit(
                            "[upload] Yt+Inst: YouTube OK — следующее видео "
                            "можно брать из очереди; Instagram догоняет в pipeline."
                        )
                    except Exception:
                        pass
                if yt_ok and not ig_ok and ig_err_s and not ig_pending:
                    try:
                        self._ui_log_line.emit(
                            f"[upload] Yt+Inst: YouTube OK, Instagram ошибка — {ig_err_s}"
                        )
                    except Exception:
                        pass
                if ig_ok and not yt_ok and yt_err_s:
                    try:
                        self._ui_log_line.emit(
                            f"[upload] Yt+Inst: Instagram OK, YouTube ошибка — {yt_err_s}"
                        )
                    except Exception:
                        pass
                return

            if is_instagram_upload:
                # Подпись Reels длиннее лимита названия Studio — только expand.
                resolved_title = expand_title_variables(task.title, var_ctx)
                resolved_description = expand_title_variables(
                    task.description, var_ctx
                )
                sess_login, sess_pwd, sess_2fa = self._instagram_session_credentials(
                    profile_id
                )
                mgr_now = mgr_holder.get("mgr")
                multi_tab = bool(
                    mgr_now is not None and getattr(mgr_now, "multi_tab_mode", False)
                )
                # Multi-tab: браузер всегда keep-open (закрытие только через менеджер),
                # иначе параллельная вкладка могла бы stop_profile чужому заливу.
                # tab0 = уже открытая вкладка Instagram; остальные — new_page().
                if multi_tab:
                    keep_open = True
                    dedicated_tab = int(tab_index) > 0
                    top_reels_scan = 5
                    tabs_n = int(
                        mgr_now.tabs_for_profile(profile_id)
                        if mgr_now is not None
                        else 1
                    )
                else:
                    keep_open = bool(ig_keep_browser_open) and (
                        mgr_now.should_keep_browser_open(profile_id)
                        if mgr_now is not None
                        else True
                    )
                    dedicated_tab = False
                    top_reels_scan = 1
                    tabs_n = 1
                ig_kw = dict(
                    video_path=task.video_path,
                    title=resolved_title,
                    description=resolved_description,
                    headless=headless,
                    session_login=sess_login,
                    session_password=sess_pwd,
                    session_twofa=sess_2fa,
                    keep_browser_open=keep_open,
                    dedicated_tab=dedicated_tab,
                    top_reels_scan=top_reels_scan,
                    tab_index=int(tab_index),
                    tabs_per_profile=max(1, tabs_n),
                    crop_aspect=ig_crop_aspect,
                )
                if _is_own_antidetect_kind(kind):
                    res = upload_instagram_reel_in_local_antidetect_profile(
                        profile_id,
                        base_url=(base_url or "").strip(),
                        remote_cdp=remote_cdp,
                        **ig_kw,
                    )
                else:
                    res = upload_instagram_reel_in_profile(
                        profile_id,
                        local_token=token or None,
                        **ig_kw,
                    )
                # После залива: первое Reel в сетке. В multi-tab параллельные
                # вкладки могут уже записать соседние ролики — берём первый
                # из топ-5, которого ещё нет в базе.
                res = _confirm_instagram_result(res, multi_tab=multi_tab)
                resolved_scheduled_batch = None
            else:
                creds = self._profile_login_credentials(profile_id)
                yt_oldest = self._profile_yt_oldest_name(profile_id) or None
                search_oldest = self._youtube_search_oldest_channel()
                task_scheduled = (
                    task.schedule_publish_at is not None or task.scheduled_batch
                )
                title_result = expand_and_limit_title(task.title, var_ctx)
                resolved_title = title_result.title
                if title_result.truncated:
                    try:
                        self._ui_log_line.emit(
                            "[upload] Название обрезано до 100 символов "
                            f"(было {title_result.original_length})."
                        )
                    except Exception:
                        pass
                resolved_description = expand_title_variables(
                    task.description, var_ctx
                )
                resolved_scheduled_batch = None
                if task.scheduled_batch:
                    resolved_scheduled_batch = []
                    for item in task.scheduled_batch:
                        item_ctx = TitleVariableContext(
                            profile_name=_profile_display_name(profile_id),
                            video_path=item.video_path,
                            index=_next_upload_var_index(),
                        )
                        item_title_result = expand_and_limit_title(
                            item.title, item_ctx
                        )
                        if item_title_result.truncated:
                            try:
                                self._ui_log_line.emit(
                                    "[upload] Название обрезано до 100 символов "
                                    f"(было {item_title_result.original_length})."
                                )
                            except Exception:
                                pass
                        resolved_scheduled_batch.append(
                            ScheduledUploadItem(
                                video_path=item.video_path,
                                title=item_title_result.title,
                                description=expand_title_variables(
                                    item.description, item_ctx
                                ),
                                schedule_publish_at=item.schedule_publish_at,
                            )
                        )
                warmup_kw = {}
                if schedule_warmup_shorts and task_scheduled:
                    warmup_kw = dict(
                        warmup_during_schedule=True,
                        warmup_shorts_recommendations=schedule_warmup_shorts_recommendations,
                        warmup_search_query=schedule_warmup_search_query or None,
                        warmup_hashtag=schedule_warmup_hashtag or None,
                        warmup_shorts_batch_count=5,
                        warmup_like_probability_pct=10.0,
                        warmup_subscribe_probability_pct=10.0,
                        warmup_shorts_watch_min_s=5.0,
                        warmup_shorts_watch_max_s=25.0,
                    )
                mgr_now = mgr_holder.get("mgr")
                keep_open = bool(ig_keep_browser_open) and (
                    mgr_now.should_keep_browser_open(profile_id)
                    if mgr_now is not None
                    else True
                )
                open_kw = dict(
                    headless=headless,
                    video_path=task.video_path,
                    title=resolved_title,
                    description=resolved_description,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    search_oldest_channel=search_oldest,
                    publish_before_checks=publish_before_checks,
                    keep_studio_title=keep_studio_title,
                    schedule_publish_at=task.schedule_publish_at,
                    scheduled_batch=resolved_scheduled_batch,
                    stats_server_username=guser or None,
                    keep_browser_open=keep_open,
                    **warmup_kw,
                )
                if _is_own_antidetect_kind(kind):
                    res = open_google_in_local_antidetect_profile(
                        profile_id,
                        base_url=(base_url or "").strip(),
                        remote_cdp=remote_cdp,
                        **open_kw,
                    )
                else:
                    res = open_google_in_profile(
                        profile_id,
                        local_token=token or None,
                        **open_kw,
                    )

            batch_results = []
            if isinstance(res, dict):
                raw_batch = res.get("batch_results")
                if isinstance(raw_batch, list):
                    batch_results = raw_batch

            if batch_results and task.scheduled_batch:
                if len(batch_results) != len(task.scheduled_batch):
                    raise RuntimeError(
                        "scheduled_batch size mismatch: "
                        f"{len(batch_results)} results vs "
                        f"{len(task.scheduled_batch)} tasks"
                    )
                items_for_record = resolved_scheduled_batch or task.scheduled_batch
                for item, item_res in zip(items_for_record, batch_results):
                    _record_one(
                        video_path=item.video_path,
                        title=item.title,
                        description=item.description,
                        one_res=item_res,
                        schedule_publish_at=item.schedule_publish_at,
                    )
            else:
                _record_one(
                    video_path=task.video_path,
                    title=resolved_title,
                    description=resolved_description,
                    one_res=res,
                    schedule_publish_at=task.schedule_publish_at,
                )

        def _on_profile_upload_attempt(pid: str, ok: bool, err: str) -> None:
            if not is_yt_inst_upload:
                try:
                    self._set_previous_upload_result_tag(
                        profile_id=pid,
                        success=bool(ok),
                        kind=kind,
                        base_url=base_url,
                        for_instagram=is_instagram_upload,
                    )
                except Exception:
                    pass
            if ok:
                self._upload_store.reset_profile_upload_errors(profile_id=pid)
                return
            n = self._upload_store.inc_profile_upload_error(
                profile_id=pid, error_text=err
            )
            if n >= 3 and not self._upload_store.is_profile_upload_error_flagged(
                profile_id=pid
            ):
                self._on_upload_profile_failed_3x(
                    profile_id=pid,
                    n=n,
                    error_text=err,
                    kind=kind,
                    base_url=base_url,
                )

        def _close_kept_upload_browser(pid: str) -> None:
            """Освободить keep-open браузер профиля (лимит параллельных)."""
            pid = (pid or "").strip()
            if not pid:
                return
            try:
                from zaliver.antydetect.antic_open import close_instagram_keep_open_hub

                close_instagram_keep_open_hub(pid)
            except Exception:
                pass
            if _is_own_antidetect_kind(kind):
                try:
                    from zaliver.antydetect.local_active_sessions import (
                        stop_registered_local_session_sync,
                    )

                    for line in stop_registered_local_session_sync(pid):
                        try:
                            self._ui_log_line.emit(line)
                        except Exception:
                            pass
                except Exception as e:
                    try:
                        self._ui_log_line.emit(
                            f"[upload] [STOP] local keep-open close "
                            f"profile={pid!r} err={e!r}"
                        )
                    except Exception:
                        pass
                return
            try:
                from zaliver.antydetect.antic_open import clear_dolphin_keep_open_cdp

                clear_dolphin_keep_open_cdp(pid)
            except Exception:
                pass
            try:
                api = DolphinAntyLocalAPI()
                try:
                    tok = (token or "").strip()
                    if tok:
                        api.login_with_token(tok)
                    api.stop_profile(pid)
                    try:
                        self._ui_log_line.emit(
                            f"[upload] [STOP] Dolphin stop_profile ok "
                            f"profile={pid!r} (keep-open slot)"
                        )
                    except Exception:
                        pass
                finally:
                    api.close()
            except Exception as e:
                try:
                    self._ui_log_line.emit(
                        f"[upload] [STOP] Dolphin keep-open close "
                        f"profile={pid!r} err={e!r}"
                    )
                except Exception:
                    pass

        mgr = MultiProfileUploader(
            profile_ids=profile_ids,
            cooldown_s=10.0,
            max_attempts_per_profile=2,
            max_concurrent_uploads=max_browsers,
            profile_upload_pause_remaining_s=lambda pid: self._upload_store.profile_upload_pause_remaining_seconds(
                pid,
                platform=self._platform,
                pause=self._upload_pause_between_uploads(),
            ),
            recent_batch_wait_s=float(
                self._upload_pause_between_uploads().total_seconds()
            ),
            keep_browser_open=ig_keep_browser_open,
            close_kept_browser=(
                _close_kept_upload_browser if ig_keep_browser_open else None
            ),
            log_sink=self._ui_log_line.emit,
            upload_one=_upload_one,
            on_profile_attempt=_on_profile_upload_attempt,
            on_video_done=lambda path, ok: (
                None if ok else self._release_ready_buffer_slot(path)
            ),
            schedule_batch_size=schedule_batch_size,
            schedule_times=schedule_times,
            await_more_videos=bool(streaming),
            tabs_per_profile=ig_tabs_per_profile,
        )
        mgr_holder["mgr"] = mgr
        self._upload_manager = mgr
        upload_title = pending.get("title", "Название")
        if pending.get("keep_studio_title"):
            upload_title = ""
        self._upload_streaming_title = upload_title
        self._upload_streaming_description = pending.get("description", "") or ""
        mgr.enqueue_videos(
            video_paths=video_paths,
            title=upload_title,
            description=pending.get("description", ""),
        )

        def _run_mgr() -> None:
            try:
                mgr.start()
                while not mgr.is_finished() and not mgr.stop_requested():
                    time.sleep(0.5)
                try:
                    mgr.join(timeout_s=120.0)
                except Exception:
                    pass
            finally:
                self._upload_session_upload_done = True
                stopped = False
                stop_reason = ""
                try:
                    stopped = bool(mgr.stop_requested())
                    stop_reason = str(mgr.stop_reason or "")
                except Exception:
                    stopped = False
                    stop_reason = ""
                status = "done"
                if stopped:
                    status = "timeout" if stop_reason == "watchdog" else "cancelled"
                else:
                    try:
                        if mgr.done_failed > 0:
                            status = "upload_failed"
                    except Exception:
                        status = "upload_failed"
                try:
                    self._ui_log_line.emit(
                        f"[upload] Очередь завершена: status={status}, "
                        f"ok={mgr.done_ok}, failed={mgr.done_failed}"
                    )
                except Exception:
                    pass
                try:
                    # Закрыть браузеры, оставленные открытыми при паузе 0.
                    self._stop_upload_antidetect_profiles()
                except Exception:
                    pass
                # Страховка: Yt+Inst успел YouTube, а Instagram так и не закрыл файл.
                with self._upload_success_lock:
                    leftover = list(self._upload_yt_inst_pending_delete)
                    self._upload_yt_inst_pending_delete.clear()
                if getattr(self, "_upload_delete_after_enabled", False):
                    for video_path in leftover:
                        self._delete_output_video_after_upload(video_path)
                else:
                    for video_path in leftover:
                        self._release_ready_buffer_slot(video_path)
                self._maybe_finish_upload_session(status=status)
                try:
                    self._youtube_upload_phase_finished.emit(status)
                except Exception:
                    pass

        threading.Thread(target=_run_mgr, daemon=True).start()
        return True

    def _on_finished(self, ok: bool, msg: str) -> None:
        self._upload_session_processing_done = True

        if not ok:
            self._upload_streaming_active = False
            self._set_processing_upload_throttle(False)
            mgr = getattr(self, "_upload_manager", None)
            if mgr is not None:
                try:
                    if msg != "Отменено.":
                        mgr.stop(reason="processing_error")
                except Exception:
                    pass
                try:
                    mgr.mark_producer_done()
                except Exception:
                    pass
                err_line = f"Ошибка: {msg}"
                if self._is_montage_mode():
                    self._append_montage_log(err_line)
                else:
                    self._append_log(err_line)
                if msg and msg != "Отменено.":
                    QMessageBox.critical(self, "Zaliver", msg)
                elif msg == "Отменено.":
                    QMessageBox.information(self, "Zaliver", "Обработка отменена.")
                # Сессию/тулбар закроет _run_mgr → _on_youtube_upload_phase_finished
                return

            self._finalize_idle_toolbar()
            self._release_youtube_progress_hold_if_any()
            err_line = f"Ошибка: {msg}"
            if self._is_montage_mode():
                self._append_montage_log(err_line)
            else:
                self._append_log(err_line)
            if msg and msg != "Отменено.":
                self._upload_session_upload_expected = False
                self._upload_session_upload_done = True
                self._maybe_finish_upload_session(status="error")
                QMessageBox.critical(self, "Zaliver", msg)
            elif msg == "Отменено.":
                self._upload_session_upload_expected = False
                self._upload_session_upload_done = True
                self._maybe_finish_upload_session(status="cancelled")
                QMessageBox.information(self, "Zaliver", "Обработка отменена.")
            return

        if self._active_work_mode == "slicing":
            self._append_slice_log("Нарезка завершена.")
        elif self._active_work_mode == "stitching":
            self._append_stitch_log("Склейка завершена.")
        else:
            self._append_log("Уникализация завершена.")

        # Режим «по мере готовности»: залив уже идёт — только закрываем producer.
        self._upload_streaming_active = False
        self._set_processing_upload_throttle(False)
        mgr = getattr(self, "_upload_manager", None)
        if mgr is not None:
            self._pending_upload = None
            try:
                mgr.mark_producer_done()
            except Exception:
                pass
            self._append_session_log(
                "Обработка завершена — очередь залива продолжается."
            )
            return

        pending = self._pending_upload
        self._pending_upload = None
        if pending is not None:
            self._upload_log_mode = self._active_work_mode
            video_paths = [
                p.strip()
                for p in (self._just_saved_outputs or [])
                if isinstance(p, str) and p.strip()
            ]
            if not video_paths:
                self._append_session_log(
                    self._brand(
                        "Загрузка в YouTube пропущена: не найден путь к сохранённому видео."
                    )
                )
                self._upload_log_mode = ""
                self._upload_session_upload_expected = False
                self._upload_session_upload_done = True
                self._maybe_finish_upload_session(status="done")
                self._release_youtube_progress_hold_if_any()
                self._finalize_idle_toolbar()
                return

            if not self._start_upload_queue_from_pending(
                pending, video_paths, streaming=False
            ):
                return
            return

        self._upload_session_upload_expected = False
        self._upload_session_upload_done = True
        self._maybe_finish_upload_session(status="done")
        self._release_youtube_progress_hold_if_any()
        self._finalize_idle_toolbar()

    def _maybe_finish_upload_session(self, *, status: str) -> None:
        s = self._upload_session
        if s is None:
            return
        if not self._upload_session_processing_done:
            return
        if self._upload_session_upload_expected and not self._upload_session_upload_done:
            return
        try:
            self._upload_store.finish_session(session_id=int(s.id), status=status)
        except Exception:
            pass

    def _bootstrap_fd_limits(self) -> None:
        from zaliver.processing.fd_limit import bootstrap_fd_limits

        msg = bootstrap_fd_limits()
        _apply_thread_slider_fd_cap(self.thread_slider)
        if msg:
            self._append_log(msg)

    def _append_log(self, line: str) -> None:
        from zaliver.log_format import format_log_line

        self.log.appendPlainText(format_log_line(self._brand(line)))
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _append_slice_log(self, line: str) -> None:
        from zaliver.log_format import format_log_line

        if not hasattr(self, "_slice_tab"):
            return
        self._slice_tab.log.appendPlainText(format_log_line(self._brand(line)))
        bar = self._slice_tab.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _append_stitch_log(self, line: str) -> None:
        from zaliver.log_format import format_log_line

        if not hasattr(self, "_stitch_tab"):
            return
        self._stitch_tab.log.appendPlainText(format_log_line(self._brand(line)))
        bar = self._stitch_tab.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _append_montage_log(self, line: str, *, mode: str | None = None) -> None:
        m = (mode or getattr(self, "_active_work_mode", "") or "").strip()
        if m == "stitching":
            self._append_stitch_log(line)
        else:
            self._append_slice_log(line)

    def _append_session_log(self, line: str) -> None:
        """Лог текущей сессии залива (нарезка/склейка или уникализация)."""
        mode = (getattr(self, "_upload_log_mode", "") or "").strip()
        if self._is_montage_mode(mode):
            self._append_montage_log(line, mode=mode)
        else:
            self._append_log(line)

    def _route_ui_log_line(self, line: str) -> None:
        """Служебные логи (upload, studio, теги) — в лог той вкладки, откуда запущен залив."""
        mode = (getattr(self, "_upload_log_mode", "") or "").strip()
        if self._is_montage_mode(mode):
            self._append_montage_log(line, mode=mode)
        else:
            self._append_log(line)

    def _refresh_profiles_list_after_zaliver_tags(self) -> None:
        """Перезагрузить профили с API после смены служебных тегов (свой антидетект)."""
        kind = self._antidetect_kind()
        if _is_own_antidetect_kind((kind or "").strip() if isinstance(kind, str) else ""):
            self._refresh_antydetect_profiles()

    def _apply_zaliver_profile_tags_from_worker(
        self,
        *,
        profile_id: str,
        kind: str,
        base_url: str,
        updates: list[tuple[bool, str, str]],
        log_prefix: str,
    ) -> None:
        """Записать теги в API и обновить список профилей в UI (из фонового потока)."""
        from zaliver.antydetect.profile_tags import (
            apply_mutually_exclusive_profile_tag,
            cross_platform_tags_to_strip,
        )

        pid = (profile_id or "").strip()
        if not pid or not updates:
            return
        base_u = (base_url or "").strip() or DEFAULT_LOCAL_API_BASE_URL
        try:
            api = LocalAntidetectHttpAPI(base_u)
            try:
                for success, success_tag, error_tag in updates:
                    apply_mutually_exclusive_profile_tag(
                        api,
                        pid,
                        success=success,
                        success_tag=success_tag,
                        error_tag=error_tag,
                    )
                    tag = success_tag if success else error_tag
                    self._ui_log_line.emit(
                        f"[{log_prefix}] profile={pid} tag_set={tag!r}"
                    )
            finally:
                api.close()
            payload = [
                {
                    "success": success,
                    "success_tag": success_tag,
                    "error_tag": error_tag,
                    "strip_tags": list(
                        cross_platform_tags_to_strip(success_tag, error_tag)
                    ),
                }
                for success, success_tag, error_tag in updates
            ]
            self._profile_zaliver_tags_cache_update.emit(pid, payload)
        except Exception as te:
            self._ui_log_line.emit(
                f"[{log_prefix}] profile={pid} tag_set_failed err={te!r}"
            )

    def _on_profile_zaliver_tags_cache_update(
        self, profile_id: str, updates_obj: object
    ) -> None:
        pid = (profile_id or "").strip()
        if not pid or self._profiles_raw is None:
            return
        if not isinstance(updates_obj, list):
            return
        pairs: list[tuple[bool, str, str]] = []
        extra_strip: set[str] = set()
        for item in updates_obj:
            if not isinstance(item, dict):
                continue
            success_tag = str(item.get("success_tag") or "").strip()
            error_tag = str(item.get("error_tag") or "").strip()
            if not success_tag or not error_tag:
                continue
            pairs.append((bool(item.get("success")), success_tag, error_tag))
            raw_strip = item.get("strip_tags")
            if isinstance(raw_strip, (list, tuple, set)):
                for t in raw_strip:
                    s = str(t or "").strip()
                    if s:
                        extra_strip.add(s)
        if not pairs:
            return
        strip_tags = {t for _ok, st, et in pairs for t in (st, et)} | extra_strip
        for i, p in enumerate(self._profiles_raw):
            if _profile_id(p) != pid:
                continue
            merged = dict(p)
            tags_raw = merged.get("tags")
            tags: list[str] = []
            if isinstance(tags_raw, list):
                for t in tags_raw:
                    if isinstance(t, str) and t.strip():
                        tags.append(t.strip())
            tags = [t for t in tags if t not in strip_tags]
            for success, success_tag, error_tag in pairs:
                tags.append(success_tag if success else error_tag)
            merged["tags"] = tags
            self._profiles_raw[i] = merged
            break
        self._refresh_profiles_list_view()

    def _local_antidetect_api_for_profile_tags(
        self, *, kind: str, base_url: str
    ) -> LocalAntidetectHttpAPI | None:
        if not _is_own_antidetect_kind(kind):
            return None
        u = (base_url or "").strip()
        if not u:
            if (kind or "").strip() == "local":
                u = DEFAULT_LOCAL_API_BASE_URL
            else:
                return None
        return LocalAntidetectHttpAPI(u)

    def _clear_previous_upload_result_tags(
        self,
        *,
        profile_ids: list[str],
        kind: str,
        base_url: str,
        for_instagram: bool = False,
        for_both: bool = False,
    ) -> None:
        from zaliver.antydetect.profile_tags import (
            IG_UPLOAD_PREVIOUS_ERROR_TAG,
            IG_UPLOAD_PREVIOUS_SUCCESS_TAG,
            UPLOAD_PREVIOUS_ERROR_TAG,
            UPLOAD_PREVIOUS_SUCCESS_TAG,
        )

        pids = [p.strip() for p in (profile_ids or []) if (p or "").strip()]
        if not pids:
            return
        api = self._local_antidetect_api_for_profile_tags(kind=kind, base_url=base_url)
        if api is None:
            self._ui_log_line.emit(
                "[upload] Сброс тегов прошлого залива доступен только "
                "для своего антидетекта."
            )
            return
        # Снимаем и актуальные, и старые имена (без суффикса платформы).
        tags_to_clear = [
            "УСПЕШНЫЙ ПРОШЛЫЙ ЗАЛИВ",
            "ОШИБКА ПРОШЛОГО ЗАЛИВА",
        ]
        if for_both or for_instagram:
            tags_to_clear.extend(
                [
                    IG_UPLOAD_PREVIOUS_SUCCESS_TAG,
                    IG_UPLOAD_PREVIOUS_ERROR_TAG,
                ]
            )
        if for_both or not for_instagram:
            tags_to_clear.extend(
                [
                    UPLOAD_PREVIOUS_SUCCESS_TAG,
                    UPLOAD_PREVIOUS_ERROR_TAG,
                ]
            )
        try:
            for pid in pids:
                for tag in tags_to_clear:
                    try:
                        api.remove_profile_tag(pid, tag)
                        self._ui_log_line.emit(
                            f"[upload] profile={pid} tag_removed={tag!r}"
                        )
                    except Exception:
                        pass
        finally:
            api.close()

    def _exclude_profile_from_current_upload_session(
        self, profile_id: str, *, reason: str = ""
    ) -> None:
        mgr = getattr(self, "_upload_manager", None)
        exclude = getattr(mgr, "exclude_profile_this_session", None)
        if not callable(exclude):
            return
        try:
            exclude(profile_id, reason=reason)
        except Exception:
            pass

    def _set_previous_upload_result_tag(
        self,
        *,
        profile_id: str,
        success: bool,
        kind: str,
        base_url: str,
        for_instagram: bool = False,
    ) -> None:
        from zaliver.antydetect.profile_tags import (
            IG_UPLOAD_PREVIOUS_ERROR_TAG,
            IG_UPLOAD_PREVIOUS_SUCCESS_TAG,
            UPLOAD_PREVIOUS_ERROR_TAG,
            UPLOAD_PREVIOUS_SUCCESS_TAG,
        )

        pid = (profile_id or "").strip()
        if not pid:
            return
        if for_instagram:
            success_tag = IG_UPLOAD_PREVIOUS_SUCCESS_TAG
            error_tag = IG_UPLOAD_PREVIOUS_ERROR_TAG
        else:
            success_tag = UPLOAD_PREVIOUS_SUCCESS_TAG
            error_tag = UPLOAD_PREVIOUS_ERROR_TAG
        if not success:
            self._exclude_profile_from_current_upload_session(
                pid, reason=error_tag
            )
        tag = success_tag if success else error_tag
        other = error_tag if success else success_tag
        api = self._local_antidetect_api_for_profile_tags(kind=kind, base_url=base_url)
        if api is None:
            self._ui_log_line.emit(
                f"[upload] profile={pid}: тег {tag!r} доступен только "
                "для своего антидетекта."
            )
            return
        try:
            try:
                try:
                    api.remove_profile_tag(pid, other)
                except Exception:
                    pass
                api.add_profile_tag(pid, tag)
            finally:
                api.close()
            self._ui_log_line.emit(f"[upload] profile={pid} tag_added={tag!r}")
            self._profile_zaliver_tags_cache_update.emit(
                pid,
                [
                    {
                        "success": success,
                        "success_tag": success_tag,
                        "error_tag": error_tag,
                    }
                ],
            )
        except Exception as e:
            self._ui_log_line.emit(
                f"[upload] profile={pid} tag_add_failed tag={tag!r} err={e!r}"
            )

    def _on_upload_profile_failed_3x(
        self,
        *,
        profile_id: str,
        n: int,
        error_text: str,
        kind: str,
        base_url: str,
    ) -> None:
        from zaliver.antydetect.profile_tags import UPLOAD_ERROR_3X_TAG

        pid = (profile_id or "").strip()
        if not pid:
            return
        try:
            self._upload_store.flag_profile_after_upload_errors(
                profile_id=pid, flagged=True, error_text=error_text
            )
        except Exception:
            pass

        self._ui_log_line.emit(
            f"[upload] [PROFILE] profile={pid} consecutive_errors={int(n)} → flagged"
        )

        # Если используем свой антидетект — помечаем профиль тегом в его «базе» (profiles.json).
        if _is_own_antidetect_kind((kind or "").strip()):
            try:
                api = self._local_antidetect_api_for_profile_tags(
                    kind=kind, base_url=base_url
                )
                if api is None:
                    return
                try:
                    api.add_profile_tag(pid, UPLOAD_ERROR_3X_TAG)
                finally:
                    api.close()
                self._ui_log_line.emit(
                    f"[upload] [PROFILE] profile={pid} tag_added={UPLOAD_ERROR_3X_TAG!r}"
                )
            except Exception as e:
                self._ui_log_line.emit(
                    f"[upload] [PROFILE] profile={pid} tag_add_failed err={e!r}"
                )
