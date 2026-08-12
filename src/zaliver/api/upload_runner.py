"""Browser upload job runner (gated; uses MultiProfileUploader + antic_open)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from zaliver.antydetect.browser_concurrency import (
    clamp_max_concurrent_browsers,
    compute_instagram_tabs_per_profile,
    instagram_tabs_per_profile_from_settings,
)
from zaliver.config.platform_settings import (
    PLATFORM_INSTAGRAM,
    PLATFORM_YOUTUBE,
    PLATFORM_YT_INST,
)
from zaliver.core.sinks import JobProgressSink
from zaliver.db.upload_store import resolve_upload_pause
from zaliver.stats_server_client import notify_uploaded_video


@dataclass
class _StreamingUploadSlot:
    mgr: Any
    title: str
    description: str
    producer_done: threading.Event = field(default_factory=threading.Event)
    enqueued_paths: set[str] = field(default_factory=set)


_STREAMING_LOCK = threading.Lock()
_STREAMING: dict[str, _StreamingUploadSlot] = {}


def upload_pause_from_settings(settings: Any) -> timedelta:
    """Пауза между заливами Instagram из настроек (минуты или legacy часы)."""
    try:
        if settings is not None and settings.contains("upload_pause_minutes"):
            mins = int(settings.value("upload_pause_minutes", 180) or 0)
            return resolve_upload_pause(timedelta(minutes=max(0, mins)))
    except Exception:
        pass
    try:
        hours = int(
            (settings.value("upload_pause_hours", 3) if settings is not None else 3)
            or 0
        )
        return resolve_upload_pause(timedelta(hours=max(0, hours)))
    except Exception:
        return resolve_upload_pause(None)


def enqueue_streaming_upload(
    job_id: str,
    *,
    video_paths: list[str],
    title: str | None = None,
    description: str | None = None,
) -> int:
    """Добавить видео в уже запущенный upload job (await_more_videos)."""
    jid = (job_id or "").strip()
    paths = [p for p in (video_paths or []) if (p or "").strip()]
    if not jid or not paths:
        return 0
    with _STREAMING_LOCK:
        slot = _STREAMING.get(jid)
    if slot is None:
        raise RuntimeError(f"Upload job {jid!r} is not accepting more videos.")
    if slot.producer_done.is_set():
        raise RuntimeError(f"Upload job {jid!r} producer is already done.")
    fresh: list[str] = []
    with _STREAMING_LOCK:
        for p in paths:
            if p in slot.enqueued_paths:
                continue
            slot.enqueued_paths.add(p)
            fresh.append(p)
    if not fresh:
        return 0
    slot.mgr.enqueue_videos(
        video_paths=fresh,
        title=(title if title is not None else slot.title) or "",
        description=(description if description is not None else slot.description)
        or "",
    )
    return len(fresh)


def mark_streaming_producer_done(job_id: str) -> bool:
    """Сигнал: обработка больше не добавит видео."""
    jid = (job_id or "").strip()
    if not jid:
        return False
    with _STREAMING_LOCK:
        slot = _STREAMING.get(jid)
    if slot is None:
        return False
    slot.producer_done.set()
    return True


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
    schedule_warmup_hashtag: str = "",
    delete_after_upload: bool = False,
    search_oldest_channel: bool = False,
    upload_store: Any | None = None,
    stats_server_username: str = "",
    settings: Any | None = None,
    await_more_videos: bool = False,
    planned_videos: int = 0,
    job_id: str = "",
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
    streaming = bool(await_more_videos)
    if not paths and not streaming:
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
    warmup_htag = (schedule_warmup_hashtag or "").strip()
    guser = (stats_server_username or "").strip()
    session_plat = PLATFORM_YT_INST if is_yt_inst else (
        PLATFORM_INSTAGRAM if is_instagram else PLATFORM_YOUTUBE
    )

    pause_td = upload_pause_from_settings(settings)
    ig_keep_browser_open = (is_instagram or is_yt_inst) and (
        pause_td.total_seconds() <= 0
    )
    max_browsers = clamp_max_concurrent_browsers(max_concurrent)
    ig_tabs_n = (
        instagram_tabs_per_profile_from_settings(settings)
        if (is_instagram or is_yt_inst)
        else 1
    )
    ig_tabs_per_profile: dict[str, int] | None = None
    if (
        is_instagram
        and ig_keep_browser_open
        and ig_tabs_n > 1
        and len(profile_ids) <= max_browsers
    ):
        ig_tabs_per_profile = compute_instagram_tabs_per_profile(
            profile_ids,
            ig_tabs_n,
            max_concurrent_browsers=max_browsers,
        )
        if max(ig_tabs_per_profile.values(), default=1) <= 1:
            ig_tabs_per_profile = None
        else:
            tabs_fmt = ", ".join(
                f"{pid}×{n}" for pid, n in ig_tabs_per_profile.items()
            )
            sink.on_log(
                "Instagram Reels: multi-tab — пауза 0, "
                f"вкладок на профиль={ig_tabs_n}, "
                f"профилей ≤ лимита окон ({max_browsers}). "
                f"Вкладки: {tabs_fmt}."
            )
    elif is_instagram or is_yt_inst:
        sink.on_log(
            f"[upload] Instagram keep_browser_open={ig_keep_browser_open} "
            f"(pause={pause_td}, tabs={ig_tabs_n})"
        )

    planned = max(int(planned_videos or 0), len(paths), 1)
    upload_session = None
    if upload_store is not None:
        try:
            upload_session = upload_store.start_session(
                planned_videos=planned, platform=session_plat
            )
            sink.on_log(
                f"[upload] сессия залитых #{upload_session.id} "
                f"(planned={planned}, platform={session_plat})"
            )
        except Exception as e:
            sink.on_log(f"[upload] не удалось создать сессию залитых: {e!r}")
            upload_session = None

    success_lock = threading.Lock()
    yt_inst_pending_delete: set[str] = set()
    record_lock = threading.Lock()
    mgr_holder: dict[str, Any] = {"mgr": None}

    def _delete_output_now(video_path: str) -> None:
        if not delete_after_upload:
            return
        try:
            path = Path(str(video_path or "").strip())
            if path.is_file():
                path.unlink()
                sink.on_log(f"[upload] Удалён после залива: {path.name}")
        except OSError as e:
            sink.on_log(
                f"[upload] Не удалось удалить после залива: {video_path} ({e!r})"
            )

    def _maybe_delete_after_success(
        video_path: str, *, record_platform: str | None = None
    ) -> None:
        if not delete_after_upload:
            return
        path = str(video_path or "").strip()
        if not path:
            return
        plat = (record_platform or "").strip().lower()
        if is_yt_inst and plat == PLATFORM_YOUTUBE:
            with success_lock:
                yt_inst_pending_delete.add(path)
            return
        with success_lock:
            yt_inst_pending_delete.discard(path)
        _delete_output_now(path)

    def _delete_yt_inst_pending(video_paths: list[str]) -> None:
        if not delete_after_upload:
            return
        for video_path in video_paths:
            path = str(video_path or "").strip()
            if not path:
                continue
            with success_lock:
                pending = path in yt_inst_pending_delete
                if pending:
                    yt_inst_pending_delete.discard(path)
            if pending:
                _delete_output_now(path)

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

        _maybe_delete_after_success(video_path, record_platform=rec_plat)

    def _close_kept_upload_browser(pid: str) -> None:
        pid = (pid or "").strip()
        if not pid:
            return
        try:
            from zaliver.antydetect.antic_open import close_instagram_keep_open_hub

            close_instagram_keep_open_hub(pid)
        except Exception:
            pass
        kind_l = (kind or "local").strip().lower()
        own = kind_l in {
            "own",
            "local",
            "remote",
            "own_antidetect",
            "local_antidetect",
            "dolphin",
        }
        if own or kind_l == "dolphin":
            try:
                from zaliver.antydetect.local_active_sessions import (
                    stop_registered_local_session_sync,
                )

                for line in stop_registered_local_session_sync(pid):
                    sink.on_log(line)
            except Exception as e:
                sink.on_log(
                    f"[upload] [STOP] local keep-open close profile={pid!r} err={e!r}"
                )

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
        mgr_now = mgr_holder.get("mgr")

        if is_yt_inst:
            keep_open = bool(ig_keep_browser_open) and (
                mgr_now.should_keep_browser_open(profile_id)
                if mgr_now is not None
                else True
            )

            def _on_yt(one_res: dict) -> None:
                batch = (
                    one_res.get("batch_results") if isinstance(one_res, dict) else None
                )
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
                ig_batch = []
                if isinstance(one_res, dict):
                    raw_ig_batch = one_res.get("batch_results")
                    if isinstance(raw_ig_batch, list):
                        ig_batch = raw_ig_batch
                if ig_batch and sched_batch:
                    for item, item_res in zip(sched_batch, ig_batch):
                        confirmed = _confirm_instagram_result(upload_store, item_res)
                        _record_one(
                            profile_id=profile_id,
                            video_path=item.video_path,
                            title=item.title,
                            description=item.description,
                            one_res=confirmed,
                            record_platform=PLATFORM_INSTAGRAM,
                        )
                else:
                    confirmed = _confirm_instagram_result(upload_store, one_res)
                    _record_one(
                        profile_id=profile_id,
                        video_path=task.video_path,
                        title=task_title,
                        description=task_desc,
                        one_res=confirmed,
                        record_platform=PLATFORM_INSTAGRAM,
                    )

            def _on_ig_error(err: BaseException) -> None:
                sink.on_log(
                    f"[upload] Yt+Inst: Instagram ошибка (pipeline) — "
                    f"{type(err).__name__}: {err}"
                )
                mgr_now = mgr_holder.get("mgr")
                if mgr_now is not None:
                    try:
                        mgr_now.exclude_profile_this_session(
                            profile_id,
                            reason=f"instagram_error:{type(err).__name__}",
                        )
                    except Exception:
                        pass
                paths_to_drop = [str(task.video_path or "").strip()]
                if sched_batch:
                    for item in sched_batch:
                        paths_to_drop.append(
                            str(getattr(item, "video_path", "") or "").strip()
                        )
                _delete_yt_inst_pending(paths_to_drop)

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
                warmup_hashtag=warmup_htag or None,
                search_oldest_channel=bool(search_oldest_channel),
                stats_server_username=guser or None,
                keep_browser_open=keep_open,
                on_youtube_success=_on_yt,
                on_instagram_success=_on_ig,
                on_instagram_error=_on_ig_error,
            )
            if own:
                from zaliver.antydetect.local_antidetect_api import local_api_token_scope

                with local_api_token_scope(token):
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
            multi_tab = bool(ig_tabs_per_profile)
            if multi_tab:
                keep_open = True
                dedicated_tab = int(tab_index) > 0
                top_reels_scan = 5
                tabs_n = int(
                    mgr_now.tabs_for_profile(profile_id)
                    if mgr_now is not None
                    else ig_tabs_n
                )
            else:
                keep_open = bool(ig_keep_browser_open) and (
                    mgr_now.should_keep_browser_open(profile_id)
                    if mgr_now is not None
                    else True
                )
                dedicated_tab = False
                top_reels_scan = 1
                tabs_n = 1
            kw = dict(
                video_path=task.video_path,
                title=task_title,
                description=task_desc,
                headless=headless,
                keep_browser_open=keep_open,
                dedicated_tab=dedicated_tab,
                top_reels_scan=top_reels_scan,
                tab_index=int(tab_index),
                tabs_per_profile=max(1, tabs_n),
            )
            if own:
                from zaliver.antydetect.local_antidetect_api import local_api_token_scope

                with local_api_token_scope(token):
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
            upload_latest_zaliver_video=True,
            publish_before_checks=pub_before,
            keep_studio_title=keep_title,
            schedule_publish_at=sched_at,
            scheduled_batch=sched_batch,
            warmup_during_schedule=warmup_on,
            warmup_shorts_recommendations=warmup_reco,
            warmup_search_query=warmup_q or None,
            warmup_hashtag=warmup_htag or None,
            search_oldest_channel=bool(search_oldest_channel),
            stats_server_username=guser or None,
        )
        if own:
            from zaliver.antydetect.local_antidetect_api import local_api_token_scope

            with local_api_token_scope(token):
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

    # Короткая пауза между попытками одного профиля (как на десктопе).
    effective_cooldown = float(cooldown_s)
    if effective_cooldown <= 0:
        effective_cooldown = 10.0

    mgr = MultiProfileUploader(
        profile_ids=list(profile_ids),
        cooldown_s=effective_cooldown,
        max_concurrent_uploads=int(max_browsers),
        profile_upload_pause_remaining_s=(
            (
                lambda pid: upload_store.profile_upload_pause_remaining_seconds(
                    pid,
                    platform=session_plat,
                    pause=pause_td,
                )
            )
            if upload_store is not None
            else None
        ),
        recent_batch_wait_s=float(pause_td.total_seconds()),
        keep_browser_open=ig_keep_browser_open,
        close_kept_browser=(
            _close_kept_upload_browser if ig_keep_browser_open else None
        ),
        log_sink=sink.on_log,
        upload_one=upload_one,
        schedule_batch_size=schedule_batch,
        schedule_times=parsed_times if schedule_batch else None,
        await_more_videos=streaming,
        tabs_per_profile=ig_tabs_per_profile,
    )
    mgr_holder["mgr"] = mgr

    jid = (job_id or "").strip()
    slot: _StreamingUploadSlot | None = None
    if streaming and jid:
        slot = _StreamingUploadSlot(
            mgr=mgr,
            title=title or "",
            description=description or "",
            enqueued_paths=set(paths),
        )
        with _STREAMING_LOCK:
            _STREAMING[jid] = slot

    def _cancel() -> None:
        mgr.stop(reason="api_cancel")
        if slot is not None:
            slot.producer_done.set()

    register_cancel(_cancel)

    sink.on_progress(0, max(planned, len(paths), 1), "upload starting")
    try:
        if paths:
            mgr.enqueue_videos(
                video_paths=paths,
                title=title,
                description=description,
            )
        mgr.start()
        if streaming:
            if slot is not None:
                sink.on_log(
                    "[upload] залив по мере готовности: ждём новые видео "
                    f"(job={jid})"
                )
                while not slot.producer_done.wait(timeout=0.5):
                    if getattr(mgr, "_stop", threading.Event()).is_set():
                        break
            mgr.mark_producer_done()
        else:
            mgr.mark_producer_done()
        mgr.join()
    finally:
        if jid:
            with _STREAMING_LOCK:
                _STREAMING.pop(jid, None)
        if ig_keep_browser_open:
            for pid in profile_ids:
                try:
                    _close_kept_upload_browser(pid)
                except Exception:
                    pass
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
    ok_n = int(mgr.done_ok) if hasattr(mgr, "done_ok") else 0
    total_done = ok_n + failed

    if delete_after_upload:
        with success_lock:
            leftover = list(yt_inst_pending_delete)
            yt_inst_pending_delete.clear()
        for video_path in leftover:
            _delete_output_now(video_path)

    for p in paths:
        sink.on_output_saved(p, False)
    sink.on_progress(total_done, max(total_done, 1), "upload done")
    if failed and ok_n <= 0:
        sink.on_finished(False, f"Upload failed ({failed} failed).")
    else:
        sink.on_finished(
            True,
            f"Upload finished: ok={ok_n}, failed={failed}, total={total_done}.",
        )
