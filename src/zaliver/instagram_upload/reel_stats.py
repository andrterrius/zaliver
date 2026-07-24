"""Метрики Instagram Reels через instagrapi (без официального API-ключа)."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,}$")
_REEL_IN_URL_RE = re.compile(r"/(?:reel|p)/([A-Za-z0-9_-]+)/?", re.I)

# Важно: параллель с одной sessionid жжёт аккаунты (TooManyRedirects / logout в браузере).
# Чекер — только последовательно, с паузой между запросами.
DEFAULT_STATS_WORKERS = 1
MAX_STATS_WORKERS = 1
DEFAULT_REQUEST_PAUSE_S = 1.4


@dataclass(frozen=True, slots=True)
class InstagramReelStats:
    video_id: str
    view_count: int
    like_count: int | None
    comment_count: int | None


def extract_reel_shortcode(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    if not s:
        return ""
    if _SHORTCODE_RE.fullmatch(s):
        return s
    m = _REEL_IN_URL_RE.search(s)
    if m:
        return m.group(1)
    return ""


def _as_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        t = v.strip().replace(" ", "").replace(",", "")
        if t.isdigit():
            return int(t)
    try:
        return int(v)
    except Exception:
        return None


def _views_from_media(media: Any) -> int:
    play = _as_int(getattr(media, "play_count", None))
    if play is not None and play >= 0:
        return play
    view = _as_int(getattr(media, "view_count", None))
    if view is not None and view >= 0:
        return view
    return 0


def fetch_reel_stats(cl: Any, shortcode: str) -> InstagramReelStats:
    code = extract_reel_shortcode(shortcode)
    if not code:
        raise ValueError(f"Некорректный shortcode рилса: {shortcode!r}")
    # media_pk_from_code — локальный расчёт, без сети.
    pk = cl.media_pk_from_code(code)
    media = None
    last_err: Exception | None = None
    # Только v1: gql/лишние fallback увеличивают шанс challenge на той же сессии.
    for getter_name in ("media_info_v1", "media_info"):
        getter = getattr(cl, getter_name, None)
        if not callable(getter):
            continue
        try:
            media = getter(pk)
            break
        except Exception as e:
            last_err = e
            continue
    if media is None:
        raise RuntimeError(
            f"media_info({code}): {last_err}" if last_err else "media_info failed"
        )
    return InstagramReelStats(
        video_id=code,
        view_count=_views_from_media(media),
        like_count=_as_int(getattr(media, "like_count", None)),
        comment_count=_as_int(getattr(media, "comment_count", None)),
    )


def _unique_shortcodes(shortcodes: list[str]) -> list[str]:
    ids = [extract_reel_shortcode(s) for s in shortcodes]
    ids = [x for x in ids if x]
    seen: set[str] = set()
    ordered: list[str] = []
    for x in ids:
        if x in seen:
            continue
        seen.add(x)
        ordered.append(x)
    return ordered


def fetch_reel_stats_many(
    cl: Any,
    shortcodes: list[str],
    *,
    workers: int = DEFAULT_STATS_WORKERS,
    request_pause_s: float = DEFAULT_REQUEST_PAUSE_S,
    abort_on_session_error: bool = True,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_item: Callable[[InstagramReelStats | None, str, str | None], None] | None = None,
) -> tuple[list[InstagramReelStats], list[tuple[str, str]]]:
    """
    Запросить метрики **последовательно** (workers игнорируется / всегда 1).

    При ошибке сессии (redirects / login_required) — сразу стоп, остаток не долбим.
    """
    from zaliver.instagram_upload.instagrapi_session import is_instagrapi_session_error

    ordered = _unique_shortcodes(shortcodes)
    ok: list[InstagramReelStats] = []
    fail: list[tuple[str, str]] = []
    total = len(ordered)
    if total <= 0:
        return ok, fail

    # Параллель намеренно отключена — жжёт sessionid и ломает Instagram в антике.
    _ = workers
    pause = max(0.0, float(request_pause_s or 0.0))

    if on_progress is not None:
        on_progress(0, total, ordered[0])

    for i, code in enumerate(ordered):
        if on_progress is not None:
            on_progress(i, total, code)
        if i > 0 and pause > 0:
            time.sleep(pause)
        try:
            st = fetch_reel_stats(cl, code)
            ok.append(st)
            if on_item is not None:
                on_item(st, code, None)
        except Exception as e:
            msg = str(e) or type(e).__name__
            fail.append((code, msg))
            if on_item is not None:
                on_item(None, code, msg)
            if abort_on_session_error and is_instagrapi_session_error(msg):
                # Не добиваем аккаунт остальными запросами.
                stop_msg = f"остановлено: сессия Instagram (после {code}: {msg})"
                for left in ordered[i + 1 :]:
                    fail.append((left, stop_msg))
                    if on_item is not None:
                        on_item(None, left, stop_msg)
                break

    if on_progress is not None:
        on_progress(total, total, ordered[-1])
    return ok, fail
