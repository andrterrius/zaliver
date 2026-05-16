from __future__ import annotations

import os
import re
import json
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True, slots=True)
class YoutubeVideoStats:
    video_id: str
    view_count: int
    like_count: int | None
    comment_count: int | None
    age_restricted: bool = False


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")

# YouTube Data API v3: `videos.list` accepts at most 50 comma-separated ids.
YOUTUBE_DATA_API_VIDEOS_LIST_MAX_IDS = 50


def extract_video_id(url_or_id: str) -> str:
    """
    Extract videoId from common YouTube URL forms:
    - https://youtu.be/<id>
    - https://www.youtube.com/watch?v=<id>
    - https://www.youtube.com/shorts/<id>
    - https://www.youtube.com/embed/<id>
    If the input already looks like an id, it is returned.
    """
    s = (url_or_id or "").strip()
    if not s:
        return ""

    if _VIDEO_ID_RE.fullmatch(s):
        return s

    m = re.search(r"(?:youtu\.be/|[?&]v=)([A-Za-z0-9_-]{6,})", s)
    if m:
        return m.group(1)

    m = re.search(r"/shorts/([A-Za-z0-9_-]{6,})", s)
    if m:
        return m.group(1)

    m = re.search(r"/embed/([A-Za-z0-9_-]{6,})", s)
    if m:
        return m.group(1)

    m = re.search(r"/video/([A-Za-z0-9_-]{6,})", s)
    if m:
        return m.group(1)

    return ""


class YoutubeDataApiError(RuntimeError):
    pass


class YoutubeNoKeyParseError(RuntimeError):
    pass


def _statistics_scalar_to_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.isdigit():
        return int(v)
    try:
        return int(v)
    except Exception:
        return None


def _item_youtube_age_restricted(item: dict[str, Any]) -> bool:
    """``contentDetails.contentRating.ytRating == ytAgeRestricted`` → 18+ на YouTube."""
    cd = item.get("contentDetails") or {}
    if not isinstance(cd, dict):
        return False
    cr = cd.get("contentRating") or {}
    if not isinstance(cr, dict):
        return False
    return str(cr.get("ytRating") or "").strip() == "ytAgeRestricted"


def _item_to_youtube_video_stats(item: dict[str, Any]) -> YoutubeVideoStats | None:
    vid = str(item.get("id") or "").strip()
    if not _VIDEO_ID_RE.fullmatch(vid):
        return None
    stats = item.get("statistics") or {}
    if not isinstance(stats, dict):
        return None
    view_count = _statistics_scalar_to_int(stats.get("viewCount"))
    if view_count is None:
        return None
    return YoutubeVideoStats(
        video_id=vid,
        view_count=view_count,
        like_count=_statistics_scalar_to_int(stats.get("likeCount")),
        comment_count=_statistics_scalar_to_int(stats.get("commentCount")),
        age_restricted=_item_youtube_age_restricted(item),
    )


def _fetch_statistics_map_for_ids(
    video_ids: list[str],
    *,
    api_key: str | None = None,
    timeout_s: float = 15.0,
    session: requests.Session | None = None,
) -> dict[str, YoutubeVideoStats]:
    """
    One `videos.list` request. `video_ids` must be non-empty, each id valid regex,
    length at most YOUTUBE_DATA_API_VIDEOS_LIST_MAX_IDS after deduplication.
    """
    if not video_ids:
        return {}
    unique = list(dict.fromkeys(video_ids))
    if len(unique) > YOUTUBE_DATA_API_VIDEOS_LIST_MAX_IDS:
        raise YoutubeDataApiError(
            f"Too many video ids in one request: {len(unique)} "
            f"(max {YOUTUBE_DATA_API_VIDEOS_LIST_MAX_IDS})"
        )

    key = (api_key or os.getenv("YOUTUBE_API_KEY") or "").strip()
    if not key:
        raise YoutubeDataApiError(
            "Missing API key. Set env var YOUTUBE_API_KEY or pass api_key=..."
        )

    ids_csv = ",".join(unique)
    http = session or requests.Session()
    try:
        r = http.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "statistics,contentDetails", "id": ids_csv, "key": key},
            timeout=timeout_s,
        )
    except Exception as e:
        raise YoutubeDataApiError(f"Request failed: {e!r}") from e

    if r.status_code != 200:
        body = (r.text or "").strip()
        raise YoutubeDataApiError(f"HTTP {r.status_code}: {body[:500]}")

    data: dict[str, Any] = r.json() or {}
    items = data.get("items") or []
    out: dict[str, YoutubeVideoStats] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        st = _item_to_youtube_video_stats(raw)
        if st is not None:
            out[st.video_id] = st
    return out


