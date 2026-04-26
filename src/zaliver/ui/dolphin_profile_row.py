from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget


def _as_str(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return str(v).strip()


def _profile_id(profile: dict[str, object]) -> str:
    return _as_str(profile.get("id") or profile.get("browserProfileId"))


def _profile_name(profile: dict[str, object]) -> str:
    name = _as_str(profile.get("name"))
    return name or "Без названия"


def _profile_status(profile: dict[str, object]) -> str:
    st = profile.get("status")
    if isinstance(st, dict):
        return _as_str(st.get("name") or st.get("title") or st.get("id"))
    return _as_str(st) or _as_str(profile.get("statusId"))


def _profile_tags(profile: dict[str, object]) -> str:
    tags = profile.get("tags")
    if not isinstance(tags, list) or not tags:
        return ""
    out: list[str] = []
    for t in tags:
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
        elif isinstance(t, dict):
            s = _as_str(t.get("name") or t.get("title") or t.get("tag") or t.get("id"))
            if s:
                out.append(s)
    return ", ".join(out[:6])


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

    def __init__(self, profile: dict[str, object], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dolphinProfileRowRoot")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        pid = _profile_id(profile)
        name = _profile_name(profile)
        status = _profile_status(profile)
        tags = _profile_tags(profile)
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

        title = QLabel(name)
        title.setObjectName("dolphinProfileTitle")
        title.setWordWrap(True)

        subtitle_parts: list[str] = []
        if site:
            subtitle_parts.append(site)
        if tags:
            subtitle_parts.append(f"теги: {tags}")
        subtitle = QLabel(" · ".join(subtitle_parts)) if subtitle_parts else QLabel("")
        subtitle.setObjectName("dolphinProfileSubtitle")
        subtitle.setVisible(bool(subtitle_parts))
        subtitle.setWordWrap(True)

        meta = QHBoxLayout()
        meta.setSpacing(10)

        id_lbl = QLabel(f"ID {pid}" if pid else "ID —")
        id_lbl.setObjectName("dolphinProfileId")

        proxy_lbl = QLabel(proxy_text)
        proxy_lbl.setObjectName("dolphinProfileProxy")
        proxy_lbl.setProperty("proxyState", proxy_kind)

        st_lbl = QLabel(status if status else "")
        st_lbl.setObjectName("dolphinProfileStatus")
        st_lbl.setVisible(bool(status))

        meta.addWidget(id_lbl, 0, Qt.AlignmentFlag.AlignLeft)
        meta.addWidget(proxy_lbl, 0, Qt.AlignmentFlag.AlignLeft)
        meta.addWidget(st_lbl, 0, Qt.AlignmentFlag.AlignLeft)
        meta.addStretch(1)

        left.addWidget(title)
        if subtitle_parts:
            left.addWidget(subtitle)
        left.addLayout(meta)

        card_l.addLayout(left, 1)

        outer.addWidget(accent)
        outer.addWidget(card, 1)

        tip_lines = [
            f"Название: {name}",
            f"ID: {pid}" if pid else "ID: —",
        ]
        if site:
            tip_lines.append(f"Сайт: {site}")
        if tags:
            tip_lines.append(f"Теги: {tags}")
        if status:
            tip_lines.append(f"Статус: {status}")
        if proxy_extra:
            tip_lines.append("Прокси (последняя проверка):")
            tip_lines.append(proxy_extra)
        self.setToolTip("\n".join([t for t in tip_lines if t.strip()]))

        self.setMinimumHeight(78)
