"""Продвижение Instagram Reels: по ссылкам залитых роликов — подписка/лайк/коммент."""

from __future__ import annotations

import random
import re

from zaliver.instagram_upload.instagram_availability import (
    verify_instagram_home_available,
)
from zaliver.instagram_upload.logutil import emit_instagram_log, instagram_entrypoint
from zaliver.instagram_upload.register import _navigate_page_to
from zaliver.instagram_upload.reels_warmup import (
    _clamp_prob_pct,
    _try_follow_current_reel,
    _try_like_current_reel,
    _watch_current_reel,
)
from zaliver.youtube_upload.studio import PromotionTargetVideo

_DEFAULT_REELS_COUNT = 15
_DEFAULT_LIKE_PROB_PCT = 35.0
_DEFAULT_WATCH_MIN_S = 4.0
_DEFAULT_WATCH_MAX_S = 12.0

# Вариант 1: классический <textarea>.
# Вариант 2: панель «Комментарии» — <input> или Lexical contenteditable.
_COMMENT_FIELD_SEL = (
    'textarea[aria-label="Добавьте комментарий..."], '
    'textarea[placeholder="Добавьте комментарий..."], '
    'textarea[aria-label="Add a comment..."], '
    'textarea[placeholder="Add a comment..."], '
    'textarea[aria-label*="коммент" i], '
    'textarea[aria-label*="comment" i], '
    'textarea[placeholder*="коммент" i], '
    'textarea[placeholder*="comment" i], '
    'input[placeholder="Добавьте комментарий..."], '
    'input[placeholder="Add a comment..."], '
    'input[placeholder*="коммент" i], '
    'input[placeholder*="comment" i], '
    'input[aria-label="Добавьте комментарий..."], '
    'input[aria-label="Add a comment..."], '
    'div[role="textbox"][aria-label="Добавьте комментарий..."], '
    'div[role="textbox"][aria-label="Add a comment..."], '
    'div[role="textbox"][aria-placeholder*="коммент" i], '
    'div[role="textbox"][aria-placeholder*="comment" i], '
    'div[contenteditable="true"][aria-label*="коммент" i], '
    'div[contenteditable="true"][aria-label*="comment" i]'
)
_COMMENT_OPEN_SVG_SEL = (
    'svg[aria-label="Комментировать"], svg[aria-label="Comment"], '
    'svg[aria-label="Add a comment"]'
)
_COMMENT_CLOSE_SVG_SEL = (
    'svg[aria-label="Закрыть"], svg[aria-label="Close"]'
)
_COMMENT_POST_RE = re.compile(r"^(Опубликовать|Post)$", re.I)


def _log(message: str) -> None:
    emit_instagram_log(message, tag="[instagram]")


def _promotion_open_url(*, url: str, video_id: str) -> str:
    u = (url or "").strip()
    if u and "instagram.com" in u.lower():
        return u
    vid = (video_id or "").strip()
    if not vid:
        return u
    return f"https://www.instagram.com/reel/{vid}/"


def _goto(page, url: str, *, label: str = "Продвижение") -> None:
    _navigate_page_to(page, url, label=label)


def _find_visible_comment_field(page):
    """Первое видимое поле комментария (textarea / input / contenteditable)."""
    loc = page.locator(_COMMENT_FIELD_SEL)
    try:
        n = min(int(loc.count() or 0), 12)
    except Exception:
        n = 0
    for i in range(n):
        el = loc.nth(i)
        try:
            if el.is_visible(timeout=400):
                return el
        except Exception:
            continue
    return None


def _comment_field_visible(page) -> bool:
    return _find_visible_comment_field(page) is not None


def _close_comment_panel(page) -> None:
    """Закрыть панель комментариев (крестик или Escape)."""
    try:
        svg = page.locator(_COMMENT_CLOSE_SVG_SEL).first
        if svg.count() and svg.is_visible(timeout=500):
            btn = svg.locator("xpath=ancestor::*[@role='button'][1]")
            target = btn if btn.count() else svg
            target.click(timeout=2_000)
            page.wait_for_timeout(400)
            return
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    except Exception:
        pass


def _open_comment_composer(page) -> bool:
    """Открыть форму комментария (клик по иконке, если поля ещё нет)."""
    if _comment_field_visible(page):
        return True
    try:
        svg = page.locator(_COMMENT_OPEN_SVG_SEL).first
        if not svg.count() or not svg.is_visible(timeout=1_500):
            return False
        btn = svg.locator("xpath=ancestor::*[@role='button'][1]")
        target = btn if btn.count() else svg
        target.click(timeout=3_000)
        page.wait_for_timeout(900)
        return _comment_field_visible(page)
    except Exception:
        return False


