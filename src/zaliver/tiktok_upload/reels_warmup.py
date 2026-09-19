"""Прогрев ленты For You: просмотр → лайк/подписка по вероятности → следующее видео."""

from __future__ import annotations

import random
import re
import time
from typing import Any
from urllib.parse import quote

from zaliver.tiktok_upload.tiktok_availability import (
    verify_tiktok_home_available,
)
from zaliver.tiktok_upload.logutil import emit_tiktok_log, tiktok_entrypoint
from zaliver.tiktok_upload.register import TIKTOK_URL, _navigate_page_to


def _log(message: str) -> None:
    emit_tiktok_log(message, tag="[tiktok]")

REELS_URL = "https://www.tiktok.com/"
KEYWORD_SEARCH_URL = "https://www.tiktok.com/search?q="

_FEED_READY_SEL = (
    "#main-content-homepage_hot, "
    "#column-list-container, "
    'article[data-e2e="recommend-list-item-container"], '
    '[data-e2e="feed-video"], '
    '[data-e2e="like-icon"]'
)
_NEXT_BTN_SEL = (
    '[data-e2e="feed-navigation-next"]:not([disabled]), '
    '[data-key-interaction="feed_nav_next"]:not([disabled])'
)

_DEFAULT_REELS_COUNT = 15
_DEFAULT_LIKE_PROB_PCT = 35.0
_DEFAULT_FOLLOW_PROB_PCT = 10.0
_DEFAULT_WATCH_MIN_S = 4.0
_DEFAULT_WATCH_MAX_S = 12.0

_FOLLOW_BTN_RE = re.compile(
    r"^(Follow|Follow back|Подписаться|Подписаться в ответ)$", re.I
)


def _clamp_prob_pct(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, v))


def _roll(prob_pct: float) -> bool:
    p = _clamp_prob_pct(prob_pct)
    if p <= 0:
        return False
    if p >= 100:
        return True
    return random.random() * 100.0 < p


def _click_foryou_nav_if_present(page) -> None:
    """For You / Рекомендации в сайдбаре — чтобы не остаться на Подписках."""
    locators = (
        page.locator('a[data-e2e="nav-foryou"]'),
        page.locator('[data-e2e="nav-foryou"]'),
        page.get_by_role(
            "link", name=re.compile(r"^(for you|рекомендации)$", re.I)
        ),
        page.get_by_role(
            "button", name=re.compile(r"^(for you|рекомендации)$", re.I)
        ),
        page.locator(
            'a[aria-label="For You" i], a[aria-label="Рекомендации" i]'
        ),
    )
    for loc in locators:
        try:
            if int(loc.count()) <= 0:
                continue
            target = loc.first
            if not target.is_visible(timeout=400):
                continue
            target.click(timeout=4_000)
            _log("TikTok: открыли ленту Рекомендации / For You.")
            page.wait_for_timeout(600)
            return
        except Exception:
            continue


def _ensure_reels_feed(page) -> None:
    """Оставить / открыть главную ленту For You."""
    cur = ""
    try:
        cur = (page.url or "").strip()
    except Exception:
        cur = ""
    low = cur.lower()
    if "tiktok.com" in low and "/search" not in low and "/video/" not in low:
        _log(f"TikTok: уже на сайте, URL={cur!r}")
    else:
        _navigate_page_to(page, TIKTOK_URL, label="For You")
        page.wait_for_timeout(1_500)
    _click_foryou_nav_if_present(page)
    try:
        page.wait_for_selector(_FEED_READY_SEL, timeout=45_000)
    except Exception as e:
        _log(f"TikTok: лента For You не прогрузилась: {type(e).__name__}: {e!r}")
        raise RuntimeError(
            "Не удалось открыть ленту TikTok For You "
            f"(URL={(getattr(page, 'url', None) or '')!r})."
        ) from e


