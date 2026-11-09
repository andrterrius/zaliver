"""One browser session, one independent Playwright connection per warmup tab."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from zaliver.log_format import log_profile_context


def _watch_one_more(page, platform, options):
    """Advance and watch one additional video in the existing feed."""
    if platform == "youtube":
        from zaliver.youtube_upload import studio

        if not studio._studio_page_on_youtube_shorts(page):
            studio._studio_open_shorts_for_warmup(
                page, login_credentials=options.get("login_credentials"),
                shorts_recommendations=options.get("shorts_recommendations", True),
                search_query=options.get("search_query"), hashtag=options.get("hashtag"),
            )
        previous = studio._studio_read_active_short_video_id(page)
        if not studio._studio_advance_shorts_feed(page, prev_video_id=previous):
            raise RuntimeError("Не удалось перейти к следующему Short")
        studio._studio_browse_youtube_shorts(
            page, count=1,
            like_probability_pct=options.get("like_probability_pct", 10),
            subscribe_probability_pct=options.get("subscribe_probability_pct", 10),
            min_watch_s=options.get("shorts_watch_min_s", 5),
            max_watch_s=options.get("shorts_watch_max_s", 25),
            watch_full_video=options.get("watch_full_video", False),
        )
        return

    if platform == "instagram":
        from zaliver.instagram_upload import reels_warmup as reels
    else:
        from zaliver.tiktok_upload import reels_warmup as reels
    previous = reels._current_video_src(page)
    if not reels._advance_to_next_reel(page, prev_src=previous):
        raise RuntimeError("Не удалось перейти к следующему ролику")
    reels._watch_current_reel(
        page, watch_min_s=options.get("shorts_watch_min_s", 5),
        watch_max_s=options.get("shorts_watch_max_s", 25),
        watch_full=options.get("watch_full_video", False),
    )
    if reels._roll(options.get("like_probability_pct", 10)):
        reels._try_like_current_reel(page)
    if reels._roll(options.get("subscribe_probability_pct", 10)):
        reels._try_follow_current_reel(page)


def warmup_combined_profile(
    profile_id,
    *,
    platforms,
    kind,
    base_url="",
    token="",
    headless=True,
    remote_cdp=None,
    youtube_kwargs=None,
    instagram_credentials=("", "", ""),
    tiktok_credentials=("", "", ""),
):
    from zaliver.antydetect.antic_open import (
        DolphinAntyLocalAPI,
        sync_playwright,
        _log,
        _playwright_page_from_cdp,
        _playwright_page_from_local_session_cdp,
        _local_studio_workflow_kwargs,
    )
    from zaliver.antydetect.local_antidetect_api import LocalAntidetectHttpAPI
    from zaliver.antydetect.local_active_sessions import (
        register_local_session,
        unregister_local_session,
    )
    from zaliver.youtube_upload.studio import run_youtube_shorts_warmup
    from zaliver.instagram_upload.reels_warmup import run_instagram_reels_warmup
    from zaliver.tiktok_upload.reels_warmup import run_tiktok_reels_warmup

    urls = {
        "youtube": "https://www.youtube.com/",
        "instagram": "https://www.instagram.com/",
        "tiktok": "https://www.tiktok.com/",
    }
    platforms = list(dict.fromkeys(p for p in platforms if p in urls))
    if not platforms:
        raise ValueError("Выберите платформы для прогрева.")
    local = kind != "dolphin"
    api = LocalAntidetectHttpAPI(base_url) if local else DolphinAntyLocalAPI()
    session_id = None
    launched = False
    try:
        if local:
            result = api.launch_profile(
                profile_id, headless=headless, expose_cdp=True, remote_cdp=remote_cdp
            )
            session_id = str(result.get("session_id") or "").strip()
            if not session_id:
                raise RuntimeError("Нет session_id в ответе launch.")
            launched = True
            register_local_session(
                profile_id=profile_id, base_url=base_url, session_id=session_id
            )
            endpoints = (api.wait_for_cdp_ws_url(session_id, timeout_s=120.0),)
        else:
            if token:
                api.login_with_token(token)
            connection = api.start_profile(profile_id, headless=headless)
            launched = True
            endpoints = (connection.ws_url(), connection.http_url())
        with sync_playwright() as owner:
            if local:
                browser, context, initial = _playwright_page_from_local_session_cdp(
                    owner, api, session_id, endpoints[0]
                )
                endpoints = (api.wait_for_cdp_ws_url(session_id, timeout_s=30.0),)
            else:
                browser, context, initial = _playwright_page_from_cdp(owner, endpoints)
            target_ids = {}
            # Keep the profile's original tab open until all workers finish.
            # Some antidetect browsers end the session when that tab closes.
            for platform in platforms:
                page = context.new_page()
                session = context.new_cdp_session(page)
                try:
                    target_ids[platform] = session.send("Target.getTargetInfo")[
                        "targetInfo"
                    ]["targetId"]
                finally:
                    session.detach()
            yt_kwargs = dict(youtube_kwargs or {})
            if local and "youtube" in platforms:
                workflow = _local_studio_workflow_kwargs(
                    api,
                    profile_id,
                    login_credentials=yt_kwargs.pop("login_credentials", None),
                    yt_oldest_name=yt_kwargs.pop("yt_oldest_name", None),
                    search_oldest_channel=yt_kwargs.pop("search_oldest_channel", True),
                )
                yt_kwargs.update(workflow)
            yt_kwargs["profile_id"] = profile_id

            # Release the setup connection before workers attach. Some antidetect
            # CDP proxies stall navigation while another client owns the socket.
            owner.stop()

            completed = set()
            completion_lock = Lock()
            all_finished = Event()

            def mark_finished(platform):
                with completion_lock:
                    completed.add(platform)
                    if len(completed) == len(platforms):
                        all_finished.set()

            def run_platform(platform):
                with log_profile_context(profile_id):
                    with sync_playwright() as worker:
                        _browser, shared_context, _ = _playwright_page_from_cdp(
                            worker, endpoints
                        )
                        page = None
                        for candidate in shared_context.pages:
                            try:
                                session = shared_context.new_cdp_session(candidate)
                                try:
                                    target = session.send("Target.getTargetInfo")[
                                        "targetInfo"
                                    ]["targetId"]
                                finally:
                                    session.detach()
                            except Exception:
                                continue
                            if target == target_ids[platform]:
                                page = candidate
                                break
                        if page is None:
                            raise RuntimeError(
                                f"Не найдена вкладка прогрева {platform}"
                            )
                        try:
                            if platform == "youtube":
                                run_youtube_shorts_warmup(page, **yt_kwargs)
                            else:
                                search_query = (yt_kwargs.get("hashtag") or "").strip().lstrip("#").strip()
                                credentials = (
                                    instagram_credentials
                                    if platform == "instagram"
                                    else tiktok_credentials
                                )
                                kwargs = dict(
                                    session_login=credentials[0],
                                    session_password=credentials[1],
                                    session_twofa=credentials[2],
                                    profile_id=profile_id,
                                    reels_count=yt_kwargs.get("shorts_count", 10),
                                    like_probability_pct=yt_kwargs.get(
                                        "like_probability_pct", 10
                                    ),
                                    follow_probability_pct=yt_kwargs.get(
                                        "subscribe_probability_pct", 10
                                    ),
                                    watch_min_s=yt_kwargs.get("shorts_watch_min_s", 5),
                                    watch_max_s=yt_kwargs.get("shorts_watch_max_s", 25),
                                    watch_full=yt_kwargs.get("watch_full_video", False),
                                    reels_recommendations=not bool(search_query),
                                    search_query=search_query,
                                )
                                runner = (
                                    run_instagram_reels_warmup
                                    if platform == "instagram"
                                    else run_tiktok_reels_warmup
                                )
                                page.goto(
                                    urls[platform],
                                    wait_until="domcontentloaded",
                                    timeout=60000,
                                )
                                runner(page, **kwargs)
                            mark_finished(platform)
                            while not all_finished.is_set():
                                try:
                                    _watch_one_more(page, platform, yt_kwargs)
                                except Exception as error:
                                    # Keep the tab open if the feed cannot advance.
                                    _log(f"{platform}: не удалось продолжить просмотр: {error}")
                                    all_finished.wait()
                                    break
                        finally:
                            page.close()
                        # Worker stop disconnects its transport, not the browser.

            def run(platform):
                try:
                    run_platform(platform)
                finally:
                    mark_finished(platform)

            errors = []
            with ThreadPoolExecutor(max_workers=len(platforms)) as pool:
                futures = {
                    platform: pool.submit(run, platform) for platform in platforms
                }
                for platform, future in futures.items():
                    try:
                        future.result()
                    except Exception as error:
                        errors.append(f"{platform}: {error}")
            if errors:
                raise RuntimeError("; ".join(errors))
    finally:
        try:
            if launched:
                if local:
                    unregister_local_session(profile_id=profile_id)
                    api.stop_session(session_id)
                else:
                    api.stop_profile(profile_id)
        finally:
            api.close()
