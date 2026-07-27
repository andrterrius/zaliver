"""Resolve login/session credentials from profile custom_data."""

from __future__ import annotations

from typing import Any, Callable, Mapping


def custom_data_for_profile(
    profiles_custom_data: Mapping[str, Mapping[str, Any]] | None,
    profile_id: str,
) -> dict[str, Any] | None:
    pid = (profile_id or "").strip()
    if not pid or not profiles_custom_data:
        return None
    raw = profiles_custom_data.get(pid)
    if isinstance(raw, dict):
        return dict(raw)
    return None


def make_login_credentials_resolver(
    profiles_custom_data: Mapping[str, Mapping[str, Any]] | None,
    *,
    platform: str,
) -> Callable[[str], Any]:
    def _resolve(profile_id: str) -> Any:
        from zaliver.youtube_upload.google_login import (
            credentials_from_custom_data,
            gmail_or_yt_credentials_from_custom_data,
        )

        cd = custom_data_for_profile(profiles_custom_data, profile_id)
        if not cd:
            return None
        if (platform or "").strip().lower() == "instagram":
            return gmail_or_yt_credentials_from_custom_data(cd)
        return credentials_from_custom_data(cd)

    return _resolve


def make_instagram_session_resolver(
    profiles_custom_data: Mapping[str, Mapping[str, Any]] | None,
) -> Callable[[str], tuple[str, str, str]]:
    def _resolve(profile_id: str) -> tuple[str, str, str]:
        from zaliver.instagram_upload.instagram_availability import (
            session_login_from_custom_data,
            session_password_from_custom_data,
            session_twofa_from_custom_data,
        )

        cd = custom_data_for_profile(profiles_custom_data, profile_id)
        if not cd:
            return "", "", ""
        return (
            session_login_from_custom_data(cd),
            session_password_from_custom_data(cd),
            session_twofa_from_custom_data(cd),
        )

    return _resolve
