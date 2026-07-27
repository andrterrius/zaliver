"""Browser upload job runner (gated; uses MultiProfileUploader + antic_open)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from zaliver.core.sinks import JobProgressSink


def run_upload_job(
    *,
    platform: str,
    profile_ids: list[str],
    video_paths: list[str],
    title: str,
    description: str,
    kind: str,
    token: str,
    base_url: str,
    headless: bool,
    max_concurrent: int,
    cooldown_s: float,
    sink: JobProgressSink,
    register_cancel: Callable[[Callable[[], None]], None],
) -> None:
    from zaliver.youtube_upload.multi_uploader import MultiProfileUploader

    is_instagram = platform == "instagram"
    paths = [p for p in video_paths if (p or "").strip()]
    total = len(paths)
    if total <= 0:
        sink.on_finished(False, "No video paths.")
        return

    def upload_one(profile_id: str, task: Any, tab_index: int = 0) -> None:
        from zaliver.antydetect.antic_open import (
            open_google_in_local_antidetect_profile,
            open_google_in_profile,
            set_log_sink,
            upload_instagram_reel_in_local_antidetect_profile,
            upload_instagram_reel_in_profile,
        )

        set_log_sink(sink.on_log)
        kind_l = (kind or "dolphin").strip().lower()
        own = kind_l in {"own", "local", "own_antidetect", "local_antidetect"}

        if is_instagram:
            kw: dict[str, Any] = dict(
                video_path=task.video_path,
                title=task.title or title,
                description=task.description or description,
                headless=headless,
                keep_browser_open=False,
                tab_index=int(tab_index),
            )
            if own:
                upload_instagram_reel_in_local_antidetect_profile(
                    profile_id,
                    base_url=(base_url or "").strip(),
                    **kw,
                )
            else:
                upload_instagram_reel_in_profile(
                    profile_id,
                    local_token=token or None,
                    **kw,
                )
            return

        yt_kw: dict[str, Any] = dict(
            video_path=task.video_path,
            title=task.title or title,
            description=task.description or description,
            headless=headless,
            upload_latest_zaliver_video=False,
        )
        if own:
            open_google_in_local_antidetect_profile(
                profile_id,
                base_url=(base_url or "").strip(),
                **yt_kw,
            )
        else:
            open_google_in_profile(
                profile_id,
                local_token=token or None,
                **yt_kw,
            )

    mgr = MultiProfileUploader(
        profile_ids=list(profile_ids),
        cooldown_s=float(cooldown_s),
        max_concurrent_uploads=int(max_concurrent),
        log_sink=sink.on_log,
        upload_one=upload_one,
    )
    register_cancel(lambda: mgr.stop(reason="api_cancel"))

    sink.on_progress(0, total, "upload starting")
    mgr.enqueue_videos(
        video_paths=paths,
        title=title,
        description=description,
    )
    mgr.mark_producer_done()
    mgr.start()
    mgr.join()

    failed = int(mgr.done_failed)
    ok_n = int(mgr.done_ok) if hasattr(mgr, "done_ok") else max(0, total - failed)
    for p in paths:
        sink.on_output_saved(p, False)
    sink.on_progress(total, total, "upload done")
    if failed and ok_n <= 0:
        sink.on_finished(False, f"Upload failed ({failed} failed).")
    else:
        sink.on_finished(
            True,
            f"Upload finished: ok={ok_n}, failed={failed}, total={total}.",
        )
