"""Resolve antidetect kind / base URL and list profiles (own browser only)."""

from __future__ import annotations

from typing import Any

from zaliver.antydetect.local_antidetect_api import (
    DEFAULT_LOCAL_API_BASE_URL,
    DEFAULT_LOCAL_API_TOKEN,
    LocalAntidetectError,
    LocalAntidetectHttpAPI,
    normalize_local_profile_for_ui,
    set_default_local_api_token,
)
from zaliver.config.platform_settings import PlatformSettings


OWN_KINDS = frozenset({"local", "remote"})
LOCAL_API_TOKEN_SETTINGS_KEY = "antydetect/local_api_token"


def is_own_kind(kind: str) -> bool:
    return (kind or "").strip().lower() in OWN_KINDS


def resolve_antidetect_kind(settings: PlatformSettings, override: str | None = None) -> str:
    raw = (override or "").strip().lower()
    if raw in {"local", "remote"}:
        return raw
    # Legacy dolphin → local (own browser only).
    if raw == "dolphin":
        return "local"
    stored = str(
        settings.value("antydetect/default_browser", "local") or "local"
    ).strip().lower()
    if stored in {"local", "remote"}:
        return stored
    return "local"


def resolve_local_base_url(
    settings: PlatformSettings, override: str | None = None
) -> str:
    u = (override or "").strip().rstrip("/")
    if u:
        return u
    for key in (
        "antydetect/local_api_base_url",
        "antydetect/own_base_url",
        "antydetect/remote_api_base_url",
    ):
        v = str(settings.value(key, "") or "").strip().rstrip("/")
        if v:
            return v
    return DEFAULT_LOCAL_API_BASE_URL


def resolve_local_api_token_setting(
    settings: PlatformSettings, override: str | None = None
) -> str:
    tok = (override or "").strip()
    if tok:
        return tok
    return str(settings.value(LOCAL_API_TOKEN_SETTINGS_KEY, "") or "").strip()


def apply_local_api_token_from_settings(settings: PlatformSettings) -> str:
    """Sync process-wide Bearer token used by LocalAntidetectHttpAPI."""
    tok = resolve_local_api_token_setting(settings)
    set_default_local_api_token(tok)
    return tok


def ensure_antidetect_defaults(settings: PlatformSettings) -> None:
    """Seed local-antidetect defaults for server deploys (same host)."""
    stored = str(
        settings.value("antydetect/default_browser", "") or ""
    ).strip().lower()
    if not stored or stored == "dolphin":
        settings.setValue("antydetect/default_browser", "local")
    if not settings.contains("antydetect/local_api_base_url"):
        settings.setValue(
            "antydetect/local_api_base_url", DEFAULT_LOCAL_API_BASE_URL
        )
    if not settings.contains("antydetect/own_base_url"):
        settings.setValue("antydetect/own_base_url", DEFAULT_LOCAL_API_BASE_URL)
    if not settings.contains("antydetect/dolphin_headless"):
        settings.setValue("antydetect/dolphin_headless", True)
    # Веб-API / `serve`: antidetect по умолчанию ждёт Bearer secret.
    if not settings.contains(LOCAL_API_TOKEN_SETTINGS_KEY):
        settings.setValue(LOCAL_API_TOKEN_SETTINGS_KEY, DEFAULT_LOCAL_API_TOKEN)
    settings.sync()
    apply_local_api_token_from_settings(settings)


def list_antidetect_profiles(
    settings: PlatformSettings,
    *,
    kind: str | None = None,
    token: str | None = None,
    base_url: str | None = None,
    session_token: str | None = None,
) -> dict[str, Any]:
    """
    Return {kind, base_url, count, profiles}.

    Always uses local/remote HTTP API (own antidetect). Dolphin is not supported.
    Prefer Zaliver session Bearer (accepted by antidetect serve) over local_api_token.
    """
    k = resolve_antidetect_kind(settings, kind)
    profiles: list[dict[str, Any]] = []

    base = resolve_local_base_url(settings, base_url)
    api_token = (
        (session_token or "").strip()
        or resolve_local_api_token_setting(settings, token)
    )
    try:
        api = LocalAntidetectHttpAPI(base, token=api_token or None)
        try:
            raw_items = api.list_profiles()
        finally:
            api.close()
    except LocalAntidetectError as e:
        raise RuntimeError(
            f"Local antidetect at {base} unavailable: {e}. "
            "Start the local antidetect API on the server "
            f"(default {DEFAULT_LOCAL_API_BASE_URL})."
        ) from e
    except Exception as e:
        raise RuntimeError(f"Local antidetect error ({base}): {e}") from e

    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        norm = normalize_local_profile_for_ui(raw)
        pid = str(norm.get("id") or "").strip()
        if not pid:
            continue
        cd = norm.get("custom_data")
        profiles.append(
            {
                "id": pid,
                "name": str(norm.get("name") or pid).strip(),
                "tags": list(norm.get("tags") or [])
                if isinstance(norm.get("tags"), list)
                else [],
                "custom_data": dict(cd) if isinstance(cd, dict) else {},
            }
        )
    return {
        "kind": k,
        "base_url": base,
        "count": len(profiles),
        "profiles": profiles,
    }
