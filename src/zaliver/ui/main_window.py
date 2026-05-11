"""Main application window."""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPointF,
    QRunnable,
    QSettings,
    QSize,
    QThread,
    QThreadPool,
    QTimer,
    Qt,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QDesktopServices, QMouseEvent, QPixmap, QShowEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QDoubleSpinBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
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
from zaliver.db.upload_store import UploadedVideo, UploadStore
from zaliver.antydetect.api import DolphinAntyError, DolphinAntyLocalAPI, DolphinAntyPublicAPI
from zaliver.antydetect.local_antidetect_api import (
    DEFAULT_LOCAL_API_BASE_URL,
    LocalAntidetectError,
)
from zaliver.processing.ffmpeg_merge import check_ffmpeg_tools
from zaliver.processing.pipeline import RandomUniquifyBounds, UniquifySettings
from zaliver.processing.thread_worker import ProcessingController
from zaliver.youtube_parsing.thumb_cache import (
    read_youtube_thumb_cache,
    write_youtube_thumb_cache,
    youtube_mq_thumbnail_url,
)
from zaliver.ui.antic_profile_row import AnticProfileRow, _profile_id, _profile_name
from zaliver.ui.ffmpeg_install_worker import FfmpegInstallWorker
from zaliver.stats_server_client import notify_uploaded_video
from zaliver.ui.uploaded_stats_refresh_worker import UploadedStatsRefreshWorker
from zaliver.ui.widgets import (
    AnimatedProgressBar,
    CollapsibleSection,
    SmoothSlider,
    ToggleSwitch,
)

from zaliver.antydetect.local_antidetect_api import (
    LocalAntidetectHttpAPI,
    normalize_local_profile_for_ui,
)

# Qt SpinBox/DoubleSpinBox всегда имеют min/max.
# Чтобы в UI не было "лимитов", используем максимально широкие диапазоны,
# но оставляем минимальные логические ограничения там, где отрицательные значения
# ломают смысл (например, количество копий).
_INT_MIN = -2_147_483_648
_INT_MAX = 2_147_483_647
_BIG_FLOAT = 1.0e12

_READY_THUMB_W = 176
_READY_THUMB_H = 99

_UPLOADED_THUMB_W = 72
_UPLOADED_THUMB_H = 128


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


class _UploadedThumbSignals(QObject):
    loaded = pyqtSignal(str, object, int)


class _FetchUploadedThumbRunnable(QRunnable):
    def __init__(self, video_id: str, generation: int, sigs: _UploadedThumbSignals) -> None:
        super().__init__()
        self._video_id = (video_id or "").strip()
        self._generation = int(generation)
        self._sigs = sigs

    def run(self) -> None:
        vid = self._video_id
        if not vid:
            self._sigs.loaded.emit("", b"", self._generation)
            return
        cached = read_youtube_thumb_cache(vid)
        if cached:
            self._sigs.loaded.emit(vid, cached, self._generation)
            return
        data = b""
        try:
            req = urllib.request.Request(
                youtube_mq_thumbnail_url(vid),
                headers={"User-Agent": "Zaliver/1.0 (uploaded list thumbnail)"},
            )
            with urllib.request.urlopen(req, timeout=14) as resp:
                data = resp.read()
        except Exception:
            data = b""
        if data:
            write_youtube_thumb_cache(vid, data)
        self._sigs.loaded.emit(vid, data, self._generation)