def fetch_video_stats_by_id(
    video_id: str,
    *,
    api_key: str | None = None,
    timeout_s: float = 15.0,
    session: requests.Session | None = None,
) -> YoutubeVideoStats:
    """
    Fetch view/like/comment counts via YouTube Data API v3.
    No OAuth required, only an API key.
    """
    vid = (video_id or "").strip()
    if not _VIDEO_ID_RE.fullmatch(vid):
        raise YoutubeDataApiError(f"Invalid video id: {video_id!r}")

    m = _fetch_statistics_map_for_ids(
        [vid], api_key=api_key, timeout_s=timeout_s, session=session
    )
    if vid not in m:
        raise YoutubeDataApiError(
            f"No items returned for video id {vid!r} (is it public / exists?)"
        )
    return m[vid]


def fetch_video_stats_batch(
    video_ids: list[str],
    *,
    api_key: str | None = None,
    timeout_s: float = 15.0,
    session: requests.Session | None = None,
) -> tuple[list[YoutubeVideoStats], list[tuple[str, str]]]:
    """
    One Data API request for up to 50 ids (comma-separated `id` parameter).

    Returns (successes in the same order as input, failures as (id, message)).
    Invalid ids do not trigger HTTP. Ids omitted from a successful response are
    reported as failures (private / deleted / not found).
    """
    successes: list[YoutubeVideoStats] = []
    failures: list[tuple[str, str]] = []
    if len(video_ids) > YOUTUBE_DATA_API_VIDEOS_LIST_MAX_IDS:
        raise YoutubeDataApiError(
            f"Batch size {len(video_ids)} exceeds max "
            f"{YOUTUBE_DATA_API_VIDEOS_LIST_MAX_IDS}"
        )

    ordered: list[str] = []
    for raw in video_ids:
        v = (raw or "").strip()
        if v:
            ordered.append(v)

    if not ordered:
        return successes, failures

    valid_for_http: list[str] = []
    for v in ordered:
        if _VIDEO_ID_RE.fullmatch(v):
            valid_for_http.append(v)

    stats_by_id: dict[str, YoutubeVideoStats] = {}
    if valid_for_http:
        stats_by_id = _fetch_statistics_map_for_ids(
            valid_for_http, api_key=api_key, timeout_s=timeout_s, session=session
        )

    for v in ordered:
        if not _VIDEO_ID_RE.fullmatch(v):
            failures.append((v, f"Invalid video id: {v!r}"))
            continue
        st = stats_by_id.get(v)
        if st is not None:
            successes.append(st)
        else:
            failures.append(
                (
                    v,
                    "No statistics returned (video may be private, deleted, or not found).",
                )
            )

    return successes, failures


_DEFAULT_HEADERS = {
    # Some minimal UA helps avoid consent/robot pages.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}


def _parse_int_from_text(s: str) -> int | None:
    """
    Parse a number from strings like:
    - "1,234"
    - "1 234"
    - "1.2K", "3.4M"
    - "1 234" (NBSP)
    """
    t = (s or "").strip()
    if not t:
        return None

    t = t.replace("\u00a0", " ").replace("\u202f", " ")

    m = re.search(r"(\d[\d\s,\.]*)\s*([KMB])?\b", t, re.IGNORECASE)
    if not m:
        return None

    num = (m.group(1) or "").strip()
    suf = (m.group(2) or "").upper()

    if suf:
        num = num.replace(" ", "").replace(",", "").strip()
        try:
            base = float(num)
        except Exception:
            return None
        mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suf)
        if not mult:
            return None
        return int(base * mult)

    digits = re.sub(r"[^\d]", "", num)
    if not digits:
        return None
    try:
        return int(digits)
    except Exception:
        return None


