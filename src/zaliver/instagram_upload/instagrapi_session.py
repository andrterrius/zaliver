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
        # soft — для чекера/антибота; fast почти не использовать (жжёт сессии).
        cl.delay_range = [0.15, 0.45] if fast else [1.0, 2.2]
    except Exception:
        pass
    return cl


def client_username(cl: Any) -> str:
    """Известный username клиента (атрибут или settings dump)."""
    try:
        u = (getattr(cl, "username", None) or "").strip()
        if u:
            return u
    except Exception:
        pass
    try:
        raw = cl.get_settings()
        if isinstance(raw, dict):
            u = str(raw.get("username") or "").strip()
            if u:
                try:
                    cl.username = u
                except Exception:
                    pass
                return u
    except Exception:
        pass
    return ""


def clone_instagrapi_client(source: Any, *, fast: bool = False) -> Any:
    """Копия сессии в новый Client (по умолчанию soft delay)."""
    settings = source.get_settings()
    cl = _new_client(fast=fast)
    cl.set_settings(settings)
    # Перенести уже известный username/user_id без лишних запросов.
    try:
        u = client_username(source)
        if u:
            cl.username = u
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


_SESSION_ERR_MARKERS = (
    "login_required",
    "login required",
    "challenge_required",
    "challenge",
    "checkpoint_required",
    "unauthorized",
    "exceeded 30 redirects",
    "toomanyredirects",
    "too many redirects",
    "user has been logged out",
    "session expired",
    "please wait a few minutes",
    "feedback_required",
)


def is_instagrapi_session_error(err: object) -> bool:
    """Ошибка похожа на мёртвую/заблокированную сессию (нужен re-login)."""
    s = (str(err) or "").strip().lower()
    if not s:
        return False
    return any(m in s for m in _SESSION_ERR_MARKERS)


def ensure_instagrapi_client(
    profile_id: str,
    *,
    username: str = "",
    password: str = "",
    twofa_secret: str = "",
    sessionid_provider: Callable[[], str] | None = None,
    allow_dump: bool = True,
) -> tuple[Any, str]:
    """
    Вернуть ``(Client, source)`` для profile_id.

    source: ``dump`` | ``password`` | ``browser_cookies`` | ``relogin``.

    Порядок:
    1) сохранённый dump сессии (если allow_dump; без сетевого пинга);
    2) логин по username/password (+ TOTP), с device fingerprint из dump если есть;
    3) sessionid из cookies антидетект-профиля.
    """
    pid = (profile_id or "").strip()
    if not pid:
        raise InstagrapiSessionError("Не выбран профиль для чекера Instagram.")

    path = session_settings_path(pid)
    # Чекер и dump: soft delay, без агрессивного fast.
    cl = _new_client(fast=False)
    dump_loaded = False
    if path.is_file():
        try:
            cl.load_settings(str(path))
            dump_loaded = True
            if allow_dump and _sessionid_from_loaded_client(cl):
                # Dump без username почти всегда битый → не доверяем, идём в re-auth.
                if client_username(cl):
                    return cl, "dump"
        except Exception:
            cl = _new_client(fast=False)
            dump_loaded = False

    errors: list[str] = []

    user = (username or "").strip()
    pwd = (password or "").strip()
    if user and pwd:
        try:
            # При обновлении сессии: сначала relogin (сохраняет device UUID из dump).
            if dump_loaded and not allow_dump:
                try:
                    cl.username = user
                    cl.password = pwd
                    secret = (twofa_secret or "").strip().replace(" ", "")
                    if secret:
                        cl.login(user, pwd, verification_code=get_totp_token(secret))
                    else:
                        cl.relogin()
                    _dump(cl, pid)
                    return cl, "relogin"
                except Exception as e:
                    errors.append(f"relogin: {e}")
                    cl = _new_client(fast=False)
                    if path.is_file():
                        try:
                            cl.load_settings(str(path))
                            dump_loaded = True
                        except Exception:
                            cl = _new_client(fast=False)
                            dump_loaded = False
            _login_with_password(
                cl, username=user, password=pwd, twofa_secret=twofa_secret
            )
            _dump(cl, pid)
            return cl, "password"
        except Exception as e:
            errors.append(str(e) or type(e).__name__)
            cl = _new_client(fast=False)
            dump_loaded = False

    if sessionid_provider is not None:
        try:
            sid = (sessionid_provider() or "").strip()
            if not sid:
                raise InstagrapiSessionError(
                    "В профиле нет cookie sessionid — войдите в Instagram в этом профиле."
                )
            cl = _new_client(fast=False)
            if path.is_file():
                try:
                    # Сохранить fingerprint устройства, подменить cookies через sessionid.
                    cl.load_settings(str(path))
                except Exception:
                    cl = _new_client(fast=False)
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


def refresh_instagrapi_client(
    profile_id: str,
    *,
    username: str = "",
    password: str = "",
    twofa_secret: str = "",
    sessionid_provider: Callable[[], str] | None = None,
) -> tuple[Any, str]:
    """
    Принудительно обновить сессию: не доверять cookies из dump, перелогиниться.
    Device fingerprint из dump сохраняется, если файл ещё на диске.
    """
    return ensure_instagrapi_client(
        profile_id,
        username=username,
        password=password,
        twofa_secret=twofa_secret,
        sessionid_provider=sessionid_provider,
        allow_dump=False,
    )


def invalidate_instagrapi_session(profile_id: str) -> None:
    """Удалить dump, если Instagram ответил login_required / session expired."""
    path = session_settings_path(profile_id)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