def _find_comment_post_button(page):
    """Кнопка «Опубликовать» / Post рядом с формой комментария."""
    candidates = [
        page.locator('form div[role="button"]:has-text("Опубликовать")'),
        page.locator('form div[role="button"]:has-text("Post")'),
        page.locator('div[role="button"]:has-text("Опубликовать")'),
        page.locator('div[role="button"]:has-text("Post")'),
        page.locator('button:has-text("Опубликовать")'),
        page.locator('button:has-text("Post")'),
        page.get_by_role("button", name=_COMMENT_POST_RE),
    ]
    for loc in candidates:
        try:
            n = min(int(loc.count() or 0), 8)
        except Exception:
            n = 0
        for i in range(n):
            btn = loc.nth(i)
            try:
                if not btn.is_visible(timeout=400):
                    continue
                text = (btn.inner_text(timeout=400) or "").strip().split("\n")[0].strip()
                if _COMMENT_POST_RE.match(text):
                    return btn
            except Exception:
                continue
    return None


def _post_button_disabled(btn) -> bool:
    try:
        aria = (btn.get_attribute("aria-disabled") or "").strip().lower()
        if aria in ("true", "1"):
            return True
        disabled = btn.get_attribute("disabled")
        if disabled is not None:
            return True
    except Exception:
        pass
    return False


def _type_into_comment_field(page, field, comment: str) -> bool:
    """Ввести текст так, чтобы появилась/включилась кнопка «Опубликовать»."""
    try:
        field.click(timeout=2_000)
        page.wait_for_timeout(250)
        # После клика input часто сменяется на Lexical contenteditable.
        active = _find_visible_comment_field(page) or field
        try:
            active.fill("")
        except Exception:
            try:
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
            except Exception:
                pass
        page.wait_for_timeout(100)
        active = _find_visible_comment_field(page) or active
        try:
            active.press_sequentially(comment, delay=20)
        except Exception:
            try:
                active.type(comment, delay=25)
            except Exception:
                page.keyboard.type(comment, delay=25)
        page.wait_for_timeout(400)
        return True
    except Exception as e:
        _log(
            f"Продвижение: ввод комментария не удался: "
            f"{type(e).__name__}: {e!r}"
        )
        return False


def _wait_for_post_button(page, *, attempts: int = 15):
    """Дождаться появления активной «Опубликовать» после ввода текста."""
    for _ in range(max(1, attempts)):
        post = _find_comment_post_button(page)
        if post is not None and not _post_button_disabled(post):
            return post
        page.wait_for_timeout(200)
    return _find_comment_post_button(page)


def _try_comment_current_reel(page, text: str) -> bool:
    """
    Комментарий под текущим рилсом:
    иконка «Комментировать» → поле (textarea / input / Lexical) → «Опубликовать».
    Успех: кнопка «Опубликовать» пропала.
    """
    comment = (text or "").strip()
    if not comment:
        return False

    opened_via_icon = not _comment_field_visible(page)
    if not _open_comment_composer(page):
        _log("Продвижение: поле комментария не найдено.")
        return False

    ok = False
    try:
        field = _find_visible_comment_field(page)
        if field is None:
            _log("Продвижение: поле комментария не найдено.")
            return False
        if not _type_into_comment_field(page, field, comment):
            return False

        # После ввода поле могло смениться (input → contenteditable).
        field = _find_visible_comment_field(page) or field

        post = _wait_for_post_button(page)
        if post is None:
            _log("Продвижение: кнопка «Опубликовать» не найдена.")
            return False
        if _post_button_disabled(post):
            _log(
                "Продвижение: «Опубликовать» осталась неактивной "
                "после ввода — комментарий не отправлен."
            )
            return False

        post.click(timeout=3_000)
        page.wait_for_timeout(600)

        for _ in range(20):
            # Успех: «Опубликовать» пропала с экрана.
            try:
                if _find_comment_post_button(page) is None:
                    _log("Продвижение: комментарий отправлен.")
                    ok = True
                    break
            except Exception:
                _log("Продвижение: комментарий отправлен.")
                ok = True
                break
            try:
                if not post.is_visible(timeout=200):
                    _log("Продвижение: комментарий отправлен.")
                    ok = True
                    break
            except Exception:
                _log("Продвижение: комментарий отправлен.")
                ok = True
                break
            page.wait_for_timeout(200)

        if not ok:
            _log(
                "Продвижение: после клика «Опубликовать» кнопка не пропала — "
                "комментарий не подтверждён."
            )
            return False
        return True
    except Exception as e:
        _log(
            f"Продвижение: комментарий не удался: {type(e).__name__}: {e!r}"
        )
        return False
    finally:
        if opened_via_icon:
            _close_comment_panel(page)


def _unique_target_reels(
    targets: list[PromotionTargetVideo],
    *,
    viewer_profile_id: str | None,
    limit: int,
) -> list[PromotionTargetVideo]:
    """Уникальные ролики чужих профилей, не больше limit."""
    viewer = (viewer_profile_id or "").strip()
    seen_vids: set[str] = set()
    out: list[PromotionTargetVideo] = []
    max_n = max(0, int(limit))
    for target in targets:
        if max_n and len(out) >= max_n:
            break
        owner = (target.profile_id or "").strip()
        if viewer and owner and viewer == owner:
            continue
        vid = (target.video_id or "").strip()
        if vid and vid in seen_vids:
            continue
        open_url = _promotion_open_url(url=target.url, video_id=vid)
        if not open_url:
            continue
        if vid:
            seen_vids.add(vid)
        out.append(target)
    return out