def _deep_find_first(obj: Any, predicate) -> Any | None:
    if predicate(obj):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            r = _deep_find_first(v, predicate)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _deep_find_first(v, predicate)
            if r is not None:
                return r
    return None


def _extract_json_object_after_marker(text: str, marker: str) -> dict[str, Any] | None:
    """
    Extract a JSON object that starts after marker, using bracket counting.
    Marker examples:
    - "var ytInitialPlayerResponse = "
    - "ytInitialData = "
    """
    i = text.find(marker)
    if i < 0:
        return None
    j = text.find("{", i + len(marker))
    if j < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for k in range(j, len(text)):
        ch = text[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        else:
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    blob = text[j : k + 1]
                    try:
                        parsed = json.loads(blob)
                    except Exception:
                        return None
                    if isinstance(parsed, dict):
                        return parsed
                    return None
    return None


def fetch_video_stats_no_key(
    url_or_id: str,
    *,
    timeout_s: float = 15.0,
    session: requests.Session | None = None,
) -> YoutubeVideoStats:
    """
    Best-effort stats without API key by parsing YouTube HTML.
    This is unofficial and may break when YouTube changes markup.
    """
    vid = extract_video_id(url_or_id)
    if not vid:
        raise YoutubeNoKeyParseError(f"Could not extract video id from: {url_or_id!r}")

    # Prefer "watch" URL; it tends to carry the same JSON for shorts too.
    url = f"https://www.youtube.com/watch?v={vid}"
    http = session or requests.Session()
    try:
        r = http.get(url, headers=_DEFAULT_HEADERS, timeout=timeout_s)
    except Exception as e:
        raise YoutubeNoKeyParseError(f"Request failed: {e!r}") from e

    if r.status_code != 200:
        raise YoutubeNoKeyParseError(f"HTTP {r.status_code} while fetching watch page")

    html = r.text or ""
    if "consent.youtube.com" in html or "Before you continue to YouTube" in html:
        raise YoutubeNoKeyParseError(
            "Got a consent page instead of video page (cookies/consent required)."
        )

    player = (
        _extract_json_object_after_marker(html, "var ytInitialPlayerResponse = ")
        or _extract_json_object_after_marker(html, "ytInitialPlayerResponse = ")
    )
    initial = _extract_json_object_after_marker(html, "var ytInitialData = ") or _extract_json_object_after_marker(
        html, "ytInitialData = "
    )

    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None

    if player:
        try:
            vd = (player.get("videoDetails") or {}) if isinstance(player, dict) else {}
            vc = vd.get("viewCount")
            if isinstance(vc, str) and vc.isdigit():
                view_count = int(vc)
        except Exception:
            pass

    if view_count is None:
        # Fallback: find any "viewCount":"12345" occurrence.
        m = re.search(r'"viewCount"\s*:\s*"(\d+)"', html)
        if m:
            try:
                view_count = int(m.group(1))
            except Exception:
                view_count = None

    if initial:
        # Likes: search for a label that contains "like" and a number.
        like_label = _deep_find_first(
            initial,
            lambda x: isinstance(x, str)
            and (" like" in x.lower() or " likes" in x.lower() or "нрав" in x.lower())
            and any(ch.isdigit() for ch in x),
        )
        if isinstance(like_label, str):
            like_count = _parse_int_from_text(like_label)

        # Comments: search common comment header count texts.
        comment_label = _deep_find_first(
            initial,
            lambda x: isinstance(x, str)
            and (" comment" in x.lower() or " comments" in x.lower() or "коммент" in x.lower())
            and any(ch.isdigit() for ch in x),
        )
        if isinstance(comment_label, str):
            comment_count = _parse_int_from_text(comment_label)

    if view_count is None:
        raise YoutubeNoKeyParseError("Could not parse view count from HTML")

    return YoutubeVideoStats(
        video_id=vid,
        view_count=view_count,
        like_count=like_count,
        comment_count=comment_count,
        age_restricted=False,
    )

