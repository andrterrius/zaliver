"""AI prompts CRUD + OpenAI-compatible generation (no Qt)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

BUILTIN_PROMPTS: tuple[tuple[str, str, str], ...] = (
    ("builtin_video_title", "Название видео", ""),
    ("builtin_video_description", "Описание видео", ""),
    ("builtin_channel_name", "Название канала", ""),
    ("builtin_channel_description", "Описание канала", ""),
    ("builtin_link_title", "Название ссылки", ""),
    ("builtin_youtube_comments", "Комментарии YouTube", ""),
)

INSTAGRAM_HIDDEN_BUILTIN_IDS: frozenset[str] = frozenset(
    {
        "builtin_video_description",
        "builtin_link_title",
    }
)

CUSTOM_ORDER_KEY = "ai/prompt_order"
_REPLY_LINES_SUFFIX = "Количество необходимых строк-ответов: {n}"


@dataclass
class PromptItem:
    id: str
    title: str
    text: str
    builtin: bool


def builtin_ids() -> frozenset[str]:
    return frozenset(pid for pid, _, _ in BUILTIN_PROMPTS)


def list_prompts(settings: Any, *, platform: str) -> list[PromptItem]:
    from zaliver.config.platform_settings import is_instagram_platform

    items: list[PromptItem] = []
    for pid, title, default_text in BUILTIN_PROMPTS:
        if is_instagram_platform(platform) and pid in INSTAGRAM_HIDDEN_BUILTIN_IDS:
            continue
        key = f"ai/prompts/{pid}/text"
        if settings.contains(key):
            text = str(settings.value(key, "", type=str) or "")
        else:
            text = default_text
        items.append(PromptItem(id=pid, title=title, text=text, builtin=True))

    migrated = {"agent", "builtin_agent", *builtin_ids()}
    raw_order = settings.value(CUSTOM_ORDER_KEY, "", type=str) or ""
    ids: list[str] = []
    if str(raw_order).strip():
        try:
            parsed = json.loads(raw_order)
            if isinstance(parsed, list):
                ids = [str(x) for x in parsed if str(x).strip()]
        except Exception:
            ids = [p.strip() for p in str(raw_order).split(",") if p.strip()]

    for pid in ids:
        if pid in migrated:
            continue
        title = str(
            settings.value(f"ai/prompts/{pid}/title", "", type=str) or ""
        ).strip() or "Промпт"
        text = str(settings.value(f"ai/prompts/{pid}/text", "", type=str) or "")
        items.append(PromptItem(id=pid, title=title, text=text, builtin=False))
    return items


def _save_custom_order(settings: Any, custom_ids: list[str]) -> None:
    settings.setValue(CUSTOM_ORDER_KEY, json.dumps(list(custom_ids), ensure_ascii=False))


def put_prompts(
    settings: Any,
    *,
    platform: str,
    prompts: list[dict[str, Any]],
) -> list[PromptItem]:
    """Replace builtin texts + custom set from payload."""
    builtins = builtin_ids()
    custom_ids: list[str] = []
    seen_custom: set[str] = set()

    for raw in prompts:
        pid = str(raw.get("id") or "").strip()
        if not pid:
            continue
        title = str(raw.get("title") or "").strip() or "Промпт"
        text = str(raw.get("text") or "")
        if pid in builtins:
            settings.setValue(f"ai/prompts/{pid}/text", text)
            continue
        if pid in seen_custom:
            continue
        seen_custom.add(pid)
        custom_ids.append(pid)
        settings.setValue(f"ai/prompts/{pid}/title", title)
        settings.setValue(f"ai/prompts/{pid}/text", text)

    _save_custom_order(settings, custom_ids)
    settings.sync()
    return list_prompts(settings, platform=platform)


def add_prompt(
    settings: Any, *, platform: str, title: str = "", text: str = ""
) -> PromptItem:
    existing = list_prompts(settings, platform=platform)
    custom = [p for p in existing if not p.builtin]
    n = len(custom) + 1
    pid = uuid.uuid4().hex[:12]
    item = PromptItem(
        id=pid,
        title=(title or "").strip() or f"Промпт {n}",
        text=text or "",
        builtin=False,
    )
    settings.setValue(f"ai/prompts/{pid}/title", item.title)
    settings.setValue(f"ai/prompts/{pid}/text", item.text)
    _save_custom_order(settings, [p.id for p in custom] + [pid])
    settings.sync()
    return item


def delete_prompt(settings: Any, *, platform: str, prompt_id: str) -> bool:
    pid = (prompt_id or "").strip()
    if not pid or pid in builtin_ids():
        return False
    existing = list_prompts(settings, platform=platform)
    custom_ids = [p.id for p in existing if not p.builtin and p.id != pid]
    for suffix in ("title", "text"):
        try:
            settings.remove(f"ai/prompts/{pid}/{suffix}")
        except Exception:
            settings.setValue(f"ai/prompts/{pid}/{suffix}", "")
    _save_custom_order(settings, custom_ids)
    settings.sync()
    return True


def generate_text(
    settings: Any,
    *,
    platform: str,
    prompt_id: str = "",
    prompt_text: str = "",
    reply_lines: int = 1,
) -> str:
    from zaliver.ai.openai_compat import chat_completion

    text = (prompt_text or "").strip()
    if not text and prompt_id:
        for item in list_prompts(settings, platform=platform):
            if item.id == prompt_id:
                text = item.text or ""
                break
    if not text:
        raise ValueError("Пустой промпт.")
    n = max(1, min(500, int(reply_lines or 1)))
    text = f"{text.rstrip()}\n\n{_REPLY_LINES_SUFFIX.format(n=n)}"

    base_url = str(settings.value("ai/base_url", "", type=str) or "")
    api_key = str(settings.value("ai/api_key", "", type=str) or "")
    model = str(settings.value("ai/model", "", type=str) or "")
    return chat_completion(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=[{"role": "user", "content": text}],
    )
