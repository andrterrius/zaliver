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
EXPLORE_URL = "https://www.instagram.com/explore/"

_DEFAULT_REELS_COUNT = 15
_DEFAULT_LIKE_PROB_PCT = 35.0
_DEFAULT_FOLLOW_PROB_PCT = 10.0
_DEFAULT_WATCH_MIN_S = 4.0
_DEFAULT_WATCH_MAX_S = 12.0
_SEARCH_TOP_ACCOUNTS = 3
_SEARCH_POOL_MAX = 15

_FOLLOW_BTN_RE = re.compile(r"^(Follow|Подписаться)$", re.I)
_FOLLOWING_BTN_RE = re.compile(
    r"^(Following|Подписки|Requested|Запрошено|Отписаться)$", re.I
)
_RESERVED_PATH_SEGMENTS = frozenset(
    {
        "explore",
        "reels",
        "p",
        "reel",
        "stories",
        "direct",
        "accounts",
        "about",
        "legal",
        "tags",
        "locations",
        "tv",
        "graphql",
        "api",
        "web",
        "notifications",
        "popular",
        "nametag",
        "directory",
        "challenge",
        "privacy",
        "safety",
        "meta",
        "ads",
        "oembed",
        "publicapi",
        "your_activity",
        "professional_dashboard",
    }
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


def _advance_to_next_reel(page, *, prev_src: str, sideways: bool = False) -> bool:
    """Перейти к следующему рилсу (кнопка Далее / стрелки / свайп)."""
    # 1) Кнопка навигации (лента рекомендаций и модалка профиля)
    for aria in (
        "Navigate to next Reel",
        "Следующий Reel",
        "Перейти к следующему Reel",
        "Next",
        "Далее",
        "Следующее",
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

    # 2) Клавиши: в модалке профиля — вправо, в ленте — вниз
    keys = ("ArrowRight", "ArrowDown") if sideways else ("ArrowDown", "ArrowRight")
    for key in keys:
        try:
            page.keyboard.press(key)
            page.wait_for_timeout(900)
            new_src = _current_video_src(page)
            if new_src and new_src != prev_src:
                return True
            if not prev_src:
                return True
        except Exception as e:
            _log(f"Reels: {key} не сработал: {type(e).__name__}")

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


def _username_from_profile_href(href: str) -> str:
    h = (href or "").strip()
    if not h:
        return ""
    try:
        from urllib.parse import urlparse

        path = urlparse(h if "://" in h else f"https://www.instagram.com{h}").path
    except Exception:
        path = h
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return ""
    user = parts[0].strip().lstrip("@")
    if not user or user.lower() in _RESERVED_PATH_SEGMENTS:
        return ""
    if parts[-1].lower() in {"reels", "tagged", "followers", "following"}:
        if len(parts) >= 2:
            user = parts[0].strip().lstrip("@")
    return user


_SEARCH_INPUT_SELECTORS = (
    'input[aria-label="Search input"]',
    'input[placeholder="Search"]',
    'input[aria-label="Поиск"]',
    'input[placeholder="Поиск"]',
    'input[aria-label="Search Input"]',
    'input[type="text"][placeholder*="Search" i]',
    'input[type="text"][placeholder*="Поиск" i]',
)


def _find_instagram_search_input(page):
    for sel in _SEARCH_INPUT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=1_500):
                return loc
        except Exception:
            continue
    return None


def _activate_instagram_search_input(page):
    """Снять оверлей «Search» поверх input и сфокусировать поле."""
    # На Explore пустое поле закрыто div[role=button] с иконкой Search.
    for sel in (
        'input[aria-label="Search input"] ~ div[role="button"]',
        'input[placeholder="Search"] ~ div[role="button"]',
        'input[aria-label="Поиск"] ~ div[role="button"]',
        'input[placeholder="Поиск"] ~ div[role="button"]',
    ):
        try:
            overlay = page.locator(sel).first
            if overlay.is_visible(timeout=800):
                overlay.click(timeout=3_000)
                page.wait_for_timeout(250)
                _log("Reels: клик по оверлею Search.")
                break
        except Exception:
            continue
    # Левый сайдбар: Search / Поиск (если Explore ещё без поля).
    for aria in ("Search", "Поиск"):
        try:
            nav = page.locator(
                f'a[role="link"]:has(svg[aria-label="{aria}"]), '
                f'span:has(> svg[aria-label="{aria}"]), '
                f'div[role="button"]:has(svg[aria-label="{aria}"])'
            ).first
            if nav.is_visible(timeout=600):
                # Не кликаем оверлей Explore повторно — только nav link.
                tag = ""
                try:
                    tag = (nav.evaluate("(n) => (n.tagName || '').toLowerCase()") or "")
                except Exception:
                    tag = ""
                if tag == "a":
                    nav.click(timeout=3_000)
                    page.wait_for_timeout(400)
                    _log("Reels: открыт поиск из навигации.")
                    break
        except Exception:
            continue
    search_input = _find_instagram_search_input(page)
    if search_input is None:
        return None
    try:
        search_input.click(timeout=3_000, force=True)
    except Exception:
        try:
            search_input.evaluate("(node) => node.focus()")
        except Exception:
            pass
    page.wait_for_timeout(150)
    return search_input


def _set_instagram_search_value(page, search_input, query: str) -> str:
    """Ввод запроса: fill → press_sequentially → keyboard → JS InputEvent."""
    q = (query or "").strip()
    if not q:
        return ""

    def _read() -> str:
        try:
            return (search_input.input_value(timeout=2_000) or "").strip()
        except Exception:
            try:
                return (
                    search_input.evaluate("(n) => (n.value || '').trim()") or ""
                ).strip()
            except Exception:
                return ""

    def _clear() -> None:
        try:
            search_input.evaluate(
                """(node) => {
                  node.focus();
                  const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                  )?.set;
                  if (setter) setter.call(node, '');
                  else node.value = '';
                  node.dispatchEvent(new Event('input', { bubbles: true }));
                }"""
            )
        except Exception:
            pass
        try:
            search_input.press("Control+A")
            search_input.press("Backspace")
        except Exception:
            pass

    # 1) Playwright fill (часто стабильнее на React-контролах).
    try:
        _clear()
        search_input.fill(q, timeout=5_000)
        page.wait_for_timeout(350)
        actual = _read()
        if actual:
            return actual
    except Exception as e:
        _log(f"Reels: fill поиска не сработал: {type(e).__name__}")

    # 2) Посимвольно.
    try:
        search_input = _activate_instagram_search_input(page) or search_input
        _clear()
        search_input.press_sequentially(q, delay=25, timeout=20_000)
        page.wait_for_timeout(400)
        actual = _read()
        if actual:
            return actual
    except Exception as e:
        _log(f"Reels: press_sequentially поиска: {type(e).__name__}")

    # 3) keyboard.type после фокуса.
    try:
        search_input = _activate_instagram_search_input(page) or search_input
        _clear()
        search_input.click(timeout=3_000, force=True)
        page.keyboard.type(q, delay=20)
        page.wait_for_timeout(400)
        actual = _read()
        if actual:
            return actual
    except Exception as e:
        _log(f"Reels: keyboard.type поиска: {type(e).__name__}")

    # 4) Native value setter + InputEvent (React).
    try:
        search_input = _find_instagram_search_input(page) or search_input
        ok = search_input.evaluate(
            """(node, text) => {
              try {
                node.focus();
                const setter = Object.getOwnPropertyDescriptor(
                  window.HTMLInputElement.prototype, 'value'
                )?.set;
                if (setter) setter.call(node, text);
                else node.value = text;
                node.dispatchEvent(
                  new InputEvent('input', {
                    bubbles: true,
                    cancelable: true,
                    inputType: 'insertText',
                    data: text,
                  })
                );
                node.dispatchEvent(new Event('change', { bubbles: true }));
                return (node.value || '').trim();
              } catch (e) {
                return '';
              }
            }""",
            q,
        )
        page.wait_for_timeout(500)
        actual = (ok or "").strip() or _read()
        if actual:
            return actual
    except Exception as e:
        _log(f"Reels: JS-ввод поиска: {type(e).__name__}")

    return _read()


def _type_instagram_search_query(page, query: str) -> bool:
    q = (query or "").strip()
    if not q:
        return False
    search_input = _activate_instagram_search_input(page)
    if search_input is None:
        _log("Reels: поле поиска Instagram не найдено.")
        return False
    try:
        actual = _set_instagram_search_value(page, search_input, q)
        if not actual:
            _log("Reels: поле поиска пустое после ввода.")
            return False
        _log(f"Reels: поисковый запрос введён: {actual!r}")
        return True
    except Exception as e:
        _log(f"Reels: не удалось ввести поиск: {type(e).__name__}: {e!r}")
        return False


def _current_instagram_username(page) -> str:
    """Юзернейм залогиненного аккаунта (сайдбар Profile), иначе ''."""
    try:
        user = page.evaluate(
            """() => {
              const reserved = new Set([
                'explore','reels','p','reel','stories','direct','accounts','about',
                'legal','tags','locations','tv','graphql','api','web','notifications',
                'popular'
              ]);
              const parse = (href) => {
                const path = (href || '').split('?')[0].split('#')[0];
                const parts = path.replace(/^\\/+|\\/+$/g, '').split('/').filter(Boolean);
                if (parts.length !== 1) return '';
                const u = parts[0];
                if (!u || reserved.has(u.toLowerCase())) return '';
                return u;
              };
              const links = Array.from(
                document.querySelectorAll('a[href^="/"][role="link"], a[href^="/"]')
              );
              for (const a of links) {
                const u = parse(a.getAttribute('href') || '');
                if (!u) continue;
                const blob = (
                  (a.getAttribute('aria-label') || '') + ' ' +
                  (a.innerText || a.textContent || '')
                ).toLowerCase();
                if (\\bprofile\\b/.test(blob) || blob.includes('профиль')) {
                  return u;
                }
              }
              // Fallback: аватар в нижнем/боковом nav без чужого текста.
              for (const a of links) {
                const u = parse(a.getAttribute('href') || '');
                if (!u) continue;
                const img = a.querySelector(
                  'img[alt*="profile picture"], img[alt*="фото профиля"]'
                );
                if (!img) continue;
                const r = a.getBoundingClientRect();
                // Профиль в nav обычно слева или снизу, компактный.
                if (r.width > 0 && r.width < 90 && r.left < 120) return u;
                if (r.width > 0 && r.width < 90 && r.bottom > (window.innerHeight - 90)) {
                  return u;
                }
              }
              return '';
            }"""
        )
        return (user or "").strip().lstrip("@")
    except Exception:
        return ""


def _collect_search_account_usernames(page, *, limit: int = _SEARCH_POOL_MAX) -> list[str]:
    """Юзернеймы только из списка подсказок под полем Search (sibling-контейнер)."""
    reserved_js = sorted(_RESERVED_PATH_SEGMENTS)
    self_user = _current_instagram_username(page)
    try:
        hrefs = page.evaluate(
            """({ reservedList, selfUser }) => {
              const reserved = new Set(reservedList);
              const self = (selfUser || '').toLowerCase();
              const input = document.querySelector(
                'input[aria-label="Search input"], input[placeholder="Search"], ' +
                'input[aria-label="Поиск"], input[placeholder="Поиск"]'
              );
              if (!input) return [];

              const parseUser = (href) => {
                const path = (href || '').split('?')[0].split('#')[0];
                const parts = path.replace(/^\\/+|\\/+$/g, '').split('/').filter(Boolean);
                if (parts.length !== 1) return '';
                const user = parts[0];
                if (!user || reserved.has(user.toLowerCase())) return '';
                if (self && user.toLowerCase() === self) return '';
                return user;
              };

              const collectFromScope = (scope) => {
                if (!scope || !scope.querySelectorAll) return [];
                const rows = Array.from(
                  scope.querySelectorAll('a[href^="/"][role="link"]')
                ).filter((a) => {
                  // Пропуск вложенной ссылки аватарки.
                  if (a.parentElement && a.parentElement.closest('a[href^="/"]')) {
                    return false;
                  }
                  if (!a.querySelector(
                    'img[alt*="profile picture"], img[alt*="фото профиля"]'
                  )) return false;
                  const r = a.getBoundingClientRect();
                  if (r.width < 40 || r.height < 24) return false;
                  // Строка выдачи обычно широкая (не иконка nav).
                  if (r.width < 120) return false;
                  return !!parseUser(a.getAttribute('href') || '');
                }).map((a) => {
                  const r = a.getBoundingClientRect();
                  return {
                    user: parseUser(a.getAttribute('href') || ''),
                    top: r.top,
                    left: r.left,
                  };
                }).filter((x) => x.user);

                rows.sort((a, b) => (a.top - b.top) || (a.left - b.left));
                const out = [];
                const seen = new Set();
                for (const row of rows) {
                  const key = row.user.toLowerCase();
                  if (seen.has(key)) continue;
                  seen.add(key);
                  out.push(row.user);
                  if (out.length >= 30) break;
                }
                return out;
              };

              // Список каналов — sibling-ветка рядом с полем (не предок со всем nav).
              let el = input;
              for (let depth = 0; depth < 12 && el; depth++) {
                const parent = el.parentElement;
                if (!parent) break;
                for (const child of Array.from(parent.children)) {
                  if (child.contains(input)) continue;
                  const found = collectFromScope(child);
                  if (found.length) return found;
                }
                el = parent;
              }
              return [];
            }""",
            {"reservedList": reserved_js, "selfUser": self_user},
        )
    except Exception as e:
        _log(f"Reels: не удалось собрать аккаунты поиска: {type(e).__name__}")
        return []
    users: list[str] = []
    self_l = self_user.lower()
    for raw in hrefs or []:
        u = _username_from_profile_href(f"/{raw}/")
        if not u:
            continue
        if self_l and u.lower() == self_l:
            continue
        if u.lower() not in {x.lower() for x in users}:
            users.append(u)
        if len(users) >= max(1, int(limit)):
            break
    return users


def _open_explore_search(page, query: str) -> list[str]:
    """Открыть Explore, ввести запрос, вернуть юзернеймы из выдачи."""
    q = (query or "").strip()
    if not q:
        return []
    _navigate_page_to(page, EXPLORE_URL, label="Explore")
    page.wait_for_timeout(1_500)
    try:
        page.wait_for_selector(
            ", ".join(_SEARCH_INPUT_SELECTORS[:4]),
            timeout=30_000,
        )
    except Exception:
        # Поле может появиться только после клика по Search в сайдбаре.
        _log("Reels: Search input сразу не виден — пробуем активировать.")
        _activate_instagram_search_input(page)
        page.wait_for_timeout(500)
        if _find_instagram_search_input(page) is None:
            raise RuntimeError(
                "Не удалось открыть поиск Instagram Explore "
                f"(URL={(getattr(page, 'url', None) or '')!r})."
            )
    typed = False
    for attempt in range(1, 4):
        if _type_instagram_search_query(page, q):
            typed = True
            break
        _log(f"Reels: повтор ввода поиска ({attempt}/3)…")
        page.wait_for_timeout(700)
        _activate_instagram_search_input(page)
    if not typed:
        raise RuntimeError(f"Не удалось ввести поисковый запрос Instagram: {q!r}")
    self_user = _current_instagram_username(page)
    if self_user:
        _log(f"Reels: свой аккаунт @{self_user} исключаем из выдачи поиска.")
    # Ждём список аккаунтов после ввода (подсказки появляются без Enter).
    deadline = page.evaluate("() => Date.now()") + 18_000
    users: list[str] = []
    while True:
        users = _collect_search_account_usernames(page, limit=_SEARCH_POOL_MAX)
        if users:
            break
        try:
            now = page.evaluate("() => Date.now()")
        except Exception:
            now = deadline
        if now >= deadline:
            break
        page.wait_for_timeout(450)
    if not users:
        raise RuntimeError(
            f"По запросу {q!r} Instagram не показал аккаунты в поиске."
        )
    _log(
        f"Reels: в поиске найдено аккаунтов: {len(users)} "
        f"(топ: {', '.join(users[:_SEARCH_TOP_ACCOUNTS])})."
    )
    return users


def _goto_profile_reels_tab(page, username: str) -> None:
    user = (username or "").strip().lstrip("@")
    if not user:
        raise RuntimeError("Пустой username профиля Instagram.")
    reels_url = f"https://www.instagram.com/{user}/reels/"
    _navigate_page_to(page, reels_url, label=f"@{user}/reels")
    page.wait_for_timeout(1_500)
    # Иногда вкладка Reels есть, но URL ещё /username/ — клик по вкладке.
    try:
        tab = page.locator(
            f'a[href="/{user}/reels/"], a[href*="/{user}/reels/"]'
        ).first
        if tab.is_visible(timeout=2_000):
            selected = (tab.get_attribute("aria-selected") or "").strip().lower()
            if selected != "true":
                tab.click(timeout=3_000)
                page.wait_for_timeout(1_000)
    except Exception:
        pass


def _profile_has_reels(page, username: str) -> bool:
    user = (username or "").strip().lstrip("@")
    try:
        page.wait_for_selector(
            f'a[href*="/{user}/reel/"], a[href*="/reel/"]',
            timeout=8_000,
        )
    except Exception:
        pass
    try:
        n = page.locator(f'a[href*="/{user}/reel/"]').count()
        if n > 0:
            return True
    except Exception:
        pass
    try:
        n = page.locator('a[href*="/reel/"]').count()
        return n > 0
    except Exception:
        return False


def _open_first_profile_reel(page, username: str) -> bool:
    user = (username or "").strip().lstrip("@")
    selectors = (
        f'a[href*="/{user}/reel/"]',
        'a[href*="/reel/"]',
    )
    for sel in selectors:
        try:
            link = page.locator(sel).first
            if not link.is_visible(timeout=3_000):
                continue
            href = (link.get_attribute("href") or "").strip()
            _log(f"Reels: открываем первый рилс профиля @{user} ({href!r})…")
            link.click(timeout=5_000)
            page.wait_for_timeout(1_200)
            try:
                page.wait_for_selector(
                    'article video, [role="dialog"] video, main video',
                    timeout=20_000,
                )
            except Exception:
                pass
            return True
        except Exception as e:
            _log(f"Reels: клик по рилсу @{user} не удался: {type(e).__name__}")
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
    for i in range(1, n + 1):
        prev_src = _current_video_src(page)
        _log(f"Reels: рилс {i}/{n} в текущем плеере…")
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
            _log("Reels: лайк пропущен по вероятности.")
        if follow_once and not followed:
            # Подписка один раз на аккаунт (с учётом вероятности).
            if _roll(follow_p):
                if _try_follow_current_reel(page):
                    followed = True
            else:
                _log("Reels: подписка пропущена по вероятности.")
                followed = True  # больше не пытаемся на этом аккаунте
        if i >= n:
            break
        if not _advance_to_next_reel(page, prev_src=prev_src, sideways=sideways):
            _log("Reels: рилсы аккаунта закончились.")
            break
        page.wait_for_timeout(random.randint(400, 900))
    return watched


def browse_instagram_reels_from_search(
    page,
    query: str,
    *,
    count: int = _DEFAULT_REELS_COUNT,
    like_probability_pct: float = _DEFAULT_LIKE_PROB_PCT,
    follow_probability_pct: float = _DEFAULT_FOLLOW_PROB_PCT,
    watch_min_s: float = _DEFAULT_WATCH_MIN_S,
    watch_max_s: float = _DEFAULT_WATCH_MAX_S,
    watch_full: bool = False,
) -> None:
    """
    Прогрев по поиску: Explore → запрос → случайный из топ-3 аккаунтов →
    вкладка Reels → просмотр. Если у аккаунта нет рилсов — другой из топ-3;
    если у всех трёх нет — ошибка. Если рилсы кончились раньше цели —
    возвращаемся в поиск к другим каналам.
    """
    q = (query or "").strip()
    if not q:
        raise RuntimeError("Пустой поисковый запрос для прогрева Instagram Reels.")
    n = max(1, int(count))
    like_p = _clamp_prob_pct(like_probability_pct)
    follow_p = _clamp_prob_pct(follow_probability_pct)
    _log(
        f"Reels: прогрев по поиску «{q}» — {n} шт., лайк {like_p:.0f}%, "
        f"подписка {follow_p:.0f}%."
    )

    users = _open_explore_search(page, q)
    top3 = users[:_SEARCH_TOP_ACCOUNTS]
    if not top3:
        raise RuntimeError(f"По запросу {q!r} нет аккаунтов в топ-{_SEARCH_TOP_ACCOUNTS}.")

    no_reels: set[str] = set()
    exhausted: set[str] = set()
    watched_total = 0
    pool = list(users[:_SEARCH_POOL_MAX])

    def _pick_next_username() -> str | None:
        # Сначала — неиспользованные из топ-3 (без no_reels / exhausted).
        candidates = [
            u for u in top3 if u.lower() not in {x.lower() for x in no_reels | exhausted}
        ]
        if candidates:
            return random.choice(candidates)
        # Когда топ-3 исчерпан, но нужно добрать просмотры — остальные из выдачи.
        rest = [
            u
            for u in pool
            if u.lower() not in {x.lower() for x in no_reels | exhausted | set(top3)}
        ]
        # Также можно повторно брать exhausted? Нет — только новые.
        if rest:
            return random.choice(rest)
        # Расширяем пул повторным поиском.
        return None

    # Сначала проверяем, что хотя бы у одного из топ-3 есть рилсы.
    probe_order = list(top3)
    random.shuffle(probe_order)
    has_any_reels = False
    for user in probe_order:
        try:
            _goto_profile_reels_tab(page, user)
            if _profile_has_reels(page, user):
                has_any_reels = True
                # Вернёмся в поиск и начнём основной цикл с рандомным выбором.
                break
            no_reels.add(user)
            _log(f"Reels: у @{user} нет рилсов — пробуем другой аккаунт.")
        except Exception as e:
            no_reels.add(user)
            _log(f"Reels: профиль @{user} недоступен: {type(e).__name__}")
    if not has_any_reels:
        raise RuntimeError(
            f"У всех {_SEARCH_TOP_ACCOUNTS} аккаунтов из поиска «{q}» нет Reels — "
            "прогрев остановлен."
        )

    # Основной цикл просмотра
    while watched_total < n:
        # Обновляем выдачу поиска перед выбором следующего канала.
        try:
            users = _open_explore_search(page, q)
            for u in users:
                if u.lower() not in {x.lower() for x in pool}:
                    pool.append(u)
            top3 = users[:_SEARCH_TOP_ACCOUNTS] or top3
        except Exception as e:
            _log(f"Reels: повторный поиск не удался: {type(e).__name__}: {e!r}")
            if watched_total > 0:
                break
            raise

        username = _pick_next_username()
        if username is None:
            # Все топ-3 без рилсов уже отсеяны на пробе — если остались exhausted,
            # и нет новых в пуле, останавливаемся.
            available_top = [
                u
                for u in top3
                if u.lower() not in {x.lower() for x in no_reels}
            ]
            if not available_top and watched_total == 0:
                raise RuntimeError(
                    f"У всех {_SEARCH_TOP_ACCOUNTS} аккаунтов из поиска «{q}» нет Reels — "
                    "прогрев остановлен."
                )
            _log(
                f"Reels: больше нет новых каналов в поиске "
                f"(просмотрено {watched_total}/{n})."
            )
            break

        _log(f"Reels: выбран аккаунт @{username} из поиска.")
        try:
            _goto_profile_reels_tab(page, username)
        except Exception as e:
            no_reels.add(username)
            _log(f"Reels: не удалось открыть @{username}: {type(e).__name__}")
            continue

        if not _profile_has_reels(page, username):
            no_reels.add(username)
            _log(f"Reels: у @{username} нет рилсов.")
            # Если все топ-3 без рилсов — ошибка.
            if all(u.lower() in {x.lower() for x in no_reels} for u in top3):
                raise RuntimeError(
                    f"У всех {_SEARCH_TOP_ACCOUNTS} аккаунтов из поиска «{q}» нет Reels — "
                    "прогрев остановлен."
                )
            continue

        if not _open_first_profile_reel(page, username):
            no_reels.add(username)
            _log(f"Reels: не удалось открыть рилс @{username}.")
            continue

        got = _watch_reels_from_current_player(
            page,
            remaining=n - watched_total,
            like_probability_pct=like_p,
            follow_once=True,
            follow_probability_pct=follow_p,
            watch_min_s=watch_min_s,
            watch_max_s=watch_max_s,
            watch_full=watch_full,
            sideways=True,
        )
        watched_total += got
        exhausted.add(username)
        _log(f"Reels: с @{username} просмотрено {got}, всего {watched_total}/{n}.")

    _log(f"Reels: прогрев по поиску завершён ({watched_total}/{n}).")
    if watched_total <= 0:
        raise RuntimeError(
            f"Прогрев Instagram Reels по поиску «{q}» не просмотрел ни одного ролика."
        )


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
    reels_recommendations: bool = True,
    search_query: str = "",
    profile_id: str | None = None,
) -> None:
    """Главная Instagram → вход → лента /reels/ или поиск Explore → прогрев."""
    _log("Reels: проверка сессии / доступности Instagram…")
    verify_instagram_home_available(
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
                "Укажите поисковый запрос для прогрева Instagram Reels "
                "или включите рекомендации."
            )
        browse_instagram_reels_from_search(
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
    browse_instagram_reels(
        page,
        count=reels_count,
        like_probability_pct=like_probability_pct,
        follow_probability_pct=follow_probability_pct,
        watch_min_s=watch_min_s,
        watch_max_s=watch_max_s,
        watch_full=watch_full,
    )
