"""Сборка proxy DSN (URL) из полей профиля антидетекта для requests/instagrapi."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlparse, urlunparse


def mask_proxy_dsn(dsn: str) -> str:
    """Скрыть пароль в URL для логов."""
    s = (dsn or "").strip()
    if not s:
        return ""
    try:
        u = urlparse(s if "://" in s else f"http://{s}")
    except Exception:
        return s
    if not u.password and not u.username:
        return s
    user = u.username or ""
    host = u.hostname or ""
    port = f":{u.port}" if u.port else ""
    scheme = u.scheme or "http"
    return f"{scheme}://{user}:***@{host}{port}"


def proxy_dsn_has_auth(dsn: str) -> bool:
    """True если в DSN есть username (иначе HTTP-прокси ответит 407)."""
    s = (dsn or "").strip()
    if not s:
        return False
    try:
        u = urlparse(s if "://" in s else f"http://{s}")
    except Exception:
        return "@" in s
    return bool(u.username)


def _scheme_from_type(raw: object) -> str:
    t = str(raw or "http").strip().lower()
    if t in ("socks5", "socks5h"):
        return "socks5"
    if t in ("socks4", "socks4a"):
        return "socks4"
    if t in ("https", "ssh"):
        return "http"
    return "http"


def _creds_from_mapping(proxy: dict[str, Any]) -> tuple[str, str]:
    login = str(
        proxy.get("login")
        or proxy.get("user")
        or proxy.get("username")
        or proxy.get("proxy_login")
        or proxy.get("proxy_username")
        or ""
    ).strip()
    password = str(
        proxy.get("password")
        or proxy.get("pass")
        or proxy.get("proxy_password")
        or ""
    )
    return login, password


def _inject_auth(dsn: str, login: str, password: str) -> str:
    """
    В URL прокси без userinfo подставить login/password.
    В своём антидетекте server = http://host:port, creds отдельно.
    """
    s = (dsn or "").strip()
    user = (login or "").strip()
    if not s or not user:
        return s
    try:
        u = urlparse(s if "://" in s else f"http://{s}")
    except Exception:
        return s
    if u.username:
        return s if "://" in s else f"{u.scheme}://{u.netloc}"
    host = u.hostname or ""
    if not host:
        return s
    port = u.port
    netloc = f"{quote(user, safe='')}:{quote(str(password or ''), safe='')}@{host}"
    if port:
        netloc = f"{netloc}:{port}"
    return urlunparse(
        (
            u.scheme or "http",
            netloc,
            u.path or "",
            u.params or "",
            u.query or "",
            u.fragment or "",
        )
    )


def _dsn_from_host_parts(
    *,
    host: str,
    port: object,
    login: str = "",
    password: str = "",
    scheme: str = "http",
) -> str:
    h = (host or "").strip()
    if not h:
        return ""
    try:
        p = int(str(port).strip()) if port is not None and str(port).strip() else 0
    except (TypeError, ValueError):
        p = 0
    if p <= 0:
        return ""
    sch = (scheme or "http").strip().lower() or "http"
    user = (login or "").strip()
    pwd = password if password is not None else ""
    if user:
        auth = f"{quote(user, safe='')}:{quote(str(pwd), safe='')}@"
    else:
        auth = ""
    return f"{sch}://{auth}{h}:{p}"


def _dsn_from_server_string(
    raw: str,
    *,
    default_scheme: str = "http",
    login: str = "",
    password: str = "",
) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if "://" in s:
        return _inject_auth(s, login, password)
    # login:password@host:port
    if "@" in s:
        return _inject_auth(f"{default_scheme}://{s}", login, password)
    parts = s.split(":")
    # host:port
    if len(parts) == 2:
        return _dsn_from_host_parts(
            host=parts[0],
            port=parts[1],
            login=login,
            password=password,
            scheme=default_scheme,
        )
    # host:port:login:password — inline creds важнее отдельных полей
    if len(parts) >= 4:
        host, port, inline_login = parts[0], parts[1], parts[2]
        inline_password = ":".join(parts[3:])
        return _dsn_from_host_parts(
            host=host,
            port=port,
            login=(inline_login or login),
            password=(inline_password if inline_login else password),
            scheme=default_scheme,
        )
    return _inject_auth(f"{default_scheme}://{s}", login, password)


def proxy_dsn_from_mapping(proxy: dict[str, Any] | None) -> str:
    """
    Dolphin: {type, host, port, login, password} или {server}.
    Local antidetect: {server: proxy_server, login/username, password}
      или сырые proxy_server + proxy_username + proxy_password.
    """
    if not isinstance(proxy, dict) or not proxy:
        return ""

    scheme = _scheme_from_type(
        proxy.get("type") or proxy.get("proxyType") or proxy.get("proxy_type")
    )
    login, password = _creds_from_mapping(proxy)

    for key in ("server", "proxy_server", "url", "dsn"):
        raw = proxy.get(key)
        if isinstance(raw, str) and raw.strip():
            built = _dsn_from_server_string(
                raw,
                default_scheme=scheme,
                login=login,
                password=password,
            )
            if built:
                return built

    host = str(
        proxy.get("host") or proxy.get("ip") or proxy.get("hostname") or ""
    ).strip()
    port = proxy.get("port")
    dsn = _dsn_from_host_parts(
        host=host, port=port, login=login, password=password, scheme=scheme
    )
    if dsn:
        return dsn
    return ""


def proxy_dsn_from_profile(profile: dict[str, Any] | None) -> str:
    """Прокси профиля (UI-dict Dolphin / local) → DSN для Client.set_proxy."""
    if not isinstance(profile, dict):
        return ""

    nested = profile.get("proxy") if isinstance(profile.get("proxy"), dict) else None
    if isinstance(nested, dict):
        # Подмешать top-level creds локального API, если в proxy их нет.
        merged = dict(nested)
        top_login = str(profile.get("proxy_username") or "").strip()
        top_pass = profile.get("proxy_password")
        if top_login and not (
            merged.get("login")
            or merged.get("user")
            or merged.get("username")
            or merged.get("proxy_username")
        ):
            merged["login"] = top_login
        if top_pass is not None and str(top_pass) and not (
            merged.get("password") or merged.get("pass") or merged.get("proxy_password")
        ):
            merged["password"] = str(top_pass)
        dsn = proxy_dsn_from_mapping(merged)
        if dsn:
            return dsn

    # Сырой ProfileOut локального API: proxy_server + proxy_username + proxy_password.
    raw_server = profile.get("proxy_server")
    if isinstance(raw_server, str) and raw_server.strip():
        return _dsn_from_server_string(
            raw_server.strip(),
            login=str(profile.get("proxy_username") or "").strip(),
            password=str(profile.get("proxy_password") or ""),
        )
    return ""
