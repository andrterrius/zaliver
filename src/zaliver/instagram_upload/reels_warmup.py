"""Прогрев ленты Instagram Reels: просмотр → лайк/подписка по вероятности → далее."""

from __future__ import annotations

import random
import re
from typing import Any

from zaliver.instagram_upload.instagram_availability import (
    verify_instagram_home_available,
)
from zaliver.instagram_upload.logutil import emit_instagram_log, instagram_entrypoint
from zaliver.instagram_upload.register import _navigate_page_to


def _log(message: str) -> None:
    emit_instagram_log(message, tag="[instagram]")

REELS_URL = "https://www.instagram.com/reels/"

_DEFAULT_REELS_COUNT = 15
_DEFAULT_LIKE_PROB_PCT = 35.0
_DEFAULT_FOLLOW_PROB_PCT = 10.0
_DEFAULT_WATCH_MIN_S = 4.0
_DEFAULT_WATCH_MAX_S = 12.0

_FOLLOW_BTN_RE = re.compile(r"^(Follow|Подписаться)$", re.I)
_FOLLOWING_BTN_RE = re.compile(
    r"^(Following|Подписки|Requested|Запрошено|Отписаться)$", re.I
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


def _ensure_reels_feed(page) -> None:
    """Открыть ленту Reels (после успешной проверки/входа на главной)."""
    cur = ""
    try:
        cur = (page.url or "").strip()
    except Exception:
        cur = ""
    if "/reels" in cur.lower():
        _log(f"Reels: уже на ленте, URL={cur!r}")
        return
    _navigate_page_to(page, REELS_URL, label="Reels")
    page.wait_for_timeout(1_500)
    try:
        page.wait_for_selector(
            'main video, svg[aria-label="Like"], svg[aria-label="Нравится"], '
            '[aria-label="Navigate to next Reel"]',
            timeout=45_000,
        )
    except Exception as e:
        _log(f"Reels: лента не прогрузилась вовремя: {type(e).__name__}: {e!r}")
        raise RuntimeError(
            "Не удалось открыть ленту Instagram Reels "
            f"(URL={(getattr(page, 'url', None) or '')!r})."
        ) from e


def _try_unmute_or_play(page) -> None:
    """Снять mute / нажать Play, если рилс на паузе."""
    for aria in ("Audio is muted", "Звук выключен", "Включить звук"):
        try:
            svg = page.locator(f'svg[aria-label="{aria}"]').first
            if svg.is_visible(timeout=400):
                btn = svg.locator(
                    "xpath=ancestor::*[@role='button'][1]"
                )
                target = btn if btn.count() else svg
                target.click(timeout=2_000)
                page.wait_for_timeout(250)
                _log("Reels: звук включён.")
                break
        except Exception:
            continue
    for aria in ("Press to play", "Нажмите для воспроизведения", "Воспроизвести"):
        try:
            btn = page.locator(f'[aria-label="{aria}"]').first
            if btn.is_visible(timeout=400):
                btn.click(timeout=2_000)
                page.wait_for_timeout(250)
                _log("Reels: воспроизведение запущено.")
                break
        except Exception:
            continue


def _active_reel_root_handle(page) -> Any | None:
    """Корневой контейнер текущего (играющего / видимого) рилса."""
    try:
        return page.evaluate_handle(
            """() => {
              const videos = Array.from(document.querySelectorAll('main video'));
              if (!videos.length) return null;
              let best = null;
              let bestScore = -1;
              const vh = window.innerHeight || 800;
              const mid = vh / 2;
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
                  best = v;
                }
              }
              if (!best) return null;
              let root =
                best.closest('article') ||
                best.closest('[role="presentation"]')?.parentElement ||
                best.closest('div[style*="--x-height"]') ||
                best.parentElement;
              // Поднимаемся, пока в контейнере нет Like / Follow
              // (кнопки часто рядом с видео, не внутри самого <video>-блока).
              const hasActions = (node) => {
                if (!node || !node.querySelector) return false;
                return !!(
                  node.querySelector(
                    'svg[aria-label="Like"], svg[aria-label="Нравится"], ' +
                    'svg[aria-label="Unlike"], svg[aria-label="Не нравится"]'
                  ) ||
                  Array.from(
                    node.querySelectorAll('[role="button"], button')
                  ).some((b) =>
                    /^(Follow|Подписаться|Following|Подписки)$/i.test(
                      ((b.innerText || b.textContent || '').trim().split('\\n')[0] || '')
                    )
                  )
                );
              };
              let guard = 0;
              while (root && root !== document.body && guard < 12) {
                if (hasActions(root)) break;
                root = root.parentElement;
                guard += 1;
              }
              return root;
            }"""
        )
    except Exception as e:
        _log(f"Reels: не удалось найти активный рилс: {type(e).__name__}")
        return None


def _current_video_src(page) -> str:
    try:
        src = page.evaluate(
            """() => {
              const videos = Array.from(document.querySelectorAll('main video'));
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
                  const videos = Array.from(document.querySelectorAll('main video'));
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
    _log(f"Reels: смотрим ~{wait_s:.1f} с…")
    page.wait_for_timeout(int(wait_s * 1000))


_UNLIKE_SVG_SEL = (
    'svg[aria-label="Unlike"], svg[aria-label="Не нравится"], '
    'svg[aria-label="Liked"]'
)
_LIKE_SVG_SEL = 'svg[aria-label="Like"], svg[aria-label="Нравится"]'


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
    Лайк внутри корня рилса через query_selector/click.
    Возвращает: 'liked' | 'already' | 'missing' | 'error'.
    """
    try:
        unlike = el.query_selector(_UNLIKE_SVG_SEL)
        if unlike is not None:
            try:
                if unlike.is_visible():
                    return "already"
            except Exception:
                return "already"
        like_svg = el.query_selector(_LIKE_SVG_SEL)
        if like_svg is None:
            return "missing"
        try:
            if not like_svg.is_visible():
                return "missing"
        except Exception:
            return "missing"
        # Кликаем по кнопке-предку, иначе по самой svg.
        clicked = el.evaluate(
            """(root) => {
              const like = root.querySelector(
                'svg[aria-label="Like"], svg[aria-label="Нравится"]'
              );
              if (!like) return false;
              const btn = like.closest('[role="button"]');
              (btn || like).click();
              return true;
            }"""
        )
        return "liked" if clicked else "missing"
    except Exception:
        return "error"


def _try_follow_in_element(el) -> str:
    """
    Follow внутри корня рилса.
    Возвращает: 'followed' | 'already' | 'missing' | 'error'.
    """
    try:
        result = el.evaluate(
            """(root) => {
              const followRe = /^(Follow|Подписаться)$/i;
              const followingRe =
                /^(Following|Подписки|Requested|Запрошено|Отписаться)$/i;
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
            _log("Reels: уже лайкнуто — пропуск.")
            return False
        if status == "liked":
            page.wait_for_timeout(400)
            _log("Reels: лайк поставлен.")
            return True
        if status == "error":
            _log("Reels: лайк не удался в корне рилса.")

    # Fallback: page.locator (у Page метод есть).
    try:
        unlike = page.locator(_UNLIKE_SVG_SEL)
        if unlike.count() and unlike.first.is_visible(timeout=500):
            _log("Reels: уже лайкнуто — пропуск.")
            return False
    except Exception:
        pass
    try:
        like_svg = page.locator(_LIKE_SVG_SEL).first
        if like_svg.is_visible(timeout=1_500):
            btn = like_svg.locator("xpath=ancestor::*[@role='button'][1]")
            target = btn if btn.count() else like_svg
            target.click(timeout=3_000)
            page.wait_for_timeout(400)
            _log("Reels: лайк поставлен.")
            return True
    except Exception as e:
        _log(f"Reels: лайк не удался (page): {type(e).__name__}")
    _log("Reels: кнопка лайка не найдена.")
    return False


def _try_follow_current_reel(page) -> bool:
    root = _active_reel_root_handle(page)
    el = _element_from_handle(root)
    if el is not None:
        status = _try_follow_in_element(el)
        if status == "already":
            _log("Reels: уже подписан / запрошено — пропуск.")
            return False
        if status == "followed":
            page.wait_for_timeout(500)
            _log("Reels: подписка оформлена.")
            return True
        if status == "error":
            _log("Reels: Follow не удался в корне рилса.")

    # Fallback по всей странице (Follow часто только в карточке рилса —
    # ищем среди видимых кнопок с точным текстом).
    try:
        follow_btn = page.get_by_role(
            "button", name=_FOLLOW_BTN_RE
        ).first
        if follow_btn.is_visible(timeout=1_200):
            follow_btn.scroll_into_view_if_needed(timeout=2_000)
            follow_btn.click(timeout=3_000)
            page.wait_for_timeout(500)
            _log("Reels: подписка оформлена.")
            return True
    except Exception:
        pass
    try:
        following = page.get_by_role("button", name=_FOLLOWING_BTN_RE).first
        if following.is_visible(timeout=400):
            _log("Reels: уже подписан / запрошено — пропуск.")
            return False
    except Exception:
        pass
    try:
        buttons = page.locator('[role="button"]')
        n = min(buttons.count(), 80)
        for i in range(n):
            btn = buttons.nth(i)
            try:
                if not btn.is_visible(timeout=300):
                    continue
                text = (btn.inner_text(timeout=400) or "").strip().split("\n")[0].strip()
            except Exception:
                continue
            if not text:
                continue
            if _FOLLOWING_BTN_RE.match(text):
                _log("Reels: уже подписан / запрошено — пропуск.")
                return False
            if _FOLLOW_BTN_RE.match(text):
                btn.scroll_into_view_if_needed(timeout=2_000)
                btn.click(timeout=3_000)
                page.wait_for_timeout(500)
                _log("Reels: подписка оформлена.")
                return True
    except Exception as e:
        _log(f"Reels: Follow не удался (page): {type(e).__name__}")
    _log("Reels: кнопка Follow не найдена.")
    return False


def _advance_to_next_reel(page, *, prev_src: str) -> bool:
    """Перейти к следующему рилсу (кнопка Далее / ArrowDown / свайп)."""
    # 1) Кнопка навигации
    for aria in (
        "Navigate to next Reel",
        "Следующий Reel",
        "Перейти к следующему Reel",
    ):
        try:
            btn = page.locator(f'[aria-label="{aria}"]').first
            if btn.is_visible(timeout=800):
                btn.click(timeout=3_000)
                page.wait_for_timeout(800)
                new_src = _current_video_src(page)
                if new_src and new_src != prev_src:
                    return True
                if not prev_src:
                    return True
        except Exception:
            continue

    # 2) Клавиша вниз
    try:
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(900)
        new_src = _current_video_src(page)
        if new_src and new_src != prev_src:
            return True
        if not prev_src:
            return True
    except Exception as e:
        _log(f"Reels: ArrowDown не сработал: {type(e).__name__}")

    # 3) Колесо мыши
    try:
        page.mouse.wheel(0, 900)
        page.wait_for_timeout(900)
        new_src = _current_video_src(page)
        if new_src and new_src != prev_src:
            return True
    except Exception:
        pass

    _log("Reels: не удалось перейти к следующему рилсу.")
    return False


def browse_instagram_reels(
    page,
    *,
    count: int = _DEFAULT_REELS_COUNT,
    like_probability_pct: float = _DEFAULT_LIKE_PROB_PCT,
    follow_probability_pct: float = _DEFAULT_FOLLOW_PROB_PCT,
    watch_min_s: float = _DEFAULT_WATCH_MIN_S,
    watch_max_s: float = _DEFAULT_WATCH_MAX_S,
    watch_full: bool = False,
) -> None:
    """Просмотр ленты Reels с вероятностными лайком и подпиской."""
    n = max(1, int(count))
    like_p = _clamp_prob_pct(like_probability_pct)
    follow_p = _clamp_prob_pct(follow_probability_pct)
    _log(
        f"Reels: старт прогрева — {n} шт., лайк {like_p:.0f}%, "
        f"подписка {follow_p:.0f}%, просмотр "
        f"{'полный' if watch_full else f'{watch_min_s:.0f}–{watch_max_s:.0f} с'}."
    )
    _ensure_reels_feed(page)

    for i in range(1, n + 1):
        prev_src = _current_video_src(page)
        _log(f"Reels: рилс {i}/{n}…")
        _watch_current_reel(
            page,
            watch_min_s=watch_min_s,
            watch_max_s=watch_max_s,
            watch_full=watch_full,
        )
        if _roll(like_p):
            _try_like_current_reel(page)
        else:
            _log("Reels: лайк пропущен по вероятности.")
        if _roll(follow_p):
            _try_follow_current_reel(page)
        else:
            _log("Reels: подписка пропущена по вероятности.")
        if i < n:
            if not _advance_to_next_reel(page, prev_src=prev_src):
                _log(f"Reels: останов на {i}/{n} — лента не прокрутилась.")
                break
            page.wait_for_timeout(random.randint(400, 900))

    _log(f"Reels: прогрев завершён ({n} запрошено).")


@instagram_entrypoint
def run_instagram_reels_warmup(
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
    watch_full: bool = False,
    profile_id: str | None = None,
) -> None:
    """Главная Instagram → при необходимости вход → /reels/ → прогрев."""
    _log("Reels: проверка сессии / доступности Instagram…")
    verify_instagram_home_available(
        page,
        session_login=session_login,
        session_password=session_password,
        session_twofa=session_twofa,
        profile_id=profile_id,
    )
    browse_instagram_reels(
        page,
        count=reels_count,
        like_probability_pct=like_probability_pct,
        follow_probability_pct=follow_probability_pct,
        watch_min_s=watch_min_s,
        watch_max_s=watch_max_s,
        watch_full=watch_full,
    )
