from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget


def _as_str(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return str(v).strip()


def _profile_id(profile: dict[str, object]) -> str:
    return _as_str(
        profile.get("id")
        or profile.get("browserProfileId")
        or profile.get("profile_id")
    )


def _profile_name(profile: dict[str, object]) -> str:
    name = _as_str(profile.get("name"))
    return name or "Без названия"


def _profile_status(profile: dict[str, object]) -> str:
    st = profile.get("status")
    if isinstance(st, dict):
        return _as_str(st.get("name") or st.get("title") or st.get("id"))
    return _as_str(st) or _as_str(profile.get("statusId"))


def _profile_tag_list(profile: dict[str, object], *, limit: int = 24) -> list[str]:
    tags = profile.get("tags")
    if not isinstance(tags, list) or not tags:
        return []
    out: list[str] = []
    for t in tags:
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
        elif isinstance(t, dict):
            s = _as_str(t.get("name") or t.get("title") or t.get("tag") or t.get("id"))
            if s:
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


class DolphinProfileRow(QWidget):
    """One profile row for QListWidget (card-like layout)."""

    def __init__(
        self,
        profile: dict[str, object],
        parent: QWidget | None = None,
        *,
        on_left_press: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_left_press = on_left_press
        self.setObjectName("dolphinProfileRowRoot")

        self.setWindowFlag(Qt.WindowType.Window, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, False)

        # Оптимизация отрисовки для QListWidget
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        self.setObjectName("dolphinProfileRowRoot")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # Убираем фокус, чтобы не создавать дополнительные окна
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)


        pid = _profile_id(profile)
        name = _profile_name(profile)
        status = _profile_status(profile)
        tag_strings = _profile_tag_list(profile)
        tags_tip = ", ".join(tag_strings) if tag_strings else ""
        description = _profile_description(profile)
        site = _profile_main_site(profile)
        proxy_text, proxy_kind, proxy_extra = _proxy_state(profile)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        accent = QFrame()
        accent.setObjectName("dolphinProfileAccent")
        accent.setFixedWidth(6)

        card = QFrame()
        card.setObjectName("dolphinProfileCard")
        card_l = QHBoxLayout(card)
        card_l.setContentsMargins(14, 12, 14, 12)
        card_l.setSpacing(14)

        left = QVBoxLayout()
        left.setSpacing(6)

        filter_widgets: list[QWidget] = []

        tags_per_row = 5
        title_row = QHBoxLayout()
        title_row.setSpacing(10)

        title = QLabel(name)
        title.setObjectName("dolphinProfileTitle")
        title.setWordWrap(True)
        title.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        title.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        filter_widgets.append(title)
        title_row.addWidget(title, 1)

        if tag_strings:
            tags_col = QWidget()
            tags_col.setSizePolicy(
                QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum
            )
            tags_v = QVBoxLayout(tags_col)
            tags_v.setContentsMargins(0, 0, 0, 0)
            tags_v.setSpacing(4)
            for i in range(0, len(tag_strings), tags_per_row):
                chunk = tag_strings[i : i + tags_per_row]
                row_w = QWidget()
                row_w.setSizePolicy(
                    QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum
                )
                row_l = QHBoxLayout(row_w)
                row_l.setContentsMargins(0, 0, 0, 0)
                row_l.setSpacing(6)
                row_l.addStretch(1)
                for text in chunk:
                    chip = QLabel(text)
                    chip.setObjectName("dolphinProfileTag")
                    chip.setSizePolicy(
                        QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed
                    )
                    row_l.addWidget(chip, 0, Qt.AlignmentFlag.AlignRight)
                    filter_widgets.append(chip)
                tags_v.addWidget(row_w)
                filter_widgets.append(row_w)
            title_row.addWidget(
                tags_col, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
            )
            filter_widgets.append(tags_col)

        left.addLayout(title_row)

        if description:
            desc_lbl = QLabel(description)
            desc_lbl.setObjectName("dolphinProfileDescription")
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
        subtitle.setObjectName("dolphinProfileSubtitle")
        subtitle.setWordWrap(True)
        if subtitle_parts:
            filter_widgets.append(subtitle)

        meta = QHBoxLayout()
        meta.setSpacing(10)

        id_lbl = QLabel(f"ID {pid}" if pid else "ID —")
        id_lbl.setObjectName("dolphinProfileId")

        proxy_lbl = QLabel(proxy_text)
        proxy_lbl.setObjectName("dolphinProfileProxy")
        proxy_lbl.setProperty("proxyState", proxy_kind)

        st_lbl = QLabel(status if status else "")
        st_lbl.setObjectName("dolphinProfileStatus")

        meta.addWidget(id_lbl, 0, Qt.AlignmentFlag.AlignLeft)
        meta.addWidget(proxy_lbl, 0, Qt.AlignmentFlag.AlignLeft)
        meta.addWidget(st_lbl, 0, Qt.AlignmentFlag.AlignLeft)
        meta.addStretch(1)

        if subtitle_parts:
            left.addWidget(subtitle)
        left.addLayout(meta)

        filter_widgets.extend((id_lbl, proxy_lbl, st_lbl))

        card_l.addLayout(left, 1)

        outer.addWidget(accent)
        outer.addWidget(card, 1)

        tip_lines = [
            f"Название: {name}",
            f"ID: {pid}" if pid else "ID: —",
        ]
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

        self.setToolTip("\n".join(tip_lines))

        min_h = 78
        if description:
            min_h += 22
        if tag_strings:
            rows = (len(tag_strings) + tags_per_row - 1) // tags_per_row
            min_h = max(min_h, 52 + rows * 24)
        self.setMinimumHeight(min_h)

        for w in (accent, card, *filter_widgets):
            w.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if (
            self._on_left_press is not None
            and event.type() == QEvent.Type.MouseButtonPress
            and isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._on_left_press()
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if (
            self._on_left_press is not None
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._on_left_press()
        super().mousePressEvent(event)
