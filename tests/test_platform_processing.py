import os
from pathlib import Path
from threading import Barrier, Event, Lock
from unittest.mock import Mock, patch
import subprocess

import pytest

from zaliver.antydetect.combined_warmup import warmup_combined_profile
from zaliver.core.sinks import JobProgressSink
from zaliver.processing.ffmpeg_merge import resolve_ffmpeg_executable
from zaliver.processing.thread_worker import ProcessingService


@pytest.mark.parametrize("streaming", [False, True])
def test_real_processing_routes_different_sources_with_identical_names(
    tmp_path, monkeypatch, streaming
):
    exe = resolve_ffmpeg_executable()
    if not exe:
        pytest.skip("ffmpeg unavailable")
    monkeypatch.setenv("ZALIVER_FORCE_THREAD_POOL", "1")
    sources = {}
    for platform, color in [
        ("youtube", "red"),
        ("instagram", "green"),
        ("tiktok", "blue"),
    ]:
        folder = tmp_path / "sources" / platform
        folder.mkdir(parents=True)
        source = folder / "video.mp4"
        subprocess.run(
            [
                exe,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=64x64:d=0.3:r=10",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(source),
            ],
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        sources[platform] = [str(source)]
    saved, finished, bundles = [], [], []
    manager = None

    def saved_output(path, eligible):
        saved.append((path, eligible))
        if manager is not None and eligible:
            manager.enqueue_videos(video_paths=[path], title="title", description="")

    service = ProcessingService(
        JobProgressSink(
            on_output_saved=saved_output,
            on_finished=lambda ok, message: finished.append((ok, message)),
        )
    )
    if streaming:
        from zaliver.youtube_upload.multi_uploader import MultiProfileUploader
        from threading import Thread

        def upload(pid, task):
            bundles.append(set(task.platform_items))
            task.completed_platforms.update(task.platform_items)
            for item in task.platform_items.values():
                service.release_ready_buffer_path(item.video_path)

        manager = MultiProfileUploader(
            profile_ids=["profile"],
            upload_one=upload,
            log_sink=lambda _: None,
            cooldown_s=0,
            recent_batch_wait_s=0,
            await_more_videos=True,
            schedule_group_for_video=lambda path: Path(path).parent.name,
            platform_expected_videos={p: 2 for p in sources},
        )
        manager.start()
    options = dict(
        input_files=[path for paths in sources.values() for path in paths],
        platform_input_files=sources,
        output_dir=str(tmp_path / "out"),
        num_workers=2,
        copies_per_file=2 if streaming else 1,
        upload_ready_buffer_limit=3 if streaming else 0,
        randomize_uniquify=False,
        one_copy_no_effects=True,
        settings={},
        use_gpu=False,
        background_music_enabled=False,
        text_overlay={"enabled": False},
    )
    if streaming:
        worker = Thread(target=service.run, args=(options,), daemon=True)
        worker.start()
        worker.join(timeout=30)
        if worker.is_alive():
            service.cancel()
            manager.stop()
            worker.join(timeout=5)
            pytest.fail("streaming source bundle stalled the bounded buffer")
        manager.mark_producer_done()
        manager.join(timeout_s=5)
        assert manager.is_finished()
        assert manager.done_ok == 6
        assert bundles == [set(sources), set(sources)]
    else:
        service.run(options)
    assert finished[-1][0], finished
    assert len(saved) == (6 if streaming else 3)
    assert {Path(path).parent.name for path, _ in saved} == set(sources)
    for path, eligible in saved:
        assert eligible and Path(path).is_file()
        raw = subprocess.check_output(
            [
                exe,
                "-v",
                "error",
                "-i",
                path,
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        means = [sum(raw[channel::3]) / (len(raw) / 3) for channel in range(3)]
        dominant = max(range(3), key=means.__getitem__)
        assert (
            dominant
            == {"youtube": 0, "instagram": 1, "tiktok": 2}[Path(path).parent.name]
        )


@pytest.mark.parametrize("hashtag, expected_query", [("#travel", "travel"), ("", "")])
def test_combined_warmup_owns_one_session_and_runs_three_tabs_concurrently(hashtag, expected_query):
    from zaliver.antydetect import antic_open
    from zaliver.youtube_upload import studio
    from zaliver.instagram_upload import reels_warmup as instagram
    from zaliver.tiktok_upload import reels_warmup as tiktok

    class Page:
        def __init__(self, target):
            self.target = target
            self.closed = False

        def goto(self, *args, **kwargs):
            pass

        def close(self):
            self.closed = True

    class Context:
        def __init__(self):
            self.pages = [Page("0")]

        def new_page(self):
            page = Page(str(len(self.pages)))
            self.pages.append(page)
            return page

        def new_cdp_session(self, page):
            session = Mock()
            session.send.return_value = {"targetInfo": {"targetId": page.target}}
            return session

    context = Context()
    api = Mock()
    api.start_profile.return_value.ws_url.return_value = "ws://test"
    api.start_profile.return_value.http_url.return_value = "http://test"
    barrier = Barrier(3)
    completed, lock = [], Lock()
    received = {}
    extra_watched = Event()
    extra_platforms = []

    def runner(page, **kwargs):
        barrier.wait(timeout=3)
        if page.target == "3":
            assert extra_watched.wait(timeout=3)
        api.stop_profile.assert_not_called()
        assert not context.pages[0].closed
        with lock:
            completed.append(page.target)
            received[page.target] = kwargs

    def watch_extra(page, platform, options):
        assert not page.closed
        if not extra_watched.is_set():
            assert not context.pages[3].closed
            extra_platforms.append(platform)
            extra_watched.set()
        Event().wait(0.01)

    playwright = Mock()
    playwright.return_value.__enter__ = Mock(return_value=Mock())
    playwright.return_value.__exit__ = Mock(return_value=False)
    with patch("zaliver.antydetect.combined_warmup._watch_one_more", side_effect=watch_extra), patch.object(
        antic_open, "DolphinAntyLocalAPI", return_value=api
    ), patch.object(antic_open, "sync_playwright", playwright), patch.object(
        antic_open,
        "_playwright_page_from_cdp",
        return_value=(Mock(), context, context.pages[0]),
    ), patch.object(
        studio, "run_youtube_shorts_warmup", side_effect=runner
    ), patch.object(
        instagram, "run_instagram_reels_warmup", side_effect=runner
    ), patch.object(tiktok, "run_tiktok_reels_warmup", side_effect=runner):
        warmup_combined_profile(
            "profile", platforms=["youtube", "instagram", "tiktok"], kind="dolphin",
            youtube_kwargs={"hashtag": hashtag},
        )
    assert sorted(completed) == ["1", "2", "3"]
    assert received["1"]["hashtag"] == hashtag
    for target in ("2", "3"):
        assert received[target]["search_query"] == expected_query
        assert received[target]["reels_recommendations"] is (not bool(expected_query))
    assert extra_platforms
    assert not context.pages[0].closed
    assert all(page.closed for page in context.pages[1:])
    api.start_profile.assert_called_once()
    api.stop_profile.assert_called_once_with("profile")
    assert playwright.call_count == 4  # owner plus one transport per worker


@pytest.mark.parametrize(
    "instagram_value,tiktok_value",
    [
        ("instagram " + "x" * 150, "tiktok title"),
        ("", ""),
        (" \n\t", "tiktok title"),
        ("instagram title", " \t"),
    ],
)
def test_combined_pipeline_receives_independent_untruncated_captions(
    instagram_value, tiktok_value
):
    from contextlib import ExitStack
    from zaliver.antydetect import antic_open
    from zaliver.youtube_upload import studio

    page = Mock()
    page.url = "https://studio.youtube.com/"
    captured = {}

    class Pipeline:
        def __init__(self, platform):
            self.platform = platform

        def enqueue(self, job):
            captured[self.platform] = job.items
            job.result = {"video_id": self.platform}
            job.done.set()

    instagram_title = instagram_value if instagram_value.strip() else "youtube title"
    tiktok_title = tiktok_value if tiktok_value.strip() else "youtube title"
    with ExitStack() as stack:
        for name in (
            "_ensure_studio_tab_for_social",
            "_ensure_one_instagram_tab",
            "_ensure_one_tiktok_tab",
        ):
            stack.enter_context(patch.object(antic_open, name, return_value=page))
        for name in (
            "_yt_inst_ig_pipeline_busy",
            "_yt_inst_tt_pipeline_busy",
            "_page_still_open",
        ):
            stack.enter_context(patch.object(antic_open, name, return_value=False))
        for name in ("_close_disabled_platform_tabs", "_focus_combined_upload_tab"):
            stack.enter_context(patch.object(antic_open, name))
        stack.enter_context(
            patch.object(
                antic_open,
                "_get_yt_inst_ig_pipeline",
                return_value=Pipeline("instagram"),
            )
        )
        stack.enter_context(
            patch.object(
                antic_open, "_get_yt_inst_tt_pipeline", return_value=Pipeline("tiktok")
            )
        )
        result = antic_open._run_youtube_and_instagram_parallel(
            browser=Mock(),
            context=Mock(),
            page=page,
            cdp_endpoints=("test",),
            zaliver_db_path=None,
            video_path="video.mp4",
            title="youtube title",
            description="",
            publish_before_checks=False,
            keep_studio_title=False,
            schedule_publish_at=None,
            scheduled_batch=None,
            stats_server_username=None,
            studio_kw={},
            profile_id="profile",
            include_tiktok=True,
            skip_youtube=True,
            platform_titles={
                "youtube": "youtube title",
                "instagram": instagram_value,
                "tiktok": tiktok_value,
            },
        )
    assert captured == {
        "instagram": [("video.mp4", instagram_title, "")],
        "tiktok": [("video.mp4", tiktok_title, "")],
    }
    assert result["instagram"]["video_id"] == "instagram"
    assert result["tiktok"]["video_id"] == "tiktok"


def test_platform_failure_keeps_other_platforms_of_the_profile_available():
    from zaliver.youtube_upload.multi_uploader import MultiProfileUploader
    from datetime import datetime, timedelta
    from zaliver.youtube_upload.schedule_publish import MSK

    calls = []

    def upload(pid, task, tab):
        calls.append((pid, task.video_path))
        if task.video_path.startswith("youtube"):
            raise RuntimeError("youtube unavailable")

    manager = MultiProfileUploader(
        profile_ids=["profile"],
        upload_one=upload,
        log_sink=lambda line: None,
        cooldown_s=0,
        recent_batch_wait_s=0,
        schedule_times=[
            (datetime.now(MSK) + timedelta(days=2)).replace(
                hour=12, minute=0, second=0, microsecond=0
            )
        ],
        schedule_group_for_video=lambda path: path.split("/")[0],
    )
    manager.enqueue_videos(
        video_paths=["youtube/video", "instagram/video"], title="", description=""
    )
    manager.start()
    manager.join(timeout_s=5)
    assert manager.done_ok == 1 and manager.done_failed == 1
    assert not manager._is_profile_skipped("profile")
    assert manager.is_platform_disabled("profile", "youtube")
    assert calls == [("profile", "youtube/video"), ("profile", "instagram/video")]


def test_failed_combined_video_is_kept_even_when_delete_after_upload_is_enabled():
    from types import SimpleNamespace
    from threading import Lock
    from zaliver.ui.main_window import MainWindow

    state = SimpleNamespace(
        _upload_delete_after_enabled=True,
        _upload_success_lock=Lock(),
        _upload_yt_inst_pending_delete={"video"},
        _upload_yt_inst_pending_remaining={"video": 2},
        _upload_failed_paths=set(),
        _platform="yt_inst_tt",
        _delete_output_video_after_upload=Mock(),
        _release_ready_buffer_slot=Mock(),
    )
    MainWindow._delete_yt_inst_pending_outputs(state, ["video"])
    MainWindow._maybe_delete_output_after_upload_success(
        state, "video", record_platform="instagram", yt_inst_upload=True
    )
    state._delete_output_video_after_upload.assert_not_called()
    assert "video" in state._upload_failed_paths


@pytest.mark.parametrize("target", ["youtube", "instagram", "tiktok", "bundle"])
def test_selected_tabs_and_simultaneous_separate_sources(target):
    from contextlib import ExitStack
    from threading import Barrier, Thread
    from zaliver.youtube_upload.multi_uploader import ScheduledUploadItem
    from datetime import datetime, timedelta
    from zaliver.youtube_upload.schedule_publish import MSK
    from zaliver.antydetect import antic_open
    from zaliver.youtube_upload import studio

    page = Mock()
    page.url = "https://studio.youtube.com/"
    opened, uploaded = {}, []
    barrier = Barrier(3)
    schedule_at = (datetime.now(MSK) + timedelta(days=2)).replace(
        minute=0, second=0, microsecond=0
    )
    items = {
        p: ScheduledUploadItem(
            p + ".mp4",
            p + " title",
            p + " description",
            None if p == "instagram" else schedule_at,
        )
        for p in ("youtube", "instagram", "tiktok")
    }

    class Pipeline:
        def __init__(self, platform):
            self.platform = platform

        def enqueue(self, job):
            def run():
                uploaded.append(self.platform)
                path = self.platform if target == "bundle" else target
                assert job.items[0][0] == path + ".mp4"
                if target == "bundle":
                    assert job.items[0][1:] == (
                        self.platform + " title",
                        self.platform + " description",
                    )
                    if self.platform == "tiktok":
                        assert job.schedule_ats == [schedule_at]
                    barrier.wait(timeout=3)
                job.result = {"video_id": self.platform}
                job.done.set()

            if target == "bundle":
                Thread(target=run, daemon=True).start()
            else:
                run()

    def upload_youtube(**kwargs):
        uploaded.append("youtube")
        assert (
            kwargs["video_path"]
            == ("youtube" if target == "bundle" else target) + ".mp4"
        )
        if target == "bundle":
            assert kwargs["title"] == "youtube title"
            assert kwargs["description"] == "youtube description"
            assert kwargs["schedule_publish_at"] == schedule_at
            barrier.wait(timeout=3)
        return {"video_id": "youtube"}

    with ExitStack() as stack:
        for name in (
            "_pick_non_instagram_page",
            "_ensure_studio_tab_for_social",
            "_ensure_one_instagram_tab",
            "_ensure_one_tiktok_tab",
        ):
            opened[name] = stack.enter_context(
                patch.object(antic_open, name, return_value=page)
            )
        for name in (
            "_yt_inst_ig_pipeline_busy",
            "_yt_inst_tt_pipeline_busy",
            "_page_still_open",
        ):
            stack.enter_context(patch.object(antic_open, name, return_value=False))
        for name in ("_focus_combined_upload_tab", "_bring_studio_tab_to_front"):
            stack.enter_context(patch.object(antic_open, name))
        close_tabs = stack.enter_context(
            patch.object(antic_open, "_close_disabled_platform_tabs")
        )
        stack.enter_context(
            patch.object(
                studio, "_studio_channel_removed_page_visible", return_value=False
            )
        )
        stack.enter_context(
            patch.object(
                antic_open, "_run_profile_studio_upload", side_effect=upload_youtube
            )
        )
        stack.enter_context(
            patch.object(
                antic_open,
                "_get_yt_inst_ig_pipeline",
                return_value=Pipeline("instagram"),
            )
        )
        stack.enter_context(
            patch.object(
                antic_open, "_get_yt_inst_tt_pipeline", return_value=Pipeline("tiktok")
            )
        )
        antic_open._run_youtube_and_instagram_parallel(
            browser=Mock(),
            context=Mock(),
            page=page,
            cdp_endpoints=("test",),
            zaliver_db_path=None,
            video_path=target + ".mp4",
            title="title",
            description="",
            publish_before_checks=False,
            keep_studio_title=False,
            schedule_publish_at=None,
            scheduled_batch=None,
            stats_server_username=None,
            studio_kw={},
            profile_id="profile",
            include_tiktok=True,
            skip_youtube=target not in ("youtube", "bundle"),
            skip_instagram=target not in ("instagram", "bundle"),
            skip_tiktok=target not in ("tiktok", "bundle"),
            open_platforms=["youtube", "instagram", "tiktok"],
            platform_items=items if target == "bundle" else None,
        )
    opened["_ensure_one_instagram_tab"].assert_called_once()
    opened["_ensure_one_tiktok_tab"].assert_called_once()
    assert close_tabs.call_args.kwargs == {
        "do_youtube": True,
        "do_instagram": True,
        "do_tiktok": True,
    }
    assert set(uploaded) == (
        {"youtube", "instagram", "tiktok"} if target == "bundle" else {target}
    )