def _try_unmute_or_play(page) -> None:
    """Включить звук, если на карточке Volume нажат (mute)."""
    try:
        mute = page.locator(
            '[data-key-interaction="video_mute"] button[aria-label="Volume"], '
            '[data-key-interaction="video_mute"] button[aria-label="Mute"], '
            '[data-key-interaction="video_mute"] button[aria-label="Unmute"], '
            '[data-key-interaction="video_mute"] button[aria-label="Громкость"], '
            '[data-key-interaction="video_mute"] button[aria-label="Звук"], '
            '[data-key-interaction="video_mute"] button[aria-label="Без звука"], '
            '[data-key-interaction="video_mute"] button[aria-label="Выключить звук"]'
        ).first
        if mute.count() and mute.is_visible(timeout=400):
            pressed = (mute.get_attribute("aria-pressed") or "").strip().lower()
            if pressed == "true":
                mute.click(timeout=2_000)
                page.wait_for_timeout(250)
                _log("TikTok: звук включён.")
    except Exception:
        pass
    for aria in (
        "Press to play",
        "Нажмите для воспроизведения",
        "Воспроизвести",
        "Watch in full screen",
        "Смотреть в полноэкранном режиме",
    ):
        try:
            btn = page.locator(f'[aria-label="{aria}"]').first
            if btn.is_visible(timeout=300):
                # Полноэкран не кликаем — только play, если видео на паузе.
                if "play" in aria.lower() or "воспроиз" in aria.lower():
                    btn.click(timeout=2_000)
                    page.wait_for_timeout(250)
                    _log("TikTok: воспроизведение запущено.")
                break
        except Exception:
            continue


def _active_reel_root_handle(page) -> Any | None:
    """Корневой article текущего (играющего / видимого) видео в ленте."""
    try:
        return page.evaluate_handle(
            """() => {
              const articles = Array.from(document.querySelectorAll(
                'article[data-e2e="recommend-list-item-container"]'
              ));
              const videos = Array.from(document.querySelectorAll(
                'article[data-e2e="recommend-list-item-container"] video, '
                + '[data-e2e="feed-video"] video, main video'
              ));
              const vh = window.innerHeight || 800;
              const mid = vh / 2;
              let bestArt = null;
              let bestScore = -1;
              const scoreEl = (el, playingBonus) => {
                const r = el.getBoundingClientRect();
                if (r.height < 40 || r.width < 40) return -1;
                const visible =
                  Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
                const centerDist = Math.abs((r.top + r.bottom) / 2 - mid);
                return playingBonus + visible * 2 - centerDist;
              };
              for (const v of videos) {
                const art = v.closest(
                  'article[data-e2e="recommend-list-item-container"]'
                );
                if (!art) continue;
                const playingBonus = (!v.paused && v.readyState >= 2) ? 5000 : 0;
                const score = scoreEl(v, playingBonus);
                if (score > bestScore) {
                  bestScore = score;
                  bestArt = art;
                }
              }
              if (bestArt) return bestArt;
              for (const art of articles) {
                const score = scoreEl(art, 0);
                if (score > bestScore) {
                  bestScore = score;
                  bestArt = art;
                }
              }
              return bestArt;
            }"""
        )
    except Exception as e:
        _log(f"TikTok: не удалось найти активное видео: {type(e).__name__}")
        return None


def _current_video_src(page) -> str:
    try:
        src = page.evaluate(
            """() => {
              const videos = Array.from(document.querySelectorAll(
                '[role="dialog"] video, article video, main video'
              ));
              const vh = window.innerHeight || 800;
              const mid = vh / 2;
              let best = null, bestScore = -1;
              for (const v of videos) {
                const r = v.getBoundingClientRect();
                if (r.height < 40) continue;
                const visible =
                  Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
                const centerDist = Math.abs((r.top + r.bottom) / 2 - mid);
                const playingBonus = (!v.paused && v.readyState >= 2) ? 5000 : 0;
                const score = playingBonus + visible * 2 - centerDist;
                if (score > bestScore) {
                  bestScore = score;
                  best = v;
                }
              }
              return best ? (best.currentSrc || best.src || '') : '';
            }"""
        )
        return (src or "").strip()
    except Exception:
        return ""


