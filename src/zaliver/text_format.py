"""Утилиты форматирования текста для полей Studio / Instagram."""

from __future__ import annotations

# YouTube: zero-width space обычно достаточно.
BLANK_LINE_ZWSP = "\u200b"
# Instagram часто выкидывает ZWSP; braille blank (U+2800) удерживает пустую строку.
BLANK_LINE_BRAILLE = "\u2800"


def preserve_blank_lines(
    text: str,
    *,
    placeholder: str = BLANK_LINE_ZWSP,
) -> str:
    """Сохранить пустые строки (два и более Enter подряд) для contenteditable."""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in raw:
        return raw
    return "\n".join(line if line else placeholder for line in raw.split("\n"))


def blank_line_gap_count(text: str) -> int:
    """Сколько «пустых» промежутков между строками (двойной Enter и больше)."""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # Плейсхолдеры считаем как пустую строку.
    cleaned = (
        raw.replace(BLANK_LINE_ZWSP, "")
        .replace(BLANK_LINE_BRAILLE, "")
        .replace("\u2063", "")
    )
    gaps = 0
    i = 0
    while i < len(cleaned) - 1:
        if cleaned[i] == "\n" and cleaned[i + 1] == "\n":
            gaps += 1
            i += 1
        else:
            i += 1
    return gaps
