"""Подстановка переменных в названия и описания видео при заливе."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from zaliver.youtube_upload.schedule_publish import MSK

_WEEKDAYS_RU = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)
_WEEKDAYS_EN = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_MONTHS_RU = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)
_MONTHS_EN = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

_RAND_TOKEN_RE = re.compile(r"\{rand:([^}]+)\}", re.IGNORECASE)
_RAND_NUM_TOKEN_RE = re.compile(r"\{rand_num:(\d+)\s*-\s*(\d+)\}", re.IGNORECASE)
_SIMPLE_TOKENS = (
    "date",
    "time",
    "weekday",
    "weekday_en",
    "month",
    "month_en",
    "year",
    "profile",
    "video",
    "index",
)


@dataclass(frozen=True, slots=True)
class TitleVariableInfo:
    token: str
    example: str
    description: str


TITLE_VARIABLES: tuple[TitleVariableInfo, ...] = (
    TitleVariableInfo("{date}", "07.03.2026", "Дата залива"),
    TitleVariableInfo("{time}", "18:45", "Время залива"),
    TitleVariableInfo("{weekday}", "Пятница", "День недели (рус.)"),
    TitleVariableInfo("{weekday_en}", "Friday", "День недели (англ.)"),
    TitleVariableInfo("{month}", "Март", "Месяц (рус.)"),
    TitleVariableInfo("{month_en}", "March", "Месяц (англ.)"),
    TitleVariableInfo("{year}", "2026", "Год"),
    TitleVariableInfo("{profile}", "Profile_123", "Имя профиля в антидетекте"),
    TitleVariableInfo("{video}", "funny_clip_04", "Имя файла без расширения"),
    TitleVariableInfo("{index}", "1, 2, 3 …", "Счётчик по сессии"),
    TitleVariableInfo("{rand:а|б|в}", "случайное из а/б/в", "Случайное слово из списка"),
    TitleVariableInfo("{rand_num:1-999}", "437", "Случайное число в диапазоне"),
)

TITLE_VARIABLES_EXAMPLE = (
    "Смешное видео #{index} — {rand:Реакция|Топ|Подборка} {date}"
)

MAX_YOUTUBE_TITLE_LENGTH = 100

_ANY_TITLE_TOKEN_RE = re.compile(
    r"\{rand_num:\d+\s*-\s*\d+\}|\{rand:[^}]+\}|"
    r"\{date\}|\{time\}|\{weekday_en\}|\{weekday\}|"
    r"\{month_en\}|\{month\}|\{year\}|\{profile\}|\{video\}|\{index\}",
    re.IGNORECASE,
)

_DETERMINISTIC_TOKEN_MAX_LEN: dict[str, int] = {
    "{date}": 10,
    "{time}": 5,
    "{weekday}": max(len(day) for day in _WEEKDAYS_RU),
    "{weekday_en}": max(len(day) for day in _WEEKDAYS_EN),
    "{month}": max(len(month) for month in _MONTHS_RU if month),
    "{month_en}": max(len(month) for month in _MONTHS_EN if month),
    "{year}": 4,
    "{index}": 5,
}


@dataclass(frozen=True, slots=True)
class TitleExpansionResult:
    title: str
    truncated: bool = False
    original_length: int = 0


@dataclass(frozen=True, slots=True)
class TitleVariableContext:
    profile_name: str = ""
    video_path: str = ""
    index: int = 1
    now: datetime | None = None


def has_title_variables(text: str) -> bool:
    raw = text or ""
    if not raw:
        return False
    lower = raw.casefold()
    for name in _SIMPLE_TOKENS:
        if f"{{{name}}}" in lower:
            return True
    return bool(_RAND_TOKEN_RE.search(raw) or _RAND_NUM_TOKEN_RE.search(raw))


def _strip_title_variable_tokens(template: str) -> str:
    return _ANY_TITLE_TOKEN_RE.sub("", template or "")


def _deterministic_token_max_len(token: str) -> int | None:
    key = token.casefold()
    return _DETERMINISTIC_TOKEN_MAX_LEN.get(key)


def limit_youtube_title(
    title: str,
    *,
    max_length: int = MAX_YOUTUBE_TITLE_LENGTH,
) -> tuple[str, bool]:
    raw = title or ""
    if len(raw) <= max_length:
        return raw, False
    return raw[:max_length], True


def analyze_title_template(
    template: str,
    *,
    max_length: int = MAX_YOUTUBE_TITLE_LENGTH,
    preview_limit: int = 48,
) -> list[str]:
    raw = (template or "").strip()
    if not raw:
        return []

    preview = raw if len(raw) <= preview_limit else raw[: preview_limit - 1] + "…"
    warnings: list[str] = []

    if len(raw) > max_length and not has_title_variables(raw):
        warnings.append(
            f"«{preview}» уже {len(raw)} символов — лимит YouTube {max_length}."
        )
        return warnings

    literal_len = len(_strip_title_variable_tokens(raw))
    if literal_len > max_length:
        warnings.append(
            f"«{preview}»: текст без переменных уже {literal_len} символов "
            f"(лимит {max_length})."
        )

    deterministic_len = literal_len
    for match in _ANY_TITLE_TOKEN_RE.finditer(raw):
        token_max = _deterministic_token_max_len(match.group(0))
        if token_max is not None:
            deterministic_len += token_max

    if deterministic_len > max_length:
        warnings.append(
            f"«{preview}» с датой, временем и другими фиксированными переменными "
            f"займёт не менее {deterministic_len} символов — лимит {max_length} "
            f"будет превышен даже без длинного профиля или имени файла."
        )

    return warnings


def collect_title_template_warnings(
    templates: list[str] | tuple[str, ...],
    *,
    max_length: int = MAX_YOUTUBE_TITLE_LENGTH,
) -> list[str]:
    seen_templates: set[str] = set()
    warnings: list[str] = []
    seen_messages: set[str] = set()
    for template in templates:
        raw = (template or "").strip()
        if not raw or raw in seen_templates:
            continue
        seen_templates.add(raw)
        for message in analyze_title_template(raw, max_length=max_length):
            if message not in seen_messages:
                seen_messages.add(message)
                warnings.append(message)
    return warnings


def expand_title_variables(text: str, ctx: TitleVariableContext) -> str:
    raw = text or ""
    if not raw or not has_title_variables(raw):
        return raw

    now = ctx.now
    if now is None:
        now = datetime.now(tz=MSK)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=MSK)
    else:
        now = now.astimezone(MSK)

    profile = (ctx.profile_name or "").strip() or "Profile"
    video_stem = Path(ctx.video_path or "").stem or "video"
    index = str(max(1, int(ctx.index or 1)))

    replacements = {
        "{date}": now.strftime("%d.%m.%Y"),
        "{time}": now.strftime("%H:%M"),
        "{weekday}": _WEEKDAYS_RU[now.weekday()],
        "{weekday_en}": _WEEKDAYS_EN[now.weekday()],
        "{month}": _MONTHS_RU[now.month],
        "{month_en}": _MONTHS_EN[now.month],
        "{year}": str(now.year),
        "{profile}": profile,
        "{video}": video_stem,
        "{index}": index,
    }

    def _replace_rand(match: re.Match[str]) -> str:
        body = match.group(1)
        options = [part.strip() for part in body.split("|") if part.strip()]
        if not options:
            return match.group(0)
        return random.choice(options)

    def _replace_rand_num(match: re.Match[str]) -> str:
        lo = int(match.group(1))
        hi = int(match.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return str(random.randint(lo, hi))

    out = _RAND_TOKEN_RE.sub(_replace_rand, raw)
    out = _RAND_NUM_TOKEN_RE.sub(_replace_rand_num, out)

    for token, value in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        out = re.sub(re.escape(token), value, out, flags=re.IGNORECASE)

    return out


def expand_and_limit_title(
    text: str,
    ctx: TitleVariableContext,
    *,
    max_length: int = MAX_YOUTUBE_TITLE_LENGTH,
) -> TitleExpansionResult:
    expanded = expand_title_variables(text, ctx)
    limited, truncated = limit_youtube_title(expanded, max_length=max_length)
    return TitleExpansionResult(
        title=limited,
        truncated=truncated,
        original_length=len(expanded),
    )