def _current_post_key(page) -> str:
    """Идентификатор текущего видео: data-scroll-index / id карточки + src."""
    try:
        key = page.evaluate(
            """() => {
              const articles = Array.from(document.querySelectorAll(
                'article[data-e2e="recommend-list-item-container"]'
              ));
              const vh = window.innerHeight || 800;
              const mid = vh / 2;
              let bestArt = null;
              let bestSrc = '';
              let bestScore = -1;
              const videos = Array.from(document.querySelectorAll(
                'article[data-e2e="recommend-list-item-container"] video, main video'
              ));
              for (const v of videos) {
                const r = v.getBoundingClientRect();
                if (r.height < 40 || r.width < 40) continue;
                const visible =
                  Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
                const centerDist = Math.abs((r.top + r.bottom) / 2 - mid);
                const playingBonus = (!v.paused && v.readyState >= 2) ? 5000 : 0;
                const score = playingBonus + visible * 2 - centerDist;
                if (score > bestScore) {
                  bestScore = score;
                  bestSrc = v.currentSrc || v.src || '';
                  bestArt = v.closest(
                    'article[data-e2e="recommend-list-item-container"]'
                  );
                }
              }
              if (!bestArt) {
                for (const art of articles) {
                  const r = art.getBoundingClientRect();
                  if (r.height < 40) continue;
                  const visible =
                    Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
                  const centerDist = Math.abs((r.top + r.bottom) / 2 - mid);
                  const score = visible * 2 - centerDist;
                  if (score > bestScore) {
                    bestScore = score;
                    bestArt = art;
                  }
                }
              }
              const idx = bestArt
                ? (bestArt.getAttribute('data-scroll-index')
                   || bestArt.id
                   || '')
                : '';
              return idx + '|' + bestSrc;
            }"""
        )
        return (key or "").strip()
    except Exception:
        return ""