def _engage_on_open_reel(
    page,
    *,
    subscribe: bool,
    like_probability_pct: float,
    watch_min_s: float,
    watch_max_s: float,
    watch_full: bool,
    comment_probability_pct: float,
    comments: list[str] | None,
) -> None:
    """На уже открытом рилсе: просмотр → подписка → лайк → комментарий."""
    like_p = _clamp_prob_pct(like_probability_pct) / 100.0
    comment_p = _clamp_prob_pct(comment_probability_pct) / 100.0
    comment_pool = [c.strip() for c in (comments or []) if (c or "").strip()]
    if not comment_pool:
        comment_p = 0.0

    _watch_current_reel(
        page,
        watch_min_s=watch_min_s,
        watch_max_s=watch_max_s,
        watch_full=watch_full,
    )
    if subscribe:
        _try_follow_current_reel(page)
        page.wait_for_timeout(random.randint(300, 700))
    if like_p > 0 and random.random() < like_p:
        _try_like_current_reel(page)
        page.wait_for_timeout(random.randint(250, 700))
    if comment_p > 0 and random.random() < comment_p:
        _try_comment_current_reel(page, random.choice(comment_pool))
        page.wait_for_timeout(random.randint(400, 900))


@instagram_entrypoint
def run_instagram_profiles_promotion(
    page,
    *,
    videos: list[PromotionTargetVideo] | None = None,
    subscribe_to_channels: bool = False,
    viewer_profile_id: str | None = None,
    session_login: str = "",
    session_password: str = "",
    session_twofa: str = "",
    profile_id: str | None = None,
    shorts_count: int = _DEFAULT_REELS_COUNT,
    like_probability_pct: float = _DEFAULT_LIKE_PROB_PCT,
    shorts_watch_min_s: float = _DEFAULT_WATCH_MIN_S,
    shorts_watch_max_s: float = _DEFAULT_WATCH_MAX_S,
    watch_full_video: bool = False,
    enable_comments: bool = False,
    comments: list[str] | None = None,
    comment_probability_pct: float = 0.0,
    **_ignored,
) -> None:
    """
    Проверка сессии → по каждому целевому рилсу (ссылка):
    открыть → просмотр → (опц.) Подписаться на месте → лайк → комментарий.
    Без захода в профиль владельца и без ленты /reels/.
    """
    _ = profile_id
    raw_targets = [
        v
        for v in (videos or [])
        if (v.video_id or "").strip() or (v.url or "").strip()
    ]
    limit = max(0, int(shorts_count))
    targets = _unique_target_reels(
        raw_targets,
        viewer_profile_id=viewer_profile_id,
        limit=limit if limit > 0 else len(raw_targets),
    )

    _log("Продвижение: проверка сессии Instagram…")
    verify_instagram_home_available(
        page,
        session_login=session_login,
        session_password=session_password,
        session_twofa=session_twofa,
    )

    if not targets:
        raise RuntimeError(
            "Продвижение: нет целевых рилсов "
            "(нужны чужие ролики с video_id или url в базе)."
        )

    comment_list = comments if enable_comments else None
    comment_prob = comment_probability_pct if enable_comments else 0.0
    if comment_list:
        _log(
            f"Продвижение: комментарии включены "
            f"({len(comment_list)} шт., вероятность "
            f"{_clamp_prob_pct(comment_prob):.0f}%)."
        )
    _log(
        f"Продвижение: {len(targets)} рилсов по ссылкам"
        + (", с подпиской" if subscribe_to_channels else ", без подписки")
        + "."
    )

    opened = 0
    for idx, target in enumerate(targets, start=1):
        owner = (target.profile_id or "").strip()
        vid = (target.video_id or "").strip()
        open_url = _promotion_open_url(url=target.url, video_id=vid)
        title_note = (target.title or "").strip()
        _log(
            f"Продвижение [{idx}/{len(targets)}]: открываем "
            f"video_id={vid!r} (профиль {owner!r}"
            + (f", «{title_note[:60]}»" if title_note else "")
            + f") → {open_url}"
        )
        try:
            _goto(page, open_url)
        except Exception as e:
            _log(
                f"Продвижение: не удалось открыть {open_url!r}: "
                f"{type(e).__name__}: {e!r}"
            )
            continue
        page.wait_for_timeout(2_000)
        opened += 1
        try:
            _engage_on_open_reel(
                page,
                subscribe=bool(subscribe_to_channels),
                like_probability_pct=like_probability_pct,
                watch_min_s=float(shorts_watch_min_s),
                watch_max_s=float(shorts_watch_max_s),
                watch_full=bool(watch_full_video),
                comment_probability_pct=float(comment_prob),
                comments=comment_list,
            )
        except Exception as e:
            _log(
                f"Продвижение [{idx}/{len(targets)}]: ошибка действий: "
                f"{type(e).__name__}: {e!r}"
            )
        page.wait_for_timeout(random.randint(400, 900))

    _log(
        f"Продвижение Instagram завершено "
        f"(открыто {opened} из {len(targets)})."
    )
