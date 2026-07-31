"""Browser upload job runner (gated; uses MultiProfileUploader + antic_open)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
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
    publish_before_checks: bool = True,
    keep_studio_title: bool = False,
    schedule_times: list[datetime] | None = None,
    schedule_warmup_shorts: bool = False,
    schedule_warmup_shorts_recommendations: bool = True,
    schedule_warmup_search_query: str = "",
    delete_after_upload: bool = False,
    search_oldest_channel: bool = False,
) -> None:
    from zaliver.youtube_upload.multi_uploader import MultiProfileUploader
    from zaliver.youtube_upload.schedule_publish import parse_msk_datetime

    plat = (platform or "").strip().lower()
    is_instagram = plat == "instagram"
    is_yt_inst = plat in {"yt_inst", "youtube_instagram", "yt+inst"}
    paths = [p for p in video_paths if (p or "").strip()]
    total = len(paths)
    if total <= 0:
        sink.on_finished(False, "No video paths.")
        return

    bu = (base_url or "").strip().rstrip("/")
    if not bu:
        from zaliver.antydetect.local_antidetect_api import DEFAULT_LOCAL_API_BASE_URL

        bu = DEFAULT_LOCAL_API_BASE_URL

    parsed_times: list[datetime] = []
    for raw in schedule_times or []:
        dt = parse_msk_datetime(raw)
        if dt is not None:
            parsed_times.append(dt)
    parsed_times = sorted(parsed_times)
    schedule_batch = len(parsed_times) if parsed_times and not is_instagram else 0
    if is_instagram and schedule_times:
        sink.on_log(
            "Instagram Reels: отложка Studio не поддерживается — публикуем сразу."
        )

    pub_before = True if is_instagram else bool(publish_before_checks)
    keep_title = False if is_instagram else bool(keep_studio_title)
    warmup_on = bool(schedule_warmup_shorts) and schedule_batch > 0
    warmup_reco = bool(schedule_warmup_shorts_recommendations)
    warmup_q = (schedule_warmup_search_query or "").strip()

    def upload_one(profile_id: str, task: Any, tab_index: int = 0) -> None:
        from zaliver.antydetect.antic_open import (
            open_google_in_local_antidetect_profile,
            open_google_in_profile,
            set_log_sink,
            upload_instagram_reel_in_local_antidetect_profile,
            upload_instagram_reel_in_profile,
            upload_youtube_and_instagram_in_local_antidetect_profile,
            upload_youtube_and_instagram_in_profile,
        )

        set_log_sink(sink.on_log)
        kind_l = (kind or "local").strip().lower()
        if kind_l == "dolphin":
            kind_l = "local"
        own = kind_l in {
            "own",
            "local",
            "remote",
            "own_antidetect",
            "local_antidetect",
        }

        sched_at = getattr(task, "schedule_publish_at", None)
        sched_batch = getattr(task, "scheduled_batch", None)

        if is_yt_inst:
            kw: dict[str, Any] = dict(
                video_path=task.video_path,
                title=task.title or title,
                description=task.description or description,
                headless=headless,
                publish_before_checks=pub_before,
                keep_studio_title=keep_title,
                schedule_publish_at=sched_at,
                scheduled_batch=sched_batch,
                warmup_during_schedule=warmup_on,
                warmup_shorts_recommendations=warmup_reco,
                warmup_search_query=warmup_q or None,
                search_oldest_channel=bool(search_oldest_channel),
            )
            if own:
                upload_youtube_and_instagram_in_local_antidetect_profile(
                    profile_id,
                    base_url=bu,
                    **kw,
                )
            else:
                upload_youtube_and_instagram_in_profile(
                    profile_id,
                    local_token=token or None,
                    **kw,
                )
            return

        if is_instagram:
            kw = dict(
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
                    base_url=bu,
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
            publish_before_checks=pub_before,
            keep_studio_title=keep_title,
            schedule_publish_at=sched_at,
            scheduled_batch=sched_batch,
            warmup_during_schedule=warmup_on,
            warmup_shorts_recommendations=warmup_reco,
            warmup_search_query=warmup_q or None,
            search_oldest_channel=bool(search_oldest_channel),
        )
        if own:
            open_google_in_local_antidetect_profile(
                profile_id,
                base_url=bu,
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
        schedule_batch_size=schedule_batch,
        schedule_times=parsed_times if schedule_batch else None,
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

    if delete_after_upload and ok_n > 0:
        from pathlib import Path

        deleted = 0
        # Best-effort: delete successfully uploaded paths when uploader tracks them.
        success_paths = getattr(mgr, "success_video_paths", None)
        to_delete = list(success_paths) if success_paths else []
        if not to_delete and failed == 0:
            to_delete = list(paths)
        for p in to_delete:
            try:
                path = Path(str(p))
                if path.is_file():
                    path.unlink()
                    deleted += 1
                    sink.on_log(f"[upload] Удалён после залива: {path.name}")
            except OSError as e:
                sink.on_log(f"[upload] Не удалось удалить после залива: {p} ({e!r})")
        if deleted:
            sink.on_log(f"[upload] Очередь завершена — удалено после залива: {deleted}")

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
