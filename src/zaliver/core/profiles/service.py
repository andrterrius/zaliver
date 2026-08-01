"""Headless profile jobs: register / 2FA / channel / warmup / promote / cookie farm."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zaliver.antydetect.browser_concurrency import DEFAULT_MAX_CONCURRENT_BROWSERS
from zaliver.antydetect.local_antidetect_api import (
    DEFAULT_LOCAL_API_BASE_URL,
    LocalAntidetectError,
)
from zaliver.core.profiles.credentials import (
    make_instagram_session_resolver,
    make_login_credentials_resolver,
)
from zaliver.core.profiles.settings import (
    CookieFarmSettings,
    PromoteSettings,
    ReelsWarmupSettings,
    ShortsWarmupSettings,
)
from zaliver.core.profiles.tags import (
    apply_result_tags,
    is_own_antidetect_kind,
    own_antidetect_api_label,
)
from zaliver.core.profiles.types import ProfileJobRequest, ProfileJobResult
from zaliver.core.sinks import JobProgressSink
from zaliver.youtube_upload.multi_availability_checker import (
    MultiProfileAvailabilityChecker,
)


@dataclass
class ProfileJobsService:
    """Run multi-profile browser jobs without Qt."""

    def run(
        self,
        request: ProfileJobRequest,
        sink: JobProgressSink | None = None,
        *,
        register_cancel: Callable[[Callable[[], None]], None] | None = None,
        on_manual_captcha: Callable[[str], None] | None = None,
        on_tags_applied: Callable[[str, list[dict[str, Any]]], None] | None = None,
    ) -> ProfileJobResult:
        sink = sink or JobProgressSink()
        kind = request.kind
        if kind == "availability":
            return self._run_availability(
                request, sink, register_cancel, on_tags_applied
            )
        if kind == "instagram_register":
            return self._run_instagram_register(
                request, sink, register_cancel, on_manual_captcha, on_tags_applied
            )
        if kind == "instagram_2fa":
            return self._run_instagram_2fa(
                request, sink, register_cancel, on_tags_applied
            )
        if kind == "channel_setup":
            return self._run_channel_setup(
                request, sink, register_cancel, on_tags_applied
            )
        if kind == "warmup":
            return self._run_warmup(request, sink, register_cancel, on_tags_applied)
        if kind == "promote":
            return self._run_promote(request, sink, register_cancel, on_tags_applied)
        if kind == "cookie_farm":
            return self._run_cookie_farm(
                request, sink, register_cancel, on_tags_applied
            )
        raise ValueError(f"Unsupported profile job kind: {kind}")

    # ------------------------------------------------------------------ helpers

    def _run_checker(
        self,
        *,
        profile_ids: list[str],
        max_concurrent: int,
        check_one: Callable[[str], None],
        on_profile_done: Callable[[str, bool, str], None] | None,
        sink: JobProgressSink,
        register_cancel: Callable[[Callable[[], None]], None] | None,
        log_prefix: str,
    ) -> ProfileJobResult:
        def _on_progress(done: int, total: int, profile_id: str) -> None:
            sink.on_progress(done, total, profile_id)

        mgr = MultiProfileAvailabilityChecker(
            profile_ids=profile_ids,
            check_one=check_one,
            on_profile_done=on_profile_done,
            on_progress=_on_progress,
            log_sink=sink.on_log,
            max_concurrent=max_concurrent or DEFAULT_MAX_CONCURRENT_BROWSERS,
        )
        if register_cancel is not None:
            register_cancel(mgr.stop)
        try:
            ok_n, fail_n, failed_ids = mgr.run()
            result = ProfileJobResult(
                ok=ok_n, fail=fail_n, failed_ids=list(failed_ids)
            )
            sink.on_finished(
                fail_n == 0,
                f"[{log_prefix}] done ok={ok_n} fail={fail_n}",
            )
            return result
        except Exception as e:
            sink.on_log(f"[{log_prefix}] critical: {e!r}")
            n = len(profile_ids)
            sink.on_finished(False, str(e))
            return ProfileJobResult(ok=0, fail=n, failed_ids=list(profile_ids))

    def _require_own_base(self, kind: str, base_url: str) -> str:
        u = (base_url or "").strip()
        if not u:
            raise LocalAntidetectError(
                f"Укажите базовый URL {own_antidetect_api_label(kind)} API в настройках."
            )
        return u

    # ------------------------------------------------------------------ jobs

    def _run_availability(
        self,
        req: ProfileJobRequest,
        sink: JobProgressSink,
        register_cancel,
        on_tags_applied,
    ) -> ProfileJobResult:
        from zaliver.antydetect.antic_open import (
            check_instagram_availability_in_local_antidetect_profile,
            check_instagram_availability_in_profile,
            check_studio_availability_in_local_antidetect_profile,
            check_studio_availability_in_profile,
            set_log_sink,
        )
        from zaliver.antydetect.profile_tags import (
            INSTAGRAM_AVAILABILITY_ERROR_TAG,
            INSTAGRAM_AVAILABILITY_SUCCESS_TAG,
            STUDIO_AVAILABILITY_ERROR_TAG,
            STUDIO_AVAILABILITY_SUCCESS_TAG,
        )

        set_log_sink(sink.on_log)
        kind_s = (req.antidetect_kind or "").strip()
        is_ig = (req.platform or "").strip().lower() == "instagram"
        login_creds = make_login_credentials_resolver(
            req.profiles_custom_data, platform=req.platform
        )
        ig_sess = make_instagram_session_resolver(req.profiles_custom_data)

        def _check_one(pid: str) -> None:
            if is_ig:
                login, password, twofa = ig_sess(pid)
                kw = dict(
                    headless=req.headless,
                    session_login=login,
                    session_password=password,
                    session_twofa=twofa,
                )
                if is_own_antidetect_kind(kind_s):
                    check_instagram_availability_in_local_antidetect_profile(
                        pid,
                        base_url=self._require_own_base(kind_s, req.base_url),
                        remote_cdp=req.remote_cdp,
                        **kw,
                    )
                else:
                    check_instagram_availability_in_profile(
                        pid, local_token=req.token or None, **kw
                    )
                return
            creds = login_creds(pid)
            yt_oldest = (req.yt_oldest_names.get(pid) or "").strip() or None
            search_oldest = bool(req.search_oldest_channel)
            if is_own_antidetect_kind(kind_s):
                check_studio_availability_in_local_antidetect_profile(
                    pid,
                    base_url=self._require_own_base(kind_s, req.base_url),
                    headless=req.headless,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    search_oldest_channel=search_oldest,
                    remote_cdp=req.remote_cdp,
                )
            else:
                check_studio_availability_in_profile(
                    pid,
                    local_token=req.token or None,
                    headless=req.headless,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    search_oldest_channel=search_oldest,
                )

        ok_tag = (
            INSTAGRAM_AVAILABILITY_SUCCESS_TAG
            if is_ig
            else STUDIO_AVAILABILITY_SUCCESS_TAG
        )
        err_tag = (
            INSTAGRAM_AVAILABILITY_ERROR_TAG
            if is_ig
            else STUDIO_AVAILABILITY_ERROR_TAG
        )

        def _on_done(pid: str, ok: bool, _err: str) -> None:
            apply_result_tags(
                kind=kind_s,
                base_url=req.base_url,
                profile_id=pid,
                updates=[(ok, ok_tag, err_tag)],
                log=sink.on_log,
                log_prefix="availability",
                on_tags_applied=on_tags_applied,
            )

        return self._run_checker(
            profile_ids=req.profile_ids,
            max_concurrent=req.max_concurrent,
            check_one=_check_one,
            on_profile_done=_on_done,
            sink=sink,
            register_cancel=register_cancel,
            log_prefix="availability",
        )

    def _run_instagram_register(
        self,
        req: ProfileJobRequest,
        sink: JobProgressSink,
        register_cancel,
        on_manual_captcha,
        on_tags_applied,
    ) -> ProfileJobResult:
        from zaliver.antydetect.antic_open import (
            register_instagram_account_in_local_antidetect_profile,
            register_instagram_account_in_profile,
            set_log_sink,
        )
        from zaliver.antydetect.profile_tags import (
            IG_REGISTER_ERROR_TAG,
            IG_REGISTER_RESULT_TAGS,
            IG_REGISTER_SMS_ERROR_TAG,
            IG_REGISTER_SUCCESS_TAG,
            apply_ig_register_result_tag,
        )
        from zaliver.instagram_upload.register import InstagramSmsCaptchaError

        set_log_sink(sink.on_log)
        kind_s = (req.antidetect_kind or "").strip()
        base_u = (req.base_url or "").strip() or DEFAULT_LOCAL_API_BASE_URL
        login_creds = make_login_credentials_resolver(
            req.profiles_custom_data, platform="instagram"
        )

        def _check_one(pid: str) -> None:
            creds = login_creds(pid)

            def _captcha() -> None:
                if on_manual_captcha is not None:
                    on_manual_captcha(pid)

            if is_own_antidetect_kind(kind_s):
                register_instagram_account_in_local_antidetect_profile(
                    pid,
                    base_url=self._require_own_base(kind_s, req.base_url),
                    headless=req.headless,
                    login_credentials=creds,
                    remote_cdp=req.remote_cdp,
                    on_manual_captcha=_captcha,
                )
            else:
                register_instagram_account_in_profile(
                    pid,
                    local_token=req.token or None,
                    headless=req.headless,
                    login_credentials=creds,
                    on_manual_captcha=_captcha,
                )

        def _on_done(pid: str, ok: bool, err: str) -> None:
            if not is_own_antidetect_kind(kind_s):
                return
            sms = (not ok) and (
                InstagramSmsCaptchaError.matches(err)
                or IG_REGISTER_SMS_ERROR_TAG in (err or "")
            )
            try:
                from zaliver.antydetect.local_antidetect_api import LocalAntidetectHttpAPI

                api = LocalAntidetectHttpAPI(base_u)
                try:
                    tag = apply_ig_register_result_tag(
                        api, pid, success=ok, sms_captcha=sms
                    )
                    sink.on_log(f"[ig-register] profile={pid} tag_set={tag!r}")
                finally:
                    api.close()
                if on_tags_applied is not None:
                    error_tag = (
                        IG_REGISTER_SMS_ERROR_TAG if sms else IG_REGISTER_ERROR_TAG
                    )
                    on_tags_applied(
                        pid,
                        [
                            {
                                "success": ok,
                                "success_tag": IG_REGISTER_SUCCESS_TAG,
                                "error_tag": error_tag,
                                "strip_tags": list(IG_REGISTER_RESULT_TAGS),
                            }
                        ],
                    )
            except Exception as te:
                sink.on_log(f"[ig-register] profile={pid} tag_set_failed err={te!r}")

        return self._run_checker(
            profile_ids=req.profile_ids,
            max_concurrent=req.max_concurrent,
            check_one=_check_one,
            on_profile_done=_on_done,
            sink=sink,
            register_cancel=register_cancel,
            log_prefix="ig-register",
        )

    def _run_instagram_2fa(
        self,
        req: ProfileJobRequest,
        sink: JobProgressSink,
        register_cancel,
        on_tags_applied,
    ) -> ProfileJobResult:
        from zaliver.antydetect.antic_open import (
            set_log_sink,
            setup_instagram_2fa_in_local_antidetect_profile,
            setup_instagram_2fa_in_profile,
        )
        from zaliver.antydetect.profile_tags import (
            IG_2FA_ERROR_TAG,
            IG_2FA_SUCCESS_TAG,
        )

        set_log_sink(sink.on_log)
        kind_s = (req.antidetect_kind or "").strip()
        login_creds = make_login_credentials_resolver(
            req.profiles_custom_data, platform="instagram"
        )
        ig_sess = make_instagram_session_resolver(req.profiles_custom_data)

        def _check_one(pid: str) -> None:
            creds = login_creds(pid)
            sess_login, sess_pwd, sess_2fa = ig_sess(pid)
            if is_own_antidetect_kind(kind_s):
                setup_instagram_2fa_in_local_antidetect_profile(
                    pid,
                    base_url=self._require_own_base(kind_s, req.base_url),
                    headless=req.headless,
                    remote_cdp=req.remote_cdp,
                    login_credentials=creds,
                    session_login=sess_login,
                    session_password=sess_pwd,
                    session_twofa=sess_2fa,
                    keep_open_on_error=False,
                )
            else:
                setup_instagram_2fa_in_profile(
                    pid,
                    local_token=req.token or None,
                    headless=req.headless,
                    login_credentials=creds,
                    session_login=sess_login,
                    session_password=sess_pwd,
                    session_twofa=sess_2fa,
                    keep_open_on_error=False,
                )

        def _on_done(pid: str, ok: bool, _err: str) -> None:
            apply_result_tags(
                kind=kind_s,
                base_url=req.base_url,
                profile_id=pid,
                updates=[(ok, IG_2FA_SUCCESS_TAG, IG_2FA_ERROR_TAG)],
                log=sink.on_log,
                log_prefix="ig-2fa",
                on_tags_applied=on_tags_applied,
            )

        return self._run_checker(
            profile_ids=req.profile_ids,
            max_concurrent=req.max_concurrent,
            check_one=_check_one,
            on_profile_done=_on_done,
            sink=sink,
            register_cancel=register_cancel,
            log_prefix="ig-2fa",
        )

    def _run_warmup(
        self,
        req: ProfileJobRequest,
        sink: JobProgressSink,
        register_cancel,
        on_tags_applied,
    ) -> ProfileJobResult:
        from zaliver.antydetect.antic_open import set_log_sink
        from zaliver.antydetect.profile_tags import (
            IG_WARMUP_ERROR_TAG,
            IG_WARMUP_SUCCESS_TAG,
            WARMUP_ERROR_TAG,
            WARMUP_SUCCESS_TAG,
        )

        set_log_sink(sink.on_log)
        kind_s = (req.antidetect_kind or "").strip()
        is_ig = (req.platform or "").strip().lower() == "instagram"

        if is_ig:
            settings = req.warmup_reels or ReelsWarmupSettings()
            return self._run_reels_warmup(
                req, sink, register_cancel, on_tags_applied, settings, kind_s
            )

        settings = req.warmup_shorts or ShortsWarmupSettings()
        from zaliver.antydetect.antic_open import (
            warmup_youtube_shorts_in_local_antidetect_profile,
            warmup_youtube_shorts_in_profile,
        )

        login_creds = make_login_credentials_resolver(
            req.profiles_custom_data, platform=req.platform
        )

        def _one(pid: str) -> None:
            creds = login_creds(pid)
            yt_oldest = (req.yt_oldest_names.get(pid) or "").strip() or None
            warmup_kw = {
                "shorts_count": settings.shorts_count,
                "like_probability_pct": settings.like_probability_pct,
                "subscribe_probability_pct": settings.subscribe_probability_pct,
                "shorts_watch_min_s": settings.shorts_watch_min_s,
                "shorts_watch_max_s": settings.shorts_watch_max_s,
                "watch_full_video": settings.watch_full_video,
                "shorts_recommendations": settings.shorts_recommendations,
                "search_query": (
                    settings.shorts_search_query or None
                    if not settings.shorts_recommendations
                    else None
                ),
                "watch_horizontal_videos": settings.watch_horizontal_videos,
                "horizontal_search_query": settings.horizontal_search_query or None,
                "horizontal_videos_count": settings.horizontal_videos_count,
                "search_oldest_channel": bool(req.search_oldest_channel),
            }
            if is_own_antidetect_kind(kind_s):
                warmup_youtube_shorts_in_local_antidetect_profile(
                    pid,
                    base_url=self._require_own_base(kind_s, req.base_url),
                    headless=req.headless,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    remote_cdp=req.remote_cdp,
                    **warmup_kw,
                )
            else:
                warmup_youtube_shorts_in_profile(
                    pid,
                    local_token=req.token or None,
                    headless=req.headless,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    **warmup_kw,
                )

        def _on_done(pid: str, ok: bool, _err: str) -> None:
            apply_result_tags(
                kind=kind_s,
                base_url=req.base_url,
                profile_id=pid,
                updates=[(ok, WARMUP_SUCCESS_TAG, WARMUP_ERROR_TAG)],
                log=sink.on_log,
                log_prefix="warmup",
                on_tags_applied=on_tags_applied,
            )

        return self._run_checker(
            profile_ids=req.profile_ids,
            max_concurrent=req.max_concurrent,
            check_one=_one,
            on_profile_done=_on_done,
            sink=sink,
            register_cancel=register_cancel,
            log_prefix="warmup",
        )

    def _run_reels_warmup(
        self,
        req: ProfileJobRequest,
        sink: JobProgressSink,
        register_cancel,
        on_tags_applied,
        settings: ReelsWarmupSettings,
        kind_s: str,
    ) -> ProfileJobResult:
        from zaliver.antydetect.antic_open import (
            warmup_instagram_reels_in_local_antidetect_profile,
            warmup_instagram_reels_in_profile,
        )
        from zaliver.antydetect.profile_tags import (
            IG_WARMUP_ERROR_TAG,
            IG_WARMUP_SUCCESS_TAG,
        )

        ig_sess = make_instagram_session_resolver(req.profiles_custom_data)

        def _one(pid: str) -> None:
            login, password, twofa = ig_sess(pid)
            warmup_kw = {
                "session_login": login,
                "session_password": password,
                "session_twofa": twofa,
                "reels_count": settings.reels_count,
                "like_probability_pct": settings.like_probability_pct,
                "follow_probability_pct": settings.follow_probability_pct,
                "watch_min_s": float(settings.watch_min_s),
                "watch_max_s": float(settings.watch_max_s),
                "watch_full": settings.watch_full,
                "reels_recommendations": settings.reels_recommendations,
                "search_query": settings.reels_search_query,
            }
            if is_own_antidetect_kind(kind_s):
                warmup_instagram_reels_in_local_antidetect_profile(
                    pid,
                    base_url=self._require_own_base(kind_s, req.base_url),
                    headless=req.headless,
                    remote_cdp=req.remote_cdp,
                    **warmup_kw,
                )
            else:
                warmup_instagram_reels_in_profile(
                    pid,
                    local_token=req.token or None,
                    headless=req.headless,
                    **warmup_kw,
                )

        def _on_done(pid: str, ok: bool, _err: str) -> None:
            apply_result_tags(
                kind=kind_s,
                base_url=req.base_url,
                profile_id=pid,
                updates=[(ok, IG_WARMUP_SUCCESS_TAG, IG_WARMUP_ERROR_TAG)],
                log=sink.on_log,
                log_prefix="ig-warmup",
                on_tags_applied=on_tags_applied,
            )

        return self._run_checker(
            profile_ids=req.profile_ids,
            max_concurrent=req.max_concurrent,
            check_one=_one,
            on_profile_done=_on_done,
            sink=sink,
            register_cancel=register_cancel,
            log_prefix="ig-warmup",
        )

    def _run_promote(
        self,
        req: ProfileJobRequest,
        sink: JobProgressSink,
        register_cancel,
        on_tags_applied,
    ) -> ProfileJobResult:
        from zaliver.antydetect.antic_open import (
            promote_instagram_reels_in_local_antidetect_profile,
            promote_instagram_reels_in_profile,
            promote_youtube_videos_in_local_antidetect_profile,
            promote_youtube_videos_in_profile,
            set_log_sink,
        )
        from zaliver.antydetect.profile_tags import (
            IG_PROMOTE_ERROR_TAG,
            IG_PROMOTE_SUCCESS_TAG,
            PROMOTE_ERROR_TAG,
            PROMOTE_SUCCESS_TAG,
        )
        from zaliver.youtube_upload.studio import PromotionTargetVideo as StudioPromoVideo

        set_log_sink(sink.on_log)
        kind_s = (req.antidetect_kind or "").strip()
        is_ig = (req.platform or "").strip().lower() == "instagram"
        settings = req.promote or PromoteSettings()
        videos_src = list(req.promote_videos or [])
        videos = [
            StudioPromoVideo(
                profile_id=v.profile_id,
                video_id=v.video_id,
                url=v.url,
                title=v.title,
            )
            for v in videos_src
        ]
        promote_kw = {
            "subscribe_to_channels": settings.subscribe_to_channels,
            "shorts_count": settings.shorts_count,
            "like_probability_pct": settings.like_probability_pct,
            "shorts_watch_min_s": settings.shorts_watch_min_s,
            "shorts_watch_max_s": settings.shorts_watch_max_s,
            "watch_full_video": settings.watch_full_video,
            "enable_comments": settings.enable_comments,
            "comments": list(settings.comments),
            "comment_probability_pct": settings.comment_probability_pct,
        }
        if not is_ig:
            promote_kw["subscribe_probability_pct"] = 0.0

        login_creds = make_login_credentials_resolver(
            req.profiles_custom_data, platform=req.platform
        )
        ig_sess = make_instagram_session_resolver(req.profiles_custom_data)

        def _one(pid: str) -> None:
            if is_ig:
                login, password, twofa = ig_sess(pid)
                ig_kw = {
                    **promote_kw,
                    "session_login": login,
                    "session_password": password,
                    "session_twofa": twofa,
                }
                if is_own_antidetect_kind(kind_s):
                    promote_instagram_reels_in_local_antidetect_profile(
                        pid,
                        base_url=self._require_own_base(kind_s, req.base_url),
                        videos=videos,
                        headless=req.headless,
                        remote_cdp=req.remote_cdp,
                        **ig_kw,
                    )
                else:
                    promote_instagram_reels_in_profile(
                        pid,
                        videos=videos,
                        local_token=req.token or None,
                        headless=req.headless,
                        **ig_kw,
                    )
                return

            creds = login_creds(pid)
            yt_oldest = (req.yt_oldest_names.get(pid) or "").strip() or None
            if is_own_antidetect_kind(kind_s):
                promote_youtube_videos_in_local_antidetect_profile(
                    pid,
                    base_url=self._require_own_base(kind_s, req.base_url),
                    videos=videos,
                    headless=req.headless,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    search_oldest_channel=bool(req.search_oldest_channel),
                    remote_cdp=req.remote_cdp,
                    **promote_kw,
                )
            else:
                promote_youtube_videos_in_profile(
                    pid,
                    videos=videos,
                    local_token=req.token or None,
                    headless=req.headless,
                    login_credentials=creds,
                    yt_oldest_name=yt_oldest,
                    search_oldest_channel=bool(req.search_oldest_channel),
                    **promote_kw,
                )

        def _on_done(pid: str, ok: bool, _err: str) -> None:
            if is_ig:
                success_tag, error_tag = IG_PROMOTE_SUCCESS_TAG, IG_PROMOTE_ERROR_TAG
            else:
                success_tag, error_tag = PROMOTE_SUCCESS_TAG, PROMOTE_ERROR_TAG
            apply_result_tags(
                kind=kind_s,
                base_url=req.base_url,
                profile_id=pid,
                updates=[(ok, success_tag, error_tag)],
                log=sink.on_log,
                log_prefix="promote",
                on_tags_applied=on_tags_applied,
            )

        return self._run_checker(
            profile_ids=req.profile_ids,
            max_concurrent=req.max_concurrent,
            check_one=_one,
            on_profile_done=_on_done,
            sink=sink,
            register_cancel=register_cancel,
            log_prefix="promote",
        )

    def _run_cookie_farm(
        self,
        req: ProfileJobRequest,
        sink: JobProgressSink,
        register_cancel,
        on_tags_applied,
    ) -> ProfileJobResult:
        from zaliver.antydetect.antic_open import (
            farm_cookies_in_local_antidetect_profile,
            farm_cookies_in_profile,
            set_log_sink,
        )
        from zaliver.antydetect.cookie_farm import set_log_sink as set_cookie_farm_log_sink
        from zaliver.antydetect.cookie_farm_domains import default_cookie_farm_domains
        from zaliver.antydetect.profile_tags import (
            COOKIE_FARM_ERROR_TAG,
            COOKIE_FARM_SUCCESS_TAG,
        )

        set_log_sink(sink.on_log)
        set_cookie_farm_log_sink(sink.on_log)
        kind_s = (req.antidetect_kind or "").strip()
        settings = req.cookie_farm or CookieFarmSettings()
        domains = list(settings.domains)
        if settings.use_preset_domains and not domains:
            domains = list(default_cookie_farm_domains(preset=settings.preset_kind))
        farm_kw = {
            "domains": domains,
            "sites_count": settings.sites_count,
            "watch_min_s": float(settings.watch_min_s),
            "watch_max_s": float(settings.watch_max_s),
        }

        def _one(pid: str) -> None:
            if is_own_antidetect_kind(kind_s):
                farm_cookies_in_local_antidetect_profile(
                    pid,
                    base_url=self._require_own_base(kind_s, req.base_url),
                    headless=req.headless,
                    remote_cdp=req.remote_cdp,
                    **farm_kw,
                )
            else:
                farm_cookies_in_profile(
                    pid,
                    local_token=req.token or None,
                    headless=req.headless,
                    **farm_kw,
                )

        def _on_done(pid: str, ok: bool, _err: str) -> None:
            apply_result_tags(
                kind=kind_s,
                base_url=req.base_url,
                profile_id=pid,
                updates=[(ok, COOKIE_FARM_SUCCESS_TAG, COOKIE_FARM_ERROR_TAG)],
                log=sink.on_log,
                log_prefix="cookie_farm",
                on_tags_applied=on_tags_applied,
            )

        return self._run_checker(
            profile_ids=req.profile_ids,
            max_concurrent=req.max_concurrent,
            check_one=_one,
            on_profile_done=_on_done,
            sink=sink,
            register_cancel=register_cancel,
            log_prefix="cookie_farm",
        )

    def _run_channel_setup(
        self,
        req: ProfileJobRequest,
        sink: JobProgressSink,
        register_cancel,
        on_tags_applied,
    ) -> ProfileJobResult:
        """Simplified channel/profile setup (assignments + optional avatar paths)."""
        from zaliver.antydetect.antic_open import (
            set_log_sink,
            setup_channel_in_local_antidetect_profile,
            setup_channel_in_profile,
            setup_instagram_profile_in_local_antidetect_profile,
            setup_instagram_profile_in_profile,
        )
        from zaliver.antydetect.profile_tags import (
            NAME_CHANGE_ERROR_TAG,
            NAME_CHANGE_SUCCESS_TAG,
        )
        from zaliver.title_variables import TitleVariableContext, expand_title_variables

        set_log_sink(sink.on_log)
        kind_s = (req.antidetect_kind or "").strip()
        is_ig = (req.platform or "").strip().lower() == "instagram"
        # Channel setup in desktop always uses a visible browser.
        headless = False
        by_id = {a.profile_id: a for a in req.channel_assignments if a.profile_id}
        profile_index = {pid: i for i, pid in enumerate(req.profile_ids)}
        login_creds = make_login_credentials_resolver(
            req.profiles_custom_data, platform=req.platform
        )
        ig_sess = make_instagram_session_resolver(req.profiles_custom_data)
        desc_lines = list(req.channel_description_lines or [])
        has_text = bool(
            (req.channel_description or "").strip()
            or desc_lines
            or any(
                (a.channel_description or a.channel_name or "").strip()
                for a in req.channel_assignments
            )
        )

        def _name_for(pid: str) -> str:
            a = by_id.get(pid)
            if a and a.profile_name:
                return a.profile_name
            return pid

        def _expand(text: str, pid: str) -> str:
            raw = (text or "").strip()
            if not raw:
                return ""
            ctx = TitleVariableContext(
                profile_name=_name_for(pid),
                video_path="",
                index=profile_index.get(pid, 0) + 1,
            )
            return expand_title_variables(raw, ctx)

        def _description_for(pid: str) -> str:
            a = by_id.get(pid)
            if a and (a.channel_description or "").strip():
                return _expand(a.channel_description, pid)
            if desc_lines:
                return _expand(desc_lines[profile_index.get(pid, 0) % len(desc_lines)], pid)
            return _expand(req.channel_description, pid)

        def _link_for(pid: str) -> tuple[str, str] | None:
            idx = profile_index.get(pid, 0)
            if req.channel_links:
                lt_raw, lu_raw = req.channel_links[idx % len(req.channel_links)]
            else:
                lt_raw, lu_raw = req.link_title, req.link_url
            lt, lu = _expand(lt_raw, pid), _expand(lu_raw, pid)
            if lt and lu:
                return (lt, lu)
            return None

        def _one(pid: str) -> None:
            item = by_id.get(pid)
            avatar_path: Path | None = None
            if item and (item.avatar_path or "").strip():
                p = Path(item.avatar_path)
                if p.is_file():
                    avatar_path = p

            if is_ig:
                profile_description = _description_for(pid) if has_text else ""
                channel_name = (
                    _expand(item.channel_name, pid) if item else ""
                ) or None
                skip_name = bool(item.skip_name_change) if item else False
                ig_username = None if skip_name else channel_name
                ig_login, ig_password, ig_twofa = ig_sess(pid)
                if is_own_antidetect_kind(kind_s):
                    setup_instagram_profile_in_local_antidetect_profile(
                        pid,
                        description=profile_description or None,
                        avatar_path=avatar_path,
                        username=ig_username,
                        change_language=req.change_language,
                        base_url=self._require_own_base(kind_s, req.base_url),
                        headless=headless,
                        remote_cdp=req.remote_cdp,
                        session_login=ig_login,
                        session_password=ig_password,
                        session_twofa=ig_twofa,
                    )
                else:
                    setup_instagram_profile_in_profile(
                        pid,
                        description=profile_description or None,
                        avatar_path=avatar_path,
                        username=ig_username,
                        change_language=req.change_language,
                        local_token=req.token or None,
                        headless=headless,
                        session_login=ig_login,
                        session_password=ig_password,
                        session_twofa=ig_twofa,
                    )
                return

            creds = login_creds(pid)
            channel_name = (_expand(item.channel_name, pid) if item else "") or None
            skip_name = bool(item.skip_name_change) if item else False
            description = _description_for(pid) if has_text else ""
            link = _link_for(pid)
            video_title = (
                _expand(item.video_default_title, pid) if item else ""
            ) or None
            yt_kw: dict[str, Any] = dict(
                channel_name=channel_name,
                skip_name_change=skip_name,
                description=description or None,
                avatar_path=avatar_path,
                change_language=req.change_language,
                headless=headless,
                login_credentials=creds,
                video_default_title=video_title,
            )
            if link:
                yt_kw["link_title"], yt_kw["link_url"] = link
            if is_own_antidetect_kind(kind_s):
                setup_channel_in_local_antidetect_profile(
                    pid,
                    base_url=self._require_own_base(kind_s, req.base_url),
                    remote_cdp=req.remote_cdp,
                    **yt_kw,
                )
            else:
                setup_channel_in_profile(
                    pid, local_token=req.token or None, **yt_kw
                )

        def _on_done(pid: str, ok: bool, _err: str) -> None:
            apply_result_tags(
                kind=kind_s,
                base_url=req.base_url,
                profile_id=pid,
                updates=[(ok, NAME_CHANGE_SUCCESS_TAG, NAME_CHANGE_ERROR_TAG)],
                log=sink.on_log,
                log_prefix="channel_setup",
                on_tags_applied=on_tags_applied,
            )

        return self._run_checker(
            profile_ids=req.profile_ids,
            max_concurrent=req.max_concurrent,
            check_one=_one,
            on_profile_done=_on_done,
            sink=sink,
            register_cancel=register_cancel,
            log_prefix="channel_setup",
        )
