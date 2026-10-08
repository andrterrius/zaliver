from datetime import datetime, timedelta
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

from zaliver.core.upload.build import build_upload_queue_request
from zaliver.core.upload.types import AntidetectLaunchConfig
from zaliver.youtube_upload.multi_uploader import MultiProfileUploader
from zaliver.youtube_upload.schedule_publish import (
    MSK,
    validate_schedule_times,
    studio_date_trigger_matches,
)


def times(count=2):
    start = (datetime.now(MSK) + timedelta(days=2)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    return [start + timedelta(hours=i) for i in range(count)]


class QueueTests(TestCase):
    def manager(self, callback, **kwargs):
        return MultiProfileUploader(
            profile_ids=["p1", "p2", "p3"],
            upload_one=callback,
            log_sink=lambda line: None,
            cooldown_s=0,
            recent_batch_wait_s=0,
            **kwargs,
        )

    def run_queue(self, manager, paths):
        manager.enqueue_videos(
            video_paths=paths, title="title", description="description"
        )
        manager.start()
        manager.join(timeout_s=5)
        self.assertFalse(manager._dispatcher.is_alive(), "dispatcher did not finish")
        self.assertFalse(
            any(worker.is_alive() for worker in manager._workers),
            "workers did not finish",
        )

    def test_all_profiles_receive_scheduled_videos_without_immediate_overflow(self):
        calls = []
        manager = self.manager(
            lambda pid, task, tab: calls.append(
                (pid, task.video_path, task.schedule_publish_at)
            ),
            schedule_times=times(),
        )
        self.run_queue(manager, [str(i) for i in range(8)])
        self.assertEqual({pid for pid, _, _ in calls}, {"p1", "p2", "p3"})
        self.assertEqual(len(calls), 6)
        self.assertTrue(all(at is not None for _, _, at in calls))
        self.assertEqual((manager.done_ok, manager.done_failed), (6, 2))
        self.assertEqual(len(set((pid, at) for pid, _, at in calls)), 6)

    def test_internal_typeerror_does_not_invoke_callback_twice(self):
        calls = []

        def upload(pid, task, tab=0):
            calls.append(pid)
            raise TypeError("inside upload")

        manager = self.manager(upload)
        self.run_queue(manager, ["video"])
        self.assertEqual(sorted(calls), ["p1", "p2", "p3"])
        self.assertEqual(manager.done_failed, 1)

    def test_legacy_two_argument_callback_still_works(self):
        calls = []

        def upload(pid, task):
            calls.append(task.video_path)

        manager = self.manager(upload)
        self.run_queue(manager, ["video"])
        self.assertEqual(calls, ["video"])

    def test_unknown_confirmation_never_retries_on_another_profile(self):
        calls = []

        def upload(pid, task, tab):
            calls.append(pid)
            raise RuntimeError("[UPLOAD_CONFIRMATION_UNKNOWN] check Studio")

        manager = self.manager(upload, schedule_times=times())
        self.run_queue(manager, ["video"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(manager.done_failed, 1)

    def test_profile_failure_does_not_stall_schedule_dispatch(self):
        calls = []

        def upload(pid, task, tab):
            if pid == "p1":
                raise RuntimeError("account unavailable")
            calls.append((task.video_path, task.schedule_publish_at))

        manager = self.manager(upload, schedule_times=times(3))
        self.run_queue(manager, ["a", "b", "c"])
        self.assertEqual(sorted(path for path, _ in calls), ["a", "b", "c"])
        self.assertEqual(manager.done_ok, 3)
        self.assertTrue(all(at for _, at in calls))

    def test_platforms_have_independent_slots_and_instagram_is_immediate(self):
        calls = []
        manager = MultiProfileUploader(
            profile_ids=["p1"],
            upload_one=lambda pid, task, tab: calls.append(
                (task.video_path, task.schedule_publish_at)
            ),
            log_sink=lambda line: None,
            cooldown_s=0,
            schedule_times=times(1),
            schedule_group_for_video=lambda path: path.split("/")[0],
        )
        self.run_queue(manager, ["youtube/a", "tiktok/b", "instagram/c"])
        self.assertEqual(
            dict(calls),
            {"youtube/a": times(1)[0], "tiktok/b": times(1)[0], "instagram/c": None},
        )

    def test_scheduling_checks_db_pause_only_before_first_upload(self):
        pause = Mock(side_effect=[0, 3600])
        manager = MultiProfileUploader(
            profile_ids=["p1"],
            upload_one=lambda *args: None,
            log_sink=lambda line: None,
            cooldown_s=3600,
            profile_upload_pause_remaining_s=pause,
            schedule_times=times(),
        )
        self.run_queue(manager, ["a", "b"])
        self.assertEqual(pause.call_count, 1)
        self.assertEqual(manager.done_ok, 2)

    def test_streaming_uses_all_profiles_before_reusing_slots(self):
        calls = []
        ready = Event()

        def upload(pid, task, tab):
            calls.append((pid, task.schedule_publish_at))
            ready.set()

        manager = self.manager(
            upload, schedule_times=times(1), await_more_videos=True, planned_videos=3
        )
        manager.enqueue_videos(video_paths=["a"], title="", description="")
        manager.start()
        for path in ["b", "c"]:
            self.assertTrue(ready.wait(3))
            ready.clear()
            manager.enqueue_videos(video_paths=[path], title="", description="")
        manager.mark_producer_done()
        manager.join(timeout_s=5)
        self.assertEqual({pid for pid, _ in calls}, {"p1", "p2", "p3"})
        self.assertEqual(manager.done_ok, 3)


class ValidationTests(TestCase):
    def test_schedule_rejects_empty_duplicate_and_rounded_times(self):
        self.assertIsNotNone(validate_schedule_times([]))
        self.assertIsNotNone(validate_schedule_times(times(1) * 2))
        self.assertIsNotNone(
            validate_schedule_times([times(1)[0] + timedelta(minutes=1)])
        )
        self.assertIsNone(validate_schedule_times(times(2)))

    def test_builder_cannot_silently_drop_schedule_intent(self):
        with self.assertRaises(ValueError):
            build_upload_queue_request(
                platform="youtube",
                pending={"schedule_publish": True, "schedule_times_iso": []},
                video_paths=["a"],
                antidetect=AntidetectLaunchConfig(),
            )

    def test_russian_numeric_date_readback(self):
        date = times(1)[0]
        self.assertTrue(studio_date_trigger_matches(date.strftime("%d.%m.%Y"), date))
        self.assertFalse(studio_date_trigger_matches("01.01.2000", date))

    def test_schedule_branch_never_selects_public_radio(self):
        from zaliver.youtube_upload import studio

        page = Mock()
        with patch.object(
            studio, "_studio_handle_interrupt_dialogs_if_present"
        ), patch.object(
            studio, "_studio_log_video_link_before_public", return_value="url"
        ), patch.object(studio, "_studio_configure_scheduled_publish") as configure:
            self.assertEqual(
                studio._studio_select_public_visibility(page, schedule_at=times(1)[0]),
                "url",
            )
        configure.assert_called_once()
        page.locator.assert_not_called()

    def test_confirm_timeout_does_not_click_or_notify_twice(self):
        from zaliver.youtube_upload import studio

        page = Mock()
        button = Mock()
        button.count.return_value = 1
        button.first.is_visible.return_value = True
        button.first.is_enabled.return_value = True
        with patch.object(
            studio, "_studio_upload_finalize_button", return_value=button
        ), patch.object(
            studio, "_studio_handle_interrupt_dialogs_if_present"
        ), patch.object(
            studio, "_studio_capture_uploads_review_video_link", return_value="url"
        ), patch.object(
            studio,
            "_studio_wait_for_publish_confirmed",
            side_effect=RuntimeError("timeout"),
        ), patch.object(studio, "_studio_try_notify_stats_server") as notify:
            with self.assertRaises(studio.UploadConfirmationUncertainError):
                studio._studio_click_publish_when_enabled(
                    page, 1, schedule=True, schedule_at=times(1)[0]
                )
        button.first.click.assert_called_once()
        notify.assert_not_called()
