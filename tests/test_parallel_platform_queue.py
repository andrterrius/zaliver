from datetime import datetime, timedelta

from zaliver.youtube_upload.multi_uploader import MultiProfileUploader
from zaliver.youtube_upload.schedule_publish import MSK


def manager(upload, **kwargs):
    return MultiProfileUploader(
        profile_ids=["p1", "p2"],
        upload_one=upload,
        log_sink=lambda _: None,
        cooldown_s=0,
        recent_batch_wait_s=0,
        schedule_group_for_video=lambda path: path.split("/")[0],
        platform_expected_videos={"youtube": 2, "instagram": 1, "tiktok": 2},
        **kwargs,
    )


def finish(mgr):
    mgr.start()
    mgr.join(timeout_s=5)
    assert mgr.is_finished()
    assert not mgr._dispatcher.is_alive()


def test_group_different_sources_and_unequal_lengths():
    calls = []

    def upload(pid, task):
        calls.append({p: item.video_path for p, item in task.platform_items.items()})
        task.completed_platforms.update(task.platform_items)

    mgr = manager(upload)
    mgr.enqueue_videos(
        video_paths=["youtube/1", "youtube/2", "instagram/1", "tiktok/1", "tiktok/2"],
        title="title",
        description="",
    )
    finish(mgr)
    assert calls == [
        dict(youtube="youtube/1", instagram="instagram/1", tiktok="tiktok/1"),
        dict(youtube="youtube/2", tiktok="tiktok/2"),
    ]
    assert (mgr.total, mgr.done_ok, mgr.done_failed) == (5, 5, 0)


def test_streaming_waits_for_all_sources_then_stops_waiting_for_exhausted_platform():
    mgr = manager(lambda *_: None, await_more_videos=True)
    for path in ["youtube/1", "tiktok/1"]:
        mgr.enqueue_videos(video_paths=[path], title="", description="")
        assert mgr._global_q.empty()
    mgr.enqueue_videos(video_paths=["instagram/1"], title="", description="")
    first = mgr._global_q.get_nowait()
    assert set(first.platform_items) == {"youtube", "instagram", "tiktok"}
    mgr.enqueue_videos(video_paths=["youtube/2", "tiktok/2"], title="", description="")
    assert set(mgr._global_q.get_nowait().platform_items) == {"youtube", "tiktok"}


def test_processing_skip_unblocks_streaming_round():
    mgr = manager(lambda *_: None, await_more_videos=True)
    mgr.enqueue_videos(video_paths=["youtube/1", "tiktok/1"], title="", description="")
    mgr.discard_expected_video("instagram/skipped")
    assert set(mgr._global_q.get_nowait().platform_items) == {"youtube", "tiktok"}


def test_producer_finish_flushes_missing_outputs():
    mgr = manager(lambda *_: None, await_more_videos=True)
    mgr.enqueue_videos(video_paths=["youtube/1"], title="", description="")
    assert mgr._global_q.empty()
    mgr.mark_producer_done()
    assert set(mgr._global_q.get_nowait().platform_items) == {"youtube"}


def test_partial_success_retries_only_failed_platform():
    calls = []

    def upload(pid, task):
        calls.append((pid, set(task.platform_items)))
        task.completed_platforms.update(
            p for p in task.platform_items if pid == "p2" or p != "instagram"
        )
        if pid == "p1":
            task.platform_errors["instagram"] = "failed"

    mgr = manager(upload)
    mgr.enqueue_videos(
        video_paths=["youtube/1", "instagram/1", "tiktok/1"], title="", description=""
    )
    finish(mgr)
    assert calls == [("p1", {"youtube", "instagram", "tiktok"}), ("p2", {"instagram"})]
    assert (mgr.done_ok, mgr.done_failed) == (3, 0)


def test_bundle_schedule_uses_independent_slots_and_never_immediate_overflow():
    calls = []
    at = (datetime.now(MSK) + timedelta(days=2)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )

    def upload(pid, task):
        calls.extend(
            (pid, p, i.schedule_publish_at) for p, i in task.platform_items.items()
        )
        task.completed_platforms.update(task.platform_items)

    mgr = manager(upload, schedule_times=[at])
    mgr.enqueue_videos(
        video_paths=[
            "youtube/1",
            "youtube/2",
            "youtube/3",
            "instagram/1",
            "tiktok/1",
            "tiktok/2",
            "tiktok/3",
        ],
        title="",
        description="",
    )
    finish(mgr)
    assert mgr.done_ok == 5 and mgr.done_failed == 2
    assert all(t == (None if p == "instagram" else at) for _, p, t in calls)
    assert len(set((pid, p) for pid, p, _ in calls)) == len(calls)


def test_unknown_confirmation_is_not_retried_after_other_platforms_succeed():
    calls = []

    def upload(pid, task):
        calls.append(pid)
        task.completed_platforms.update({"instagram", "tiktok"})
        task.platform_errors["youtube"] = "[UPLOAD_CONFIRMATION_UNKNOWN]"

    mgr = manager(upload)
    mgr.enqueue_videos(
        video_paths=["youtube/1", "instagram/1", "tiktok/1"], title="", description=""
    )
    finish(mgr)
    assert calls == ["p1"]
    assert (mgr.done_ok, mgr.done_failed) == (2, 1)
