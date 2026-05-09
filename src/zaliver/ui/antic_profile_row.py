from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from PyQt6.QtCore import QEvent, QObject, QSize, Qt
from PyQt6.QtGui import QCursor, QMouseEvent
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget


def _as_str(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return str(v).strip()


def _profile_id(profile: dict[str, object]) -> str:
    return _as_str(profile.get("id") or profile.get("browserProfileId") or profile.get("profile_id"))


def _profile_name(profile: dict[str, object]) -> str:
    name = _as_str(profile.get("name"))
    return name or "Без названия"


def _profile_status(profile: dict[str, object]) -> str:
    st = profile.get("status")
    if isinstance(st, dict):
        s = _as_str(st.get("name") or st.get("title") or st.get("id"))
    else:
        s = _as_str(st) or _as_str(profile.get("statusId"))
    sl = s.strip().lower()
    if not sl:
        return ""
    if sl.startswith("автоматизация") or sl == "автоматизация":
        return ""
    return s


def _clean_tag_visible(s: str) -> str:
    if not isinstance(s, str):
        s = str(s)
    return (
        s.replace("\ufeff", "")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .strip()
    )


# Тег «автоматизация» / «Автоматизация: да» и пустые оболочки после фильтра — не показываем.
_SKIP_AUTOMATION_TAG = re.compile(
    r"^\s*автоматизация\s*([:.,;—\-–]*\s*(да|нет)?\s*)?$",
    re.IGNORECASE | re.UNICODE,
)
_SKIP_AUTOMATION_EN = re.compile(
    r"^\s*automation\s*([:.,;—\-–]*\s*(yes|no)?\s*)?$",
    re.IGNORECASE | re.UNICODE,
)


def _tag_label_skip(s: str) -> bool:
    raw = _clean_tag_visible(s)
    if not raw:
        return True
    low = raw.lower()
    if low == "автоматизация" or low.startswith("автоматизация"):
        return True
    if low == "automation" or low.startswith("automation"):
        return True
    if _SKIP_AUTOMATION_TAG.match(raw) or _SKIP_AUTOMATION_EN.match(raw):
        return True
    return False


def _strip_automation_tail(s: str) -> str:
    """Убирает хвост «· Автоматизация» / «- Automation» у составных подписей тегов."""
    raw = _clean_tag_visible(s)
    if not raw:
        return ""
    raw = re.sub(
        r"[\s·•,;—–\-]+автоматизация(?:[:,\s]+(да|нет))?\s*$",
        "",
        raw,
        count=1,
        flags=re.IGNORECASE,
    )
    raw = re.sub(
        r"[\s·•,;—–\-]+automation(?:[:,\s]+(yes|no))?\s*$",
        "",
        raw,
        count=1,
        flags=re.IGNORECASE,
    )
    return raw.strip(" ·•,;—–-")


def _profile_tag_list(profile: dict[str, object], *, limit: int = 24) -> list[str]:
    tags = profile.get("tags")
    if not isinstance(tags, list) or not tags:
        return []
    out: list[str] = []
    for t in tags:
        if isinstance(t, str):
            s = _clean_tag_visible(t)
        elif isinstance(t, dict):
            s = _clean_tag_visible(
                _as_str(t.get("name") or t.get("title") or t.get("tag") or t.get("id"))
            )
        else:
            continue
        s = _strip_automation_tail(s)
        if not s or _tag_label_skip(s):
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _profile_description(profile: dict[str, object]) -> str:
    return _as_str(profile.get("description"))


def _profile_main_site(profile: dict[str, object]) -> str:
    return _as_str(profile.get("mainWebsite"))


def _proxy_last_check(profile: dict[str, object]) -> dict[str, object] | None:
    proxy = profile.get("proxy")
    if not isinstance(proxy, dict):
        return None
    lc = proxy.get("lastCheck")
    return lc if isinstance(lc, dict) else None


def _parse_uploaded_at_iso(s: str) -> datetime | None:
    t = (s or "").strip()
    if not t:
        return None
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_upload_cooldown_line(last_uploaded_iso: str | None) -> tuple[str, str]:
    """
    Returns (label, kind) for QLabel property uploadCooldown:
    ok — прошёл час; wait — ещё ждать; none — заливов не было.
    """
    if not (last_uploaded_iso or "").strip():
        return "Пауза 1 ч: заливов не было", "none"
    dt = _parse_uploaded_at_iso(last_uploaded_iso)
    if dt is None:
        return "Пауза 1 ч: дата залива неизвестна", "unknown"
    now = datetime.now(tz=timezone.utc)
    delta = now - dt
    if delta >= timedelta(hours=1):
        return "Пауза 1 ч: можно заливать", "ok"
    rem = timedelta(hours=1) - delta
    mins = max(1, int(rem.total_seconds() // 60))
    return f"Пауза 1 ч: ждите ещё ~{mins} мин", "wait"


def _proxy_state(profile: dict[str, object]) -> tuple[str, str, str]:
    """
    Returns (label, kind, tooltip_extra).

    kind:
    - none: no proxy configured
    - ok: last connectivity check passed
    - bad: last connectivity check failed
    - unknown: not checked / unknown
    """
    proxy = profile.get("proxy")
    if not isinstance(proxy, dict):
        return "Прокси: нет", "none", ""

    head = "Прокси:"

    lc = _proxy_last_check(profile)
    if lc and ("status" in lc):
        ok = bool(lc.get("status"))
        ip = _as_str(lc.get("ip"))
        created = _as_str(lc.get("createdAt"))
        extra_bits = [b for b in (ip, created) if b]
        extra = "\n".join(extra_bits)
        if ok:
            return f"{head} · активен", "ok", extra
        return f"{head} · не активен", "bad", extra

    return f"{head} · не проверен", "unknown", ""


def _tag_chip_column_min_height(n_tags: int, tags_per_row: int) -> int:
    """Минимальная высота колонки фиолетовых чипов (строки × высота чипа + отступы)."""
    if n_tags <= 0 or tags_per_row <= 0:
        return 0
    n_rows = (n_tags + tags_per_row - 1) // tags_per_row
    chip_row = 34
    vgap = 6
    return n_rows * chip_row + max(0, n_rows - 1) * vgap


class AnticProfileRow(QWidget):
    """One profile row for QListWidget (card-like layout)."""

    def __init__(
        self,
        profile: dict[str, object],
        parent: QWidget | None = None,
        *,
        last_uploaded_at: str | None = None,
        on_left_press: Callable[[QMouseEvent], None] | None = None,
        on_left_drag: Callable[[QMouseEvent], None] | None = None,
        on_left_release: Callable[[QMouseEvent], None] | None = None,
        on_upload_pause_click: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_left_press = on_left_press
        self._on_left_drag = on_left_drag
        self._on_left_release = on_left_release
        self._upload_pause_cb = on_upload_pause_click
        self._upload_lbl: QLabel | None = None
        self._upload_cooldown_kind: str = ""
        self.setObjectName("anticProfileRowRoot")

        self.setWindowFlag(Qt.WindowType.Window, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, False)

        # Оптимизация отрисовки для QListWidget
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        self.setObjectName("anticProfileRowRoot")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )

        # Убираем фокус, чтобы не создавать дополнительные окна
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        pid = _profile_id(profile)
        name = _profile_name(profile)
        status = _profile_status(profile)
        tag_strings = _profile_tag_list(profile)
        tags_tip = ", ".join(tag_strings) if tag_strings else ""
        upload_text, upload_kind = format_upload_cooldown_line(last_uploaded_at)
        self._upload_cooldown_kind = upload_kind
        description = _profile_description(profile)
        site = _profile_main_site(profile)
        proxy_text, proxy_kind, proxy_extra = _proxy_state(profile)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        accent = QFrame()
        accent.setObjectName("anticProfileAccent")
        accent.setFixedWidth(8)

        card = QFrame()
        card.setObjectName("anticProfileCard")
        card_l = QHBoxLayout(card)
        card_l.setContentsMargins(16, 18, 16, 18)
        card_l.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(10)

        filter_widgets: list[QWidget] = []

        tags_per_row = 5
        title_row = QHBoxLayout()
        title_row.setSpacing(12)

        title = QLabel(name)
        title.setObjectName("anticProfileTitle")
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        filter_widgets.append(title)
        title_row.addWidget(title, 1)

        had_chips = False
        if tag_strings:
            tags_col = QWidget()
            tags_col.setSizePolicy(
                QSizePolicy.Policy.Maximum,
                QSizePolicy.Policy.Minimum,
            )
            tags_v = QVBoxLayout(tags_col)
            tags_v.setContentsMargins(0, 0, 0, 0)
            tags_v.setSpacing(6)
            for i in range(0, len(tag_strings), tags_per_row):
                chunk = tag_strings[i : i + tags_per_row]
                row_w = QWidget()
                row_w.setSizePolicy(
                    QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum
                )
                row_l = QHBoxLayout(row_w)
                row_l.setContentsMargins(0, 0, 0, 0)
                row_l.setSpacing(6)
                row_w.setMinimumHeight(30)
                placed = 0
                for text in chunk:
                    display = _strip_automation_tail(_clean_tag_visible(text))
                    if not display or _tag_label_skip(display):
                        continue
                    chip = QLabel(display)
                    chip.setObjectName("anticProfileTag")
                    chip.setMinimumHeight(28)
                    chip.setAlignment(
                        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter
                    )
                    chip.setSizePolicy(
                        QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed
                    )
                    row_l.addWidget(chip, 0, Qt.AlignmentFlag.AlignLeft)
                    filter_widgets.append(chip)
                    placed += 1
                if placed == 0:
                    continue
                tags_v.addWidget(row_w)
                filter_widgets.append(row_w)
            if tags_v.count() > 0:
                had_chips = True
                tags_col.setMinimumHeight(
                    _tag_chip_column_min_height(len(tag_strings), tags_per_row)
                )
                title_row.addWidget(
                    tags_col, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
                )
                filter_widgets.append(tags_col)

        left.addLayout(title_row)

        if description:
            desc_lbl = QLabel(description)
            desc_lbl.setObjectName("anticProfileDescription")
            desc_lbl.setWordWrap(True)
            desc_lbl.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
            )
            left.addWidget(desc_lbl)
            filter_widgets.append(desc_lbl)

        subtitle_parts: list[str] = []
        if site:
            subtitle_parts.append(site)
        subtitle = QLabel(" · ".join(subtitle_parts)) if subtitle_parts else QLabel("")
        subtitle.setObjectName("anticProfileSubtitle")
        subtitle.setWordWrap(True)
        if subtitle_parts:
            filter_widgets.append(subtitle)

        meta = QHBoxLayout()
        meta.setSpacing(12)

        id_lbl = QLabel(f"ID {pid}" if pid else "ID —")
        id_lbl.setObjectName("anticProfileId")

        proxy_lbl = QLabel(proxy_text)
        proxy_lbl.setObjectName("anticProfileProxy")
        proxy_lbl.setProperty("proxyState", proxy_kind)

        st_lbl: QLabel | None = None
        if (status or "").strip():
            st_lbl = QLabel(status)
            st_lbl.setObjectName("anticProfileStatus")

        upload_lbl = QLabel(upload_text)
        upload_lbl.setObjectName("anticProfileUpload")
        upload_lbl.setProperty("uploadCooldown", upload_kind)
        self._upload_lbl = upload_lbl
        if on_upload_pause_click and upload_kind == "wait":
            upload_lbl.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            upload_lbl.setToolTip(
                "Нажмите, чтобы обновить время паузы с последнего залива "
                "(как если бы прошёл час) и снова разрешить загрузку с этого профиля."
            )

        meta.addWidget(id_lbl, 0, Qt.AlignmentFlag.AlignLeft)
        meta.addWidget(proxy_lbl, 0, Qt.AlignmentFlag.AlignLeft)
        meta.addWidget(upload_lbl, 0, Qt.AlignmentFlag.AlignLeft)
        if st_lbl is not None:
            meta.addWidget(st_lbl, 0, Qt.AlignmentFlag.AlignLeft)
        meta.addStretch(1)

        if subtitle_parts:
            left.addWidget(subtitle)
        left.addLayout(meta)

        _meta_fw = [id_lbl, proxy_lbl, upload_lbl]
        if st_lbl is not None:
            _meta_fw.append(st_lbl)
        filter_widgets.extend(_meta_fw)

        card_l.addLayout(left, 1)

        outer.addWidget(accent)
        outer.addWidget(card, 1)

        tip_lines = [f"Название: {name}", f"ID: {pid}" if pid else "ID: —"]
        if site:
            tip_lines.append(f"Сайт: {site}")
        if description:
            tip_lines.append(f"Описание: {description}")
        if tags_tip:
            tip_lines.append(f"Теги: {tags_tip}")
        if status:
            tip_lines.append(f"Статус: {status}")
        if proxy_extra:
            tip_lines.append("Прокси (последняя проверка):")
            tip_lines.append(proxy_extra)
        tip_lines.append(upload_text)

        self.setToolTip("\n".join(tip_lines))

        min_h = 100 + 36
        if description:
            min_h += 36
        if had_chips:
            tag_col_h = _tag_chip_column_min_height(len(tag_strings), tags_per_row)
            min_h = max(min_h, 88 + tag_col_h)
        lay_outer = self.layout()
        if lay_outer is not None:
            min_h = max(min_h, lay_outer.minimumSize().height())
        self.setMinimumHeight(min_h)

        for w in (accent, card, *filter_widgets):
            w.installEventFilter(self)

        self.updateGeometry()

    def set_last_upload_cooldown(self, last_uploaded_iso: str | None) -> None:
        """Обновляет подпись «Пауза 1 ч» после смены времени последнего залива в БД."""
        if self._upload_lbl is None:
            return
        text, kind = format_upload_cooldown_line(last_uploaded_iso)
        self._upload_lbl.setText(text)
        self._upload_lbl.setProperty("uploadCooldown", kind)
        self._upload_cooldown_kind = kind
        self._upload_lbl.style().unpolish(self._upload_lbl)
        self._upload_lbl.style().polish(self._upload_lbl)
        if self._upload_pause_cb and kind == "wait":
            self._upload_lbl.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self._upload_lbl.setToolTip(
                "Нажмите, чтобы обновить время паузы с последнего залива "
                "(как если бы прошёл час) и снова разрешить загрузку с этого профиля."
            )
        else:
            self._upload_lbl.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            self._upload_lbl.setToolTip("")

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        lay = self.layout()
        base = super().minimumSizeHint()
        if lay is None:
            return base
        mh = lay.minimumSize()
        h = max(int(base.height()), int(mh.height()))
        w = max(int(base.width()), int(mh.width()))
        return QSize(w, h)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        lay = self.layout()
        base = super().sizeHint()
        if lay is None:
            return base
        lh = lay.sizeHint()
        m = self.minimumSizeHint()
        h = max(int(base.height()), int(lh.height()), int(m.height()))
        w = max(int(base.width()), int(lh.width()), int(m.width()))
        return QSize(w, h)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if isinstance(event, QMouseEvent):
            if (
                self._upload_lbl is not None
                and watched is self._upload_lbl
                and event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and self._upload_pause_cb is not None
                and self._upload_cooldown_kind == "wait"
            ):
                self._upload_pause_cb()
            if (
                self._on_left_press is not None
                and event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._on_left_press(event)
            if (
                self._on_left_drag is not None
                and event.type() == QEvent.Type.MouseMove
                and event.buttons() & Qt.MouseButton.LeftButton
            ):
                self._on_left_drag(event)
            if (
                self._on_left_release is not None
                and event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._on_left_release(event)
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._on_left_press is not None and event.button() == Qt.MouseButton.LeftButton:
            self._on_left_press(event)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if (
            self._on_left_drag is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self._on_left_drag(event)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if self._on_left_release is not None and event.button() == Qt.MouseButton.LeftButton:
            self._on_left_release(event)
        super().mouseReleaseEvent(event)