class _UploadedVideoRow(QWidget):
    """
    Строка залитого видео: превью с YouTube, метрики справа, открыть ролик — кнопка «↗».
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
        row.setContentsMargins(8, 8, 10, 8)

        self._thumb = QLabel()
        self._thumb.setFixedSize(_UPLOADED_THUMB_W, _UPLOADED_THUMB_H)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setObjectName("uploadedThumb")
        self._thumb.setText("…")
        self._thumb.setScaledContents(False)

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

        if stats_unavailable and stats_unavailable_data_api:
            stats_html = (
                "<span style='color:#94a3b8;font-weight:700;'>API</span> "
                "<span style='color:#f0abfc;font-weight:700;'>нет данных</span>"
            )
        elif stats_unavailable:
            stats_html = (
                "<span style='color:#f0abfc;font-weight:700;'>недоступно</span>"
            )
        else:
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
        metrics = QLabel(stats_html)
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

        row.addWidget(self._thumb)
        row.addLayout(text_col, 1)
        row.addLayout(stats_wrap, 0)

        for w in (self._thumb, title_lbl, id_lbl, prof_lbl, metrics, ago):
            w.installEventFilter(self)

    def thumb_label(self) -> QLabel:
        return self._thumb

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
    # Для одиночного длинного ролика приложение умеет нарезать на части (если есть ffmpeg)
    # и тем самым эффективно загрузить все CPU. Поэтому по умолчанию используем все
    # логические ядра, а не (CPU-1).
    return max(1, os.cpu_count() or 2)


def _max_worker_slider() -> int:
    # До всех логических CPU: при разбиении ролика на части полезнее занять последнее ядро.
    return max(1, os.cpu_count() or 2)


class MainWindow(QWidget):
    _after_video_saved = pyqtSignal()
    _profiles_loaded = pyqtSignal(object)
    _profiles_load_failed = pyqtSignal(str)
    _dolphin_google_ready = pyqtSignal(str)
    _dolphin_google_failed = pyqtSignal(str, str)
    _ui_log_line = pyqtSignal(str)
    _youtube_upload_phase_finished = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Zaliver — уникализация видео")
        self.setObjectName("zaliverRoot")
        self._work_thread: QThread | None = None
        self._processor: ProcessingController | None = None
        self._ff_thread: QThread | None = None
        self._ff_worker: FfmpegInstallWorker | None = None
        self._ffmpeg_progress_dlg: QProgressDialog | None = None
        self._stats_thread: QThread | None = None
        self._stats_worker: UploadedStatsRefreshWorker | None = None
        self._stats_progress_dlg: QProgressDialog | None = None
        self._selected_input_files: list[str] = []
        self._video_store = VideoStore()
        self._upload_store = UploadStore(db_path=self._video_store.db_path)
        self._upload_session = None
        self._upload_session_processing_done = False
        self._upload_session_upload_done = False
        self._upload_session_upload_expected = False

        self._settings = QSettings("Zaliver", "Zaliver")
        self._profiles_raw: list[dict[str, object]] | None = None
        self._profiles_list_render_gen: int = 0
        self._profiles_list_populating: bool = False
        self._profiles_drag: dict[str, object] = {"anchor": None, "extending": False}
        self._profiles_filter_timer = QTimer(self)
        self._profiles_filter_timer.setSingleShot(True)
        self._profiles_filter_timer.timeout.connect(self._apply_profiles_filter)
        self._build_ui()
        self._ui_log_line.connect(self._append_log)
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
        self._youtube_upload_phase_finished.connect(self._on_youtube_upload_phase_finished)
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
            "Имена: имя_u_<случайные hex>.mp4 — у каждого выхода свой суффикс (не счётчик)."
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
        rl.addWidget(self.log, 1)

        splitter.addWidget(scroll_left)
        splitter.addWidget(right)
        splitter.setSizes([420, 580])
        home_l.addWidget(splitter, 1)

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

        self._uploaded_thumb_gen = 0
        self._uploaded_thumb_labels: dict[str, QLabel] = {}
        self._uploaded_thumb_signals = _UploadedThumbSignals(self)
        self._uploaded_thumb_signals.loaded.connect(self._on_uploaded_thumb_loaded)
        self._uploaded_thumb_pool = QThreadPool(self)
        self._uploaded_thumb_pool.setMaxThreadCount(5)

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

        profiles_top = QHBoxLayout()
        self._dolphin_query = QLineEdit()
        self._dolphin_query.setPlaceholderText("Поиск по загруженным профилям…")
        self._btn_profiles_refresh = QPushButton("Обновить")
        self._btn_profiles_refresh.setObjectName("secondary")
        self._btn_profiles_refresh.setAutoDefault(False)
        self._btn_profiles_refresh.setDefault(False)
        self._btn_profiles_refresh.clicked.connect(self._refresh_antydetect_profiles)
        profiles_top.addWidget(self._profiles_title)
        profiles_top.addStretch()
        profiles_top.addWidget(self._dolphin_query, 1)
        profiles_top.addWidget(self._btn_profiles_refresh)

        self._profiles_status = QLabel("")
        self._profiles_status.setObjectName("hint")
        self._profiles_status.setWordWrap(True)

        self._profiles_list = QListWidget()
        self._profiles_list.setObjectName("profilesList")
        self._profiles_list.setSpacing(4)
        self._profiles_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._profiles_list.setMouseTracking(True)
        self._dolphin_query.textChanged.connect(self._schedule_profiles_filter)
        self._dolphin_query.returnPressed.connect(self._refresh_antydetect_profiles)
        self._profiles_list.itemSelectionChanged.connect(
            self._on_profiles_list_selection_changed
        )

        profiles_l.addLayout(profiles_top)
        profiles_l.addWidget(self._profiles_hint)
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
        self._default_browser_combo.currentIndexChanged.connect(
            self._on_default_browser_combo_changed
        )
        browser_pick.addWidget(self._default_browser_combo, 1)

        self._dolphin_headless = QCheckBox("Headless (без окна браузера)")
        self._dolphin_headless.setChecked(True)
        self._dolphin_headless.setToolTip(
            "Если включено — профиль запускается без окна браузера (headless): "
            "и Dolphin, и локальный API."
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
        self._btn_save_youtube = QPushButton("Сохранить ключ")
        self._btn_save_youtube.setObjectName("secondary")
        self._btn_save_youtube.clicked.connect(self._save_youtube_settings)
        self._youtube_settings_status = QLabel("")
        self._youtube_settings_status.setObjectName("hint")
        self._youtube_settings_status.setWordWrap(True)

        gy.addWidget(QLabel("API key (для статистики):"), 0, 0)
        gy.addWidget(self._youtube_api_key, 0, 1)
        gy.addWidget(self._youtube_show_key, 1, 0, 1, 2)
        yt_btns = QHBoxLayout()
        yt_btns.addStretch()
        yt_btns.addWidget(self._btn_save_youtube)
        w_yt_btns = QWidget()
        w_yt_btns.setLayout(yt_btns)
        gy.addWidget(w_yt_btns, 2, 0, 1, 2)
        gy.addWidget(self._youtube_settings_status, 3, 0, 1, 2)

        settings_l.addWidget(settings_title)
        settings_l.addWidget(settings_hint)
        settings_l.addWidget(self._gb_stats_username)
        settings_l.addLayout(browser_pick)
        settings_l.addWidget(self._dolphin_headless)
        settings_l.addWidget(self._gb_antydetect_dolphin)
        settings_l.addWidget(self._gb_antydetect_local)
        settings_l.addWidget(gb_yt)
        settings_l.addStretch()
        self._sync_antydetect_settings_groups_visibility()

        self._stack = QStackedWidget()
        self._stack.addWidget(home)
        self._stack.addWidget(ready)
        self._stack.addWidget(uploaded)
        self._stack.addWidget(profiles)
        self._stack.addWidget(settings)

        self._nav = QListWidget()
        self._nav.setObjectName("sideNav")
        self._nav.setFixedWidth(210)
        self._nav.addItems(["Главная", "Готовые видео", "Залитые видео", "Профили", "Настройки"])
        self._nav.setCurrentRow(0)
        self._nav.currentRowChanged.connect(self._on_nav_row_changed)

        outer = QHBoxLayout(self)
        outer.setSpacing(12)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.addWidget(self._nav)
        outer.addWidget(self._stack, 1)

    def _on_nav_row_changed(self, row: int) -> None:
        self._stack.setCurrentIndex(max(0, min(row, self._stack.count() - 1)))
        if row == 1:
            self._refresh_ready_list()
        if row == 2:
            self._refresh_uploaded_list()
        if row == 3:
            self._refresh_antydetect_profiles()

    def _sorted_uploaded_videos(
        self, videos: list[UploadedVideo], mode: str
    ) -> list[UploadedVideo]:
        m = (mode or "views").strip().lower()
        if m == "likes":

            def key_l(v: UploadedVideo) -> tuple:
                if v.stats_unavailable or v.like_count is None:
                    return (1, 0, v.uploaded_at or "", v.id)
                return (0, -int(v.like_count), v.uploaded_at or "", v.id)

            return sorted(videos, key=key_l)

        def key_v(v: UploadedVideo) -> tuple:
            if v.stats_unavailable or v.view_count is None:
                return (1, 0, v.uploaded_at or "", v.id)
            return (0, -int(v.view_count), v.uploaded_at or "", v.id)

        return sorted(videos, key=key_v)

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
        n_18 = sum(1 for v in videos if _video_might_be_18_plus(v.title, v.description))
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

    def _schedule_uploaded_thumb(self, video_id: str, label: QLabel) -> None:
        vid = (video_id or "").strip()
        if not vid:
            return
        self._uploaded_thumb_labels[vid] = label
        self._uploaded_thumb_pool.start(
            _FetchUploadedThumbRunnable(
                vid, self._uploaded_thumb_gen, self._uploaded_thumb_signals
            )
        )

    def _on_uploaded_thumb_loaded(self, video_id: str, data: object, generation: int) -> None:
        if int(generation) != int(self._uploaded_thumb_gen):
            return
        vid = (video_id or "").strip()
        if not vid:
            return
        lbl = self._uploaded_thumb_labels.get(vid)
        if lbl is None:
            return
        blob = data if isinstance(data, (bytes, bytearray)) else b""
        if blob:
            pm = QPixmap()
            if pm.loadFromData(bytes(blob)):
                scaled = pm.scaled(
                    _UPLOADED_THUMB_W,
                    _UPLOADED_THUMB_H,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                lbl.setPixmap(scaled)
                lbl.setObjectName("uploadedThumb")
                lbl.setText("")
                st = lbl.style()
                if st is not None:
                    st.unpolish(lbl)
                    st.polish(lbl)
                return
        lbl.clear()
        lbl.setPixmap(QPixmap())
        lbl.setText("—")
        lbl.setObjectName("uploadedThumbEmpty")
        st = lbl.style()
        if st is not None:
            st.unpolish(lbl)
            st.polish(lbl)

    def _refresh_uploaded_list(self) -> None:
        if not hasattr(self, "_uploaded_list"):
            return

        self._populate_uploaded_session_filter()

        only_session_id = 0
        try:
            if hasattr(self, "_uploaded_session_filter"):
                only_session_id = int(self._uploaded_session_filter.currentData() or 0)
        except Exception:
            only_session_id = 0

        self._uploaded_thumb_gen += 1
        self._uploaded_thumb_labels.clear()
        self._uploaded_list.clear()

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
            flat.sort(key=lambda v: (v.uploaded_at or "", v.id), reverse=True)

        mode = getattr(self, "_uploaded_sort_mode", "views")
        flat = self._sorted_uploaded_videos(flat, mode)
        self._update_uploaded_side_panel(flat)

        vw = self._uploaded_list.viewport().width()
        w_hint = max(520, vw - 8) if vw > 80 else 560
        row_h = _UPLOADED_THUMB_H + 44

        if not flat:
            it = QListWidgetItem()
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            it.setSizeHint(QSize(w_hint, 80))
            self._uploaded_list.addItem(it)
            empty = QLabel("Нет залитых видео для этого фильтра.")
            empty.setObjectName("hint")
            empty.setWordWrap(True)
            self._uploaded_list.setItemWidget(it, empty)
            return

        for v in flat:
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
            tip = "\n".join(tip_lines)

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
                profile_caption=prof_cap,
                tooltip=tip,
                list_widget=self._uploaded_list,
                parent=self._uploaded_list,
            )
            row_w.activated.connect(self._open_uploaded_url)
            self._uploaded_list.setItemWidget(it, row_w)
            self._schedule_uploaded_thumb((v.video_id or "").strip(), row_w.thumb_label())

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
        vids: list[str] = []
        for i in range(self._uploaded_list.count()):
            it = self._uploaded_list.item(i)
            if it is None:
                continue
            vid = (it.data(Qt.ItemDataRole.UserRole + 1) or "").strip()
            if vid:
                vids.append(vid)
        vids = sorted(set(vids))
        if not vids:
            QMessageBox.information(
                self,
                "Zaliver",
                "В списке нет видео для обновления статистики. "
                "Выберите сессию с роликами или нажмите «Список».",
            )
            return
        key = ""
        if hasattr(self, "_youtube_api_key"):
            key = (self._youtube_api_key.text() or "").strip()

        dlg = QProgressDialog(self)
        dlg.setWindowTitle("Обновление статистики")
        dlg.setLabelText("Подготовка…")
        n = len(vids)
        dlg.setRange(0, max(1, n))
        dlg.setValue(0)
        dlg.setMinimumDuration(0)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        try:
            dlg.setCancelButton(None)
        except (TypeError, AttributeError):
            pass
        self._stats_progress_dlg = dlg
        dlg.show()

        self._btn_uploaded_check.setEnabled(False)
        self._stats_thread = QThread()
        self._stats_worker = UploadedStatsRefreshWorker(vids, key)
        self._stats_worker.moveToThread(self._stats_thread)
        self._stats_thread.started.connect(self._stats_worker.run)
        self._stats_worker.progress.connect(self._on_uploaded_stats_progress)
        self._stats_worker.finished.connect(self._on_uploaded_stats_worker_finished)
        self._stats_thread.finished.connect(self._on_uploaded_stats_thread_finished)
        self._stats_thread.start()

    def _on_uploaded_stats_progress(self, step: int, total: int, video_id: str) -> None:
        dlg = self._stats_progress_dlg
        if dlg is None:
            return
        t = max(1, int(total))
        s = max(0, min(int(step), t))
        dlg.setMaximum(t)
        dlg.setValue(s)
        vid = (video_id or "").strip()
        dlg.setLabelText(f"{s} / {t} — {vid}" if vid else f"{s} / {t}")

    def _on_uploaded_stats_worker_finished(self, successes: object, errors: object) -> None:
        dlg = self._stats_progress_dlg
        if dlg is not None:
            mx = dlg.maximum()
            dlg.setValue(mx)
            dlg.close()
        self._stats_progress_dlg = None
        try:
            succ = successes if isinstance(successes, list) else []
            failures_raw = errors if isinstance(errors, list) else []
            err_lines: list[str] = []
            for item in failures_raw:
                if isinstance(item, (list, tuple)) and len(item) >= 3:
                    vid_e, msg_e, is_api = item[0], item[1], bool(item[2])
                    err_lines.append(f"{vid_e}: {msg_e}")
                    ve = str(vid_e or "").strip()
                    if ve:
                        try:
                            self._upload_store.mark_video_stats_unavailable(
                                video_id=ve,
                                youtube_data_api_error=is_api,
                            )
                        except Exception:
                            pass
                else:
                    line = str(item)
                    err_lines.append(line)
                    ev = _uploaded_stats_error_video_id(line)
                    if ev:
                        is_api = "YoutubeDataApiError" in line
                        try:
                            self._upload_store.mark_video_stats_unavailable(
                                video_id=ev,
                                youtube_data_api_error=is_api,
                            )
                        except Exception:
                            pass
            for row in succ:
                if not isinstance(row, (list, tuple)) or len(row) < 4:
                    continue
                vid, vc, lc, cc = row[0], row[1], row[2], row[3]
                try:
                    self._upload_store.update_video_stats(
                        video_id=str(vid),
                        view_count=int(vc),
                        like_count=lc if lc is None else int(lc),
                        comment_count=cc if cc is None else int(cc),
                    )
                except Exception:
                    pass
            if succ or err_lines:
                self._refresh_uploaded_list()
            if err_lines and not succ:
                detail = "\n".join(str(x) for x in err_lines[:8])
                if len(err_lines) > 8:
                    detail += f"\n… и ещё {len(err_lines) - 8}"
                QMessageBox.warning(
                    self,
                    "Zaliver",
                    f"Не удалось обновить статистику:\n{detail}",
                )
            elif err_lines:
                detail = "\n".join(str(x) for x in err_lines[:5])
                if len(err_lines) > 5:
                    detail += f"\n… и ещё {len(err_lines) - 5}"
                QMessageBox.information(
                    self,
                    "Zaliver",
                    f"Обновлено записей: {len(succ)} из {len(succ) + len(err_lines)}.\n"
                    f"Ошибки по части роликов:\n{detail}",
                )
        finally:
            t = self._stats_thread
            if t is not None:
                t.quit()

    def _on_uploaded_stats_thread_finished(self) -> None:
        self._stats_thread = None
        if self._stats_worker is not None:
            self._stats_worker.deleteLater()
            self._stats_worker = None
        if self._stats_progress_dlg is not None:
            self._stats_progress_dlg.close()
            self._stats_progress_dlg = None
        if hasattr(self, "_btn_uploaded_check"):
            self._btn_uploaded_check.setEnabled(True)

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

    def _on_output_saved(self, path: str) -> None:
        if isinstance(path, str) and path.strip():
            self._just_saved_outputs.append(path.strip())
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

    def _prompt_title_desc_and_profile(self) -> dict[str, str] | None:
        profiles = self._profiles_raw or []
        if not profiles:
            # Без pop-up: просто инициируем загрузку и даём подсказку в статусе.
            try:
                self._profiles_status.setText("Профили ещё не загружены — запускаю загрузку…")
            except Exception:
                pass
            self._refresh_antydetect_profiles()
            return None

        dlg = QDialog(self)
        dlg.setWindowTitle("Загрузка в YouTube после уникализации")
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
        lw.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
        try:
            if hasattr(self, "_profiles_list"):
                lw_m = self._profiles_list
                for sel in lw_m.selectedItems():
                    pids = str(sel.data(Qt.ItemDataRole.UserRole + 1) or "").strip()
                    if pids:
                        preselect.add(pids)
                if not preselect:
                    it0 = lw_m.currentItem()
                    if it0 is not None:
                        pid0 = str(it0.data(Qt.ItemDataRole.UserRole + 1) or "").strip()
                        if pid0:
                            preselect.add(pid0)
        except Exception:
            pass

        last_upload_map = self._upload_store.last_uploaded_at_by_profiles(ids)
        dlg_drag: dict[str, object] = {"anchor": None, "extending": False}

        for pid, p in profile_rows:
            lw_item = QListWidgetItem()
            lw_item.setData(Qt.ItemDataRole.UserRole, p)
            lw_item.setData(Qt.ItemDataRole.UserRole + 1, pid)
            lw_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

            row_w = AnticProfileRow(
                p,
                lw,
                last_uploaded_at=last_upload_map.get(pid),
                on_left_press=lambda e, li=lw_item: self._profile_list_row_mouse_press(
                    lw, li, e, dlg_drag
                ),
                on_left_drag=lambda e: self._profile_list_row_mouse_drag(lw, e, dlg_drag),
                on_left_release=lambda e: self._profile_list_row_mouse_release(e, dlg_drag),
                on_upload_pause_click=lambda pid=pid: self._ask_reset_upload_cooldown_for_profile(
                    pid, dialog_parent=dlg, dialog_profile_list=lw
                ),
                select_checkbox_item=lw_item,
            )
            lw.addItem(lw_item)
            lw.setItemWidget(lw_item, row_w)
            row_w.updateGeometry()
            lw_item.setSizeHint(row_w.sizeHint())

        lw.blockSignals(True)
        first_sel: QListWidgetItem | None = None
        for i in range(lw.count()):
            it_sel = lw.item(i)
            if it_sel is None:
                continue
            pids = str(it_sel.data(Qt.ItemDataRole.UserRole + 1) or "").strip()
            if pids and pids in preselect:
                it_sel.setSelected(True)
                if first_sel is None:
                    first_sel = it_sel
        if first_sel is not None:
            lw.setCurrentItem(first_sel)
        lw.blockSignals(False)

        n_inputs = len(self._selected_input_files or [])
        try:
            copies_n = max(1, int(self.copies_per_file.value()))
        except Exception:
            copies_n = 1
        uniquify_planned = n_inputs * copies_n

        dlg_profile_count_lbl = QLabel("")
        dlg_profile_count_lbl.setObjectName("hint")
        dlg_profile_count_lbl.setWordWrap(True)

        def _update_dlg_upload_profile_count() -> None:
            n = len(lw.selectedItems())
            uniq_line = f"Будет уникализировано видео: {uniquify_planned}"
            if n <= 0:
                dlg_profile_count_lbl.setText(
                    uniq_line
                    + "\nВыбрано профилей для залива: 0 — без залива в YouTube "
                    "(только уникализация)."
                )
            else:
                dlg_profile_count_lbl.setText(
                    uniq_line + f"\nВыбрано профилей для залива: {n}"
                )

        def _sync_dlg_profile_checkboxes() -> None:
            for i in range(lw.count()):
                it_cb = lw.item(i)
                if it_cb is None:
                    continue
                rw_cb = lw.itemWidget(it_cb)
                if isinstance(rw_cb, AnticProfileRow):
                    rw_cb.sync_select_checkbox_from_item()

        def _on_dlg_profile_selection_changed() -> None:
            _sync_dlg_profile_checkboxes()
            _update_dlg_upload_profile_count()

        lw.itemSelectionChanged.connect(_on_dlg_profile_selection_changed)
        _sync_dlg_profile_checkboxes()
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
        picked: list[str] = []
        for i in range(lw.count()):
            it = lw.item(i)
            if it is None:
                continue
            try:
                if it.isSelected():
                    pid = str(it.data(Qt.ItemDataRole.UserRole + 1) or "").strip()
                    if pid:
                        picked.append(pid)
            except Exception:
                continue

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

    def _on_default_browser_combo_changed(self, _index: int) -> None:
        self._update_profiles_section_header()
        self._sync_antydetect_settings_groups_visibility()

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
        self._gb_antydetect_local.setVisible(not show_dolphin)

    def _update_profiles_section_header(self) -> None:
        if not hasattr(self, "_profiles_title"):
            return
        kind = self._default_browser_combo.currentData()
        if not isinstance(kind, str) or not kind:
            kind = "dolphin"
        if kind == "local":
            self._profiles_title.setText("Профили (локальный антидетект)")
            self._profiles_hint.setText(
                "Клик по строке — запуск профиля и сценарий YouTube Studio."
            )
            if hasattr(self, "_dolphin_query"):
                self._dolphin_query.setPlaceholderText(
                    "Поиск по загруженным профилям (имя, ID, движок)…"
                )
        else:
            self._profiles_title.setText("Профили Dolphin Anty")
            self._profiles_hint.setText(
                "Подгрузка профилей через глобальный Public API Dolphin{anty} "
            )
            if hasattr(self, "_dolphin_query"):
                self._dolphin_query.setPlaceholderText("Поиск по загруженным профилям…")

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

    @staticmethod
    def _profile_matches(profile: dict[str, object], needle: str) -> bool:
        q = (needle or "").strip().lower()
        if not q:
            return True
        return q in MainWindow._profile_search_blob(profile)

    def _render_profiles_items(self, profiles: list[dict[str, object]]) -> int:
        self._profiles_list_populating = True
        self._profiles_list.blockSignals(True)
        n = 0
        try:
            self._profiles_list.clear()
            pids: list[str] = []
            for it in profiles:
                pid = str(
                    it.get("id")
                    or it.get("browserProfileId")
                    or it.get("profile_id")
                    or ""
                ).strip()
                if pid:
                    pids.append(pid)
            last_upload_map = self._upload_store.last_uploaded_at_by_profiles(pids)
            for it in profiles:
                pid = str(
                    it.get("id")
                    or it.get("browserProfileId")
                    or it.get("profile_id")
                    or ""
                ).strip()
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, it)
                item.setData(Qt.ItemDataRole.UserRole + 1, pid)

                row = AnticProfileRow(
                    it,
                    self._profiles_list,
                    last_uploaded_at=last_upload_map.get(pid) if pid else None,
                    on_left_press=lambda e, li=item: self._profiles_row_mouse_press(e, li),
                    on_left_drag=self._profiles_row_mouse_drag,
                    on_left_release=self._profiles_row_mouse_release,
                    on_upload_pause_click=(
                        lambda pid=pid: self._ask_reset_upload_cooldown_for_profile(pid)
                    ),
                )
                self._profiles_list.addItem(item)
                self._profiles_list.setItemWidget(item, row)
                row.updateGeometry()
                item.setSizeHint(row.sizeHint())
                n += 1
        finally:
            self._profiles_list.blockSignals(False)
            self._profiles_list_render_gen += 1
            self._profiles_list_populating = False
        if hasattr(self, "_dolphin_query") and (self._dolphin_query.text() or "").strip():
            self._profiles_filter_timer.start(0)
        return n

    def _schedule_profiles_filter(self) -> None:
        if not hasattr(self, "_profiles_list"):
            return
        if self._profiles_raw is None:
            return
        self._profiles_filter_timer.start(150)

    def _apply_profiles_filter(self) -> None:
        if not hasattr(self, "_profiles_list"):
            return
        if self._profiles_list_populating:
            return
        raw = self._profiles_raw
        if raw is None:
            return

        q = (self._dolphin_query.text() if hasattr(self, "_dolphin_query") else "") or ""
        q = q.strip()
        filtered = [p for p in raw if isinstance(p, dict) and self._profile_matches(p, q)]

        shown = self._render_profiles_items(filtered)
        total = len(raw)
        if q:
            self._profiles_status.setText(f"Фильтр: показано {shown} из {total}")
        else:
            self._profiles_status.setText(f"Загружено профилей: {total}")

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
        base_url = (self._local_api_base_url.text() or "").strip()
        if not base_url:
            base_url = (
                self._settings.value("antydetect/local_api_base_url", "", type=str) or ""
            ).strip()
        if not base_url and kind == "local":
            base_url = DEFAULT_LOCAL_API_BASE_URL

        self._btn_profiles_refresh.setEnabled(False)
        self._profiles_status.setText("Загрузка профилей…")

        t = threading.Thread(
            target=self._profiles_worker,
            kwargs={"kind": kind, "token": token, "base_url": base_url},
            daemon=True,
        )
        t.start()

    def _profiles_worker(self, *, kind: str, token: str, base_url: str) -> None:
        try:
            if kind == "local":
                u = (base_url or "").strip()
                if not u:
                    self._profiles_load_failed.emit(
                        "Укажите базовый URL локального API в настройках (раздел «Свой антидетект») и сохраните."
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
                "Проверьте, что локальный сервис запущен и базовый URL верен.\n" + str(e)
            )
        except Exception as e:
            self._profiles_load_failed.emit(repr(e))

    def _on_profiles_loaded(self, profiles_obj: object) -> None:
        self._btn_profiles_refresh.setEnabled(True)
        profiles = profiles_obj if isinstance(profiles_obj, list) else []
        cleaned: list[dict[str, object]] = [p for p in profiles if isinstance(p, dict)]
        self._profiles_raw = cleaned
        self._apply_profiles_filter()

    def _on_profiles_load_failed(self, message: str) -> None:
        self._btn_profiles_refresh.setEnabled(True)
        self._profiles_raw = None
        if hasattr(self, "_profiles_list"):
            self._profiles_list.blockSignals(True)
            try:
                self._profiles_list.clear()
            finally:
                self._profiles_list.blockSignals(False)
            self._profiles_list_render_gen += 1
        self._profiles_status.setText(f"Не удалось загрузить список профилей.\n{message}")

    def _ask_reset_upload_cooldown_for_profile(
        self,
        profile_id: str,
        *,
        dialog_parent: QWidget | None = None,
        dialog_profile_list: QListWidget | None = None,
    ) -> None:
        pid = (profile_id or "").strip()
        if not pid:
            return
        parent = dialog_parent or self
        ans = QMessageBox.question(
            parent,
            "Пауза 1 ч",
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
        if dialog_profile_list is not None:
            new_map = self._upload_store.last_uploaded_at_by_profiles([pid])
            new_iso = new_map.get(pid)
            lw2 = dialog_profile_list
            for i in range(lw2.count()):
                it2 = lw2.item(i)
                if it2 is None:
                    continue
                if str(it2.data(Qt.ItemDataRole.UserRole + 1) or "").strip() != pid:
                    continue
                row_w = lw2.itemWidget(it2)
                if isinstance(row_w, AnticProfileRow):
                    row_w.set_last_upload_cooldown(new_iso)
                break
        elif hasattr(self, "_profiles_list"):
            self._apply_profiles_filter()

    def _profile_list_row_mouse_press(
        self,
        lw: QListWidget,
        item: QListWidgetItem,
        event: QMouseEvent,
        state: dict[str, object],
    ) -> None:
        if lw is self._profiles_list and self._profiles_list_populating:
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        mods = event.modifiers()
        ctrl = mods & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
        )
        shift = mods & Qt.KeyboardModifier.ShiftModifier
        if ctrl:
            item.setSelected(not item.isSelected())
            lw.setCurrentItem(item)
            state["extending"] = False
            return
        if shift:
            anchor = lw.currentItem()
            if anchor is None:
                item.setSelected(True)
                lw.setCurrentItem(item)
            else:
                i_a = lw.row(anchor)
                i_b = lw.row(item)
                top, bottom = sorted((i_a, i_b))
                lw.clearSelection()
                for r in range(top, bottom + 1):
                    ri = lw.item(r)
                    if ri is not None:
                        ri.setSelected(True)
                lw.setCurrentItem(item)
            state["extending"] = False
            return
        lw.clearSelection()
        item.setSelected(True)
        lw.setCurrentItem(item)
        state["anchor"] = lw.row(item)
        state["extending"] = True

    def _profile_list_row_mouse_drag(
        self, lw: QListWidget, event: QMouseEvent, state: dict[str, object]
    ) -> None:
        if lw is self._profiles_list and self._profiles_list_populating:
            return
        if not state.get("extending") or state.get("anchor") is None:
            return
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        gp = event.globalPosition().toPoint()
        loc = lw.viewport().mapFromGlobal(gp)
        hit = lw.itemAt(loc)
        if hit is None:
            return
        r1 = lw.row(hit)
        r0 = int(state["anchor"])
        top, bottom = sorted((r0, r1))
        lw.clearSelection()
        for r in range(top, bottom + 1):
            ri = lw.item(r)
            if ri is not None:
                ri.setSelected(True)
        lw.setCurrentItem(hit)

    def _profile_list_row_mouse_release(
        self, event: QMouseEvent, state: dict[str, object]
    ) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            state["extending"] = False

    def _profiles_row_mouse_press(self, event: QMouseEvent, item: QListWidgetItem) -> None:
        self._profile_list_row_mouse_press(
            self._profiles_list, item, event, self._profiles_drag
        )

    def _profiles_row_mouse_drag(self, event: QMouseEvent) -> None:
        self._profile_list_row_mouse_drag(self._profiles_list, event, self._profiles_drag)

    def _profiles_row_mouse_release(self, event: QMouseEvent) -> None:
        self._profile_list_row_mouse_release(event, self._profiles_drag)

    def _on_profiles_list_selection_changed(self) -> None:
        if self._profiles_list_populating:
            return
        lw = self._profiles_list
        it = lw.currentItem()
        if it is None:
            sel = lw.selectedItems()
            it = sel[0] if sel else None
        if it is None:
            self._profiles_status.setText("Профиль не выбран")
            return
        pid = str(it.data(Qt.ItemDataRole.UserRole + 1) or "").strip()
        if not pid:
            self._profiles_status.setText("У профиля нет ID — запуск через Local API невозможен.")
            return
        self._selected_profile_id = pid
        prof = it.data(Qt.ItemDataRole.UserRole)
        label = ""
        if isinstance(prof, dict):
            label = str(prof.get("name") or "").strip()
        nsel = len(lw.selectedItems())
        if nsel > 1:
            self._profiles_status.setText(
                f"Выбрано профилей: {nsel}. Активный: {label or pid}"
            )
        else:
            self._profiles_status.setText(
                f"Выбран профиль: {label}" if label else f"Выбран профиль: {pid}"
            )

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

            if kind == "local":
                u = (base_url or "").strip()
                if not u:
                    raise LocalAntidetectError(
                        "Сначала укажите базовый URL локального API в настройках."
                    )
                res = open_google_in_local_antidetect_profile(
                    profile_id,
                    base_url=u,
                    headless=headless,
                    video_path=upload_video_path,
                    title=upload_title,
                    description=upload_description,
                )
            else:
                res = open_google_in_profile(
                    profile_id,
                    local_token=token or None,
                    headless=headless,
                    video_path=upload_video_path,
                    title=upload_title,
                    description=upload_description,
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
        if kind == "local":
            hint = (
                "Нужны запущенный локальный API антидетекта, Playwright и сессия Studio в профиле. "
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

    def _thread_cleanup(self) -> None:
        self._work_thread = None
        self._processor = None

    def _cancel(self) -> None:
        if self._processor is not None:
            self._processor.cancel()
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
                mgr.stop()
        except Exception:
            pass
        kind_u = (getattr(self, "_upload_cancel_kind", "") or "").strip()
        ids = [p for p in getattr(self, "_upload_cancel_profile_ids", []) if str(p).strip()]
        if mgr is not None and kind_u == "local":
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
        if kind_u != "local" and ids:
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

    def _on_youtube_upload_phase_finished(self, status: str) -> None:
        self._upload_manager = None
        self._upload_cancel_profile_ids = []
        self._upload_cancel_kind = ""
        self._upload_cancel_dolphin_token = ""
        self._release_youtube_progress_hold_if_any()
        mx = max(1, int(self.progress.maximum()))
        self.progress.setRange(0, mx)
        self.progress.setValueImmediate(mx)
        self._finalize_idle_toolbar()
        if status == "cancelled":
            self.progress_label.setText("Загрузка на YouTube отменена.")
            self._append_log("YouTube: загрузка отменена пользователем.")
            QMessageBox.information(self, "Zaliver", "Загрузка на YouTube отменена.")
        elif status == "upload_failed":
            self.progress_label.setText("Готово (есть ошибки загрузки на YouTube).")
            self._append_log("YouTube: очередь завершена, часть загрузок завершилась с ошибками.")
        else:
            self.progress_label.setText("Готово")
            self._append_log("YouTube: очередь загрузок завершена.")

    def _on_progress(self, cur: int, total: int, msg: str) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(cur)
        self.progress_label.setText(msg or f"{cur} / {total} кадров")

    def _on_finished(self, ok: bool, msg: str) -> None:
        self._upload_session_processing_done = True

        if not ok:
            self._finalize_idle_toolbar()
            self._release_youtube_progress_hold_if_any()
            self._append_log(f"Ошибка: {msg}")
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

        self._append_log("Уникализация завершена.")
        pending = self._pending_upload
        self._pending_upload = None
        if pending is not None:
            video_paths = [
                p.strip()
                for p in (self._just_saved_outputs or [])
                if isinstance(p, str) and p.strip()
            ]
            if not video_paths:
                self._append_log(
                    "Загрузка в YouTube пропущена: не найден путь к сохранённому видео."
                )
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
            base_url = (self._local_api_base_url.text() or "").strip()
            if not base_url:
                base_url = (
                    self._settings.value(
                        "antydetect/local_api_base_url", "", type=str
                    )
                    or ""
                ).strip()
            if not base_url and kind == "local":
                base_url = DEFAULT_LOCAL_API_BASE_URL

            raw_ids = (pending.get("profile_ids", "") or "").strip()
            profile_ids = [p.strip() for p in raw_ids.split(",") if p.strip()]
            if not profile_ids:
                self._append_log("YouTube: профили не выбраны — заливка пропущена.")
                self._upload_session_upload_expected = False
                self._upload_session_upload_done = True
                self._maybe_finish_upload_session(status="done")
                self._release_youtube_progress_hold_if_any()
                self._finalize_idle_toolbar()
                return

            from zaliver.youtube_upload.multi_uploader import MultiProfileUploader, VideoTask

            self._append_log(
                f"YouTube: многопоточная заливка стартует. Видео={len(video_paths)}, профили={len(profile_ids)}…"
            )
            self.btn_cancel.setEnabled(True)
            self.btn_start.setEnabled(False)
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

                if kind == "local":
                    res = open_google_in_local_antidetect_profile(
                        profile_id,
                        base_url=(base_url or "").strip(),
                        headless=headless,
                        video_path=task.video_path,
                        title=task.title,
                        description=task.description,
                    )
                else:
                    res = open_google_in_profile(
                        profile_id,
                        local_token=token or None,
                        headless=headless,
                        video_path=task.video_path,
                        title=task.title,
                        description=task.description,
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

            def _on_profile_upload_attempt(pid: str, ok: bool, err: str) -> None:
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
                finally:
                    self._upload_session_upload_done = True
                    stopped = False
                    try:
                        stopped = bool(mgr.stop_requested())
                    except Exception:
                        stopped = False
                    status = "done"
                    if stopped:
                        status = "cancelled"
                    else:
                        try:
                            if mgr.done_failed > 0:
                                status = "upload_failed"
                        except Exception:
                            status = "upload_failed"
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

    def _append_log(self, line: str) -> None:
        self.log.appendPlainText(line)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _on_upload_profile_failed_3x(
        self,
        *,
        profile_id: str,
        n: int,
        error_text: str,
        kind: str,
        base_url: str,
    ) -> None:
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

        # Если используем локальный антидетект — помечаем профиль тегом в его «базе» (profiles.json).
        if (kind or "").strip() == "local":
            try:
                from zaliver.antydetect.local_antidetect_api import LocalAntidetectHttpAPI

                api = LocalAntidetectHttpAPI((base_url or "").strip() or DEFAULT_LOCAL_API_BASE_URL)
                try:
                    api.add_profile_tag(pid, "upload_error_3x")
                finally:
                    api.close()
                self._ui_log_line.emit(
                    f"[upload] [PROFILE] profile={pid} tag_added=upload_error_3x"
                )
            except Exception as e:
                self._ui_log_line.emit(
                    f"[upload] [PROFILE] profile={pid} tag_add_failed err={e!r}"
                )
