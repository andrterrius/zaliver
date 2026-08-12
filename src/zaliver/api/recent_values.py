"""List / remember recent text field values for the web UI (shared SQLite store)."""

from __future__ import annotations

from typing import Any

from zaliver.api.schemas import (
    ChannelSetupJobRequest,
    PromoteJobRequest,
    RecentValuesResponse,
)


def list_recent_values(upload_store: Any, *, platform: str) -> RecentValuesResponse:
    plat = str(platform or "youtube")
    return RecentValuesResponse(
        platform=plat,
        upload_titles=upload_store.list_recent_upload_titles(platform=plat),
        channel_name_fields=upload_store.list_recent_channel_name_fields(
            platform=plat
        ),
        channel_descriptions=upload_store.list_recent_channel_descriptions(
            platform=plat
        ),
        channel_link_titles=upload_store.list_recent_channel_link_titles(
            platform=plat
        ),
        channel_link_urls=upload_store.list_recent_channel_link_urls(platform=plat),
        video_default_title_fields=upload_store.list_recent_video_default_title_fields(
            platform=plat
        ),
        promote_comment_fields=upload_store.list_recent_promote_comment_fields(
            platform=plat
        ),
        text_overlay_texts=upload_store.list_recent_text_overlay_texts(platform=plat),
    )


def remember_upload_title(
    upload_store: Any,
    *,
    title: str,
    platform: str,
    keep_studio_title: bool = False,
) -> None:
    if keep_studio_title:
        return
    text = (title or "").strip()
    if not text:
        return
    try:
        upload_store.remember_upload_title(text, platform=platform)
    except Exception:
        pass


def remember_channel_setup(
    upload_store: Any,
    body: ChannelSetupJobRequest,
    *,
    platform: str,
) -> None:
    try:
        names_field = (body.names_field or "").strip()
        if not names_field:
            names = [
                str(a.channel_name or "").strip()
                for a in (body.assignments or [])
                if str(a.channel_name or "").strip()
            ]
            names_field = "\n".join(dict.fromkeys(names))
        if names_field:
            upload_store.remember_channel_name_field(names_field, platform=platform)
            upload_store.remember_channel_names(
                [
                    line.strip()
                    for line in names_field.splitlines()
                    if line.strip()
                ],
                platform=platform,
            )

        desc_field = (body.description_field or "").strip()
        if not desc_field:
            lines = [str(x).strip() for x in (body.description_lines or []) if str(x).strip()]
            if not lines and (body.description or "").strip():
                lines = [(body.description or "").strip()]
            desc_field = "\n".join(lines)
        if desc_field:
            upload_store.remember_channel_description(desc_field, platform=platform)

        link_titles_field = (body.link_titles_field or "").strip()
        link_urls_field = (body.link_urls_field or "").strip()
        links = list(body.channel_links or [])
        if not link_titles_field and links:
            link_titles_field = "\n".join(
                str(pair[0]).strip() for pair in links if len(pair) >= 1 and str(pair[0]).strip()
            )
        if not link_urls_field and links:
            link_urls_field = "\n".join(
                str(pair[1]).strip() for pair in links if len(pair) >= 2 and str(pair[1]).strip()
            )
        for line in link_titles_field.splitlines() if link_titles_field else []:
            t = line.strip()
            if t:
                upload_store.remember_channel_link_title(t, platform=platform)
        for line in link_urls_field.splitlines() if link_urls_field else []:
            u = line.strip()
            if u:
                upload_store.remember_channel_link_url(u, platform=platform)

        video_field = (body.video_titles_field or "").strip()
        if not video_field:
            titles = [
                str(a.video_default_title or "").strip()
                for a in (body.assignments or [])
                if str(a.video_default_title or "").strip()
            ]
            video_field = "\n".join(dict.fromkeys(titles))
        if video_field:
            upload_store.remember_video_default_title_field(
                video_field, platform=platform
            )
            for line in video_field.splitlines():
                vt = line.strip()
                if vt:
                    upload_store.remember_video_default_title(vt, platform=platform)
    except Exception:
        pass


def remember_promote_comments(
    upload_store: Any,
    body: PromoteJobRequest,
    *,
    platform: str,
) -> None:
    settings = body.settings
    if not settings.enable_comments:
        return
    field = (settings.comments_field or "").strip()
    if not field:
        field = "\n".join(
            str(c).strip() for c in (settings.comments or []) if str(c).strip()
        )
    if not field:
        return
    try:
        upload_store.remember_promote_comment_field(field, platform=platform)
    except Exception:
        pass
