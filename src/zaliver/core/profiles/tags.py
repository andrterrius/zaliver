"""Apply Zaliver result tags on own-antidetect profiles."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from zaliver.antydetect.local_antidetect_api import (
    DEFAULT_LOCAL_API_BASE_URL,
    LocalAntidetectHttpAPI,
)


OWN_ANTYDETECT_KINDS = frozenset({"local", "remote"})


def is_own_antidetect_kind(kind: str) -> bool:
    return (kind or "").strip() in OWN_ANTYDETECT_KINDS


def own_antidetect_api_label(kind: str) -> str:
    if (kind or "").strip() == "remote":
        return "удалённого"
    return "локального"


def apply_result_tags(
    *,
    kind: str,
    base_url: str,
    profile_id: str,
    updates: list[tuple[bool, str, str]],
    log: Callable[[str], None],
    log_prefix: str,
    on_tags_applied: Callable[[str, list[dict[str, Any]]], None] | None = None,
) -> None:
    if not is_own_antidetect_kind(kind) or not updates:
        return
    pid = (profile_id or "").strip()
    if not pid:
        return
    from zaliver.antydetect.profile_tags import apply_mutually_exclusive_profile_tag

    base_u = (base_url or "").strip() or DEFAULT_LOCAL_API_BASE_URL
    try:
        api = LocalAntidetectHttpAPI(base_u)
        try:
            for success, success_tag, error_tag in updates:
                apply_mutually_exclusive_profile_tag(
                    api,
                    pid,
                    success=success,
                    success_tag=success_tag,
                    error_tag=error_tag,
                )
                tag = success_tag if success else error_tag
                log(f"[{log_prefix}] profile={pid} tag_set={tag!r}")
        finally:
            api.close()
        if on_tags_applied is not None:
            payload = [
                {
                    "success": success,
                    "success_tag": success_tag,
                    "error_tag": error_tag,
                }
                for success, success_tag, error_tag in updates
            ]
            on_tags_applied(pid, payload)
    except Exception as te:
        log(f"[{log_prefix}] profile={pid} tag_set_failed err={te!r}")
