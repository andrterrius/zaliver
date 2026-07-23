"""Хранение и восстановление сессии instagrapi по profile_id антидетекта."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from zaliver.youtube_parsing.thumb_cache import zaliver_app_data_dir
from zaliver.youtube_upload.totp import get_totp_token

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


class InstagrapiSessionError(RuntimeError):
    """Не удалось получить рабочую сессию Instagram для чекера."""


def instagrapi_sessions_dir() -> Path:
    d = zaliver_app_data_dir() / "instagram_sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_settings_path(profile_id: str) -> Path:
    pid = _SAFE_ID_RE.sub("_", (profile_id or "").strip()) or "unknown"
    return instagrapi_sessions_dir() / f"{pid}.json"


def normalize_instagram_sessionid(raw: str) -> str:
    """Cookie sessionid часто URL-encoded (%3A → :)."""
    s = (raw or "").strip().strip('"').strip("'")
    if not s:
        return ""
    # Один-два прохода unquote на случай двойного кодирования.
    for _ in range(2):
        decoded = unquote(s)
        if decoded == s:
            break
        s = decoded
    return s.strip()


def _new_client(*, fast: bool = False):
    from instagrapi import Client

    cl = Client()
    try:
        # fast: для параллельного чека метрик; иначе мягче к антиботу.
        cl.delay_range = [0.0, 0.05] if fast else [0.35, 0.9]
    except Exception:
        pass
    return cl


def clone_instagrapi_client(source: Any, *, fast: bool = True) -> Any:
    """Копия сессии в новый Client (поток-безопасный экземпляр)."""
    settings = source.get_settings()
    cl = _new_client(fast=fast)
    cl.set_settings(settings)
    # Перенести уже известный username/user_id без лишних запросов.
    try:
        if getattr(source, "username", None):
            cl.username = source.username
    except Exception:
        pass
    try:
        if getattr(source, "user_id", None):
            cl.user_id = source.user_id
    except Exception:
        pass
    return cl


def _client_looks_logged_in(cl: Any) -> bool:
    """Мягкая проверка: timeline часто падает даже на живой sessionid."""
    uid = getattr(cl, "user_id", None)
    if uid:
        try:
            cl.user_info_v1(int(uid))
            return True
        except Exception:
            pass
        try:
            cl.account_info()
            return True
        except Exception:
            pass
    try:
        cl.account_info()
        return True
    except Exception:
        pass
    try:
        cl.get_timeline_feed()
        return True
    except Exception:
        return False


def _dump(cl: Any, profile_id: str) -> None:
    path = session_settings_path(profile_id)
    try:
        cl.dump_settings(str(path))
    except Exception as e:
        raise InstagrapiSessionError(
            f"Не удалось сохранить сессию instagrapi: {e}"
        ) from e


def _login_with_password(
    cl: Any,
    *,
    username: str,
    password: str,
    twofa_secret: str = "",
) -> None:
    user = (username or "").strip()
    pwd = (password or "").strip()
    if not user or not pwd:
        raise InstagrapiSessionError("Пустой логин или пароль Instagram.")
    secret = (twofa_secret or "").strip().replace(" ", "")
    code = get_totp_token(secret) if secret else ""
    try:
        if code:
            cl.login(user, pwd, verification_code=code)
        else:
            cl.login(user, pwd)
    except Exception as e:
        if secret:
            try:
                cl.login(user, pwd, verification_code=get_totp_token(secret))
                return
            except Exception:
                pass
        raise InstagrapiSessionError(f"Логин Instagram не удался: {e}") from e


def _sessionid_from_loaded_client(cl: Any) -> str:
    """Достать sessionid из уже загруженных settings (без сети)."""
    settings: dict[str, Any] = {}
    try:
        raw = cl.get_settings()
        if isinstance(raw, dict):
            settings = raw
    except Exception:
        raw2 = getattr(cl, "settings", None)
        if isinstance(raw2, dict):
            settings = raw2
    cookies = settings.get("cookies")
    if isinstance(cookies, dict):
        sid = normalize_instagram_sessionid(str(cookies.get("sessionid") or ""))
        if sid:
            return sid
    auth = settings.get("authorization_data")
    if isinstance(auth, dict):
        sid = normalize_instagram_sessionid(str(auth.get("sessionid") or ""))
        if sid:
            return sid
    try:
        sid = normalize_instagram_sessionid(str(getattr(cl, "sessionid", "") or ""))
        if sid:
            return sid
    except Exception:
        pass
    return ""


def _login_by_sessionid(cl: Any, sessionid: str) -> None:
    sid = normalize_instagram_sessionid(sessionid)
    if not sid:
        raise InstagrapiSessionError("Пустой sessionid.")
    if len(sid) <= 30:
        raise InstagrapiSessionError(
            f"Подозрительно короткий sessionid ({len(sid)} символов)."
        )
    if not re.match(r"^\d+", sid):
        raise InstagrapiSessionError(
            "sessionid должен начинаться с user id (цифры). "
            "Возможно, cookie битая или не из Instagram."
        )
    try:
        cl.login_by_sessionid(sid)
    except Exception as e:
        raise InstagrapiSessionError(f"login_by_sessionid не удался: {e}") from e


def ensure_instagrapi_client(
    profile_id: str,
    *,
    username: str = "",
    password: str = "",
    twofa_secret: str = "",
    sessionid_provider: Callable[[], str] | None = None,
) -> tuple[Any, str]:
    """
    Вернуть ``(Client, source)`` для profile_id.

    source: ``dump`` | ``password`` | ``browser_cookies``.

    Порядок:
    1) сохранённый dump сессии (без лишнего сетевого пинга — живёт дни–месяцы);
    2) логин по username/password (+ TOTP);
    3) sessionid из cookies антидетект-профиля.
    """
    pid = (profile_id or "").strip()
    if not pid:
        raise InstagrapiSessionError("Не выбран профиль для чекера Instagram.")

    path = session_settings_path(pid)
    cl = _new_client(fast=True)
    if path.is_file():
        try:
            cl.load_settings(str(path))
            if _sessionid_from_loaded_client(cl):
                # Не пингуем Instagram каждый раз: dump переиспользуем.
                # Если протух — упадёт media_info, тогда удалим и перелогинимся.
                return cl, "dump"
        except Exception:
            pass
        cl = _new_client(fast=True)

    errors: list[str] = []

    user = (username or "").strip()
    pwd = (password or "").strip()
    if user and pwd:
        try:
            _login_with_password(
                cl, username=user, password=pwd, twofa_secret=twofa_secret
            )
            _dump(cl, pid)
            return cl, "password"
        except Exception as e:
            errors.append(str(e) or type(e).__name__)
            cl = _new_client(fast=True)

    if sessionid_provider is not None:
        try:
            sid = (sessionid_provider() or "").strip()
            if not sid:
                raise InstagrapiSessionError(
                    "В профиле нет cookie sessionid — войдите в Instagram в этом профиле."
                )
            cl = _new_client(fast=True)
            _login_by_sessionid(cl, sid)
            _dump(cl, pid)
            return cl, "browser_cookies"
        except Exception as e:
            errors.append(str(e) or type(e).__name__)

    if errors:
        raise InstagrapiSessionError(" → ".join(errors))
    raise InstagrapiSessionError(
        "Нет сохранённой сессии и нет логина/пароля Instagram в данных профиля. "
        "Укажите inst_login/inst_password или войдите в Instagram в браузере профиля."
    )


def invalidate_instagrapi_session(profile_id: str) -> None:
    """Удалить dump, если Instagram ответил login_required / session expired."""
    path = session_settings_path(profile_id)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
