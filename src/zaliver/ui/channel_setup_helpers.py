"""Общие утилиты для настройки канала (диалог и вкладка)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

_SMALL_PREVIEW_MAX_SIDE = 520


def recent_editable_combo(*, placeholder: str, recent: list[str]) -> QComboBox:
    combo = QComboBox()
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    line_edit = combo.lineEdit()
    if line_edit is not None:
        line_edit.setPlaceholderText(placeholder)
    for value in recent:
        combo.addItem(value)
    combo.setCurrentIndex(-1)
    if line_edit is not None:
        line_edit.clear()
    return combo


def format_recent_picker_label(value: str, *, max_len: int = 120) -> str:
    raw = str(value)
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) > 1:
        head = lines[0]
        if len(head) > max_len:
            head = head[: max_len - 1] + "…"
        return f"{head}  ·  {len(lines)} строк"
    one_line = " ".join(raw.splitlines())
    if len(one_line) > max_len:
        return one_line[: max_len - 1] + "…"
    return one_line


def recent_values_picker(*, recent: list[str], tooltip: str = "") -> QToolButton:
    btn = QToolButton()
    btn.setObjectName("recentValuesPicker")
    btn.setToolTip(tooltip or "Недавно введённые значения")
    btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    btn.setFixedSize(34, 28)
    fill_recent_values_picker(btn, recent)
    return btn


def recent_picker_has_items(picker: QToolButton) -> bool:
    menu = picker.menu()
    return bool(menu and menu.actions())


def fill_recent_values_picker(picker: QToolButton, recent: list[str]) -> None:
    menu = picker.menu()
    if menu is None:
        menu = QMenu(picker)
        menu.setObjectName("recentValuesMenu")
        picker.setMenu(menu)
    else:
        menu.clear()
    seen: set[str] = set()
    for value in recent:
        text = str(value).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        action = menu.addAction(format_recent_picker_label(text))
        action.setData(text)
        action.setToolTip(text if "\n" in text or len(text) > 120 else "")
    picker.setEnabled(recent_picker_has_items(picker))


def connect_recent_values_picker(
    picker: QToolButton,
    target: QPlainTextEdit | QLineEdit,
    *,
    on_filled=None,
) -> None:
    menu = picker.menu()
    if menu is None:
        return

    def on_pick(action) -> None:
        value = action.data()
        if value is None:
            return
        value = str(value)
        if isinstance(target, QPlainTextEdit):
            target.setPlainText(value)
        else:
            target.setText(value)
        if on_filled is not None:
            on_filled()

    menu.triggered.connect(on_pick)


def make_magic_wand_button(*, tooltip: str = "") -> QToolButton:
    """Кнопка «волшебные частички» рядом с полем (под стрелкой недавних значений)."""
    btn = QToolButton()
    btn.setObjectName("magicWandButton")
    btn.setText("✨")
    btn.setToolTip(tooltip or "Сгенерировать через ИИ")
    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    btn.setFixedSize(34, 28)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setAutoRaise(False)
    return btn


def field_with_recent_picker(
    field: QPlainTextEdit | QLineEdit,
    *,
    recent: list[str],
    tooltip: str = "",
    on_filled=None,
    side_extras: list[QWidget] | None = None,
) -> tuple[QWidget, QToolButton]:
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lay.addWidget(field, 1)
    picker = recent_values_picker(recent=recent, tooltip=tooltip)
    connect_recent_values_picker(picker, field, on_filled=on_filled)

    side = QWidget()
    side_l = QVBoxLayout(side)
    side_l.setContentsMargins(0, 0, 0, 0)
    side_l.setSpacing(4)
    side_l.addWidget(picker, 0, Qt.AlignmentFlag.AlignHCenter)
    for extra in side_extras or []:
        side_l.addWidget(extra, 0, Qt.AlignmentFlag.AlignHCenter)
    side_l.addStretch(1)
    lay.addWidget(side, 0, Qt.AlignmentFlag.AlignTop)
    return row, picker


def fit_preview_pixmap(pix: QPixmap, max_w: int, max_h: int) -> QPixmap:
    if pix.isNull() or max_w < 1 or max_h < 1:
        return pix
    w, h = pix.width(), pix.height()
    if w < 1 or h < 1:
        return pix

    scale = min(max_w / w, max_h / h, 1.0)
    max_side = max(w, h)
    if max_side < _SMALL_PREVIEW_MAX_SIDE:
        small_cap = 0.32 + 0.5 * (max_side / _SMALL_PREVIEW_MAX_SIDE)
        scale = min(scale, small_cap)

    target_w = max(1, int(w * scale))
    target_h = max(1, int(h * scale))
    if target_w == w and target_h == h:
        return pix
    return pix.scaled(
        target_w,
        target_h,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def pixmap_from_png(png_bytes: bytes, size: int = 48) -> QPixmap:
    pix = QPixmap()
    if not png_bytes:
        return pix
    if not pix.loadFromData(png_bytes, "PNG"):
        return pix
    return pix.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def format_source_files(paths: list[str]) -> str:
    if not paths:
        return "Файлы не выбраны"
    if len(paths) == 1:
        return paths[0]
    names = [Path(p).name for p in paths]
    if len(names) <= 3:
        return f"{len(paths)} файла: {', '.join(names)}"
    return f"{len(paths)} файлов: {', '.join(names[:2])}, …"


def image_paths_in_directory(directory: str | Path) -> list[Path]:
    root = Path(directory)
    if not root.is_dir():
        return []
    exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
    paths: list[Path] = []
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix.lower() in exts:
            paths.append(path)
    return paths


_NAME_THEMES: list[tuple[list[str], list[str]]] = [
    (
        ["Мир", "Авто", "Игровой", "Pro", "Top", "Best", "Mega", "Ultra"],
        ["машин", "истории", "канал", "хаб", "мир", "zone", "play", "live"],
    ),
    (
        ["Lucky", "Gold", "Win", "Jackpot", "Bonus", "Spin", "Bet", "Casino"],
        ["games", "play", "hub", "zone", "win", "pro", "max", "vip"],
    ),
    (
        ["Ретро", "Ностальгия", "Классика", "Легенда", "Эпик", "Старый", "Добрый"],
        ["гейминг", "игры", "play", "stream", "vibes", "time", "шоу"],
    ),
]


def generate_channel_names(count: int) -> list[str]:
    import random

    count = max(1, min(int(count), 50))
    prefixes, suffixes = random.choice(_NAME_THEMES)
    names: list[str] = []
    seen: set[str] = set()
    attempts = 0
    while len(names) < count and attempts < count * 30:
        attempts += 1
        if random.random() < 0.35:
            name = f"{random.choice(prefixes)} {random.choice(suffixes)}"
        else:
            name = random.choice(prefixes) + random.choice(suffixes)
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    while len(names) < count:
        names.append(f"Канал {len(names) + 1}")
    return names
