"""Browser upload job runner (gated; uses MultiProfileUploader + antic_open)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from zaliver.config.platform_settings import (
    PLATFORM_INSTAGRAM,
    PLATFORM_YOUTUBE,
    PLATFORM_YT_INST,
)
from zaliver.core.sinks import JobProgressSink
from zaliver.stats_server_client import notify_uploaded_video


def _canonical_watch_url(video_id: str) -> str:
    vid = (video_id or "").strip()
    return f"https://www.youtube.com/watch?v={vid}" if vid else ""


def _extract_vid_url(one_res: Any, *, is_ig: bool) -> tuple[str, str]:
    vid = ""
    url = ""
    if isinstance(one_res, dict):
        vid = str(one_res.get("video_id") or "").strip()
        url = str(one_res.get("url") or "").strip()
    if not vid and url:
        if is_ig:
            for marker in ("/reel/", "/p/"):
                if marker in url:
                    part = url.split(marker, 1)[1]
                    vid = part.split("/", 1)[0].split("?", 1)[0].strip()
                    break
        else:
            try:
                from zaliver.youtube_parsing.video_stats import extract_video_id

                vid = extract_video_id(url) or ""
            except Exception:
                pass
    if not url and vid:
        if is_ig:
            url = f"https://www.instagram.com/reel/{vid}/"
        else:
            url = _canonical_watch_url(vid)
    return vid, url


def _confirm_instagram_result(upload_store: Any, res: Any) -> dict:
    ig_vid = ""
    ig_url = ""
    candidates: list[dict] = []
    if isinstance(res, dict):
        ig_vid = str(res.get("video_id") or "").strip()
        ig_url = str(res.get("url") or "").strip()
        raw_cands = res.get("candidate_reels")
        if isinstance(raw_cands, list):
            for item in raw_cands:
                if not isinstance(item, dict):
                    continue
                c_vid = str(item.get("video_id") or "").strip()
                c_url = str(item.get("url") or "").strip()
                if c_vid or c_url:
                    candidates.append({"video_id": c_vid, "url": c_url})
    if not candidates and (ig_vid or ig_url):
        candidates = [{"video_id": ig_vid, "url": ig_url}]

    chosen = None
    skipped: list[str] = []
    for cand in candidates:
        c_vid = str(cand.get("video_id") or "").strip()
        c_url = str(cand.get("url") or "").strip()
        if upload_store is not None and upload_store.has_uploaded_video(
            video_id=c_vid,
            url=c_url,
            platform=PLATFORM_INSTAGRAM,
        ):
            skipped.append(c_vid or c_url)
            continue
        chosen = cand
        break
    if chosen is None:
        raise RuntimeError(
            "Instagram Reels: первое видео в профиле уже есть в базе залитых "
            f"(video_id={ig_vid!r}, url={ig_url!r}, already={skipped!r}) "
            "— заливка не подтверждена."
        )
    ig_vid = str(chosen.get("video_id") or "").strip()
    ig_url = str(chosen.get("url") or "").strip()
    if isinstance(res, dict):
        out = dict(res)
        out["video_id"] = ig_vid
        out["url"] = ig_url
        return out
    return {"video_id": ig_vid, "url": ig_url}


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
    upload_store: Any | None = None,
    stats_server_username: str = "",
) -> None:
    from zaliver.youtube_upload.multi_uploader import MultiProfileUploader
    from zaliver.youtube_upload.schedule_publish import parse_msk_datetime

    plat = (platform or "").strip().lower()
    is_instagram = plat == PLATFORM_INSTAGRAM
    is_yt_inst = plat in {
        PLATFORM_YT_INST,
        "youtube_instagram",
        "yt+inst",
    }
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
    guser = (stats_server_username or "").strip()
    session_plat = PLATFORM_YT_INST if is_yt_inst else (
        PLATFORM_INSTAGRAM if is_instagram else PLATFORM_YOUTUBE
    )

    upload_session = None
    if upload_store is not None:
        try:
            upload_session = upload_store.start_session(
                planned_videos=total, platform=session_plat
            )
            sink.on_log(
                f"[upload] сессия залитых #{upload_session.id} "
                f"(planned={total}, platform={session_plat})"
            )
        except Exception as e:
            sink.on_log(f"[upload] не удалось создать сессию залитых: {e!r}")
            upload_session = None

    success_paths: set[str] = set()
    success_lock = threading.Lock()
    record_lock = threading.Lock()

    def _record_one(
        *,
        profile_id: str,
        video_path: str,
        title: str,
        description: str,
        one_res: Any,
        schedule_publish_at: datetime | None = None,
        record_platform: str | None = None,
    ) -> None:
        if upload_store is None or upload_session is None:
            sink.on_log(
                "[upload] upload_store/session недоступны — "
                "пропуск записи в залитые и stats_server."
            )
            return

        rec_plat = (record_platform or session_plat or "").strip() or PLATFORM_YOUTUBE
        if rec_plat == PLATFORM_YT_INST:
            rec_plat = PLATFORM_YOUTUBE
        is_ig_rec = rec_plat == PLATFORM_INSTAGRAM
        vid, url = _extract_vid_url(one_res, is_ig=is_ig_rec)
        if not vid:
            raise RuntimeError(f"Empty video_id (res={one_res!r})")
        if not url:
            raise RuntimeError(f"Empty url (res={one_res!r})")

        sid = int(upload_session.id)
        stored_title = title or ""
        if keep_title and not stored_title and not is_ig_rec:
            stored_title = Path(video_path).stem

        with record_lock:
            upload_store.add_uploaded_video(
                session_id=sid,
                title=stored_title,
                description=description or "",
                url=url,
                video_id=vid,
                profile_id=profile_id,
                platform=rec_plat,
            )
            try:
                upload_store.inc_uploaded_ok(session_id=sid, delta=1)
            except Exception:
                pass

        sink.on_log(
            f"[upload] записано в залитые: platform={rec_plat} "
            f"video_id={vid!r} url={url!r}"
        )

        try:
            stats_notified = bool(
                isinstance(one_res, dict) and one_res.get("stats_notified")
            )
            if guser and not stats_notified:
                scheduled_unix = None
                if not is_ig_rec:
                    sched_dt = parse_msk_datetime(schedule_publish_at)
                    if sched_dt is not None:
                        scheduled_unix = int(sched_dt.timestamp())
                ok = notify_uploaded_video(
                    video_id=vid,
                    username=guser,
                    profile_id=profile_id,
                    scheduled=scheduled_unix,
                    platform=rec_plat,
                )
                if ok:
                    sink.on_log(
                        f"[stats_server] уведомление отправлено: videoId={vid}"
                    )
                else:
                    sink.on_log(
                        f"[stats_server] сервер не принял уведомление: videoId={vid}"
                    )
            elif not guser:
                sink.on_log(
                    "[stats_server] username не задан — уведомление пропущено."
                )
        except Exception as e:
            sink.on_log(f"[stats_server] ошибка уведомления: {e!r}")

        with success_lock:
            success_paths.add(video_path)

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
        task_title = task.title or title
        task_desc = task.description or description

        if is_yt_inst:
            def _on_yt(one_res: dict) -> None:
                batch = one_res.get("batch_results") if isinstance(one_res, dict) else None
                if isinstance(batch, list) and sched_batch:
                    for item, item_res in zip(sched_batch, batch):
                        _record_one(
                            profile_id=profile_id,
                            video_path=item.video_path,
                            title=item.title,
                            description=item.description,
                            one_res=item_res,
                            schedule_publish_at=item.schedule_publish_at,
                            record_platform=PLATFORM_YOUTUBE,
                        )
                else:
                    _record_one(
                        profile_id=profile_id,
                        video_path=task.video_path,
                        title=task_title,
                        description=task_desc,
                        one_res=one_res,
                        schedule_publish_at=sched_at,
                        record_platform=PLATFORM_YOUTUBE,
                    )

            def _on_ig(one_res: dict) -> None:
                confirmed = _confirm_instagram_result(upload_store, one_res)
                _record_one(
                    profile_id=profile_id,
                    video_path=task.video_path,
                    title=task_title,
                    description=task_desc,
                    one_res=confirmed,
                    record_platform=PLATFORM_INSTAGRAM,
                )

            kw: dict[str, Any] = dict(
                video_path=task.video_path,
                title=task_title,
                description=task_desc,
                headless=headless,
                publish_before_checks=pub_before,
                keep_studio_title=keep_title,
                schedule_publish_at=sched_at,
                scheduled_batch=sched_batch,
                warmup_during_schedule=warmup_on,
                warmup_shorts_recommendations=warmup_reco,
                warmup_search_query=warmup_q or None,
                search_oldest_channel=bool(search_oldest_channel),
                stats_server_username=guser or None,
                on_youtube_success=_on_yt,
                on_instagram_success=_on_ig,
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
                title=task_title,
                description=task_desc,
                headless=headless,
                keep_browser_open=False,
                tab_index=int(tab_index),
            )
            if own:
                res = upload_instagram_reel_in_local_antidetect_profile(
                    profile_id,
                    base_url=bu,
                    **kw,
                )
            else:
                res = upload_instagram_reel_in_profile(
                    profile_id,
                    local_token=token or None,
                    **kw,
                )
            confirmed = _confirm_instagram_result(upload_store, res)
            _record_one(
                profile_id=profile_id,
                video_path=task.video_path,
                title=task_title,
                description=task_desc,
                one_res=confirmed,
                record_platform=PLATFORM_INSTAGRAM,
            )
            return

        yt_kw: dict[str, Any] = dict(
            video_path=task.video_path,
            title=task_title,
            description=task_desc,
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
            stats_server_username=guser or None,
        )
        if own:
            res = open_google_in_local_antidetect_profile(
                profile_id,
                base_url=bu,
                **yt_kw,
            )
        else:
            res = open_google_in_profile(
                profile_id,
                local_token=token or None,
                **yt_kw,
            )

        batch = res.get("batch_results") if isinstance(res, dict) else None
        if isinstance(batch, list) and sched_batch:
            if len(batch) != len(sched_batch):
                raise RuntimeError(
                    "scheduled_batch size mismatch: "
                    f"{len(batch)} results vs {len(sched_batch)} tasks"
                )
            for item, item_res in zip(sched_batch, batch):
                _record_one(
                    profile_id=profile_id,
                    video_path=item.video_path,
                    title=item.title,
                    description=item.description,
                    one_res=item_res,
                    schedule_publish_at=item.schedule_publish_at,
                    record_platform=PLATFORM_YOUTUBE,
                )
        else:
            _record_one(
                profile_id=profile_id,
                video_path=task.video_path,
                title=task_title,
                description=task_desc,
                one_res=res,
                schedule_publish_at=sched_at,
                record_platform=PLATFORM_YOUTUBE,
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
    try:
        mgr.enqueue_videos(
            video_paths=paths,
            title=title,
            description=description,
        )
        mgr.mark_producer_done()
        mgr.start()
        mgr.join()
    finally:
        if upload_store is not None and upload_session is not None:
            failed_n = int(mgr.done_failed)
            ok_n = int(mgr.done_ok) if hasattr(mgr, "done_ok") else 0
            status = "done" if ok_n > 0 or failed_n <= 0 else "failed"
            try:
                upload_store.finish_session(
                    session_id=int(upload_session.id), status=status
                )
            except Exception as e:
                sink.on_log(f"[upload] finish_session: {e!r}")

    failed = int(mgr.done_failed)
    ok_n = int(mgr.done_ok) if hasattr(mgr, "done_ok") else max(0, total - failed)

    if delete_after_upload and ok_n > 0:
        to_delete = list(success_paths) if success_paths else []
        if not to_delete and failed == 0:
            to_delete = list(paths)
        deleted = 0
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
