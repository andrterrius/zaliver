"""Resolve antidetect kind / base URL and list profiles (local-first)."""

from __future__ import annotations

from typing import Any

from zaliver.antydetect.local_antidetect_api import (
    DEFAULT_LOCAL_API_BASE_URL,
    LocalAntidetectError,
    LocalAntidetectHttpAPI,
    normalize_local_profile_for_ui,
)
from zaliver.config.platform_settings import PlatformSettings


OWN_KINDS = frozenset({"local", "remote"})


def is_own_kind(kind: str) -> bool:
    return (kind or "").strip().lower() in OWN_KINDS


def resolve_antidetect_kind(settings: PlatformSettings, override: str | None = None) -> str:
    raw = (override or "").strip().lower()
    if raw in {"dolphin", "local", "remote"}:
        return raw
    stored = str(
        settings.value("antydetect/default_browser", "local") or "local"
    ).strip().lower()
    if stored in {"dolphin", "local", "remote"}:
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


def ensure_antidetect_defaults(settings: PlatformSettings) -> None:
    """Seed local-antidetect defaults for server deploys (same host)."""
    if not settings.contains("antydetect/default_browser"):
        settings.setValue("antydetect/default_browser", "local")
    if not settings.contains("antydetect/local_api_base_url"):
        settings.setValue(
            "antydetect/local_api_base_url", DEFAULT_LOCAL_API_BASE_URL
        )
    if not settings.contains("antydetect/own_base_url"):
        settings.setValue("antydetect/own_base_url", DEFAULT_LOCAL_API_BASE_URL)
    if not settings.contains("antydetect/dolphin_headless"):
        settings.setValue("antydetect/dolphin_headless", True)
    settings.sync()


def list_antidetect_profiles(
    settings: PlatformSettings,
    *,
    kind: str | None = None,
    token: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """
    Return {kind, base_url, count, profiles}.

    Local/remote: LocalAntidetectHttpAPI (no Dolphin token).
    Dolphin: requires token.
    """
    k = resolve_antidetect_kind(settings, kind)
    profiles: list[dict[str, Any]] = []

    if is_own_kind(k):
        base = resolve_local_base_url(settings, base_url)
        try:
            api = LocalAntidetectHttpAPI(base)
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

    # Dolphin
    tok = (token or "").strip() or str(
        settings.value("antydetect/dolphin_token", "") or ""
    ).strip()
    if not tok:
        raise RuntimeError(
            "Dolphin mode: set antydetect/dolphin_token via PATCH /v1/settings, "
            "or switch antydetect/default_browser to 'local'."
        )
    try:
        from zaliver.antydetect.api import DolphinAntyLocalAPI

        api = DolphinAntyLocalAPI()
        try:
            api.login_with_token(tok)
            items = api.list_profiles()
        finally:
            api.close()
    except Exception as e:
        raise RuntimeError(f"Dolphin antidetect error: {e}") from e

    if not isinstance(items, list):
        items = []
    for p in items:
        if not isinstance(p, dict):
            continue
        profiles.append(
            {
                "id": str(p.get("id") or p.get("uuid") or "").strip(),
                "name": str(p.get("name") or p.get("title") or "").strip(),
                "tags": p.get("tags") if isinstance(p.get("tags"), list) else [],
                "custom_data": {},
            }
        )
    return {
        "kind": "dolphin",
        "base_url": "",
        "count": len(profiles),
        "profiles": profiles,
    }
