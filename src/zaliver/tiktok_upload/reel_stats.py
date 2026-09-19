"""Метрики TikTok: публичная страница ролика (HTTP), без антидетект-профиля."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import requests

_VIDEO_ID_RE = re.compile(r"^\d{8,}$")
_VIDEO_IN_URL_RE = re.compile(r"/video/(\d{8,})", re.I)

logger = logging.getLogger(__name__)

DEFAULT_REQUEST_PAUSE_S = 0.0
DEFAULT_CHECKER_TABS = 100
DEFAULT_STATS_WORKERS = 100
MAX_STATS_WORKERS = 100
DEFAULT_REQUEST_TIMEOUT_S = 20.0

_SCRIPT_RE = re.compile(
    r'<script[^>]+id=["\'](__UNIVERSAL_DATA_FOR_REHYDRATION__|SIGI_STATE|__NEXT_DATA__)["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_UNAVAILABLE_TITLE_RE = re.compile(
    r"<title>[^<]*(couldn't find this video|video currently unavailable|"
    r"это видео недоступно)[^<]*</title>",
    re.I,
)
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}
_SLOT_TIMEOUT_S = 40.0
_HYDRATE_WAIT_S = 1.6

_STATS_DOM_SEL = (
    '[data-e2e="like-count"], '
    '[data-e2e="browse-like-count"], '
    '[data-e2e="comment-count"], '
    '[data-e2e="browse-comment-count"]'
)

_EXTRACT_STATS_JS = """
(videoId) => {
  const vid = String(videoId || '');
  const asNum = (v) => {
    if (v == null || v === '') return null;
    if (typeof v === 'bigint') return Number(v);
    if (typeof v === 'number' && Number.isFinite(v)) return Math.round(v);
    let s = String(v).trim().replace(/\\u00a0/g, ' ');
    if (!s || s === '-' || s === '—' || s === '…' || s === '...') return null;
    s = s.replace(/\\s+/g, '');
    const ru = s.match(/^([0-9]+(?:[.,][0-9]+)?)(тыс\\.?|млн\\.?|млрд\\.?|[KMB])$/i);
    if (ru) {
      let n = parseFloat(ru[1].replace(',', '.'));
      const u = ru[2].toLowerCase();
      if (u.startsWith('тыс') || u === 'k') n *= 1e3;
      else if (u.startsWith('млн') || u === 'm') n *= 1e6;
      else if (u.startsWith('млрд') || u === 'b') n *= 1e9;
      return Number.isFinite(n) ? Math.round(n) : null;
    }
    const m = s.match(/^([0-9]*\\.?[0-9]+)([KMB])?$/i);
    if (m) {
      let n = parseFloat(m[1]);
      const u = (m[2] || '').toUpperCase();
      if (u === 'K') n *= 1e3;
      if (u === 'M') n *= 1e6;
      if (u === 'B') n *= 1e9;
      return Number.isFinite(n) ? Math.round(n) : null;
    }
    const digits = s.replace(/[^0-9]/g, '');
    if (digits.length >= 1 && digits.length <= 15) return parseInt(digits, 10);
    return null;
  };
  const pickStats = (item) => {
    if (!item || typeof item !== 'object') return null;
    let play = null, digg = null, comment = null;
    for (const key of ['stats', 'statsV2', 'statistics']) {
      const st = item[key];
      if (!st || typeof st !== 'object') continue;
      if (play == null) {
        play = asNum(st.playCount ?? st.play_count ?? st.viewCount ?? st.view_count);
      }
      if (digg == null) {
        digg = asNum(st.diggCount ?? st.digg_count ?? st.likeCount ?? st.like_count);
      }
      if (comment == null) {
        comment = asNum(st.commentCount ?? st.comment_count);
      }
    }
    if (play == null && digg == null && comment == null) return null;
    return { playCount: play, diggCount: digg, commentCount: comment };
  };
  const idsOf = (node) => {
    const raw = [
      node && node.id,
      node && node.videoId,
      node && node.awemeId,
      node && node.aweme_id,
    ];
    return raw.map((x) => (x == null ? '' : String(x)));
  };
  const fromItem = (item) => {
    if (!item || typeof item !== 'object') return null;
    const ids = idsOf(item);
    if (vid && ids.some(Boolean) && !ids.includes(vid)) return null;
    return pickStats(item);
  };
  const targeted = (root) => {
    if (!root || typeof root !== 'object') return null;
    const scope = root.__DEFAULT_SCOPE__ || root;
    const detail =
      scope['webapp.video-detail'] ||
      scope.videoDetail ||
      null;
    const item =
      (detail && detail.itemInfo && detail.itemInfo.itemStruct) ||
      (detail && detail.itemStruct) ||
      null;
    let st = fromItem(item);
    if (st) return st;
    const mod = scope.ItemModule || root.ItemModule;
    if (mod && vid && mod[vid]) return fromItem(mod[vid]);
    return null;
  };
  const parseJsonSafe = (t) => {
    const quoted = String(t).replace(/:(?:\\s*)(-?\\d{16,})/g, ':"$1"');
    return JSON.parse(quoted);
  };
  const seen = new Set();
  let matched = null;
  const visit = (node, depth) => {
    if (!node || depth > 28 || matched) return;
    if (typeof node !== 'object') return;
    if (seen.has(node)) return;
    seen.add(node);
    if (Array.isArray(node)) {
      for (const x of node) visit(x, depth + 1);
      return;
    }
    const st = pickStats(node);
    if (st && vid && idsOf(node).includes(vid)) {
      matched = st;
      return;
    }
    for (const k of Object.keys(node)) {
      try { visit(node[k], depth + 1); } catch (_) {}
      if (matched) return;
    }
  };
  const roots = [];
  const readScript = (el) => {
    if (!el) return;
    const t = (el.textContent || el.innerText || '').trim();
    if (!t) return;
    try { roots.push(parseJsonSafe(t)); } catch (_) {}
  };
  readScript(document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__'));
  readScript(document.getElementById('SIGI_STATE'));
  readScript(document.getElementById('__NEXT_DATA__'));
  try {
    if (window.__UNIVERSAL_DATA_FOR_REHYDRATION__) {
      roots.push(window.__UNIVERSAL_DATA_FOR_REHYDRATION__);
    }
  } catch (_) {}
  try {
    if (window.SIGI_STATE) roots.push(window.SIGI_STATE);
  } catch (_) {}
  for (const r of roots) {
    const hit = targeted(r);
    if (hit) {
      matched = hit;
      break;
    }
  }
  if (!matched) {
    for (const r of roots) visit(r, 0);
  }
  const textOf = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return '';
    return String(el.textContent || el.innerText || '').trim();
  };
  const likeText = textOf('[data-e2e="like-count"]')
    || textOf('[data-e2e="browse-like-count"]');
  const commentText = textOf('[data-e2e="comment-count"]')
    || textOf('[data-e2e="browse-comment-count"]');
  const viewText = textOf('[data-e2e="video-views"]')
    || textOf('[data-e2e="browse-video-views"]');
  const likeEl = document.querySelector(
    '[data-e2e="like-count"], [data-e2e="browse-like-count"]'
  );
  const commentEl = document.querySelector(
    '[data-e2e="comment-count"], [data-e2e="browse-comment-count"]'
  );
  const bodyText = String((document.body && document.body.innerText) || '');
  const chosen = matched || {};
  const title = String((document.title || '')).toLowerCase();
  return {
    playCount: chosen.playCount ?? asNum(viewText) ?? null,
    diggCount: chosen.diggCount ?? asNum(likeText) ?? null,
    commentCount: chosen.commentCount ?? asNum(commentText) ?? null,
    matched: !!matched,
    domReady: !!(likeEl && commentEl && /\\d/.test(likeText + commentText)),
    href: String((location && location.href) || ''),
    unavailable: /couldn't find this video|video currently unavailable|это видео недоступно/.test(title),
  };
}
"""


@dataclass(frozen=True, slots=True)
class TikTokReelStats:
    video_id: str
    view_count: int
    like_count: int | None
    comment_count: int | None


@dataclass(frozen=True, slots=True)
class _ItemCounts:
    play: int | None
    likes: int | None
    comments: int | None

    def has_any(self) -> bool:
        return self.play is not None or self.likes is not None or self.comments is not None


def _log(message: str) -> None:
    from zaliver.tiktok_upload.logutil import emit_tiktok_log

    emit_tiktok_log(message, tag="[tiktok]")


def extract_tiktok_video_id(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    if not s:
        return ""
    if _VIDEO_ID_RE.fullmatch(s):
        return s
    m = _VIDEO_IN_URL_RE.search(s)
    if m:
        return m.group(1)
    return ""


def extract_reel_shortcode(url_or_id: str) -> str:
    """Совместимое имя: числовой id TikTok-ролика."""
    return extract_tiktok_video_id(url_or_id)


def canonical_tiktok_video_url(url_or_id: str, *, video_id: str = "") -> str:
    raw = (url_or_id or "").strip()
    vid = extract_tiktok_video_id(raw) or extract_tiktok_video_id(video_id)
    if not vid:
        return ""
    m = re.search(r"/@([^/?#]+)/video/" + re.escape(vid), raw, re.I)
    if m:
        user = m.group(1).strip().lstrip("@")
        if user:
            return f"https://www.tiktok.com/@{user}/video/{vid}"
    return f"https://www.tiktok.com/@i/video/{vid}"


def _as_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        t = v.strip().replace(" ", "").replace(",", "").replace("\xa0", "")
        if t.isdigit():
            return int(t)
        m = re.fullmatch(r"([0-9]*\.?[0-9]+)([KMB]|тыс\.?|млн\.?|млрд\.?)?", t, re.I)
        if m:
            n = float(m.group(1))
            u = (m.group(2) or "").upper()
            if u in ("K",) or u.lower().startswith("тыс"):
                n *= 1_000
            elif u in ("M",) or u.lower().startswith("млн"):
                n *= 1_000_000
            elif u in ("B",) or u.lower().startswith("млрд"):
                n *= 1_000_000_000
            return int(round(n))
    try:
        return int(v)
    except Exception:
        return None


def fetch_reel_stats_from_page(page, url_or_id: str) -> TikTokReelStats:
    vid = extract_tiktok_video_id(url_or_id)
    if not vid:
        raise ValueError(f"Некорректный id TikTok-ролика: {url_or_id!r}")
    url = canonical_tiktok_video_url(url_or_id, video_id=vid)
    from zaliver.tiktok_upload.register import _navigate_page_to

    _navigate_page_to(page, url, label="TikTok stats")
    _log(f"TikTok stats: ждём счётчики на странице ролика {vid}…")
    try:
        page.wait_for_selector(_STATS_DOM_SEL, timeout=25_000)
    except Exception as e:
        _log(f"TikTok stats: like/comment в DOM ещё нет: {type(e).__name__}")

    deadline = time.monotonic() + 40.0
    ready_since: float | None = None
    last: dict[str, Any] | None = None
    last_log = 0.0
    while time.monotonic() < deadline:
        try:
            raw = page.evaluate(_EXTRACT_STATS_JS, vid)
        except Exception as e:
            raw = None
            if time.monotonic() - last_log >= 5.0:
                last_log = time.monotonic()
                _log(f"TikTok stats: evaluate: {type(e).__name__}: {e!r}")
        if isinstance(raw, dict):
            last = raw
            if raw.get("unavailable"):
                raise RuntimeError("Ролик недоступен на TikTok.")
            play = _as_int(raw.get("playCount"))
            digg = _as_int(raw.get("diggCount"))
            comment = _as_int(raw.get("commentCount"))
            dom_ready = bool(raw.get("domReady"))
            matched = bool(raw.get("matched"))
            now = time.monotonic()
            if now - last_log >= 5.0:
                last_log = now
                _log(
                    "TikTok stats: "
                    f"views={play!r} likes={digg!r} comments={comment!r} "
                    f"dom={'ok' if dom_ready else 'wait'} "
                    f"json={'ok' if matched else 'wait'}"
                )
            has_counts = play is not None or digg is not None or comment is not None
            if has_counts and (dom_ready or matched):
                if ready_since is None:
                    ready_since = now
                # Дать гидратации дорисовать цифры (не уходим на 0 сразу после commit).
                if now - ready_since >= _HYDRATE_WAIT_S:
                    st = TikTokReelStats(
                        video_id=vid,
                        view_count=int(play or 0),
                        like_count=digg,
                        comment_count=comment,
                    )
                    _log(
                        f"TikTok stats: готово video_id={vid} "
                        f"views={st.view_count} likes={st.like_count} "
                        f"comments={st.comment_count}"
                    )
                    return st
            else:
                ready_since = None
        try:
            page.wait_for_timeout(400)
        except Exception:
            time.sleep(0.4)
    href = ""
    if isinstance(last, dict):
        href = str(last.get("href") or "")
        play = _as_int(last.get("playCount"))
        digg = _as_int(last.get("diggCount"))
        comment = _as_int(last.get("commentCount"))
        if play is not None or digg is not None or comment is not None:
            st = TikTokReelStats(
                video_id=vid,
                view_count=int(play or 0),
                like_count=digg,
                comment_count=comment,
            )
            _log(
                f"TikTok stats: таймаут ожидания DOM, берём что есть "
                f"views={st.view_count} likes={st.like_count} "
                f"comments={st.comment_count}"
            )
            return st
    raise RuntimeError(
        "Не удалось прочитать просмотры/лайки/комментарии со страницы ролика"
        + (f" ({href})" if href else "")
        + "."
    )


def _normalize_stats_items(
    items: list[dict[str, str]] | list[str],
) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            vid = extract_tiktok_video_id(
                str(item.get("video_id") or item.get("url") or "")
            )
            url = canonical_tiktok_video_url(
                str(item.get("url") or ""), video_id=vid
            )
        else:
            vid = extract_tiktok_video_id(str(item))
            url = canonical_tiktok_video_url(str(item), video_id=vid)
        if not vid or vid in seen:
            continue
        seen.add(vid)
        ordered.append((vid, url or canonical_tiktok_video_url(vid)))
    return ordered


def _kickoff_stats_navigation(page, url: str) -> None:
    """Начать загрузку ролика, не блокируя остальные вкладки."""
    try:
        cdp = page.context.new_cdp_session(page)
        try:
            cdp.send("Page.navigate", {"url": url})
            return
        finally:
            try:
                cdp.detach()
            except Exception:
                pass
    except Exception as e:
        _log(f"TikTok stats: CDP navigate: {type(e).__name__}: {e!r}")
    try:
        page.evaluate("(u) => { location.assign(u); }", url)
        return
    except Exception:
        pass
    page.goto(url, wait_until="commit", timeout=45_000)


def _page_href(page) -> str:
    try:
        return str(page.url or "")
    except Exception:
        return ""


def _open_checker_tab_pages(seed_page, want: int) -> tuple[list, list]:
    pages = [seed_page]
    created: list = []
    ctx = seed_page.context
    while len(pages) < want:
        try:
            extra = ctx.new_page()
        except Exception as e:
            _log(
                "TikTok stats: не удалось открыть вкладку "
                f"{len(pages) + 1}/{want}: {type(e).__name__}: {e!r}"
            )
            break
        created.append(extra)
        pages.append(extra)
    return pages, created


def _close_pages(pages: list) -> None:
    for pg in pages:
        try:
            pg.close()
        except Exception:
            pass


def _stats_from_raw(vid: str, raw: dict[str, Any]) -> TikTokReelStats | None:
    play = _as_int(raw.get("playCount"))
    digg = _as_int(raw.get("diggCount"))
    comment = _as_int(raw.get("commentCount"))
    if play is None and digg is None and comment is None:
        return None
    return TikTokReelStats(
        video_id=vid,
        view_count=int(play or 0),
        like_count=digg,
        comment_count=comment,
    )


def _tick_stats_slot(slot: dict[str, Any]) -> tuple[str, Any]:
    """Один шаг опроса вкладки: wait | ok+stats | fail+msg."""
    page = slot["page"]
    vid = str(slot["vid"])
    now = time.monotonic()
    href = _page_href(page)
    slot["href"] = href
    low = href.lower()
    if low.startswith("chrome-error:") or "chromewebdata" in low:
        if now - float(slot["t0"]) >= 8.0:
            return "fail", f"сеть не поднялась ({href})"
        return "wait", None
    try:
        raw = page.evaluate(_EXTRACT_STATS_JS, vid)
    except Exception as e:
        raw = None
        if now - float(slot["last_log"]) >= 5.0:
            slot["last_log"] = now
            _log(f"TikTok stats [{vid}]: evaluate: {type(e).__name__}: {e!r}")
    if isinstance(raw, dict):
        slot["last"] = raw
        if raw.get("unavailable"):
            return "fail", "Ролик недоступен на TikTok."
        play = _as_int(raw.get("playCount"))
        digg = _as_int(raw.get("diggCount"))
        comment = _as_int(raw.get("commentCount"))
        dom_ready = bool(raw.get("domReady"))
        matched = bool(raw.get("matched"))
        if now - float(slot["last_log"]) >= 5.0:
            slot["last_log"] = now
            _log(
                f"TikTok stats [{vid}]: "
                f"views={play!r} likes={digg!r} comments={comment!r} "
                f"dom={'ok' if dom_ready else 'wait'} "
                f"json={'ok' if matched else 'wait'}"
            )
        has_counts = play is not None or digg is not None or comment is not None
        if has_counts and (dom_ready or matched):
            ready_since = slot.get("ready_since")
            if ready_since is None:
                slot["ready_since"] = now
                ready_since = now
            if now - float(ready_since) >= _HYDRATE_WAIT_S:
                st = _stats_from_raw(vid, raw)
                if st is not None:
                    return "ok", st
        else:
            slot["ready_since"] = None
    if now - float(slot["t0"]) >= _SLOT_TIMEOUT_S:
        last = slot.get("last")
        if isinstance(last, dict):
            st = _stats_from_raw(vid, last)
            if st is not None:
                _log(
                    f"TikTok stats [{vid}]: таймаут ожидания DOM, берём что есть "
                    f"views={st.view_count} likes={st.like_count} "
                    f"comments={st.comment_count}"
                )
                return "ok", st
        return (
            "fail",
            "Не удалось прочитать просмотры/лайки/комментарии со страницы ролика"
            + (f" ({href})" if href else "")
            + ".",
        )
    return "wait", None


def fetch_reel_stats_many_on_page(
    page,
    items: list[dict[str, str]] | list[str],
    *,
    request_pause_s: float = DEFAULT_REQUEST_PAUSE_S,
    parallel_tabs: int = DEFAULT_CHECKER_TABS,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_item: Callable[[TikTokReelStats | None, str, str | None], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[TikTokReelStats], list[tuple[str, str]]]:
    """Параллельно открыть ролики во вкладках и сразу брать следующее после чека."""
    ordered = _normalize_stats_items(items)
    ok: list[TikTokReelStats] = []
    fail: list[tuple[str, str]] = []
    total = len(ordered)
    if total <= 0:
        return ok, fail
    if on_progress is not None:
        on_progress(0, total, ordered[0][0])

    want = max(1, min(int(parallel_tabs or 1), DEFAULT_CHECKER_TABS, total))
    pages, created = _open_checker_tab_pages(page, want)
    want = len(pages)
    pause = max(0.0, float(request_pause_s or 0.0))
    _log(
        f"TikTok stats: параллельно вкладок={want}, роликов={total}"
        + (f", пауза слота={pause:.1f}с" if pause > 0 else "")
    )

    pending = list(ordered)
    slots: list[dict[str, Any] | None] = [None] * want
    done = 0

    def _emit_item(st: TikTokReelStats | None, vid: str, err: str | None) -> None:
        nonlocal done
        done += 1
        if st is not None:
            ok.append(st)
            _log(
                f"TikTok stats: готово video_id={vid} "
                f"views={st.view_count} likes={st.like_count} "
                f"comments={st.comment_count} ({done}/{total})"
            )
        else:
            fail.append((vid, err or "unknown"))
        if on_item is not None:
            on_item(st, vid, err)
        if on_progress is not None:
            on_progress(done, total, vid)

    def _fail_left(msg: str) -> None:
        left_vids = [
            str(s["vid"]) for s in slots if s is not None and s.get("vid")
        ]
        left_vids.extend(v for v, _ in pending)
        pending.clear()
        for i in range(len(slots)):
            slots[i] = None
        seen_left: set[str] = set()
        for left_vid in left_vids:
            if left_vid in seen_left:
                continue
            seen_left.add(left_vid)
            _emit_item(None, left_vid, msg)

    def _assign(tab_i: int) -> None:
        pg = pages[tab_i]
        while pending:
            vid, url = pending.pop(0)
            try:
                _kickoff_stats_navigation(pg, url)
            except Exception as e:
                _emit_item(None, vid, str(e) or type(e).__name__)
                continue
            now = time.monotonic()
            slots[tab_i] = {
                "page": pg,
                "vid": vid,
                "url": url,
                "t0": now,
                "ready_since": None,
                "last": None,
                "last_log": 0.0,
                "href": "",
            }
            _log(f"TikTok stats: вкладка {tab_i + 1}/{want} → {vid}")
            return
        slots[tab_i] = None

    try:
        for i in range(want):
            _assign(i)
        while any(s is not None for s in slots):
            if should_cancel is not None and should_cancel():
                _fail_left("остановлено")
                break
            for i, slot in enumerate(slots):
                if slot is None:
                    continue
                next_at = slot.get("next_at")
                if next_at is not None:
                    if time.monotonic() < float(next_at):
                        continue
                    slot.pop("next_at", None)
                    _assign(i)
                    continue
                kind, payload = _tick_stats_slot(slot)
                if kind == "wait":
                    continue
                vid = str(slot["vid"])
                if kind == "ok":
                    _emit_item(payload, vid, None)
                else:
                    _emit_item(None, vid, str(payload or "ошибка"))
                if pause > 0:
                    slots[i] = {
                        "page": pages[i],
                        "vid": "",
                        "next_at": time.monotonic()
                        + pause
                        + random.uniform(0.0, 0.4),
                    }
                    continue
                _assign(i)
            try:
                page.wait_for_timeout(350)
            except Exception:
                time.sleep(0.35)
    finally:
        _close_pages(created)

    if on_progress is not None:
        on_progress(total, total, ordered[-1][0])
    return ok, fail


def _quote_bigints(raw: str) -> str:
    return re.sub(r":\s*(-?\d{16,})\b", r':"\1"', raw)


def _ids_of(node: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k in ("id", "videoId", "awemeId", "aweme_id"):
        v = node.get(k)
        if v is not None:
            out.append(str(v))
    return out


def _first_int(d: dict[str, Any], *keys: str) -> int | None:
    for k in keys:
        if k not in d:
            continue
        n = _as_int(d.get(k))
        if n is not None:
            return n
    return None


def _pick_stats(node: dict[str, Any]) -> _ItemCounts:
    play = likes = comments = None
    for key in ("stats", "statsV2", "statistics"):
        st = node.get(key)
        if not isinstance(st, dict):
            continue
        if play is None:
            play = _first_int(st, "playCount", "play_count", "viewCount", "view_count")
        if likes is None:
            likes = _first_int(st, "diggCount", "digg_count", "likeCount", "like_count")
        if comments is None:
            comments = _first_int(st, "commentCount", "comment_count")
    return _ItemCounts(play, likes, comments)


def _from_item(item: Any, vid: str) -> _ItemCounts | None:
    if not isinstance(item, dict):
        return None
    ids = _ids_of(item)
    if vid and ids and vid not in ids:
        return None
    counts = _pick_stats(item)
    return counts if counts.has_any() else None


def _targeted(root: Any, vid: str) -> _ItemCounts | None:
    if not isinstance(root, dict):
        return None
    scope = root.get("__DEFAULT_SCOPE__") or root
    if not isinstance(scope, dict):
        scope = root
    detail = scope.get("webapp.video-detail") or scope.get("videoDetail")
    item = None
    if isinstance(detail, dict):
        info = detail.get("itemInfo")
        if isinstance(info, dict):
            item = info.get("itemStruct")
        item = item or detail.get("itemStruct")
    hit = _from_item(item, vid)
    if hit is not None:
        return hit
    mod = scope.get("ItemModule") or root.get("ItemModule")
    if isinstance(mod, dict) and vid and isinstance(mod.get(vid), dict):
        return _from_item(mod[vid], vid)
    return None


def _walk_stats(
    node: Any, vid: str, *, depth: int = 0, seen: set[int] | None = None
) -> _ItemCounts | None:
    if node is None or depth > 28:
        return None
    if seen is None:
        seen = set()
    oid = id(node)
    if oid in seen:
        return None
    if isinstance(node, list):
        seen.add(oid)
        for x in node:
            hit = _walk_stats(x, vid, depth=depth + 1, seen=seen)
            if hit is not None:
                return hit
        return None
    if not isinstance(node, dict):
        return None
    seen.add(oid)
    counts = _pick_stats(node)
    if counts.has_any() and vid in _ids_of(node):
        return counts
    for v in node.values():
        hit = _walk_stats(v, vid, depth=depth + 1, seen=seen)
        if hit is not None:
            return hit
    return None


def _parse_html_stats(html: str, vid: str) -> _ItemCounts | None:
    roots: list[Any] = []
    for m in _SCRIPT_RE.finditer(html or ""):
        body = (m.group(2) or "").strip()
        if not body:
            continue
        parsed = None
        for raw in (body, _quote_bigints(body)):
            try:
                parsed = json.loads(raw)
                break
            except json.JSONDecodeError as e:
                logger.debug("TikTok JSON decode: %s", e)
        if parsed is not None:
            roots.append(parsed)
    for root in roots:
        hit = _targeted(root, vid)
        if hit is not None:
            return hit
    for root in roots:
        hit = _walk_stats(root, vid)
        if hit is not None:
            return hit
    return None


def fetch_reel_stats(
    video_id: str, *, timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
) -> TikTokReelStats:
    vid = extract_tiktok_video_id(video_id)
    if not vid:
        raise ValueError(f"Некорректный id TikTok-ролика: {video_id!r}")
    url = canonical_tiktok_video_url(vid)
    r = requests.get(url, headers=_HTTP_HEADERS, timeout=timeout_s)
    html = r.text or ""
    if r.status_code == 404:
        raise RuntimeError("Ролик недоступен на TikTok.")
    if r.status_code >= 400:
        raise RuntimeError(f"TikTok HTTP {r.status_code}")
    counts = _parse_html_stats(html, vid)
    if counts is None or counts.play is None:
        if _UNAVAILABLE_TITLE_RE.search(html):
            raise RuntimeError("Ролик недоступен на TikTok.")
        raise RuntimeError("Не удалось прочитать просмотры со страницы TikTok.")
    return TikTokReelStats(
        video_id=vid,
        view_count=int(counts.play),
        like_count=counts.likes,
        comment_count=counts.comments,
    )


def fetch_reel_stats_many(
    shortcodes: list[str] | list[dict[str, str]],
    *,
    workers: int = DEFAULT_STATS_WORKERS,
    timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_item: Callable[[TikTokReelStats | None, str, str | None], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[TikTokReelStats], list[tuple[str, str]]]:
    ids: list[str] = []
    seen: set[str] = set()
    for item in shortcodes:
        if isinstance(item, dict):
            raw = str(item.get("video_id") or item.get("url") or "")
        else:
            raw = str(item)
        vid = extract_tiktok_video_id(raw)
        if not vid or vid in seen:
            continue
        seen.add(vid)
        ids.append(vid)
    ok: list[TikTokReelStats] = []
    fail: list[tuple[str, str]] = []
    total = len(ids)
    if total <= 0:
        return ok, fail
    n_workers = max(1, min(int(workers or 1), MAX_STATS_WORKERS, total))
    if on_progress is not None:
        on_progress(0, total, ids[0])
    _log(f"TikTok stats: HTTP workers={n_workers}, роликов={total}")

    def _one(vid: str) -> tuple[str, TikTokReelStats | None, str | None]:
        if should_cancel is not None and should_cancel():
            return vid, None, "Cancelled"
        try:
            return vid, fetch_reel_stats(vid, timeout_s=timeout_s), None
        except Exception as e:
            return vid, None, str(e) or type(e).__name__

    done = 0
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = [pool.submit(_one, vid) for vid in ids]
        for fut in as_completed(futs):
            vid, st, err = fut.result()
            done += 1
            if st is not None:
                ok.append(st)
                if on_item is not None:
                    on_item(st, vid, None)
            else:
                fail.append((vid, err or "unknown"))
                if on_item is not None:
                    on_item(None, vid, err or "unknown")
            if on_progress is not None:
                on_progress(done, total, vid)
    return ok, fail