def _wait_for_post_change(
    page,
    prev_key: str,
    *,
    timeout_ms: int = 5_000,
) -> bool:
    """Ждём смены поста после листания (по URL/video), не по одному тику."""
    prev = (prev_key or "").strip()
    deadline = time.monotonic() + max(0.5, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        cur = _current_post_key(page)
        if cur and cur != prev:
            page.wait_for_timeout(350)
            return True
        page.wait_for_timeout(200)
    return False


def _wait_video_ready(page, *, timeout_ms: int = 8_000) -> bool:
    """Дождаться появления видео у текущего поста (пропуск фото)."""
    deadline = time.monotonic() + max(0.5, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        src = _current_video_src(page)
        if src:
            try:
                ready = page.evaluate(
                    """() => {
                      const videos = Array.from(document.querySelectorAll(
                        '[role="dialog"] video, article video, main video'
                      ));
                      const v = videos.find((x) => {
                        const r = x.getBoundingClientRect();
                        return r.height > 40 && r.width > 40;
                      });
                      return !!(v && v.readyState >= 2);
                    }"""
                )
            except Exception:
                ready = False
            if ready:
                return True
        page.wait_for_timeout(250)
    return bool(_current_video_src(page))


def _watch_current_reel(
    page,
    *,
    watch_min_s: float,
    watch_max_s: float,
    watch_full: bool,
) -> None:
    _try_unmute_or_play(page)
    lo = max(1.0, float(watch_min_s))
    hi = max(lo, float(watch_max_s))
    if watch_full:
        # Полный просмотр: ждём окончания video.ended или таймаут по duration.
        try:
            duration = page.evaluate(
                """() => {
                  const videos = Array.from(document.querySelectorAll(
                    '[role="dialog"] video, article video, main video'
                  ));
                  const playing = videos.find(v => !v.paused && v.readyState >= 2);
                  const v = playing || videos[0];
                  if (!v) return 0;
                  const d = Number(v.duration);
                  return Number.isFinite(d) && d > 0 ? d : 0;
                }"""
            )
            duration_f = float(duration or 0)
        except Exception:
            duration_f = 0.0
        if duration_f > 0:
            # Чуть меньше полной длины, чтобы не зависнуть на loop.
            wait_s = min(max(lo, duration_f * 0.95), 90.0)
        else:
            wait_s = random.uniform(lo, hi)
    else:
        wait_s = random.uniform(lo, hi)
    _log(f"TikTok: смотрим ~{wait_s:.1f} с…")
    page.wait_for_timeout(int(wait_s * 1000))


def _element_from_handle(handle) -> Any | None:
    """JSHandle → ElementHandle (у ElementHandle нет .locator() в patchright)."""
    if handle is None:
        return None
    try:
        el = handle.as_element()
    except Exception:
        return None
    return el


def _try_like_in_element(el) -> str:
    """
    Лайк внутри карточки ленты ([data-e2e="like-icon"]).
    Возвращает: 'liked' | 'already' | 'missing' | 'error'.
    """
    try:
        result = el.evaluate(
            """(root) => {
              const like = root.querySelector(
                '[data-e2e="like-icon"], [data-key-interaction="action_like"]'
              );
              if (!like) return 'missing';
              const pressed = (like.getAttribute('aria-pressed') || '').toLowerCase();
              const aria = (like.getAttribute('aria-label') || '');
              if (
                pressed === 'true' ||
                /^(Unlike video|Unlike|Убрать лайк|Больше не нравится|Не нравится)/i.test(aria)
              ) {
                return 'already';
              }
              like.click();
              return 'liked';
            }"""
        )
        if result in ("liked", "already", "missing"):
            return str(result)
        return "missing"
    except Exception:
        return "error"


def _try_follow_in_element(el) -> str:
    """
    Follow внутри карточки ([data-e2e="feed-follow"] или текст Follow/Подписаться).
    Возвращает: 'followed' | 'already' | 'missing' | 'error'.
    """
    try:
        result = el.evaluate(
            """(root) => {
              const plus = root.querySelector('[data-e2e="feed-follow"]');
              if (plus) {
                const style = window.getComputedStyle(plus);
                if (
                  style.display !== 'none' &&
                  style.visibility !== 'hidden' &&
                  plus.getClientRects().length
                ) {
                  plus.click();
                  return 'followed';
                }
              }
              const followRe = /^(Follow|Follow back|Подписаться|Подписаться в ответ)$/i;
              const followingRe =
                /^(Following|Подписки|Requested|Запрошено|Отписаться|Вы подписаны)$/i;
              const buttons = Array.from(
                root.querySelectorAll('[role="button"], button')
              );
              for (const btn of buttons) {
                const text = (btn.innerText || btn.textContent || '')
                  .trim()
                  .split('\\n')[0]
                  .trim();
                if (!text) continue;
                const style = window.getComputedStyle(btn);
                if (
                  style.display === 'none' ||
                  style.visibility === 'hidden' ||
                  btn.getClientRects().length === 0
                ) {
                  continue;
                }
                if (followingRe.test(text)) return 'already';
                if (followRe.test(text)) {
                  btn.click();
                  return 'followed';
                }
              }
              return 'missing';
            }"""
        )
        if result in ("followed", "already", "missing"):
            return str(result)
        return "missing"
    except Exception:
        return "error"


def _try_like_current_reel(page) -> bool:
    root = _active_reel_root_handle(page)
    el = _element_from_handle(root)
    if el is not None:
        status = _try_like_in_element(el)
        if status == "already":
            _log("TikTok: уже лайкнуто — пропуск.")
            return False
        if status == "liked":
            page.wait_for_timeout(400)
            _log("TikTok: лайк поставлен.")
            return True
        if status == "error":
            _log("TikTok: лайк не удался в карточке видео.")

    try:
        result = page.evaluate(
            """() => {
              const articles = Array.from(document.querySelectorAll(
                'article[data-e2e="recommend-list-item-container"]'
              ));
              const vh = window.innerHeight || 800;
              const mid = vh / 2;
              let best = null;
              let bestScore = -1;
              for (const art of articles) {
                const r = art.getBoundingClientRect();
                const visible =
                  Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
                const centerDist = Math.abs((r.top + r.bottom) / 2 - mid);
                const score = visible * 2 - centerDist;
                if (score > bestScore) {
                  bestScore = score;
                  best = art;
                }
              }
              const like = (best || document).querySelector(
                '[data-e2e="like-icon"]'
              );
              if (!like) return 'missing';
              const pressed = (like.getAttribute('aria-pressed') || '').toLowerCase();
              if (pressed === 'true') return 'already';
              like.click();
              return 'liked';
            }"""
        )
        if result == "already":
            _log("TikTok: уже лайкнуто — пропуск.")
            return False
        if result == "liked":
            page.wait_for_timeout(400)
            _log("TikTok: лайк поставлен.")
            return True
    except Exception as e:
        _log(f"TikTok: лайк не удался (page): {type(e).__name__}")
    _log("TikTok: кнопка лайка не найдена.")
    return False


def _try_follow_current_reel(page) -> bool:
    root = _active_reel_root_handle(page)
    el = _element_from_handle(root)
    if el is not None:
        status = _try_follow_in_element(el)
        if status == "already":
            _log("TikTok: уже подписан — пропуск.")
            return False
        if status == "followed":
            page.wait_for_timeout(500)
            _log("TikTok: подписка оформлена.")
            return True
        if status == "error":
            _log("TikTok: Follow не удался в карточке видео.")

    try:
        result = page.evaluate(
            """() => {
              const articles = Array.from(document.querySelectorAll(
                'article[data-e2e="recommend-list-item-container"]'
              ));
              const vh = window.innerHeight || 800;
              const mid = vh / 2;
              let best = null;
              let bestScore = -1;
              for (const art of articles) {
                const r = art.getBoundingClientRect();
                const visible =
                  Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
                const centerDist = Math.abs((r.top + r.bottom) / 2 - mid);
                const score = visible * 2 - centerDist;
                if (score > bestScore) {
                  bestScore = score;
                  best = art;
                }
              }
              const plus = (best || document).querySelector(
                '[data-e2e="feed-follow"]'
              );
              if (!plus) return 'missing';
              plus.click();
              return 'followed';
            }"""
        )
        if result == "followed":
            page.wait_for_timeout(500)
            _log("TikTok: подписка оформлена.")
            return True
    except Exception:
        pass
    try:
        follow_btn = page.get_by_role("button", name=_FOLLOW_BTN_RE).first
        if follow_btn.is_visible(timeout=1_200):
            follow_btn.click(timeout=3_000)
            page.wait_for_timeout(500)
            _log("TikTok: подписка оформлена.")
            return True
    except Exception:
        pass
    _log("TikTok: кнопка Follow не найдена.")
    return False


def _advance_to_next_reel(page, *, prev_src: str, sideways: bool = False) -> bool:
    """Перейти к следующему видео (стрелка вниз / ArrowDown / колесо)."""
    prev_key = _current_post_key(page) or (prev_src or "").strip()

    def _changed(*, timeout_ms: int = 4_500) -> bool:
        if _wait_for_post_change(page, prev_key, timeout_ms=timeout_ms):
            return True
        cur = _current_post_key(page)
        return bool(cur and prev_key and cur != prev_key)

    if sideways:
        for attempt in range(1, 4):
            try:
                page.keyboard.press("ArrowRight")
            except Exception as e:
                _log(f"TikTok: ArrowRight не сработал: {type(e).__name__}")
                return False
            if _changed(timeout_ms=5_000 if attempt == 1 else 6_000):
                return True
            _log(f"TikTok: ArrowRight попытка {attempt}/3 — видео ещё не сменилось.")
        _log("TikTok: не удалось перейти к следующему видео (вправо).")
        return False

    try:
        btn = page.locator(_NEXT_BTN_SEL).first
        if btn.count() and not btn.is_disabled():
            btn.click(timeout=3_000)
            if _changed():
                return True
    except Exception:
        pass

    try:
        card = page.locator('[data-e2e="feed-video"]').first
        if card.count():
            card.click(timeout=1_500)
    except Exception:
        pass

    for key in ("ArrowDown", "PageDown"):
        try:
            page.keyboard.press(key)
            if _changed():
                return True
        except Exception as e:
            _log(f"TikTok: {key} не сработал: {type(e).__name__}")

    try:
        page.mouse.wheel(0, 900)
        if _changed():
            return True
    except Exception:
        pass

    _log("TikTok: не удалось перейти к следующему видео.")
    return False


def _click_search_videos_tab_if_present(page) -> None:
    """Вкладка Videos / Видео в выдаче поиска."""
    locators = (
        page.locator('[data-e2e="search_video-tab"], [data-e2e="search-video-tab"]'),
        page.get_by_role("tab", name=re.compile(r"^(videos|видео)$", re.I)),
        page.get_by_role("link", name=re.compile(r"^(videos|видео)$", re.I)),
        page.get_by_role("button", name=re.compile(r"^(videos|видео)$", re.I)),
    )
    for loc in locators:
        try:
            if int(loc.count()) <= 0:
                continue
            target = loc.first
            if not target.is_visible(timeout=400):
                continue
            target.click(timeout=3_000)
            _log("TikToks: вкладка Видео / Videos в поиске.")
            page.wait_for_timeout(800)
            return
        except Exception:
            continue


def _keyword_search_url(query: str) -> str:
    """URL выдачи TikTok keyword search (q URL-encoded)."""
    q = (query or "").strip()
    return f"{KEYWORD_SEARCH_URL}{quote(q, safe='')}"


def _open_keyword_search(page, query: str) -> str:
    """Открыть /explore/search/keyword/?q=… и дождаться сетки постов."""
    q = (query or "").strip()
    if not q:
        raise RuntimeError("Пустой поисковый запрос для прогрева Тиктоков.")
    url = _keyword_search_url(q)
    _log(f"TikToks: открываем поиск по запросу «{q}» → {url}")
    _navigate_page_to(page, url, label="TikTok keyword search")
    page.wait_for_timeout(1_800)
    _click_search_videos_tab_if_present(page)
    try:
        page.wait_for_selector(
            'main a[href*="/video/"], main a[href*="/p/"], main a[href*="/reel/"]',
            timeout=45_000,
        )
    except Exception as e:
        _log(f"TikToks: сетка поиска не прогрузилась: {type(e).__name__}: {e!r}")
        raise RuntimeError(
            f"Не удалось открыть выдачу TikTok по запросу {q!r} "
            f"(URL={(getattr(page, 'url', None) or '')!r})."
        ) from e
    return url


def _open_first_keyword_search_reel(page) -> bool:
    """Клик по первому рилсу в сетке keyword search (слева сверху)."""
    try:
        href = page.evaluate(
            """() => {
              const links = Array.from(
                document.querySelectorAll(
                  'main a[href*="/video/"], main a[href*="/p/"], main a[href*="/reel/"]'
                )
              );
              const isReel = (a) => {
                const href = a.getAttribute('href') || '';
                if (!(href.includes('/video/') || href.includes('/p/') || href.includes('/reel/'))) return false;
                if (href.includes('/video/')) return true;
                if (a.querySelector('video')) return true;
                const svgs = Array.from(a.querySelectorAll('svg[aria-label], svg title'));
                for (const node of svgs) {
                  const label = (
                    node.getAttribute('aria-label') ||
                    node.textContent ||
                    ''
                  ).toLowerCase();
                  if (
                    label.includes('reel') ||
                    label.includes('clip') ||
                    label.includes('видео') ||
                    label.includes('video') ||
                    label.includes('тикток')
                  ) {
                    return true;
                  }
                }
                return false;
              };
              const scored = [];
              for (const a of links) {
                if (!isReel(a)) continue;
                const r = a.getBoundingClientRect();
                if (r.width < 40 || r.height < 40) continue;
                if (r.bottom < 0 || r.top > (window.innerHeight || 800)) continue;
                scored.push({
                  href: a.getAttribute('href') || '',
                  top: r.top,
                  left: r.left,
                });
              }
              scored.sort((a, b) => (a.top - b.top) || (a.left - b.left));
              return scored.length ? scored[0].href : '';
            }"""
        )
    except Exception as e:
        _log(f"TikToks: поиск первого ролика в выдаче: {type(e).__name__}: {e!r}")
        href = ""

    href = (href or "").strip()
    candidates: list[str] = []
    if href:
        candidates.append(f'a[href="{href}"]')
        if href.startswith("/"):
            candidates.append(f'a[href="{href.split("?")[0]}"]')
    candidates.extend(
        (
            'main a[href*="/video/"]',
            'main a[href*="/p/"]:has(svg[aria-label*="Reel" i])',
            'main a[href*="/p/"]:has(svg[aria-label*="Видео" i])',
            'main a[href*="/p/"]:has(svg[aria-label*="Clip" i])',
            'main a[href*="/reel/"]',
            'main a[href*="/p/"]:has(video)',
        )
    )

    for sel in candidates:
        try:
            link = page.locator(sel).first
            if not link.is_visible(timeout=2_500):
                continue
            got_href = (link.get_attribute("href") or href or "").strip()
            _log(f"TikToks: открываем первый ролик из поиска ({got_href!r})…")
            link.click(timeout=5_000)
            page.wait_for_timeout(1_200)
            try:
                page.wait_for_selector(
                    'article video, [role="dialog"] video, main video',
                    timeout=25_000,
                )
            except Exception:
                pass
            return True
        except Exception as e:
            _log(f"TikToks: клик по ролику поиска ({sel!r}): {type(e).__name__}")
            continue
    return False


def _watch_reels_from_current_player(
    page,
    *,
    remaining: int,
    like_probability_pct: float,
    follow_once: bool,
    follow_probability_pct: float,
    watch_min_s: float,
    watch_max_s: float,
    watch_full: bool,
    sideways: bool,
) -> int:
    """
    Смотреть рилсы в текущем плеере, пока не кончатся или не наберём remaining.
    Возвращает число просмотренных.
    """
    n = max(0, int(remaining))
    if n <= 0:
        return 0
    liked_p = _clamp_prob_pct(like_probability_pct)
    follow_p = _clamp_prob_pct(follow_probability_pct)
    followed = False
    watched = 0
    # В поиске между рилсами бывают фото — пропускаем без засчёта в count.
    skip_budget = max(40, n * 8) if sideways else 0
    skips = 0

    while watched < n:
        if sideways and not _current_video_src(page):
            if skips >= skip_budget:
                _log("TikToks: слишком много постов без видео — останавливаем.")
                break
            prev_src = _current_video_src(page)
            _log("TikToks: пост без видео — листаем вправо без засчёта.")
            if not _advance_to_next_reel(page, prev_src=prev_src, sideways=True):
                _log("TikToks: дальше листать не удалось.")
                break
            skips += 1
            page.wait_for_timeout(random.randint(500, 900))
            continue

        if sideways:
            _wait_video_ready(page, timeout_ms=8_000)

        i = watched + 1
        prev_src = _current_video_src(page)
        prev_key = _current_post_key(page)
        _log(f"TikToks: Тикток {i}/{n} в текущем плеере…")
        _watch_current_reel(
            page,
            watch_min_s=watch_min_s,
            watch_max_s=watch_max_s,
            watch_full=watch_full,
        )
        watched += 1
        if _roll(liked_p):
            _try_like_current_reel(page)
        else:
            _log("TikToks: лайк пропущен по вероятности.")
        if follow_once:
            # Подписка один раз на аккаунт (с учётом вероятности).
            if not followed:
                if _roll(follow_p):
                    if _try_follow_current_reel(page):
                        followed = True
                else:
                    _log("TikToks: подписка пропущена по вероятности.")
                    followed = True  # больше не пытаемся на этом аккаунте
        else:
            if _roll(follow_p):
                _try_follow_current_reel(page)
            else:
                _log("TikToks: подписка пропущена по вероятности.")
        if watched >= n:
            break
        if not _advance_to_next_reel(
            page,
            prev_src=prev_src or prev_key,
            sideways=sideways,
        ):
            _log("TikToks: дальше листать не удалось.")
            break
        # Дать модалке догрузить следующий пост до старта просмотра.
        page.wait_for_timeout(random.randint(800, 1_400))
        if sideways:
            _wait_video_ready(page, timeout_ms=6_000)
    return watched


def browse_tiktok_reels_from_search(
    page,
    query: str,
    *,
    count: int = _DEFAULT_REELS_COUNT,
    like_probability_pct: float = _DEFAULT_LIKE_PROB_PCT,
    follow_probability_pct: float = _DEFAULT_FOLLOW_PROB_PCT,
    watch_min_s: float = _DEFAULT_WATCH_MIN_S,
    watch_max_s: float = _DEFAULT_WATCH_MAX_S,
    watch_full: bool = True,
) -> None:
    """
    Прогрев по поиску: /explore/search/keyword/?q=… → первый рилс в сетке →
    просмотр с листанием вправо. Лайк и подписка — по заданной вероятности.
    """
    q = (query or "").strip()
    if not q:
        raise RuntimeError("Пустой поисковый запрос для прогрева Тиктоков.")
    n = max(1, int(count))
    like_p = _clamp_prob_pct(like_probability_pct)
    follow_p = _clamp_prob_pct(follow_probability_pct)
    _log(
        f"TikToks: прогрев по поиску «{q}» — {n} шт., лайк {like_p:.0f}%, "
        f"подписка {follow_p:.0f}%."
    )

    _open_keyword_search(page, q)
    if not _open_first_keyword_search_reel(page):
        raise RuntimeError(
            f"По запросу {q!r} не удалось открыть первый ролик в выдаче."
        )
    if not _wait_video_ready(page, timeout_ms=12_000):
        _log("TikToks: видео первого ролика ещё не готово — продолжаем.")

    watched = _watch_reels_from_current_player(
        page,
        remaining=n,
        like_probability_pct=like_p,
        follow_once=False,
        follow_probability_pct=follow_p,
        watch_min_s=watch_min_s,
        watch_max_s=watch_max_s,
        watch_full=watch_full,
        sideways=True,
    )
    _log(f"TikToks: прогрев по поиску завершён ({watched}/{n}).")
    if watched <= 0:
        raise RuntimeError(
            f"Прогрев Тиктоков по поиску «{q}» не просмотрел ни одного ролика."
        )


def browse_tiktok_reels(
    page,
    *,
    count: int = _DEFAULT_REELS_COUNT,
    like_probability_pct: float = _DEFAULT_LIKE_PROB_PCT,
    follow_probability_pct: float = _DEFAULT_FOLLOW_PROB_PCT,
    watch_min_s: float = _DEFAULT_WATCH_MIN_S,
    watch_max_s: float = _DEFAULT_WATCH_MAX_S,
    watch_full: bool = True,
) -> None:
    """Просмотр ленты For You с вероятностными лайком и подпиской."""
    n = max(1, int(count))
    like_p = _clamp_prob_pct(like_probability_pct)
    follow_p = _clamp_prob_pct(follow_probability_pct)
    _log(
        f"TikTok: старт прогрева — {n} шт., лайк {like_p:.0f}%, "
        f"подписка {follow_p:.0f}%, просмотр "
        f"{'полный' if watch_full else f'{watch_min_s:.0f}–{watch_max_s:.0f} с'}."
    )
    _ensure_reels_feed(page)

    for i in range(1, n + 1):
        prev_src = _current_video_src(page)
        _log(f"TikTok: видео {i}/{n}…")
        _watch_current_reel(
            page,
            watch_min_s=watch_min_s,
            watch_max_s=watch_max_s,
            watch_full=watch_full,
        )
        if _roll(like_p):
            _try_like_current_reel(page)
        else:
            _log("TikTok: лайк пропущен по вероятности.")
        if _roll(follow_p):
            _try_follow_current_reel(page)
        else:
            _log("TikTok: подписка пропущена по вероятности.")
        if i < n:
            if not _advance_to_next_reel(page, prev_src=prev_src):
                _log(f"TikTok: останов на {i}/{n} — лента не прокрутилась.")
                break
            page.wait_for_timeout(random.randint(400, 900))

    _log(f"TikTok: прогрев завершён ({n} запрошено).")


@tiktok_entrypoint
def run_tiktok_reels_warmup(
    page,
    *,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    reels_count: int = _DEFAULT_REELS_COUNT,
    like_probability_pct: float = _DEFAULT_LIKE_PROB_PCT,
    follow_probability_pct: float = _DEFAULT_FOLLOW_PROB_PCT,
    watch_min_s: float = _DEFAULT_WATCH_MIN_S,
    watch_max_s: float = _DEFAULT_WATCH_MAX_S,
    watch_full: bool = True,
    reels_recommendations: bool = True,
    search_query: str = "",
    profile_id: str | None = None,
) -> None:
    """Главная TikTok (лента For You) → вход → просмотр / лайк / листание."""
    _log("TikTok: проверка сессии / доступности…")
    verify_tiktok_home_available(
        page,
        session_login=session_login,
        session_password=session_password,
        session_twofa=session_twofa,
        profile_id=profile_id,
    )
    q = (search_query or "").strip()
    # Рекомендации по умолчанию; поиск — если галочка снята.
    if not reels_recommendations:
        if not q:
            raise RuntimeError(
                "Укажите поисковый запрос для прогрева Тиктоков "
                "или включите рекомендации."
            )
        browse_tiktok_reels_from_search(
            page,
            q,
            count=reels_count,
            like_probability_pct=like_probability_pct,
            follow_probability_pct=follow_probability_pct,
            watch_min_s=watch_min_s,
            watch_max_s=watch_max_s,
            watch_full=watch_full,
        )
        return
    browse_tiktok_reels(
        page,
        count=reels_count,
        like_probability_pct=like_probability_pct,
        follow_probability_pct=follow_probability_pct,
        watch_min_s=watch_min_s,
        watch_max_s=watch_max_s,
        watch_full=watch_full,
    )
