"""Отложенная публикация в YouTube Studio (MSK)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        MSK = ZoneInfo("Europe/Moscow")
    except ZoneInfoNotFoundError:
        # Windows без пакета tzdata — МСК с 2014 года без перехода на DST (UTC+3).
        MSK = timezone(timedelta(hours=3))
except ImportError:
    MSK = timezone(timedelta(hours=3))
_YT_TIME_STEP_MIN = 15


def parse_msk_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = (value or "").strip()
        if not raw:
            return None
        dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    else:
        dt = dt.astimezone(MSK)
    return dt


def snap_studio_time_minutes(minute: int) -> int:
    return max(0, min(59, (int(minute) // _YT_TIME_STEP_MIN) * _YT_TIME_STEP_MIN))


def studio_time_label_en(dt: datetime) -> str:
    """Метка времени для ytcp-time-of-day-picker (англ. UI, шаг 15 мин)."""
    dt = dt.astimezone(MSK)
    minute = snap_studio_time_minutes(dt.minute)
    hour = dt.hour
    h12 = hour % 12
    if h12 == 0:
        h12 = 12
    ampm = "AM" if hour < 12 else "PM"
    return f"{h12}:{minute:02d} {ampm}"


def studio_time_label_en_variants(dt: datetime) -> list[str]:
    """Варианты 12-часовой метки для англ. списка Studio (1:00 PM / 01:00 PM)."""
    dt = dt.astimezone(MSK)
    minute = snap_studio_time_minutes(dt.minute)
    hour = dt.hour
    h12 = hour % 12 or 12
    ampm = "AM" if hour < 12 else "PM"
    out = [
        f"{h12}:{minute:02d} {ampm}",
        f"{h12:02d}:{minute:02d} {ampm}",
    ]
    deduped: list[str] = []
    for item in out:
        if item not in deduped:
            deduped.append(item)
    return deduped


def studio_time_label_ru(dt: datetime) -> str:
    """Метка времени для русского UI (24 ч, шаг 15 мин)."""
    dt = dt.astimezone(MSK)
    minute = snap_studio_time_minutes(dt.minute)
    return f"{dt.hour:02d}:{minute:02d}"


def normalize_studio_time_label(text: str) -> str:
    """Схлопнуть пробелы Studio (в т.ч. U+202F между временем и AM/PM)."""
    raw = (text or "").replace("\u200b", "").replace("\u00ad", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", raw, flags=re.UNICODE).strip().casefold()


def studio_time_match_patterns(
    dt: datetime,
    *,
    locale: str = "en",
) -> list[re.Pattern[str]]:
    dt = dt.astimezone(MSK)
    minute = snap_studio_time_minutes(dt.minute)
    hour = dt.hour
    # Playwright сравнивает regex с сырым textContent: узкий пробел U+202F
    # и перевод строки после подписи слота.
    if locale == "ru":
        return [
            re.compile(rf"^\s*{re.escape(f'{hour:02d}:{minute:02d}')}\s*$"),
            re.compile(rf"^\s*{re.escape(f'{hour}:{minute:02d}')}\s*$"),
        ]
    h12 = hour % 12 or 12
    ampm = "AM" if hour < 12 else "PM"
    patterns: list[re.Pattern[str]] = []
    for h_text in (str(h12), f"{h12:02d}"):
        patterns.append(
            re.compile(
                rf"^\s*{re.escape(h_text)}:{minute:02d}\s*{ampm}\s*$",
                re.I,
            )
        )
    return patterns


def studio_time_picker_locale_from_text(text: str) -> str:
    """en — список в 12 ч (AM/PM), иначе ru (24 ч)."""
    raw = (text or "").strip()
    if re.search(r"\b\d{1,2}:\d{2}\s*(AM|PM)\b", raw, re.I):
        return "en"
    return "ru"


def studio_time_input_label(dt: datetime, *, locale: str) -> str:
    if locale == "ru":
        return studio_time_label_ru(dt)
    return studio_time_label_en(dt)


_RU_DATE_MONTH_PARTS: dict[int, tuple[str, ...]] = {
    1: ("янв",),
    2: ("фев",),
    3: ("мар",),
    4: ("апр",),
    5: ("май", "мая"),
    6: ("июн",),
    7: ("июл",),
    8: ("авг",),
    9: ("сен",),
    10: ("окт",),
    11: ("ноя",),
    12: ("дек",),
}


def studio_date_picker_locale_from_text(text: str) -> str:
    """ru — кириллица в подписи поля/триггера, иначе en (M/D/YYYY)."""
    if re.search(r"[а-яё]", (text or "").lower()):
        return "ru"
    return "en"


def studio_date_input_candidates(dt: datetime, *, locale: str) -> list[str]:
    """Строки для tp-yt-paper-input в ytcp-date-picker."""
    dt = dt.astimezone(MSK)
    if locale == "ru":
        return [dt.strftime("%d.%m.%Y")]
    return [
        f"{dt.month}/{dt.day}/{dt.year}",
        dt.strftime("%m/%d/%Y"),
        dt.strftime("%b %d, %Y"),
        dt.strftime("%B %d, %Y"),
    ]


def studio_date_trigger_matches(text: str, dt: datetime) -> bool:
    """Проверка, что в триггере даты выбран нужный день (после ввода/клика)."""
    shown = (text or "").strip().lower()
    if not shown or str(dt.year) not in shown:
        return False
    if not re.search(rf"(?<!\d){int(dt.day)}(?!\d)", shown):
        return False
    anchor = datetime(dt.year, dt.month, dt.day)
    if anchor.strftime("%b").lower() in shown or anchor.strftime("%B").lower() in shown:
        return True
    if any(part in shown for part in _RU_DATE_MONTH_PARTS.get(dt.month, ())):
        return True
    m_slash = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", shown)
    if m_slash:
        m, d, y = int(m_slash.group(1)), int(m_slash.group(2)), int(m_slash.group(3))
        if y < 100:
            y += 2000
        if m == dt.month and d == dt.day and y == dt.year:
            return True
    return False


def validate_schedule_times(
    times: list[datetime],
    *,
    now: datetime | None = None,
) -> str | None:
    """Возвращает текст ошибки или None, если всё ок."""
    if not times:
        return "Укажите хотя бы одно время отложенной публикации."
    cur = now or datetime.now(tz=MSK)
    for t in times:
        dt = parse_msk_datetime(t)
        if dt is None:
            return "Некорректное время отложенной публикации."
        if dt <= cur + timedelta(minutes=1):
            return "Время отложенной публикации должно быть в будущем (МСК)."
    return None
