"""Main application window."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from collections.abc import Callable
from typing import NamedTuple
from functools import partial
from pathlib import Path

from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPointF,
    QSettings,
    QSize,
    QThread,
    QTimer,
    Qt,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QDesktopServices, QMouseEvent, QPixmap, QShowEvent, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
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
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from zaliver.db.video_store import VideoStore
from zaliver.db.upload_store import UploadedVideo, UploadStore, uploaded_at_sort_ts
from zaliver.antydetect.api import DolphinAntyError, DolphinAntyLocalAPI, DolphinAntyPublicAPI
from zaliver.antydetect.local_antidetect_api import (
    DEFAULT_LOCAL_API_BASE_URL,
    LocalAntidetectError,
    LocalAntidetectHttpAPI,
    RemoteCdpLaunchOptions,
    normalize_local_profile_for_ui,
)
from zaliver.processing.pipeline import RandomUniquifyBounds, UniquifySettings
from zaliver.processing.text_overlay import (
    NEON_WAVE_AMP_FRAC,
    NEON_WAVE_CHAR_PHASE,
    NEON_WAVE_FRAME_SPEED,
    TextOverlaySettings,
    list_bundled_overlay_fonts,
)
from zaliver.processing.thread_worker import ProcessingController
from zaliver.processing.slicing_worker import SlicingController
from zaliver.ui.antic_profile_row import _profile_id, _profile_name
from zaliver.ui.profile_list_helpers import (
    profile_matches_search,
    profile_search_rank,
    profile_search_tokens,
)
from zaliver.ui.profile_account_data_dialog import (
    ProfileAccountDataDialog,
    YT_LOGIN_KEY,
)
from zaliver.ui.profile_accounts_import_dialog import ProfileAccountsImportDialog
from zaliver.ui.profile_avatars_import_dialog import ProfileAvatarsImportDialog
from zaliver.ui.profile_preview_dialog import ProfileCdpPreviewDialog
from zaliver.ui.profiles_list_interaction import ProfilesListInteraction
from zaliver.ui.ffmpeg_install_worker import FfmpegInstallWorker
from zaliver.stats_server_client import notify_uploaded_video
from zaliver.ui.uploaded_stats_refresh_worker import UploadedStatsRefreshWorker
from zaliver.ui.widgets import (
    AnimatedProgressBar,
    CollapsibleSection,
    SmoothSlider,
    ToggleSwitch,
    configure_log_splitter,
    make_log_export_button,
)
from zaliver.ui.text_overlay_preview import TextOverlayPreviewWidget
from zaliver.ui.slicing_tab_pane import SlicingTabPane

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


class ShortsWarmupSettings(NamedTuple):
    shorts_count: int
    like_probability_pct: float
    subscribe_probability_pct: float
    shorts_watch_min_s: int
    shorts_watch_max_s: int
    watch_horizontal_videos: bool
    horizontal_search_query: str
    horizontal_videos_count: int


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
    _studio_channel_fill_progress = pyqtSignal(int, int, str)
    _studio_channel_fill_finished = pyqtSignal(int, int)
    _studio_avatar_upload_progress = pyqtSignal(int, int, str)
    _studio_avatar_upload_finished = pyqtSignal(int, int)
    _studio_warmup_progress = pyqtSignal(int, int, str)
    _studio_warmup_finished = pyqtSignal(int, int)
    _zaliver_profile_tags_clear_progress = pyqtSignal(int, int, str)
    _zaliver_profile_tags_clear_finished = pyqtSignal(int, int)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Zaliver — уникализация видео")
        self.setObjectName("zaliverRoot")
        self._work_thread: QThread | None = None
        self._processor: ProcessingController | None = None
        self._slice_processor: SlicingController | None = None
        self._active_work_mode = "uniquify"
        self._ff_thread: QThread | None = None
        self._ff_worker: FfmpegInstallWorker | None = None
        self._ffmpeg_progress_dlg: QProgressDialog | None = None
        self._stats_thread: QThread | None = None
        self._stats_worker: UploadedStatsRefreshWorker | None = None
        self._stats_progress_dlg: QProgressDialog | None = None
        self._selected_input_files: list[str] = []
        self._background_music_files: list[str] = []
        self._video_store = VideoStore()
        self._upload_store = UploadStore(db_path=self._video_store.db_path)
        self._upload_session = None
        self._upload_session_processing_done = False
        self._upload_session_upload_done = False
        self._upload_session_upload_expected = False

        self._settings = QSettings("Zaliver", "Zaliver")
        self._profiles_raw: list[dict[str, object]] | None = None
        self._profiles_list_render_gen: int = 0
        self._profiles_interaction: ProfilesListInteraction | None = None
        self._profile_cdp_previews: dict[str, ProfileCdpPreviewDialog] = {}
        self._profiles_filter_timer = QTimer(self)
        self._profiles_filter_timer.setSingleShot(True)
        self._profiles_filter_timer.timeout.connect(self._apply_profiles_filter)
        self._profiles_availability_running = False
        self._profiles_channel_fill_running = False
        self._profiles_avatar_upload_running = False
        self._profiles_warmup_running = False
        self._profiles_tags_clear_running = False
        self._profiles_refresh_running = False
        self._last_availability_failed_ids: list[str] = []
        self._last_channel_fill_failed_ids: list[str] = []
        self._last_avatar_upload_failed_ids: list[str] = []
        self._last_warmup_failed_ids: list[str] = []
        self._build_ui()
        self._bootstrap_fd_limits()
        self._ui_log_line.connect(self._route_ui_log_line)
        self._profiles_loaded.connect(self._on_profiles_loaded)
        self._profiles_load_failed.connect(self._on_profiles_load_failed)
        self._dolphin_google_ready.connect(self._on_dolphin_google_ready)
        self._dolphin_google_failed.connect(self._on_dolphin_google_failed)
        self._after_video_saved.connect(self._refresh_ready_list)
        self._apply_theme()
        self.showMaximized()
        self._load_folder_settings()
        self._load_antydetect_settings()
        self._load_youtube_settings()
        self._update_profiles_section_header()
        self._sync_ffmpeg_install_row()
        self._pending_upload: dict[str, str] | None = None
        self._just_saved_outputs: list[str] = []
        self._upload_manager = None
        self._progress_hold_youtube = False
        self._upload_cancel_kind = ""
        self._upload_cancel_dolphin_token = ""
        self._upload_cancel_profile_ids: list[str] = []
        self._upload_log_mode = ""
        self._youtube_upload_phase_finished.connect(self._on_youtube_upload_phase_finished)
        self._studio_availability_progress.connect(self._on_studio_availability_progress)
        self._studio_availability_finished.connect(self._on_studio_availability_finished)
        self._studio_channel_fill_progress.connect(self._on_studio_channel_fill_progress)
        self._studio_channel_fill_finished.connect(self._on_studio_channel_fill_finished)
        self._studio_avatar_upload_progress.connect(self._on_studio_avatar_upload_progress)
        self._studio_avatar_upload_finished.connect(self._on_studio_avatar_upload_finished)
        self._studio_warmup_progress.connect(self._on_studio_warmup_progress)
        self._studio_warmup_finished.connect(self._on_studio_warmup_finished)
        self._zaliver_profile_tags_clear_progress.connect(
            self._on_zaliver_profile_tags_clear_progress
        )
        self._zaliver_profile_tags_clear_finished.connect(
            self._on_zaliver_profile_tags_clear_finished
        )
        # Автозагрузка профилей при запуске (асинхронно).
        QTimer.singleShot(0, self._refresh_antydetect_profiles)

    def _theme_path(self) -> Path:
        return Path(__file__).with_name("theme.qss")

    def _apply_theme(self) -> None:
        p = self._theme_path()
        if p.is_file():
            self.setStyleSheet(p.read_text(encoding="utf-8"))

    def _build_ui(self) -> None:
        home = QWidget()
        home_l = QVBoxLayout(home)
        home_l.setSpacing(12)
        home_l.setContentsMargins(12, 8, 12, 12)

        title = QLabel("Zaliver")
        title.setObjectName("title")
        sub = QLabel("Выбор видео → папка результатов · случайная уникализация ")
        sub.setObjectName("hint")

        self.btn_start = QPushButton("Старт")
        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setEnabled(False)
        self.btn_start.clicked.connect(self._start)
        self.btn_cancel.clicked.connect(self._cancel)

        self.progress = AnimatedProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValueImmediate(0)
        self.progress.setMinimumWidth(160)
        self.progress.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("hint")

        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        header_row.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(self.progress, 1, Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(self.btn_start, 0, Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(self.btn_cancel, 0, Qt.AlignmentFlag.AlignVCenter)
        home_l.addLayout(header_row)
        home_l.addWidget(self.progress_label)
        home_l.addWidget(sub)

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
        self.one_copy_no_effects = QCheckBox("1 копия без эффектов")
        self.one_copy_no_effects.setChecked(False)
        self.one_copy_no_effects.setToolTip(
            "Первая копия каждого исходника без уникализации: "
            "яркость, контраст, шум и прочие эффекты не применяются; "
            "добавляются только фоновый трек и текст на видео."
        )
        io_grid.addWidget(QLabel("Копий на исходник:"), 2, 0)
        io_grid.addWidget(self.copies_per_file, 2, 1)
        io_grid.addWidget(self.one_copy_no_effects, 2, 2)
        copies_hint = QLabel(
            "Каждая копия — отдельный прогон со своими случайными параметрами "
            "(при включённой случайной уникализации). Например: 10 видео × 5 = 50 файлов."
        )
        copies_hint.setObjectName("hint")
        copies_hint.setWordWrap(True)
        io_grid.addWidget(copies_hint, 3, 0, 1, 3)
        io_hint = QLabel(
            "Имена: имя_u_<случайные hex>.mp4 — у каждого выхода свой суффикс (не счётчик)."
        )
        io_hint.setObjectName("hint")
        io_hint.setWordWrap(True)
        io_grid.addWidget(io_hint, 4, 0, 1, 3)
        self.delete_after_upload = QCheckBox("Удалять после залива")
        self.delete_after_upload.setChecked(False)
        self.delete_after_upload.setToolTip(
            "После успешной загрузки на YouTube файл удаляется из выходной папки."
        )
        self.delete_after_upload.toggled.connect(self._save_folder_settings)
        io_grid.addWidget(self.delete_after_upload, 5, 0, 1, 3)

        bg_tracks = QGroupBox("Фоновые треки")
        bg_tracks_l = QVBoxLayout(bg_tracks)
        bg_tracks_l.setSpacing(8)
        self.background_music = ToggleSwitch(
            "Случайный трек и отрезок для каждого выхода (по списку ниже)"
        )
        self.background_music.setChecked(False)
        bg_tracks_l.addWidget(self.background_music)
        music_btns = QHBoxLayout()
        self.btn_add_music = QPushButton("Добавить треки…")
        self.btn_add_music.setObjectName("secondary")
        self.btn_add_music.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_add_music.clicked.connect(self._browse_background_music)
        self.btn_remove_music = QPushButton("Удалить выбранные")
        self.btn_remove_music.setObjectName("secondary")
        self.btn_remove_music.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_remove_music.clicked.connect(self._remove_selected_music)
        music_btns.addWidget(self.btn_add_music)
        music_btns.addWidget(self.btn_remove_music)
        music_btns.addStretch()
        mw_music = QWidget()
        mw_music.setLayout(music_btns)
        bg_tracks_l.addWidget(mw_music)
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
        bg_tracks_l.addWidget(self._music_list)
        self._music_hint = QLabel()
        self._music_hint.setObjectName("hint")
        self._music_hint.setWordWrap(True)
        bg_tracks_l.addWidget(self._music_hint)
        self.background_music_mix = ToggleSwitch(
            "Смешивать с аудио исходника (иначе — полная замена дорожки)"
        )
        self.background_music_mix.setChecked(False)
        self.background_music_mix.toggled.connect(self._update_music_mix_controls)
        self.background_music_mix.toggled.connect(self._save_folder_settings)
        bg_tracks_l.addWidget(self.background_music_mix)
        self.background_music_volume = SmoothSlider(Qt.Orientation.Horizontal)
        self.background_music_volume.setMinimum(0)
        self.background_music_volume.setMaximum(100)
        self.background_music_volume.setValue(35)
        self.background_music_volume.setSingleStep(1)
        self.background_music_volume.setPageStep(5)
        self.background_music_volume.setToolTip(
            "Громкость слоя музыки при смешивании (0…100 %). Звук видео не ослабляется."
        )
        self.background_music_volume_label = QLabel("35 %")
        self.background_music_volume_label.setObjectName("hint")
        self.background_music_volume.valueChanged.connect(self._on_music_volume_slider_changed)
        vol_row = QHBoxLayout()
        vol_row.setContentsMargins(0, 0, 0, 0)
        vol_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        vol_lbl = QLabel("Громкость музыки:")
        vol_row.addWidget(vol_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        vol_row.addWidget(self.background_music_volume, 1, Qt.AlignmentFlag.AlignVCenter)
        vol_row.addWidget(self.background_music_volume_label, 0, Qt.AlignmentFlag.AlignVCenter)
        vw_vol = QWidget()
        vw_vol.setLayout(vol_row)
        bg_tracks_l.addWidget(vw_vol)
        self.background_music.toggled.connect(self._update_music_mix_controls)
        self.background_music.toggled.connect(self._save_folder_settings)
        self._update_music_mix_controls()
        self._sync_music_list_widget()

        proc = QGroupBox("Обработка")
        pg = QGridLayout(proc)
        self.thread_slider = SmoothSlider(Qt.Orientation.Horizontal)
        self.thread_slider.setMinimum(1)
        # Максимум слайдера — число доступных логических потоков CPU.
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

        self.use_gpu = ToggleSwitch("GPU при обработке кадров (декод, фильтры, кодирование)")
        self.use_gpu.setChecked(
            bool(self._settings.value("use_gpu_enabled", False, type=bool))
        )
        self.use_gpu.toggled.connect(self._save_folder_settings)
        self.use_gpu_finalize = ToggleSwitch(
            "GPU при склейке и mux звука (concat, ускорение, фон)"
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
        pg.addWidget(self.use_gpu, 2, 0, 1, 2)
        pg.addWidget(self.use_gpu_finalize, 3, 0, 1, 2)
        pg.addWidget(gpu_hint, 4, 0, 1, 2)

        pg.addWidget(QLabel("Потоков процессов:"), 5, 0, Qt.AlignmentFlag.AlignVCenter)
        thr_row = QHBoxLayout()
        thr_row.setSpacing(8)
        thr_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        thr_row.addWidget(self.thread_slider, 1, Qt.AlignmentFlag.AlignVCenter)
        thr_row.addWidget(self.thread_label, 0, Qt.AlignmentFlag.AlignVCenter)
        w_thr = QWidget()
        w_thr.setLayout(thr_row)
        pg.addWidget(w_thr, 5, 1, Qt.AlignmentFlag.AlignVCenter)

        fx = QGroupBox("Уникализация (лёгкие эффекты)")
        fx_layout = QVBoxLayout(fx)
        fx_layout.setSpacing(8)

        self._text_overlay_section = CollapsibleSection("Текст на видео (неон)")
        text_inner = QWidget()
        text_l = QVBoxLayout(text_inner)
        text_l.setSpacing(8)
        self.text_overlay_enabled = ToggleSwitch("Накладывать свой текст на каждое видео")
        self.text_overlay_enabled.setChecked(True)
        self.text_overlay_enabled.toggled.connect(self._update_text_overlay_controls)
        self.text_overlay_enabled.toggled.connect(self._save_folder_settings)
        text_l.addWidget(self.text_overlay_enabled)

        self._text_overlay_panel = QWidget()
        text_controls_l = QVBoxLayout(self._text_overlay_panel)
        text_controls_l.setContentsMargins(0, 0, 0, 0)
        text_controls_l.setSpacing(8)

        self.text_overlay_edit = QPlainTextEdit()
        self.text_overlay_edit.setPlaceholderText("Текст для наложения…")
        self.text_overlay_edit.setPlainText("GAME IN BIO")
        self.text_overlay_edit.setMaximumHeight(72)
        self.text_overlay_edit.textChanged.connect(self._on_text_overlay_content_changed)
        text_controls_l.addWidget(self.text_overlay_edit)

        self.text_overlay_from_middle = QCheckBox(
            "Текст с середины видео до конца"
        )
        self.text_overlay_from_middle.setChecked(True)
        self.text_overlay_from_middle.toggled.connect(self._save_folder_settings)
        text_controls_l.addWidget(self.text_overlay_from_middle)

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
        text_opts.addWidget(QLabel("Размер шрифта (на примере):"), 0, 0)
        text_opts.addWidget(self.text_overlay_font_size, 0, 1)
        text_opts.addWidget(QLabel("Пример кадра:"), 1, 0)
        text_opts.addWidget(self.text_overlay_orientation, 1, 1)
        text_opts.addWidget(QLabel("Свечение:"), 2, 0)
        text_opts.addWidget(glow_row_w, 2, 1)
        text_opts.addWidget(QLabel("Текст:"), 3, 0)
        text_opts.addWidget(self.text_overlay_text_btn, 3, 1)
        text_opts.addWidget(QLabel("Межбуквенный интервал:"), 4, 0)
        text_opts.addWidget(self.text_overlay_letter_spacing, 4, 1)
        text_opts.addWidget(QLabel("Шрифт:"), 5, 0)
        text_opts.addWidget(font_row_w, 5, 1)

        self.text_overlay_wave_amp = SmoothSlider(Qt.Orientation.Horizontal)
        self.text_overlay_wave_amp.setMinimum(0)
        self.text_overlay_wave_amp.setMaximum(35)
        self.text_overlay_wave_amp.setValue(int(round(NEON_WAVE_AMP_FRAC * 100)))
        self.text_overlay_wave_amp.valueChanged.connect(self._on_text_overlay_wave_changed)
        self.text_overlay_wave_amp_label = QLabel()
        self.text_overlay_wave_speed = SmoothSlider(Qt.Orientation.Horizontal)
        self.text_overlay_wave_speed.setMinimum(0)
        self.text_overlay_wave_speed.setMaximum(25)
        self.text_overlay_wave_speed.setValue(int(round(NEON_WAVE_FRAME_SPEED * 100)))
        self.text_overlay_wave_speed.valueChanged.connect(self._on_text_overlay_wave_changed)
        self.text_overlay_wave_speed_label = QLabel()
        self._sync_text_overlay_wave_labels()
        text_opts.addWidget(QLabel("Волна — амплитуда:"), 6, 0)
        wave_amp_row = QHBoxLayout()
        wave_amp_row.setContentsMargins(0, 0, 0, 0)
        wave_amp_row.addWidget(self.text_overlay_wave_amp, 1)
        wave_amp_row.addWidget(self.text_overlay_wave_amp_label)
        wave_amp_w = QWidget()
        wave_amp_w.setLayout(wave_amp_row)
        text_opts.addWidget(wave_amp_w, 6, 1)
        text_opts.addWidget(QLabel("Скорость:"), 7, 0)
        wave_spd_row = QHBoxLayout()
        wave_spd_row.setContentsMargins(0, 0, 0, 0)
        wave_spd_row.addWidget(self.text_overlay_wave_speed, 1)
        wave_spd_row.addWidget(self.text_overlay_wave_speed_label)
        wave_spd_w = QWidget()
        wave_spd_w.setLayout(wave_spd_row)
        text_opts.addWidget(wave_spd_w, 7, 1)

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

        text_hint = QLabel(
            "Перетащите текст"
        )
        text_hint.setObjectName("hint")
        text_hint.setWordWrap(True)
        text_controls_l.addWidget(text_hint)

        text_l.addWidget(self._text_overlay_panel)
        self._text_overlay_section.content_layout().addWidget(text_inner)
        self._text_overlay_section.set_expanded(True)
        fx_layout.addWidget(self._text_overlay_section)
        self._update_text_overlay_controls()

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
        self.rb_noise_min, self.rb_noise_max = _dspin(0.5, 4.0, 0.05, 2)
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
        inner_left_l.addWidget(bg_tracks)
        inner_left_l.addWidget(proc)
        inner_left_l.addWidget(fx)
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
            max_workers_fn=_max_worker_slider,
            default_workers_fn=_default_workers,
            apply_thread_cap_fn=_apply_thread_slider_fd_cap,
        )
        self._slice_tab.start_requested.connect(self._start_slicing)
        self._slice_tab.cancel_requested.connect(self._cancel)
        self._slice_tab.install_ffmpeg_requested.connect(self._on_install_ffmpeg)

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
        self._uploaded_session_filter = QComboBox()
        self._uploaded_session_filter.setObjectName("uploadedSessionFilter")
        self._uploaded_session_filter.setMinimumWidth(320)
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
        uploaded_top.addWidget(self._btn_uploaded_refresh)
        uploaded_top.addWidget(self._btn_uploaded_check)
        uploaded_l.addLayout(uploaded_top)

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
        side.setFixedWidth(278)
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
        sort_row = QHBoxLayout()
        sort_row.setSpacing(8)
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
        sort_row.addStretch()
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
        self._profiles_title = QLabel("Профили Dolphin Anty")
        self._profiles_title.setObjectName("title")
        self._profiles_hint = QLabel(
            "Подгрузка профилей через глобальный Public API Dolphin{anty} "
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
        self._btn_profiles_refresh = QPushButton("Обновить")
        self._btn_profiles_refresh.setObjectName("secondary")
        self._btn_profiles_refresh.setAutoDefault(False)
        self._btn_profiles_refresh.setDefault(False)
        self._btn_profiles_refresh.clicked.connect(self._refresh_antydetect_profiles)
        profiles_search_row.addWidget(self._dolphin_query, 1)
        profiles_search_row.addWidget(self._btn_profiles_refresh)

        profiles_actions_row = QHBoxLayout()
        profiles_actions_row.setSpacing(8)
        self._btn_profiles_check_availability = QPushButton("Проверить доступность YouTube")
        self._btn_profiles_check_availability.setObjectName("secondary")
        self._btn_profiles_check_availability.setAutoDefault(False)
        self._btn_profiles_check_availability.setDefault(False)
        self._btn_profiles_check_availability.setToolTip(
            "Только для отмеченных профилей (квадратики): режим Headless из настроек, "
            "до 4 одновременно, создание канала и «Далее» при "
            "необходимости, проверка окна загрузки в YouTube Studio без выбора файла."
        )
        self._btn_profiles_check_availability.clicked.connect(
            self._start_profiles_availability_check
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
        self._btn_profiles_fill_channel = QPushButton("Заполнить описание и ссылку")
        self._btn_profiles_fill_channel.setObjectName("secondary")
        self._btn_profiles_fill_channel.setAutoDefault(False)
        self._btn_profiles_fill_channel.setDefault(False)
        self._btn_profiles_fill_channel.setToolTip(
            "Только для отмеченных профилей: Studio → «Настройка канала», "
            "описание канала и ссылка, затем «Опубликовать». "
            "Браузер всегда с окном (не headless), до 5 параллельно."
        )
        self._btn_profiles_fill_channel.clicked.connect(
            self._start_profiles_channel_fill
        )
        self._btn_profiles_add_avatars = QPushButton("Аватарки и названия")
        self._btn_profiles_add_avatars.setObjectName("secondary")
        self._btn_profiles_add_avatars.setAutoDefault(False)
        self._btn_profiles_add_avatars.setDefault(False)
        self._btn_profiles_add_avatars.setToolTip(
            "Только для отмеченных профилей: аватарки и/или названия каналов "
            "в YouTube Studio («Настройка канала»). "
            "Можно задать только аватарки, только названия или оба варианта. "
            "Браузер всегда с окном (не headless), до 5 параллельно."
        )
        self._btn_profiles_add_avatars.clicked.connect(
            self._open_profiles_avatars_import_dialog
        )
        self._btn_profiles_warmup = QPushButton("Прогрев")
        self._btn_profiles_warmup.setObjectName("secondary")
        self._btn_profiles_warmup.setAutoDefault(False)
        self._btn_profiles_warmup.setDefault(False)
        self._btn_profiles_warmup.setToolTip(
            "Только для отмеченных профилей: авторизация, выбор канала, "
            "затем просмотр ленты YouTube Shorts (пауза на каждом ролике, "
            "случайные лайки, подписки и прокрутка вниз). "
            "Режим Headless из настроек, до 5 параллельно."
        )
        self._btn_profiles_warmup.clicked.connect(self._start_profiles_warmup)
        self._btn_profiles_clear_zaliver_tags = QPushButton("Очистить теги залива")
        self._btn_profiles_clear_zaliver_tags.setObjectName("secondary")
        self._btn_profiles_clear_zaliver_tags.setAutoDefault(False)
        self._btn_profiles_clear_zaliver_tags.setDefault(False)
        self._btn_profiles_clear_zaliver_tags.setToolTip(
            "С отмеченных профилей снимает служебные теги Zaliver "
            "(ошибки залива, проверки Studio и т.д.). Только свой антидетект."
        )
        self._btn_profiles_clear_zaliver_tags.clicked.connect(
            self._start_clear_zaliver_profile_tags
        )
        profiles_actions_row.addWidget(self._btn_profiles_fill_channel)
        profiles_actions_row.addWidget(self._btn_profiles_add_avatars)
        profiles_actions_row.addWidget(self._btn_profiles_warmup)
        profiles_actions_row.addWidget(self._btn_profiles_check_availability)
        profiles_actions_row.addWidget(self._btn_profiles_import_accounts)
        profiles_actions_row.addStretch()

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
            on_preview_click=self._open_profile_cdp_preview,
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

        settings = QWidget()
        settings_l = QVBoxLayout(settings)
        settings_l.setSpacing(12)
        settings_l.setContentsMargins(12, 12, 12, 12)
        settings_title = QLabel("Настройки")
        settings_title.setObjectName("title")
        settings_hint = QLabel(
            "Выберите браузер по умолчанию для раздела «Профили». "
            "Токен Dolphin — https://dolphin-anty.net/panel/#/api"
        )
        settings_hint.setObjectName("hint")
        settings_hint.setWordWrap(True)

        self._gb_stats_username = QGroupBox("Имя пользователя")
        gsu = QVBoxLayout(self._gb_stats_username)
        self._stats_server_username = QLineEdit()
        self._btn_save_stats_username = QPushButton("Сохранить")
        self._btn_save_stats_username.setObjectName("secondary")
        self._btn_save_stats_username.setAutoDefault(False)
        self._btn_save_stats_username.setDefault(False)
        self._btn_save_stats_username.clicked.connect(
            self._save_stats_server_username_settings
        )
        gsu_btns = QHBoxLayout()
        gsu_btns.addStretch()
        gsu_btns.addWidget(self._btn_save_stats_username)
        w_gsu_btns = QWidget()
        w_gsu_btns.setLayout(gsu_btns)
        gsu.addWidget(self._stats_server_username)
        gsu.addWidget(w_gsu_btns)

        browser_pick = QHBoxLayout()
        browser_pick.addWidget(QLabel("Браузер по умолчанию:"))
        self._default_browser_combo = QComboBox()
        self._default_browser_combo.setObjectName("defaultBrowserCombo")
        self._default_browser_combo.addItem("Dolphin Anty", "dolphin")
        self._default_browser_combo.addItem("Свой (локальный API)", "local")
        self._default_browser_combo.addItem("Свой (удалённый API)", "remote")
        self._default_browser_combo.currentIndexChanged.connect(
            self._on_default_browser_combo_changed
        )
        browser_pick.addWidget(self._default_browser_combo, 1)

        self._dolphin_headless = QCheckBox("Headless (без окна браузера)")
        self._dolphin_headless.setChecked(True)
        self._dolphin_headless.setToolTip(
            "Если включено — профиль запускается без окна браузера (headless): "
            "Dolphin и свой антидетект (локальный или удалённый API)."
        )

        self._gb_antydetect_dolphin = QGroupBox("Dolphin Anty")
        gg = QGridLayout(self._gb_antydetect_dolphin)
        public_host = QLabel("Public API: https://dolphin-anty-api.com")
        public_host.setObjectName("hint")
        public_host.setWordWrap(True)
        self._dolphin_token = QLineEdit()
        self._dolphin_token.setPlaceholderText("JWT токен (Public API)…")
        self._dolphin_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._dolphin_token.setToolTip(
            "Токен из личного кабинета Dolphin. "
            "Используется для Public API как заголовок Authorization: Bearer <token>."
        )

        self._gb_antydetect_local = QGroupBox("Свой антидетект (локальный HTTP API)")
        gl = QGridLayout(self._gb_antydetect_local)
        self._local_api_base_url = QLineEdit()
        self._local_api_base_url.setPlaceholderText(DEFAULT_LOCAL_API_BASE_URL)
        self._local_api_base_url.setToolTip(
            "Корень HTTP-сервиса (без завершающего слэша), как в OpenAPI: /profiles, /health, …"
        )
        gl.addWidget(QLabel("Базовый URL:"), 0, 0)
        gl.addWidget(self._local_api_base_url, 0, 1)

        self._gb_antydetect_remote = QGroupBox("Свой антидетект (удалённый HTTP API)")
        gr = QGridLayout(self._gb_antydetect_remote)
        self._remote_api_base_url = QLineEdit()
        self._remote_api_base_url.setPlaceholderText("https://example.com:18765")
        self._remote_api_base_url.setToolTip(
            "Корень HTTP-сервиса на удалённой машине (без завершающего слэша), "
            "как в OpenAPI: /profiles, /health, …"
        )
        gr.addWidget(QLabel("Базовый URL:"), 0, 0)
        gr.addWidget(self._remote_api_base_url, 0, 1)
        self._remote_cdp_public_host = QLineEdit()
        self._remote_cdp_public_host.setPlaceholderText("Публичный IP или хост для CDP")
        self._remote_cdp_public_host.setToolTip(
            "Значение cdp_public_host в запросе /launch: адрес, по которому Zaliver "
            "подключится к CDP удалённого браузера."
        )
        gr.addWidget(QLabel("CDP public host:"), 1, 0)
        gr.addWidget(self._remote_cdp_public_host, 1, 1)

        self._btn_save_antydetect = QPushButton("Сохранить")
        self._btn_save_antydetect.setObjectName("secondary")
        self._btn_save_antydetect.clicked.connect(self._save_antydetect_settings)

        self._settings_status = QLabel("")
        self._settings_status.setObjectName("hint")
        self._settings_status.setWordWrap(True)

        gg.addWidget(public_host, 0, 0, 1, 2)
        gg.addWidget(QLabel("JWT:"), 1, 0)
        gg.addWidget(self._dolphin_token, 1, 1)
        btns = QHBoxLayout()
        btns.addStretch()
        btns.addWidget(self._btn_save_antydetect)
        w_btns = QWidget()
        w_btns.setLayout(btns)
        gg.addWidget(w_btns, 2, 0, 1, 2)
        gg.addWidget(self._settings_status, 3, 0, 1, 2)

        gb_yt = QGroupBox("YouTube")
        gy = QGridLayout(gb_yt)
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
        self._youtube_search_oldest.setChecked(True)
        self._youtube_search_oldest.setToolTip(
            "Если включено — перед заливом и проверкой Studio ищется самый старый канал "
            "или проверяется, что открыт сохранённый yt_oldest_name.\n"
            "Если выключено — используется текущий открытый канал без переключения."
        )
        self._youtube_search_oldest.stateChanged.connect(
            self._on_youtube_search_oldest_changed
        )
        self._btn_save_youtube = QPushButton("Сохранить ключ")
        self._btn_save_youtube.setObjectName("secondary")
        self._btn_save_youtube.clicked.connect(self._save_youtube_settings)
        self._youtube_settings_status = QLabel("")
        self._youtube_settings_status.setObjectName("hint")
        self._youtube_settings_status.setWordWrap(True)

        gy.addWidget(QLabel("API key (для статистики):"), 0, 0)
        gy.addWidget(self._youtube_api_key, 0, 1)
        gy.addWidget(self._youtube_show_key, 1, 0, 1, 2)
        gy.addWidget(self._youtube_search_oldest, 2, 0, 1, 2)
        yt_btns = QHBoxLayout()
        yt_btns.addStretch()
        yt_btns.addWidget(self._btn_save_youtube)
        w_yt_btns = QWidget()
        w_yt_btns.setLayout(yt_btns)
        gy.addWidget(w_yt_btns, 3, 0, 1, 2)
        gy.addWidget(self._youtube_settings_status, 4, 0, 1, 2)

        settings_l.addWidget(settings_title)
        settings_l.addWidget(settings_hint)
        settings_l.addWidget(self._gb_stats_username)
        settings_l.addLayout(browser_pick)
        settings_l.addWidget(self._dolphin_headless)
        settings_l.addWidget(self._gb_antydetect_dolphin)
        settings_l.addWidget(self._gb_antydetect_local)
        settings_l.addWidget(self._gb_antydetect_remote)
        settings_l.addWidget(gb_yt)
        settings_l.addStretch()
        self._sync_antydetect_settings_groups_visibility()

        self._stack = QStackedWidget()
        self._stack.addWidget(home)
        self._stack.addWidget(self._slice_tab)
        self._stack.addWidget(ready)
        self._stack.addWidget(uploaded)
        self._stack.addWidget(profiles)
        self._stack.addWidget(settings)

        self._nav = QListWidget()
        self._nav.setObjectName("sideNav")
        self._nav.setFixedWidth(210)
        self._nav.addItems(
            [
                "Уникализация",
                "Нарезка",
                "Готовые видео",
                "Залитые видео",
                "Профили",
                "Настройки",
            ]
        )
        self._nav.setCurrentRow(0)
        self._nav.currentRowChanged.connect(self._on_nav_row_changed)

        outer = QHBoxLayout(self)
        outer.setSpacing(12)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.addWidget(self._nav)
        outer.addWidget(self._stack, 1)

    def _on_nav_row_changed(self, row: int) -> None:
        self._stack.setCurrentIndex(max(0, min(row, self._stack.count() - 1)))
        if row == 2:
            self._refresh_ready_list()
        if row == 3:
            self._refresh_uploaded_list()
        if row == 4:
            self._refresh_antydetect_profiles()

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
            sessions = self._upload_store.list_sessions(limit=400)
        except Exception:
            sessions = []
        ids = [int(s.id) for s in sessions]
        m: dict[int, list[UploadedVideo]] = {}
        try:
            if ids:
                raw = self._upload_store.list_uploaded_videos_for_sessions(ids)
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
        tip = self._uploaded_video_tooltip(v)
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
            sessions = self._upload_store.list_sessions(limit=400)
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
        key = ""
        if hasattr(self, "_youtube_api_key"):
            key = (self._youtube_api_key.text() or "").strip()

        self._uploaded_stats_status.setText(
            f"Обновление статистики: 0 / {len(vids)}…"
        )
        self._btn_uploaded_check.setEnabled(False)
        self._stats_thread = QThread()
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
        del successes, errors
        if hasattr(self, "_uploaded_stats_status"):
            self._uploaded_stats_status.setText("")
        t = self._stats_thread
        if t is not None:
            t.quit()

    def _on_uploaded_stats_thread_finished(self) -> None:
        self._stats_thread = None
        if self._stats_worker is not None:
            self._stats_worker.deleteLater()
            self._stats_worker = None
        if hasattr(self, "_uploaded_stats_status"):
            self._uploaded_stats_status.setText("")
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

    def _delete_after_upload_enabled(self) -> bool:
        mode = (getattr(self, "_upload_log_mode", "") or "").strip()
        if not mode:
            mode = (getattr(self, "_active_work_mode", "") or "").strip()
        if mode == "slicing":
            tab = getattr(self, "_slice_tab", None)
            return bool(
                tab is not None
                and hasattr(tab, "delete_after_upload")
                and tab.delete_after_upload.isChecked()
            )
        return bool(
            hasattr(self, "delete_after_upload") and self.delete_after_upload.isChecked()
        )

    def _delete_output_video_after_upload(self, video_path: str) -> None:
        p = Path(str(video_path or "").strip()).expanduser()
        if not str(p):
            return
        try:
            if not p.is_file():
                return
            p.unlink()
        except OSError as e:
            try:
                self._ui_log_line.emit(
                    f"[upload] Не удалось удалить файл после залива: {p.name} ({e!r})"
                )
            except Exception:
                pass
            return
        try:
            self._video_store.prune_missing_files()
        except Exception:
            pass
        try:
            self._ui_log_line.emit(f"[upload] Удалён после залива: {p.name}")
        except Exception:
            pass
        try:
            QTimer.singleShot(0, self._refresh_ready_list)
        except Exception:
            pass

    def _on_output_saved(self, path: str, include_in_upload: bool = True) -> None:
        if isinstance(path, str) and path.strip():
            p = path.strip()
            if include_in_upload:
                self._just_saved_outputs.append(p)
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
                self._profiles_status.setText(
                    "Профили ещё не загружены — запускаю загрузку… "
                    "Уникализация без залива в YouTube (профили не выбраны)."
                )
            except Exception:
                pass
            self._refresh_antydetect_profiles()
            return {"title": "", "description": "", "profile_ids": ""}

        dlg = QDialog(self)
        dlg_title = (
            "Загрузка в YouTube после нарезки"
            if mode == "slicing"
            else "Загрузка в YouTube после уникализации"
        )
        dlg.setWindowTitle(dlg_title)
        dlg.setModal(True)
        dlg.setMinimumSize(QSize(980, 780))
        dlg.resize(1100, 860)

        grid = QGridLayout(dlg)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        title_edit = QLineEdit()
        title_edit.setPlaceholderText("Название видео (обязательное для загрузки в YouTube)…")
        desc_edit = QPlainTextEdit()
        desc_edit.setPlaceholderText("Описание (необязательно)…")
        desc_edit.setMinimumHeight(44)
        desc_edit.setMaximumHeight(72)
        desc_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

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

        last_upload_map = self._upload_store.last_uploaded_at_by_profiles(ids)
        dlg_profiles = [p for _pid, p in profile_rows]
        total_dlg_profiles = len(dlg_profiles)

        dlg_query = QLineEdit()
        dlg_query.setPlaceholderText("Поиск по профилям (имя, ID, теги)…")
        dlg_filter_timer = QTimer(dlg)
        dlg_filter_timer.setSingleShot(True)

        def _dlg_profiles_matched(q_raw: str) -> list[dict[str, object]]:
            tokens = profile_search_tokens(q_raw)
            matched: list[tuple[int, dict[str, object]]] = []
            for i, p in enumerate(dlg_profiles):
                if isinstance(p, dict) and profile_matches_search(p, tokens):
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
        )
        dlg_interaction.populate(dlg_profiles, last_upload_map, preserve_checked=preselect)

        def _apply_dlg_profiles_filter() -> None:
            visible = _dlg_profiles_matched(dlg_query.text())
            pids = [_profile_id(p) for p in visible]
            pids = [x for x in pids if x]
            filtered_last = {k: last_upload_map[k] for k in pids if k in last_upload_map}
            dlg_interaction.populate(visible, filtered_last, prune_checked_to_existing=False)

        def _schedule_dlg_profiles_filter() -> None:
            dlg_filter_timer.start(150)

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
        uniquify_planned = n_inputs * copies_n
        if mode == "slicing" and hasattr(self, "_slice_tab"):
            try:
                copies_t = max(1, int(self._slice_tab.copies_per_track.value()))
            except Exception:
                copies_t = 1
            uniquify_planned = copies_t

        dlg_profile_count_lbl = QLabel("")
        dlg_profile_count_lbl.setObjectName("hint")
        dlg_profile_count_lbl.setWordWrap(True)

        planned_label = (
            "Будет нарезано видео"
            if mode == "slicing"
            else "Будет уникализировано видео"
        )
        only_label = (
            "(только нарезка)."
            if mode == "slicing"
            else "(только уникализация)."
        )

        def _update_dlg_upload_profile_count() -> None:
            n = dlg_interaction.checked_count()
            shown = dlg_interaction.lw.count()
            q = dlg_query.text().strip()
            lines = [f"{planned_label}: {uniquify_planned}"]
            if q:
                lines.append(f"Показано профилей: {shown} из {total_dlg_profiles}")
            if n <= 0:
                lines.append(
                    f"Выбрано профилей для залива: 0 — без залива в YouTube {only_label}"
                )
            else:
                lines.append(f"Выбрано профилей для залива: {n}")
            dlg_profile_count_lbl.setText("\n".join(lines))

        dlg_interaction.selection_changed.connect(_update_dlg_upload_profile_count)
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

        grid.addWidget(QLabel("Название:"), 0, 0)
        grid.addWidget(title_edit, 0, 1)
        grid.addWidget(
            QLabel("Описание:"),
            1,
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        grid.addWidget(desc_edit, 1, 1)
        profiles_col = QWidget()
        profiles_col_l = QVBoxLayout(profiles_col)
        profiles_col_l.setContentsMargins(0, 0, 0, 0)
        profiles_col_l.setSpacing(8)
        profiles_col_l.addWidget(dlg_profile_count_lbl)
        profiles_col_l.addWidget(dlg_query)
        dlg_sel_row, _dlg_checked_lbl = self._build_profiles_selection_toolbar(
            dlg,
            dlg_interaction,
            on_select_filter=_dlg_select_filter,
            on_clear=dlg_interaction.clear_checked_selection,
        )
        profiles_col_l.addLayout(dlg_sel_row)
        profiles_col_l.addWidget(lw, 1)

        grid.addWidget(QLabel("Профили:"), 2, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(profiles_col, 2, 1)
        grid.addWidget(btns, 3, 0, 1, 2)
        grid.setRowStretch(2, 1)

        title_edit.setFocus()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None

        title = (title_edit.text() or "").strip()
        description = (desc_edit.toPlainText() or "").strip()
        picked = dlg_interaction.batch_profile_ids()

        # Если профили не выбраны, считаем, что пользователь хочет только уникализировать видео,
        # без загрузки в YouTube. В этом случае title не обязателен.
        if not picked:
            return {"title": title, "description": description, "profile_ids": ""}
        if not title:
            QMessageBox.warning(self, "Zaliver", "Название видео обязательно для загрузки в YouTube.")
            return None

        return {"title": title, "description": description, "profile_ids": ",".join(picked)}

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._sync_ffmpeg_install_row()

    def _sync_ffmpeg_install_row(self) -> None:
        needs = needs_ffmpeg_install_prompt()
        if not needs:
            self._ffmpeg_row.setVisible(False)
            if hasattr(self, "_slice_tab"):
                self._slice_tab.sync_ffmpeg_install_row(visible=False)
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
        if hasattr(self, "_slice_tab"):
            self._slice_tab.sync_ffmpeg_install_row(
                visible=True, hint=hint, button_text=self.btn_install_ffmpeg.text()
            )

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
        if hasattr(self, "background_music_mix"):
            self.background_music_mix.setChecked(
                bool(self._settings.value("background_music_mix_with_source", False, type=bool))
            )
        if hasattr(self, "background_music_volume"):
            try:
                vv = int(self._settings.value("background_music_volume_pct", 35, type=int))
            except Exception:
                vv = 35
            vv = max(0, min(100, vv))
            self.background_music_volume.blockSignals(True)
            self.background_music_volume.setValue(vv)
            self.background_music_volume.blockSignals(False)
            if hasattr(self, "background_music_volume_label"):
                self.background_music_volume_label.setText(f"{vv} %")
        self._sync_music_list_widget()
        self._update_music_mix_controls()
        if hasattr(self, "text_overlay_enabled"):
            self.text_overlay_enabled.setChecked(
                bool(self._settings.value("text_overlay_enabled", True, type=bool))
            )
            self.text_overlay_edit.setPlainText(
                self._settings.value("text_overlay_text", "GAME IN BIO", type=str) or "GAME IN BIO"
            )
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
                waf = float(
                    self._settings.value(
                        "text_overlay_wave_amp_frac", NEON_WAVE_AMP_FRAC, type=float
                    )
                )
            except Exception:
                waf = NEON_WAVE_AMP_FRAC
            try:
                wfs = float(
                    self._settings.value(
                        "text_overlay_wave_frame_speed", NEON_WAVE_FRAME_SPEED, type=float
                    )
                )
            except Exception:
                wfs = NEON_WAVE_FRAME_SPEED
            waf = max(0.0, min(0.35, waf))
            wfs = max(0.0, min(0.25, wfs))
            self.text_overlay_wave_amp.blockSignals(True)
            self.text_overlay_wave_amp.setValue(int(round(waf * 100)))
            self.text_overlay_wave_amp.blockSignals(False)
            self.text_overlay_wave_speed.blockSignals(True)
            self.text_overlay_wave_speed.setValue(int(round(wfs * 100)))
            self.text_overlay_wave_speed.blockSignals(False)
            self._sync_text_overlay_wave_labels()
            self._sync_text_overlay_color_btn(
                self.text_overlay_glow_btn, self._text_overlay_glow_color
            )
            self._sync_text_overlay_color_btn(
                self.text_overlay_text_btn, self._text_overlay_text_color
            )
            self._sync_text_overlay_preview(ax, ay)
            self._update_text_overlay_controls()

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
        if hasattr(self, "background_music_mix"):
            self._settings.setValue(
                "background_music_mix_with_source",
                bool(self.background_music_mix.isChecked()),
            )
        if hasattr(self, "background_music_volume"):
            self._settings.setValue(
                "background_music_volume_pct", int(self.background_music_volume.value())
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
            waf, wfs = self._text_overlay_wave_values()
            self._settings.setValue("text_overlay_wave_amp_frac", float(waf))
            self._settings.setValue("text_overlay_wave_frame_speed", float(wfs))

    def _on_default_browser_combo_changed(self, _index: int) -> None:
        self._update_profiles_section_header()
        self._sync_antydetect_settings_groups_visibility()
        self._sync_profiles_tab_action_buttons()

    def _sync_antydetect_settings_groups_visibility(self) -> None:
        if not hasattr(self, "_gb_antydetect_dolphin") or not hasattr(
            self, "_default_browser_combo"
        ):
            return
        kind = self._default_browser_combo.currentData()
        if not isinstance(kind, str) or not kind:
            kind = "dolphin"
        show_dolphin = kind == "dolphin"
        self._gb_antydetect_dolphin.setVisible(show_dolphin)
        self._gb_antydetect_local.setVisible(kind == "local")
        if hasattr(self, "_gb_antydetect_remote"):
            self._gb_antydetect_remote.setVisible(kind == "remote")

    def _update_profiles_section_header(self) -> None:
        if not hasattr(self, "_profiles_title"):
            return
        kind = self._default_browser_combo.currentData()
        if not isinstance(kind, str) or not kind:
            kind = "dolphin"
        if kind == "local":
            self._profiles_title.setText("Профили (локальный антидетект)")
            self._profiles_hint.setText(
                "Отметьте квадратиками профили для залива; «Пауза 3 ч» — можно ли снова загружать "
                "(клик по оранжевой подписи сбрасывает паузу)."
            )
            if hasattr(self, "_dolphin_query"):
                self._dolphin_query.setPlaceholderText(
                    "Поиск по загруженным профилям (имя, ID, движок)…"
                )
        elif kind == "remote":
            self._profiles_title.setText("Профили (удалённый антидетект)")
            self._profiles_hint.setText(
                "Отметьте квадратиками профили для залива; «Пауза 3 ч» — можно ли снова загружать "
                "(клик по оранжевой подписи сбрасывает паузу)."
            )
            if hasattr(self, "_dolphin_query"):
                self._dolphin_query.setPlaceholderText(
                    "Поиск по загруженным профилям (имя, ID, движок)…"
                )
        else:
            self._profiles_title.setText("Профили Dolphin Anty")
            self._profiles_hint.setText(
                "Подгрузка через Public API Dolphin{anty}. Квадратик — участие в заливе; "
                "удерживайте ЛКМ по квадратикам для групповой отметки (Ctrl — добавить)."
            )
            if hasattr(self, "_dolphin_query"):
                self._dolphin_query.setPlaceholderText("Поиск по загруженным профилям…")
        self._sync_profiles_tab_action_buttons()

    def _sync_profiles_tab_action_buttons(self) -> None:
        kind = self._default_browser_combo.currentData()
        if not isinstance(kind, str) or not kind:
            kind = "dolphin"
        own = _is_own_antidetect_kind(kind)
        busy = (
            self._profiles_availability_running
            or self._profiles_channel_fill_running
            or self._profiles_avatar_upload_running
            or self._profiles_warmup_running
            or self._profiles_tags_clear_running
            or self._profiles_refresh_running
        )
        if hasattr(self, "_btn_profiles_clear_zaliver_tags"):
            self._btn_profiles_clear_zaliver_tags.setEnabled(own and not busy)
        if hasattr(self, "_btn_profiles_check_availability"):
            self._btn_profiles_check_availability.setEnabled(not busy)
        if hasattr(self, "_btn_profiles_fill_channel"):
            self._btn_profiles_fill_channel.setEnabled(not busy)
        if hasattr(self, "_btn_profiles_add_avatars"):
            self._btn_profiles_add_avatars.setEnabled(own and not busy)
        if hasattr(self, "_btn_profiles_warmup"):
            self._btn_profiles_warmup.setEnabled(not busy)
        if hasattr(self, "_btn_profiles_refresh"):
            self._btn_profiles_refresh.setEnabled(not busy)
        if hasattr(self, "_btn_profiles_import_accounts"):
            self._btn_profiles_import_accounts.setEnabled(own and not busy)

    def _load_antydetect_settings(self) -> None:
        if not hasattr(self, "_dolphin_token"):
            return
        token = self._settings.value("antydetect/dolphin_token", "", type=str) or ""
        self._dolphin_token.setText((token or "").strip())
        if hasattr(self, "_dolphin_headless"):
            headless = self._settings.value(
                "antydetect/dolphin_headless", True, type=bool
            )
            self._dolphin_headless.setChecked(bool(headless))
        if hasattr(self, "_default_browser_combo"):
            br = (
                self._settings.value("antydetect/default_browser", "dolphin", type=str)
                or "dolphin"
            ).strip()
            idx = self._default_browser_combo.findData(br)
            if idx < 0:
                idx = 0
            self._default_browser_combo.blockSignals(True)
            self._default_browser_combo.setCurrentIndex(idx)
            self._default_browser_combo.blockSignals(False)
        if hasattr(self, "_local_api_base_url"):
            if self._settings.contains("antydetect/local_api_base_url"):
                url = (self._settings.value("antydetect/local_api_base_url", "", type=str) or "").strip()
            else:
                url = DEFAULT_LOCAL_API_BASE_URL
            self._local_api_base_url.setText(url)
        if hasattr(self, "_remote_api_base_url"):
            remote_url = (
                self._settings.value("antydetect/remote_api_base_url", "", type=str) or ""
            ).strip()
            self._remote_api_base_url.setText(remote_url)
        if hasattr(self, "_remote_cdp_public_host"):
            remote_host = (
                self._settings.value("antydetect/remote_cdp_public_host", "", type=str) or ""
            ).strip()
            self._remote_cdp_public_host.setText(remote_host)
        self._sync_antydetect_settings_groups_visibility()

    def _save_antydetect_settings(self) -> None:
        token = (self._dolphin_token.text() or "").strip()
        if token:
            self._settings.setValue("antydetect/dolphin_token", token)
        if hasattr(self, "_dolphin_headless"):
            self._settings.setValue(
                "antydetect/dolphin_headless",
                bool(self._dolphin_headless.isChecked()),
            )
        if hasattr(self, "_default_browser_combo"):
            k = self._default_browser_combo.currentData()
            if isinstance(k, str) and k:
                self._settings.setValue("antydetect/default_browser", k)
        if hasattr(self, "_local_api_base_url"):
            self._settings.setValue(
                "antydetect/local_api_base_url",
                (self._local_api_base_url.text() or "").strip(),
            )
        if hasattr(self, "_remote_api_base_url"):
            self._settings.setValue(
                "antydetect/remote_api_base_url",
                (self._remote_api_base_url.text() or "").strip(),
            )
        if hasattr(self, "_remote_cdp_public_host"):
            self._settings.setValue(
                "antydetect/remote_cdp_public_host",
                (self._remote_cdp_public_host.text() or "").strip(),
            )
        try:
            self._settings.sync()
        except Exception:
            pass
        if hasattr(self, "_settings_status"):
            self._settings_status.setText("Сохранено.")

    def _load_youtube_settings(self) -> None:
        if not hasattr(self, "_youtube_api_key"):
            return
        key = (self._settings.value("youtube/api_key", "", type=str) or "").strip()
        self._youtube_api_key.setText(key)
        if key:
            os.environ["YOUTUBE_API_KEY"] = key
        if hasattr(self, "_youtube_search_oldest"):
            search_oldest = self._settings.value(
                "youtube/search_oldest_channel", True, type=bool
            )
            self._youtube_search_oldest.blockSignals(True)
            self._youtube_search_oldest.setChecked(bool(search_oldest))
            self._youtube_search_oldest.blockSignals(False)
        if hasattr(self, "_stats_server_username"):
            gu = (
                self._settings.value("stats_server/username", "", type=str) or ""
            ).strip()
            self._stats_server_username.setText(gu)

    def _stats_server_username_stripped(self) -> str:
        if not hasattr(self, "_stats_server_username"):
            return ""
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
        key = (self._youtube_api_key.text() or "").strip()
        if key:
            self._settings.setValue("youtube/api_key", key)
            os.environ["YOUTUBE_API_KEY"] = key
            try:
                self._settings.sync()
            except Exception:
                pass
            if hasattr(self, "_youtube_settings_status"):
                self._youtube_settings_status.setText(
                    "Ключ YouTube Data API сохранён."
                )
            return

        try:
            self._settings.remove("youtube/api_key")
        except Exception:
            self._settings.setValue("youtube/api_key", "")
        os.environ.pop("YOUTUBE_API_KEY", None)
        try:
            self._settings.sync()
        except Exception:
            pass
        if hasattr(self, "_youtube_settings_status"):
            self._youtube_settings_status.setText("Ключ API очищен.")

    def _on_youtube_show_key_changed(self, _state: int) -> None:
        if not hasattr(self, "_youtube_api_key") or not hasattr(self, "_youtube_show_key"):
            return
        show = bool(self._youtube_show_key.isChecked())
        self._youtube_api_key.setEchoMode(
            QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        )

    def _youtube_search_oldest_channel(self) -> bool:
        if hasattr(self, "_youtube_search_oldest"):
            return bool(self._youtube_search_oldest.isChecked())
        return bool(
            self._settings.value("youtube/search_oldest_channel", True, type=bool)
        )

    def _on_youtube_search_oldest_changed(self, _state: int) -> None:
        if not hasattr(self, "_youtube_search_oldest"):
            return
        self._settings.setValue(
            "youtube/search_oldest_channel",
            bool(self._youtube_search_oldest.isChecked()),
        )
        try:
            self._settings.sync()
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
        matched: list[tuple[int, dict[str, object]]] = []
        for i, p in enumerate(raw):
            if isinstance(p, dict) and profile_matches_search(p, tokens):
                matched.append((i, p))
        matched.sort(key=lambda ip: profile_search_rank(ip[1], tokens, q_raw, ip[0]))
        return [p for _i, p in matched]

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
    ) -> tuple[QHBoxLayout, QLabel]:
        """Строка «Выделено» + «Выделить…» + «Снять выделение» для списка профилей."""
        row = QHBoxLayout()
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
        btn_select.setToolTip(
            "Отметить профили по условию (пауза 3 ч, ошибки, данные учётки, старейший канал)"
        )
        select_menu = QMenu(parent)
        act_all = select_menu.addAction("Все видимые")
        act_all.setToolTip("Отметить все профили в списке")
        act_all.triggered.connect(lambda: on_select_filter("all"))
        act_avail = select_menu.addAction("Доступные (пауза 3 ч прошла)")
        act_avail.setToolTip(
            "Профили, с которых снова можно заливать: прошли 3 часа после последнего залива "
            "или заливов ещё не было"
        )
        act_avail.triggered.connect(lambda: on_select_filter("available"))
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
        row.addStretch()
        row.addWidget(btn_select)
        row.addWidget(btn_clear)

        interaction.selection_changed.connect(_sync_count)
        _sync_count()
        return row, lbl

    def _refresh_profiles_list_view(self) -> int:
        if self._profiles_interaction is None:
            return 0
        visible = self._profiles_visible_matched()
        pids = [_profile_id(p) for p in visible]
        pids = [x for x in pids if x]
        last_upload_map = self._upload_store.last_uploaded_at_by_profiles(pids)
        kind = (
            self._default_browser_combo.currentData()
            if hasattr(self, "_default_browser_combo")
            else "dolphin"
        )
        show_account = _is_own_antidetect_kind(kind if isinstance(kind, str) else "")
        show_preview = isinstance(kind, str) and kind.strip() == "remote"
        self._profiles_interaction.populate(
            visible,
            last_upload_map,
            prune_checked_to_existing=False,
            show_account_data_button=show_account,
            show_preview_button=show_preview,
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
        q = (self._dolphin_query.text() if hasattr(self, "_dolphin_query") else "") or ""
        q = q.strip()
        n_checked = (
            self._profiles_interaction.checked_count()
            if self._profiles_interaction
            else 0
        )
        if q:
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
        last_upload_map = self._upload_store.last_uploaded_at_by_profiles(pids)
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
        q = (self._dolphin_query.text() if hasattr(self, "_dolphin_query") else "") or ""
        q = q.strip()
        n_checked = (
            self._profiles_interaction.checked_count()
            if self._profiles_interaction
            else 0
        )
        if q:
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

        token = (self._dolphin_token.text() or "").strip()
        if not token:
            token = (self._settings.value("antydetect/dolphin_token", "", type=str) or "").strip()

        kind = self._default_browser_combo.currentData()
        if not isinstance(kind, str) or not kind.strip():
            kind = "dolphin"
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
        try:
            if _is_own_antidetect_kind(kind):
                u = (base_url or "").strip()
                if not u:
                    self._profiles_load_failed.emit(
                        f"Укажите базовый URL {_own_antidetect_api_label(kind)} API в настройках "
                        "(раздел «Свой антидетект») и сохраните."
                    )
                    return
                api = LocalAntidetectHttpAPI(u)
                try:
                    raw = api.list_profiles()
                finally:
                    api.close()
                profiles = [normalize_local_profile_for_ui(p) for p in raw]
                self._profiles_loaded.emit(profiles)
                return

            api = DolphinAntyPublicAPI(token=token)
            try:
                # Public API: limit max 100 (OpenAPI). Поиск в UI — локально по загруженному списку.
                profiles = api.list_profiles(limit=100, query=None)
            finally:
                api.close()
            self._profiles_loaded.emit(profiles)
        except DolphinAntyError as e:
            self._profiles_load_failed.emit(
                "Проверьте JWT токен (Public API: https://dolphin-anty-api.com).\n" + str(e)
            )
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
            QMessageBox.warning(
                self,
                "Проверка доступности",
                "Отметьте квадратиками профили, для которых нужно проверить YouTube Studio.",
            )
            return

        token = (self._dolphin_token.text() or "").strip()
        if not token:
            token = (
                self._settings.value("antydetect/dolphin_token", "", type=str) or ""
            ).strip()
        kind = self._default_browser_combo.currentData()
        if not isinstance(kind, str) or not kind.strip():
            kind = "dolphin"
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
        self._profiles_status.setText(
            f"Проверка доступности Studio: 0 / {len(profile_ids)}…"
        )
        headless_label = "headless" if headless else "с окном браузера"
        from zaliver.youtube_upload.multi_availability_checker import (
            _MAX_CONCURRENT_AVAILABILITY_CHECKS,
        )

        try:
            self._append_log(
                f"[availability] Старт проверки {len(profile_ids)} профилей "
                f"({headless_label}, до {_MAX_CONCURRENT_AVAILABILITY_CHECKS} параллельно)…"
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
    ) -> None:
        from zaliver.antydetect.antic_open import (
            check_studio_availability_in_local_antidetect_profile,
            check_studio_availability_in_profile,
            set_log_sink,
        )
        from zaliver.youtube_upload.multi_availability_checker import (
            MultiProfileAvailabilityChecker,
            _MAX_CONCURRENT_AVAILABILITY_CHECKS,
        )
        from zaliver.antydetect.profile_tags import STUDIO_AVAILABILITY_ERROR_TAG

        set_log_sink(self._ui_log_line.emit)
        kind_s = (kind or "").strip()
        base_u = (base_url or "").strip() or DEFAULT_LOCAL_API_BASE_URL

        def _check_one(pid: str) -> None:
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
                        f"[availability] profile={pid}: тег "
                        f"{STUDIO_AVAILABILITY_ERROR_TAG!r} доступен только "
                        "для своего антидетекта."
                    )
                return
            try:
                api = LocalAntidetectHttpAPI(base_u)
                try:
                    if ok:
                        try:
                            api.remove_profile_tag(pid, STUDIO_AVAILABILITY_ERROR_TAG)
                            self._ui_log_line.emit(
                                f"[availability] profile={pid} tag_removed="
                                f"{STUDIO_AVAILABILITY_ERROR_TAG!r}"
                            )
                        except Exception:
                            pass
                    else:
                        api.add_profile_tag(pid, STUDIO_AVAILABILITY_ERROR_TAG)
                        self._ui_log_line.emit(
                            f"[availability] profile={pid} tag_added="
                            f"{STUDIO_AVAILABILITY_ERROR_TAG!r}"
                        )
                finally:
                    api.close()
            except Exception as te:
                action = "tag_remove_failed" if ok else "tag_add_failed"
                self._ui_log_line.emit(
                    f"[availability] profile={pid} {action} err={te!r}"
                )

        def _on_progress(done: int, total: int, profile_id: str) -> None:
            self._studio_availability_progress.emit(done, total, profile_id)

        mgr = MultiProfileAvailabilityChecker(
            profile_ids=profile_ids,
            check_one=_check_one,
            on_profile_done=_on_profile_done,
            on_progress=_on_progress,
            log_sink=self._ui_log_line.emit,
            max_concurrent=_MAX_CONCURRENT_AVAILABILITY_CHECKS,
        )
        try:
            ok_n, fail_n, failed_ids = mgr.run()
            self._last_availability_failed_ids = list(failed_ids)
            self._studio_availability_finished.emit(ok_n, fail_n)
        except Exception as e:
            self._ui_log_line.emit(f"[availability] Критическая ошибка воркера: {e!r}")
            self._last_availability_failed_ids = list(profile_ids)
            self._studio_availability_finished.emit(0, len(profile_ids))

    def _prompt_channel_description_and_link(
        self,
    ) -> tuple[str, str, str] | None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Описание и ссылка канала")
        dlg.setModal(True)
        dlg.setMinimumWidth(520)
        v = QVBoxLayout(dlg)

        hint = QLabel(
            "Текст будет применён ко всем отмеченным профилям. "
            "Описание и ссылку можно указать вместе или по отдельности "
            "(для ссылки нужны и название, и URL)."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        v.addWidget(hint)

        desc_edit = QPlainTextEdit()
        desc_edit.setPlaceholderText("Описание канала…")
        desc_edit.setMinimumHeight(120)
        v.addWidget(QLabel("Описание"))
        v.addWidget(desc_edit)

        link_title_edit = QLineEdit()
        link_title_edit.setPlaceholderText("Название ссылки…")
        v.addWidget(QLabel("Название ссылки"))
        v.addWidget(link_title_edit)

        link_url_edit = QLineEdit()
        link_url_edit.setPlaceholderText("https://…")
        v.addWidget(QLabel("Ссылка"))
        v.addWidget(link_url_edit)

        row = QHBoxLayout()
        row.addStretch()
        btn_cancel = QPushButton("Отмена")
        btn_cancel.setObjectName("danger")
        btn_start = QPushButton("Заполнить")
        btn_start.setDefault(True)
        btn_start.setAutoDefault(True)

        def on_start() -> None:
            desc = (desc_edit.toPlainText() or "").strip()
            link_title = (link_title_edit.text() or "").strip()
            link_url = (link_url_edit.text() or "").strip()
            if not desc and not (link_title and link_url):
                QMessageBox.warning(
                    dlg,
                    "Описание и ссылка канала",
                    "Укажите описание и/или пару «Название ссылки + Ссылка».",
                )
                return
            if (link_title and not link_url) or (link_url and not link_title):
                QMessageBox.warning(
                    dlg,
                    "Описание и ссылка канала",
                    "Для ссылки нужны и название, и URL.",
                )
                return
            dlg.accept()

        btn_start.clicked.connect(on_start)
        btn_cancel.clicked.connect(dlg.reject)
        row.addWidget(btn_cancel)
        row.addWidget(btn_start)
        v.addLayout(row)

        desc_edit.setFocus()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return (
            (desc_edit.toPlainText() or "").strip(),
            (link_title_edit.text() or "").strip(),
            (link_url_edit.text() or "").strip(),
        )

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
        form.addRow("Длительность просмотра Short:", watch_range_row)
        v.addLayout(form)

        horizontal_group = QGroupBox("Горизонтальные видео")
        horizontal_form = QFormLayout(horizontal_group)
        watch_horizontal_cb = QCheckBox("Смотреть после Shorts")
        horizontal_form.addRow(watch_horizontal_cb)

        search_label = QLabel("Поисковый запрос:")
        search_edit = QLineEdit()
        search_edit.setPlaceholderText("Текст для поиска на главной YouTube")
        horizontal_form.addRow(search_label, search_edit)

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
                search_label,
                search_edit,
                horizontal_count_label,
                horizontal_count_spin,
                horizontal_watch_hint,
            ):
                w.setVisible(checked)

        watch_horizontal_cb.toggled.connect(_sync_horizontal_fields)
        _sync_horizontal_fields(False)
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

        def _try_accept() -> None:
            if watch_min_spin.value() > watch_max_spin.value():
                QMessageBox.warning(
                    dlg,
                    "Прогрев YouTube",
                    "Минимальная длительность просмотра Short не может быть "
                    "больше максимальной.",
                )
                return
            if watch_horizontal_cb.isChecked() and not search_edit.text().strip():
                QMessageBox.warning(
                    dlg,
                    "Прогрев YouTube",
                    "Укажите текст для поиска горизонтальных видео.",
                )
                return
            dlg.accept()

        btn_start.clicked.connect(_try_accept)

        count_spin.setFocus()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return ShortsWarmupSettings(
            shorts_count=count_spin.value(),
            like_probability_pct=like_spin.value(),
            subscribe_probability_pct=subscribe_spin.value(),
            shorts_watch_min_s=watch_min_spin.value(),
            shorts_watch_max_s=watch_max_spin.value(),
            watch_horizontal_videos=watch_horizontal_cb.isChecked(),
            horizontal_search_query=(search_edit.text() or "").strip(),
            horizontal_videos_count=horizontal_count_spin.value(),
        )

    def _start_profiles_channel_fill(self) -> None:
        if self._profiles_channel_fill_running:
            QMessageBox.information(
                self,
                "Описание и ссылка канала",
                "Заполнение уже выполняется. Дождитесь завершения.",
            )
            return
        if self._profiles_raw is None:
            QMessageBox.warning(
                self,
                "Описание и ссылка канала",
                "Сначала загрузите список профилей (кнопка «Обновить»).",
            )
            return
        profile_ids = self._collect_checked_profile_ids()
        if not profile_ids:
            QMessageBox.warning(
                self,
                "Описание и ссылка канала",
                "Отметьте квадратиками профили, для которых нужно заполнить канал.",
            )
            return

        values = self._prompt_channel_description_and_link()
        if values is None:
            return
        description, link_title, link_url = values

        token = (self._dolphin_token.text() or "").strip()
        if not token:
            token = (
                self._settings.value("antydetect/dolphin_token", "", type=str) or ""
            ).strip()
        kind = self._default_browser_combo.currentData()
        if not isinstance(kind, str) or not kind.strip():
            kind = "dolphin"
        base_url = self._own_antidetect_base_url_from_settings(kind)

        headless = False

        try:
            remote_cdp = self._remote_cdp_launch_options_for_kind(kind)
        except LocalAntidetectError as e:
            QMessageBox.warning(self, "Описание и ссылка канала", str(e))
            return

        self._profiles_channel_fill_running = True
        self._sync_profiles_tab_action_buttons()
        self._profiles_status.setText(
            f"Заполнение описания/ссылки канала: 0 / {len(profile_ids)}…"
        )
        self._append_log(
            f"[channel_fill] Старт для {len(profile_ids)} профилей "
            f"(с окном браузера, до 5 параллельно)…"
        )

        threading.Thread(
            target=self._profiles_channel_fill_worker,
            kwargs={
                "profile_ids": profile_ids,
                "kind": kind,
                "token": token,
                "base_url": base_url,
                "headless": headless,
                "description": description,
                "link_title": link_title,
                "link_url": link_url,
                "remote_cdp": remote_cdp,
            },
            daemon=True,
        ).start()

    def _profiles_channel_fill_worker(
        self,
        *,
        profile_ids: list[str],
        kind: str,
        token: str,
        base_url: str,
        headless: bool,
        description: str,
        link_title: str,
        link_url: str,
        remote_cdp: RemoteCdpLaunchOptions | None = None,
    ) -> None:
        from zaliver.antydetect.antic_open import (
            fill_channel_description_and_link_in_local_antidetect_profile,
            fill_channel_description_and_link_in_profile,
            set_log_sink,
        )
        from zaliver.youtube_upload.multi_availability_checker import (
            MultiProfileAvailabilityChecker,
        )

        set_log_sink(self._ui_log_line.emit)
        kind_s = (kind or "").strip()
        base_u = (base_url or "").strip() or DEFAULT_LOCAL_API_BASE_URL

        def _fill_one(pid: str) -> None:
            creds = self._profile_login_credentials(pid)
            yt_oldest = self._profile_yt_oldest_name(pid) or None
            search_oldest = self._youtube_search_oldest_channel()
            if _is_own_antidetect_kind(kind_s):
                u = (base_url or "").strip()
                if not u:
                    raise LocalAntidetectError(
                        f"Укажите базовый URL {_own_antidetect_api_label(kind_s)} API в настройках."
                    )
                fill_channel_description_and_link_in_local_antidetect_profile(
                    pid,
                    description=description or None,
                    link_title=link_title or None,
                    link_url=link_url or None,
                    base_url=u,
                    headless=headless,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    search_oldest_channel=search_oldest,
                    remote_cdp=remote_cdp,
                )
            else:
                fill_channel_description_and_link_in_profile(
                    pid,
                    description=description or None,
                    link_title=link_title or None,
                    link_url=link_url or None,
                    local_token=token or None,
                    headless=headless,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    search_oldest_channel=search_oldest,
                )

        def _on_progress(done: int, total: int, profile_id: str) -> None:
            self._studio_channel_fill_progress.emit(done, total, profile_id)

        mgr = MultiProfileAvailabilityChecker(
            profile_ids=profile_ids,
            check_one=_fill_one,
            on_progress=_on_progress,
            log_sink=self._ui_log_line.emit,
        )
        ok_n, fail_n, failed_ids = mgr.run()
        self._last_channel_fill_failed_ids = list(failed_ids)
        self._studio_channel_fill_finished.emit(ok_n, fail_n)

    def _start_profiles_warmup(self) -> None:
        if self._profiles_warmup_running:
            QMessageBox.information(
                self,
                "Прогрев Shorts",
                "Прогрев уже выполняется. Дождитесь завершения.",
            )
            return
        if self._profiles_raw is None:
            QMessageBox.warning(
                self,
                "Прогрев Shorts",
                "Сначала загрузите список профилей (кнопка «Обновить»).",
            )
            return
        profile_ids = self._collect_checked_profile_ids()
        if not profile_ids:
            QMessageBox.warning(
                self,
                "Прогрев Shorts",
                "Отметьте квадратиками профили, для которых нужен прогрев.",
            )
            return

        warmup_settings = self._prompt_shorts_warmup_settings()
        if warmup_settings is None:
            return

        token = (self._dolphin_token.text() or "").strip()
        if not token:
            token = (
                self._settings.value("antydetect/dolphin_token", "", type=str) or ""
            ).strip()
        kind = self._default_browser_combo.currentData()
        if not isinstance(kind, str) or not kind.strip():
            kind = "dolphin"
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
            QMessageBox.warning(self, "Прогрев Shorts", str(e))
            return

        self._profiles_warmup_running = True
        self._sync_profiles_tab_action_buttons()
        self._profiles_status.setText(
            f"Прогрев Shorts: 0 / {len(profile_ids)}…"
        )
        headless_label = "headless" if headless else "с окном браузера"
        self._append_log(
            f"[warmup] Старт для {len(profile_ids)} профилей "
            f"(Shorts: {warmup_settings.shorts_count}, "
            f"просмотр {warmup_settings.shorts_watch_min_s}–"
            f"{warmup_settings.shorts_watch_max_s} с, "
            f"лайк {warmup_settings.like_probability_pct:g}%, "
            f"подписка {warmup_settings.subscribe_probability_pct:g}%"
            + (
                f", горизонтальные: {warmup_settings.horizontal_videos_count}, "
                f"поиск «{warmup_settings.horizontal_search_query}»"
                if warmup_settings.watch_horizontal_videos
                else ""
            )
            + f", {headless_label}, до 5 параллельно)…"
        )

        threading.Thread(
            target=self._profiles_warmup_worker,
            kwargs={
                "profile_ids": profile_ids,
                "kind": kind,
                "token": token,
                "base_url": base_url,
                "headless": headless,
                "warmup_settings": warmup_settings,
                "remote_cdp": remote_cdp,
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

        mgr = MultiProfileAvailabilityChecker(
            profile_ids=profile_ids,
            check_one=_warmup_one,
            on_progress=_on_progress,
            log_sink=self._ui_log_line.emit,
        )
        ok_n, fail_n, failed_ids = mgr.run()
        self._last_warmup_failed_ids = list(failed_ids)
        self._studio_warmup_finished.emit(ok_n, fail_n)

    def _collect_checked_profile_ids(self) -> list[str]:
        if self._profiles_interaction is None:
            return []
        return self._profiles_interaction.batch_profile_ids()

    def _own_antidetect_base_url_from_settings(self, kind: str | None = None) -> str:
        if kind is None:
            kind = self._default_browser_combo.currentData()
        k = (kind or "").strip() if isinstance(kind, str) else "dolphin"
        if not k:
            k = "dolphin"
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
            base_url = (self._remote_api_base_url.text() or "").strip()
            if not base_url:
                base_url = (
                    self._settings.value("antydetect/remote_api_base_url", "", type=str) or ""
                ).strip()
            return base_url
        return ""

    def _remote_cdp_launch_options_for_kind(self, kind: str) -> RemoteCdpLaunchOptions | None:
        if (kind or "").strip() != "remote":
            return None
        host = (self._remote_cdp_public_host.text() or "").strip()
        if not host:
            host = (
                self._settings.value("antydetect/remote_cdp_public_host", "", type=str) or ""
            ).strip()
        if not host:
            raise LocalAntidetectError(
                "Укажите CDP public host (IP) для удалённого антидетекта в настройках."
            )
        return RemoteCdpLaunchOptions(cdp_public_host=host)

    def _local_antidetect_base_url_from_settings(self) -> str:
        return self._own_antidetect_base_url_from_settings("local")

    def _profile_login_credentials(self, profile_id: str):
        from zaliver.youtube_upload.google_login import credentials_from_custom_data

        pid = (profile_id or "").strip()
        for p in self._profiles_raw or []:
            if _profile_id(p) != pid:
                continue
            cd = p.get("custom_data")
            if isinstance(cd, dict):
                return credentials_from_custom_data(cd)
        return None

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
        kind = self._default_browser_combo.currentData()
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

    def _profiles_avatars_dialog_title(self) -> str:
        return "Аватарки и названия"

    def _open_profiles_avatars_import_dialog(self) -> None:
        title = self._profiles_avatars_dialog_title()
        if self._profiles_avatar_upload_running:
            QMessageBox.information(
                self,
                title,
                "Применение аватарок и названий уже выполняется. Дождитесь завершения.",
            )
            return
        kind = self._default_browser_combo.currentData()
        if not _is_own_antidetect_kind(kind if isinstance(kind, str) else ""):
            QMessageBox.information(
                self,
                title,
                "Импорт аватарок и названий доступен только для своего антидетекта "
                "(локальный или удалённый API).",
            )
            return
        if not self._profiles_raw:
            QMessageBox.information(
                self,
                title,
                "Сначала загрузите список профилей (кнопка «Обновить»).",
            )
            return
        if self._profiles_interaction is None:
            return
        profile_ids = self._profiles_interaction.batch_profile_ids()
        if not profile_ids:
            QMessageBox.warning(
                self,
                title,
                "Отметьте квадратиками профили, которым нужно применить изменения.",
            )
            return
        by_id = self._profiles_by_id_map(self._profiles_raw)
        selected_profiles = [by_id[pid] for pid in profile_ids if pid in by_id]
        if not selected_profiles:
            QMessageBox.warning(
                self,
                title,
                "Не удалось найти отмеченные профили в загруженном списке.",
            )
            return

        dlg = ProfileAvatarsImportDialog(
            selected_profiles=selected_profiles,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        assignments = dlg.profile_assignments()
        if not assignments:
            return

        token = (self._dolphin_token.text() or "").strip()
        if not token:
            token = (
                self._settings.value("antydetect/dolphin_token", "", type=str) or ""
            ).strip()
        kind_s = kind if isinstance(kind, str) else "dolphin"
        base_url = self._own_antidetect_base_url_from_settings(kind_s)
        if not (base_url or "").strip():
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

        self._profiles_avatar_upload_running = True
        self._sync_profiles_tab_action_buttons()
        self._profiles_status.setText(
            f"Аватарки и названия в Studio: 0 / {len(assignments)}…"
        )
        self._append_log(
            f"[avatar_upload] Старт для {len(assignments)} профилей "
            f"(с окном браузера, до 5 параллельно)…"
        )

        threading.Thread(
            target=self._profiles_avatar_upload_worker,
            kwargs={
                "assignments": assignments,
                "kind": kind_s,
                "token": token,
                "base_url": base_url,
                "headless": headless,
                "remote_cdp": remote_cdp,
            },
            daemon=True,
        ).start()

    def _profiles_avatar_upload_worker(
        self,
        *,
        assignments: list[dict[str, object]],
        kind: str,
        token: str,
        base_url: str,
        headless: bool,
        remote_cdp: RemoteCdpLaunchOptions | None = None,
    ) -> None:
        from zaliver.antydetect.antic_open import (
            set_log_sink,
            upload_channel_avatar_in_local_antidetect_profile,
            upload_channel_avatar_in_profile,
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

        def _upload_one(pid: str) -> None:
            item = by_id.get(pid)
            if not item:
                raise LocalAntidetectError(f"Нет задания для профиля {pid!r}.")
            png = item.get("avatar_png")
            avatar_path: Path | None = None
            if isinstance(png, (bytes, bytearray)) and png:
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                    tf.write(bytes(png))
                    avatar_path = Path(tf.name)
            channel_name = str(item.get("channel_name") or "").strip() or None
            skip_name_change = bool(item.get("skip_name_change"))
            if avatar_path is None and not (
                channel_name and not skip_name_change
            ):
                raise LocalAntidetectError(
                    f"Нет аватарки и названия для профиля {pid!r}."
                )
            try:
                creds = self._profile_login_credentials(pid)
                yt_oldest = self._profile_yt_oldest_name(pid) or None
                search_oldest = self._youtube_search_oldest_channel()
                if _is_own_antidetect_kind(kind_s):
                    u = (base_url or "").strip()
                    if not u:
                        raise LocalAntidetectError(
                            f"Укажите базовый URL {_own_antidetect_api_label(kind_s)} API в настройках."
                        )
                    upload_channel_avatar_in_local_antidetect_profile(
                        pid,
                        avatar_path=avatar_path,
                        channel_name=channel_name,
                        skip_name_change=skip_name_change,
                        base_url=u,
                        headless=headless,
                        login_credentials=creds,
                        yt_oldest_name=yt_oldest,
                        search_oldest_channel=search_oldest,
                        remote_cdp=remote_cdp,
                    )
                else:
                    upload_channel_avatar_in_profile(
                        pid,
                        avatar_path=avatar_path,
                        channel_name=channel_name,
                        skip_name_change=skip_name_change,
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
            self._studio_avatar_upload_progress.emit(done, total, profile_id)

        mgr = MultiProfileAvailabilityChecker(
            profile_ids=list(by_id.keys()),
            check_one=_upload_one,
            on_progress=_on_progress,
            log_sink=self._ui_log_line.emit,
        )
        ok_n, fail_n, failed_ids = mgr.run()
        self._last_avatar_upload_failed_ids = list(failed_ids)
        self._studio_avatar_upload_finished.emit(ok_n, fail_n)

    def _open_profile_cdp_preview(self, profile_id: str) -> None:
        pid = (profile_id or "").strip()
        if not pid:
            return
        kind = self._default_browser_combo.currentData()
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
        self._show_profile_account_data_dialog(pid)

    def _show_profile_account_data_dialog(self, profile_id: str) -> None:
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

        dlg = ProfileAccountDataDialog(
            profile_name=name,
            profile_id=pid,
            custom_data=custom_data,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        payload = dlg.account_data_payload()
        login = str(payload.get(YT_LOGIN_KEY) or "").strip()
        if login:
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
                    "Данные учетки",
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
                "Данные учетки",
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
        QMessageBox.information(self, "Данные учетки", msg)

    def _start_clear_zaliver_profile_tags(self) -> None:
        if self._profiles_tags_clear_running:
            QMessageBox.information(
                self,
                "Очистка тегов",
                "Очистка уже выполняется. Дождитесь завершения.",
            )
            return
        kind = self._default_browser_combo.currentData()
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
        from zaliver.antydetect.profile_tags import ZALIVER_PROFILE_TAGS

        tags_hint = ", ".join(ZALIVER_PROFILE_TAGS)
        answer = QMessageBox.question(
            self,
            "Очистка тегов залива",
            f"Снять служебные теги Zaliver с {len(profile_ids)} отмеченных профилей?\n\n"
            f"Теги: {tags_hint}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
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
            f"[tags] Старт очистки тегов у {len(profile_ids)} профилей…"
        )
        threading.Thread(
            target=self._clear_zaliver_profile_tags_worker,
            kwargs={"profile_ids": profile_ids, "base_url": base_url},
            daemon=True,
        ).start()

    def _clear_zaliver_profile_tags_worker(
        self, *, profile_ids: list[str], base_url: str
    ) -> None:
        from zaliver.antydetect.profile_tags import (
            ZALIVER_PROFILE_TAGS,
            clear_zaliver_tags_on_profile,
        )

        api = LocalAntidetectHttpAPI((base_url or "").strip())
        total = len(profile_ids)
        removed_total = 0
        try:
            for i, pid in enumerate(profile_ids, start=1):
                self._zaliver_profile_tags_clear_progress.emit(i, total, pid)
                n = clear_zaliver_tags_on_profile(api, pid)
                removed_total += n
                if n > 0:
                    self._ui_log_line.emit(
                        f"[tags] profile={pid}: снято тегов {n} "
                        f"из {len(ZALIVER_PROFILE_TAGS)}"
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
        self._profiles_status.setText(
            f"Проверка доступности Studio: {current} / {total}"
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
        kind = self._default_browser_combo.currentData()
        if _is_own_antidetect_kind((kind or "").strip()) and total > 0:
            self._refresh_antydetect_profiles()
        QMessageBox.information(
            self,
            "Проверка доступности",
            f"Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}.",
        )

    def _on_studio_channel_fill_progress(
        self, current: int, total: int, profile_id: str
    ) -> None:
        pid = (profile_id or "").strip()
        self._profiles_status.setText(
            f"Заполнение описания/ссылки канала: {current} / {total}"
            + (f" — профиль {pid}" if pid else "…")
        )

    def _on_studio_channel_fill_finished(self, ok_n: int, fail_n: int) -> None:
        self._profiles_channel_fill_running = False
        self._sync_profiles_tab_action_buttons()
        total = int(ok_n) + int(fail_n)
        self._profiles_status.setText(
            f"Заполнение канала завершено: успешно {ok_n}, с ошибкой {fail_n} "
            f"(всего {total})."
        )
        self._append_log(
            f"[channel_fill] Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}."
        )
        if int(fail_n) > 0:
            failed = getattr(self, "_last_channel_fill_failed_ids", None) or []
            if failed:
                self._append_log(
                    "[channel_fill] Ошибки (ID): " + ", ".join(failed)
                )
        QMessageBox.information(
            self,
            "Описание и ссылка канала",
            f"Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}.",
        )

    def _on_studio_avatar_upload_progress(
        self, current: int, total: int, profile_id: str
    ) -> None:
        pid = (profile_id or "").strip()
        self._profiles_status.setText(
            f"Аватарки и названия в Studio: {current} / {total}"
            + (f" — профиль {pid}" if pid else "…")
        )

    def _on_studio_avatar_upload_finished(self, ok_n: int, fail_n: int) -> None:
        self._profiles_avatar_upload_running = False
        self._sync_profiles_tab_action_buttons()
        total = int(ok_n) + int(fail_n)
        title = self._profiles_avatars_dialog_title()
        self._profiles_status.setText(
            f"Аватарки и названия: успешно {ok_n}, с ошибкой {fail_n} "
            f"(всего {total})."
        )
        self._append_log(
            f"[avatar_upload] Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}."
        )
        if int(fail_n) > 0:
            failed = getattr(self, "_last_avatar_upload_failed_ids", None) or []
            if failed:
                self._append_log(
                    "[avatar_upload] Ошибки (ID): " + ", ".join(failed)
                )
        QMessageBox.information(
            self,
            title,
            f"Итог: успешно {ok_n}, с ошибкой {fail_n}, всего {total}.",
        )

    def _on_studio_warmup_progress(
        self, current: int, total: int, profile_id: str
    ) -> None:
        pid = (profile_id or "").strip()
        self._profiles_status.setText(
            f"Прогрев Shorts: {current} / {total}"
            + (f" — профиль {pid}" if pid else "…")
        )

    def _on_studio_warmup_finished(self, ok_n: int, fail_n: int) -> None:
        self._profiles_warmup_running = False
        self._sync_profiles_tab_action_buttons()
        total = int(ok_n) + int(fail_n)
        self._profiles_status.setText(
            f"Прогрев Shorts завершён: успешно {ok_n}, с ошибкой {fail_n} "
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
        QMessageBox.information(
            self,
            "Прогрев Shorts",
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
        ans = QMessageBox.question(
            parent,
            "Пауза 3 ч",
            "Обновить время паузы с последнего залива? После подтверждения с этим профилем снова можно будет загружать видео.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        n = self._upload_store.reset_latest_upload_time_for_profile(profile_id=pid)
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
        new_map = self._upload_store.last_uploaded_at_by_profiles([pid])
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
        kind = self._default_browser_combo.currentData()
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

    def _on_music_volume_slider_changed(self, value: int) -> None:
        if hasattr(self, "background_music_volume_label"):
            self.background_music_volume_label.setText(f"{int(value)} %")
        self._save_folder_settings()

    def _update_music_mix_controls(self, _checked: bool = False) -> None:
        if not hasattr(self, "background_music_mix"):
            return
        music_on = bool(self.background_music.isChecked())
        self.background_music_mix.setEnabled(music_on)
        mix_on = music_on and self.background_music_mix.isChecked()
        self.background_music_volume.setEnabled(mix_on)
        if hasattr(self, "background_music_volume_label"):
            self.background_music_volume_label.setEnabled(mix_on)

    def _sync_music_list_widget(self) -> None:
        if not hasattr(self, "_music_list"):
            return
        self._music_list.clear()
        for p in self._background_music_files:
            it = QListWidgetItem(Path(p).name)
            it.setToolTip(p)
            it.setData(Qt.ItemDataRole.UserRole, p)
            self._music_list.addItem(it)
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
        seen = {str(Path(x).resolve()) for x in self._background_music_files}
        for f in files:
            p = str(Path(f).resolve())
            if p not in seen:
                seen.add(p)
                self._background_music_files.append(p)
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

    def _text_overlay_wave_values(self) -> tuple[float, float]:
        amp = max(0.0, min(0.35, int(self.text_overlay_wave_amp.value()) / 100.0))
        speed = max(0.0, min(0.25, int(self.text_overlay_wave_speed.value()) / 100.0))
        return amp, speed

    def _sync_text_overlay_wave_labels(self) -> None:
        if not hasattr(self, "text_overlay_wave_amp_label"):
            return
        amp, speed = self._text_overlay_wave_values()
        self.text_overlay_wave_amp_label.setText(f"{int(round(amp * 100))} %")
        self.text_overlay_wave_speed_label.setText(f"{speed:.2f}")

    def _text_overlay_settings(self) -> TextOverlaySettings:
        orient = self.text_overlay_orientation.currentData()
        ax, ay = self.text_overlay_preview.anchor()
        waf, wfs = self._text_overlay_wave_values()
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
        if not bool(self.text_overlay_enabled.isChecked()):
            return
        orient = self.text_overlay_orientation.currentData()
        preview = self.text_overlay_preview
        preview.blockSignals(True)
        preview.set_orientation(orient if isinstance(orient, str) else "vertical")
        preview.set_font_size(int(self.text_overlay_font_size.value()))
        preview.set_glow_enabled(bool(self.text_overlay_glow_enabled.isChecked()))
        preview.set_glow_color(self._text_overlay_glow_color)
        preview.set_text_color(self._text_overlay_text_color)
        preview.set_letter_spacing(int(self.text_overlay_letter_spacing.value()))
        preview.set_font_path(self._text_overlay_font_path)
        preview.set_font_bold(bool(self.text_overlay_font_bold.isChecked()))
        waf, wfs = self._text_overlay_wave_values()
        preview.set_wave_settings(waf, wfs)
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
        self._text_overlay_panel.setEnabled(on)
        glow_on = bool(self.text_overlay_glow_enabled.isChecked())
        self.text_overlay_glow_btn.setEnabled(glow_on)
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
        self._save_folder_settings()

    def _on_text_overlay_font_size_changed(self, _value: int) -> None:
        self._schedule_text_overlay_preview_sync()
        self._save_folder_settings()

    def _on_text_overlay_orientation_changed(self, _index: int) -> None:
        self._sync_text_overlay_preview()
        self._save_folder_settings()

    def _on_text_overlay_wave_changed(self, _value: int) -> None:
        self._sync_text_overlay_wave_labels()
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
            "use_gpu_finalize": bool(self.use_gpu_finalize.isChecked()),
            "settings": st.to_dict(),
            "randomize_uniquify": self.random_uniquify.isChecked(),
            "copies_per_file": int(self.copies_per_file.value()),
            "one_copy_no_effects": bool(self.one_copy_no_effects.isChecked()),
            "playback_speed_enabled": bool(self.audio_speed.isChecked()),
            "audio_chorus_enabled": bool(self.audio_chorus.isChecked()),
            "background_music_enabled": bool(self.background_music.isChecked()),
            "background_music_mix_with_source": bool(self.background_music_mix.isChecked()),
            "background_music_volume_pct": int(self.background_music_volume.value()),
            "background_music_files": [
                p for p in self._background_music_files if Path(p).is_file()
            ],
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
            "text_overlay": self._text_overlay_settings().to_dict(),
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

        # Upload session starts only on "Start".
        try:
            planned = len(list(opts.get("input_files") or [])) * max(
                1, int(opts.get("copies_per_file") or 1)
            )
        except Exception:
            planned = 0
        try:
            self._upload_session = self._upload_store.start_session(planned_videos=planned)
        except Exception:
            self._upload_session = None
        self._upload_session_processing_done = False
        self._upload_session_upload_done = False
        self._upload_session_upload_expected = True

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
        self._processor.output_saved.connect(self._on_output_saved)
        self._processor.log_line.connect(self._append_log)
        self._processor.finished.connect(self._work_thread.quit)
        self._processor.finished.connect(self._processor.deleteLater)
        self._work_thread.finished.connect(self._thread_cleanup)
        self._work_thread.start()

    def _start_slicing(self) -> None:
        self._active_work_mode = "slicing"
        self._slice_tab.save_settings()
        if not self._prompt_stats_server_username_if_empty():
            return
        pending = self._prompt_title_desc_and_profile(mode="slicing")
        if pending is None:
            return
        self._pending_upload = pending
        self._just_saved_outputs = []

        opts = self._slice_tab.build_options()
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

        try:
            planned = len(list(opts.get("music_files") or [])) * max(
                1, int(opts.get("copies_per_track") or 1)
            )
        except Exception:
            planned = 0
        try:
            self._upload_session = self._upload_store.start_session(planned_videos=planned)
        except Exception:
            self._upload_session = None
        self._upload_session_processing_done = False
        self._upload_session_upload_done = False
        self._upload_session_upload_expected = True

        self._slice_tab.log.clear()
        self._slice_tab.progress.setRange(0, 1)
        self._slice_tab.progress.setValueImmediate(0)
        self._slice_tab.progress_label.setText("Подготовка…")
        self._slice_tab.set_running(running=True)

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

    def _thread_cleanup(self) -> None:
        self._work_thread = None
        self._processor = None
        self._slice_processor = None

    def _cancel(self) -> None:
        if self._processor is not None:
            self._processor.cancel()
        if self._slice_processor is not None:
            self._slice_processor.cancel()
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

    def _sync_toolbar_for_upload_phase(self) -> None:
        """Во время залива на YouTube: отмена доступна, старт выключен."""
        self.btn_cancel.setEnabled(True)
        self.btn_start.setEnabled(False)
        if self._active_work_mode == "slicing" and hasattr(self, "_slice_tab"):
            self._slice_tab.set_busy()
            self._slice_tab.progress_label.setText("YouTube: загрузка…")

    def _finish_slice_tab_after_upload(self, status: str) -> None:
        if self._active_work_mode != "slicing" or not hasattr(self, "_slice_tab"):
            return
        st = self._slice_tab
        st.set_idle()
        mx = max(1, int(st.progress.maximum()))
        st.progress.setRange(0, mx)
        st.progress.setValueImmediate(mx)
        if status == "cancelled":
            st.progress_label.setText("Загрузка на YouTube отменена.")
        elif status == "timeout":
            st.progress_label.setText("Загрузка на YouTube остановлена по таймауту.")
        elif status == "upload_failed":
            st.progress_label.setText("Готово (ошибки загрузки на YouTube).")
        else:
            st.progress_label.setText("Готово")

    def _on_slice_progress(self, cur: int, total: int, msg: str) -> None:
        if not hasattr(self, "_slice_tab"):
            return
        self._slice_tab.progress.setRange(0, max(1, total))
        self._slice_tab.progress.setValue(cur)
        if msg:
            self._slice_tab.progress_label.setText(msg)

    def _on_youtube_upload_phase_finished(self, status: str) -> None:
        upload_mode = (
            (getattr(self, "_upload_log_mode", "") or "").strip() or self._active_work_mode
        )
        self._upload_log_mode = ""
        self._upload_manager = None
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
            if upload_mode == "slicing":
                self._append_slice_log("YouTube: загрузка отменена пользователем.")
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
            if upload_mode == "slicing":
                self._append_slice_log(f"YouTube: {timeout_msg}")
            else:
                self.progress_label.setText("Загрузка на YouTube остановлена по таймауту.")
                self._append_log(f"YouTube: {timeout_msg}")
            QMessageBox.warning(self, "Zaliver", timeout_msg)
        elif status == "upload_failed":
            if upload_mode == "slicing":
                self._append_slice_log(
                    "YouTube: очередь завершена, залив не удался (см. лог выше)."
                )
            else:
                self.progress_label.setText("Готово (есть ошибки загрузки на YouTube).")
                self._append_log(
                    "YouTube: очередь завершена, часть загрузок завершилась с ошибками."
                )
        else:
            if upload_mode == "slicing":
                self._append_slice_log("YouTube: очередь загрузок завершена.")
            else:
                self.progress_label.setText("Готово")
                self._append_log("YouTube: очередь загрузок завершена.")

    def _on_progress(self, cur: int, total: int, msg: str) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(cur)
        if msg:
            self.progress_label.setText(msg)

    def _on_finished(self, ok: bool, msg: str) -> None:
        self._upload_session_processing_done = True

        if not ok:
            self._finalize_idle_toolbar()
            self._release_youtube_progress_hold_if_any()
            err_line = f"Ошибка: {msg}"
            if self._active_work_mode == "slicing":
                self._append_slice_log(err_line)
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
        else:
            self._append_log("Уникализация завершена.")
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
                    "Загрузка в YouTube пропущена: не найден путь к сохранённому видео."
                )
                self._upload_log_mode = ""
                self._upload_session_upload_expected = False
                self._upload_session_upload_done = True
                self._maybe_finish_upload_session(status="done")
                self._release_youtube_progress_hold_if_any()
                self._finalize_idle_toolbar()
                return

            token = (self._dolphin_token.text() or "").strip()
            if not token:
                token = (
                    self._settings.value("antydetect/dolphin_token", "", type=str) or ""
                ).strip()

            kind = self._default_browser_combo.currentData()
            if not isinstance(kind, str) or not kind.strip():
                kind = "dolphin"
            base_url = self._own_antidetect_base_url_from_settings(kind)

            try:
                remote_cdp = self._remote_cdp_launch_options_for_kind(kind)
            except LocalAntidetectError as e:
                self._append_session_log(f"YouTube: заливка пропущена — {e}")
                self._upload_log_mode = ""
                self._upload_session_upload_expected = False
                self._upload_session_upload_done = True
                self._maybe_finish_upload_session(status="upload_failed")
                self._release_youtube_progress_hold_if_any()
                self._finalize_idle_toolbar()
                QMessageBox.warning(self, "Zaliver", str(e))
                return

            raw_ids = (pending.get("profile_ids", "") or "").strip()
            profile_ids = [p.strip() for p in raw_ids.split(",") if p.strip()]
            if not profile_ids:
                self._append_session_log("YouTube: профили не выбраны — заливка пропущена.")
                self._upload_log_mode = ""
                self._upload_session_upload_expected = False
                self._upload_session_upload_done = True
                self._maybe_finish_upload_session(status="done")
                self._release_youtube_progress_hold_if_any()
                self._finalize_idle_toolbar()
                return

            from zaliver.youtube_upload.multi_uploader import (
                MultiProfileUploader,
                VideoTask,
            )
            from zaliver.youtube_upload.studio import _studio_canonical_watch_url

            self._clear_previous_upload_result_tags(
                profile_ids=profile_ids, kind=kind, base_url=base_url
            )

            self._append_session_log(
                f"YouTube: многопоточная заливка стартует. "
                f"Видео={len(video_paths)}, профили={len(profile_ids)}…"
            )
            self._sync_toolbar_for_upload_phase()
            self._upload_cancel_kind = (kind or "").strip()
            self._upload_cancel_dolphin_token = token
            self._upload_cancel_profile_ids = list(profile_ids)

            def _upload_one(profile_id: str, task: VideoTask) -> None:
                from zaliver.antydetect.antic_open import (
                    open_google_in_local_antidetect_profile,
                    open_google_in_profile,
                    set_log_sink,
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

                creds = self._profile_login_credentials(profile_id)
                yt_oldest = self._profile_yt_oldest_name(profile_id) or None
                search_oldest = self._youtube_search_oldest_channel()
                if _is_own_antidetect_kind(kind):
                    res = open_google_in_local_antidetect_profile(
                        profile_id,
                        base_url=(base_url or "").strip(),
                        headless=headless,
                        video_path=task.video_path,
                        title=task.title,
                        description=task.description,
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
                        video_path=task.video_path,
                        title=task.title,
                        description=task.description,
                        login_credentials=creds,
                        yt_oldest_name=yt_oldest,
                        search_oldest_channel=search_oldest,
                    )

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
                if not vid:
                    raise RuntimeError(f"Empty video_id (res={res!r})")
                if not url:
                    url = _studio_canonical_watch_url(vid)
                if not url:
                    raise RuntimeError(f"Empty url (res={res!r})")

                sid = int(self._upload_session.id) if self._upload_session is not None else 0
                if sid <= 0:
                    raise RuntimeError("upload_session is not set (sid=0)")

                self._upload_store.add_uploaded_video(
                    session_id=sid,
                    title=task.title or "",
                    description=task.description or "",
                    url=url,
                    video_id=vid,
                    profile_id=profile_id,
                )
                try:
                    self._upload_store.inc_uploaded_ok(session_id=sid, delta=1)
                except Exception:
                    pass
                try:
                    guser = (
                        self._settings.value("stats_server/username", "", type=str)
                        or ""
                    ).strip()
                    if guser:
                        notify_uploaded_video(
                            video_id=vid,
                            username=guser,
                            profile_id=profile_id,
                        )
                except Exception:
                    pass
                try:
                    QTimer.singleShot(0, self._refresh_uploaded_list)
                except Exception:
                    pass
                if self._delete_after_upload_enabled():
                    self._delete_output_video_after_upload(task.video_path)

            def _on_profile_upload_attempt(pid: str, ok: bool, err: str) -> None:
                try:
                    self._set_previous_upload_result_tag(
                        profile_id=pid,
                        success=bool(ok),
                        kind=kind,
                        base_url=base_url,
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

            mgr = MultiProfileUploader(
                profile_ids=profile_ids,
                cooldown_s=10.0,
                max_attempts_per_profile=2,
                profile_upload_pause_remaining_s=self._upload_store.profile_upload_pause_remaining_seconds,
                log_sink=self._ui_log_line.emit,
                upload_one=_upload_one,
                on_profile_attempt=_on_profile_upload_attempt,
            )
            self._upload_manager = mgr
            mgr.enqueue_videos(
                video_paths=video_paths,
                title=pending.get("title", "Название"),
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
                    self._maybe_finish_upload_session(status=status)
                    try:
                        self._youtube_upload_phase_finished.emit(status)
                    except Exception:
                        pass

            threading.Thread(target=_run_mgr, daemon=True).start()
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
        if hasattr(self, "_slice_tab"):
            _apply_thread_slider_fd_cap(self._slice_tab.thread_slider)
        if msg:
            self._append_log(msg)

    def _append_log(self, line: str) -> None:
        from zaliver.log_format import format_log_line

        self.log.appendPlainText(format_log_line(line))
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _append_slice_log(self, line: str) -> None:
        from zaliver.log_format import format_log_line

        if not hasattr(self, "_slice_tab"):
            return
        self._slice_tab.log.appendPlainText(format_log_line(line))
        bar = self._slice_tab.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _append_session_log(self, line: str) -> None:
        """Лог текущей сессии залива (нарезка или уникализация — откуда стартовали)."""
        if (getattr(self, "_upload_log_mode", "") or "").strip() == "slicing":
            self._append_slice_log(line)
        else:
            self._append_log(line)

    def _route_ui_log_line(self, line: str) -> None:
        """Служебные логи (upload, studio, теги) — в лог той вкладки, откуда запущен залив."""
        mode = (getattr(self, "_upload_log_mode", "") or "").strip()
        if mode == "slicing":
            self._append_slice_log(line)
        else:
            self._append_log(line)

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
    ) -> None:
        from zaliver.antydetect.profile_tags import PREVIOUS_UPLOAD_RESULT_TAGS

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
        try:
            for pid in pids:
                for tag in PREVIOUS_UPLOAD_RESULT_TAGS:
                    try:
                        api.remove_profile_tag(pid, tag)
                        self._ui_log_line.emit(
                            f"[upload] profile={pid} tag_removed={tag!r}"
                        )
                    except Exception:
                        pass
        finally:
            api.close()

    def _set_previous_upload_result_tag(
        self,
        *,
        profile_id: str,
        success: bool,
        kind: str,
        base_url: str,
    ) -> None:
        from zaliver.antydetect.profile_tags import (
            UPLOAD_PREVIOUS_ERROR_TAG,
            UPLOAD_PREVIOUS_SUCCESS_TAG,
        )

        pid = (profile_id or "").strip()
        if not pid:
            return
        tag = UPLOAD_PREVIOUS_SUCCESS_TAG if success else UPLOAD_PREVIOUS_ERROR_TAG
        other = UPLOAD_PREVIOUS_ERROR_TAG if success else UPLOAD_PREVIOUS_SUCCESS_TAG
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
