from unittest.mock import MagicMock, patch

from zaliver.youtube_upload import studio


def test_search_warmup_opens_encoded_search_then_shorts_filter():
    page = MagicMock()
    events = []
    page.goto.side_effect = lambda url, **kwargs: events.append(("goto", url))
    with patch.object(studio, "_studio_goto_youtube_home"), patch.object(
        studio, "_studio_click_search_shorts_chip",
        side_effect=lambda page: events.append(("filter",)) or True,
    ), patch.object(
        studio, "_studio_open_first_search_short",
        side_effect=lambda page: events.append(("open",)) or True,
    ):
        assert studio._studio_goto_shorts_from_search(page, " luxury life & кот ")
    assert events == [
        ("goto", "https://www.youtube.com/results?search_query=luxury+life+%26+%D0%BA%D0%BE%D1%82"),
        ("filter",),
        ("open",),
    ]


def test_search_warmup_does_not_open_unfiltered_result():
    page = MagicMock()
    with patch.object(studio, "_studio_goto_youtube_home"), patch.object(
        studio, "_studio_click_search_shorts_chip", return_value=False
    ), patch.object(studio, "_studio_open_first_search_short") as open_short:
        assert not studio._studio_goto_shorts_from_search(page, "luxury life")
        open_short.assert_not_called()


def test_hashtag_warmup_opens_first_short_from_grid():
    page = MagicMock()
    page.url = "https://www.youtube.com/"
    with patch.object(studio, "_studio_wait_for_google_session"), patch.object(
        studio, "_studio_open_first_search_short", return_value=True
    ) as open_short:
        assert studio._studio_goto_shorts_from_hashtag(page, "#luxury")
    page.goto.assert_called_once_with(
        "https://www.youtube.com/hashtag/luxury/shorts",
        wait_until="domcontentloaded", timeout=60_000,
    )
    open_short.assert_called_once_with(page)


def test_shorts_like_does_not_remove_existing_like():
    page = MagicMock()
    loc = page.locator.return_value.or_.return_value.or_.return_value
    loc.count.return_value = 1
    btn = loc.nth.return_value
    btn.is_visible.return_value = True
    btn.get_attribute.side_effect = lambda name: {
        "aria-label": "like this video along with 66 other people",
        "aria-pressed": "true",
    }.get(name)
    assert not studio._studio_try_like_current_short(page)
    btn.click.assert_not_called()


def test_shorts_like_clicks_unpressed_button():
    page = MagicMock()
    loc = page.locator.return_value.or_.return_value.or_.return_value
    loc.count.return_value = 1
    btn = loc.nth.return_value
    btn.is_visible.return_value = True
    btn.get_attribute.side_effect = lambda name: {
        "aria-label": "like this video along with 66 other people",
        "aria-pressed": "false",
    }.get(name)
    assert studio._studio_try_like_current_short(page)
    btn.click.assert_called_once()


def test_shorts_advance_does_not_report_same_video_as_new():
    page = MagicMock()
    with patch.object(studio, "_studio_scroll_shorts_to_next", return_value=""), patch.object(
        studio, "_studio_read_active_short_video_id", return_value="same_id"
    ):
        assert studio._studio_advance_shorts_feed(page, prev_video_id="same_id") == ""


def test_shorts_advance_accepts_new_video_after_fallback():
    page = MagicMock()
    with patch.object(studio, "_studio_scroll_shorts_to_next", return_value=""), patch.object(
        studio, "_studio_read_active_short_video_id", return_value="next_id"
    ):
        assert studio._studio_advance_shorts_feed(page, prev_video_id="same_id") == "next_id"


def test_shorts_scroll_waits_one_second_before_checking_video():
    page = MagicMock()
    with patch.object(studio, "_studio_focus_shorts_player"), patch.object(
        studio, "_studio_read_active_short_video_id", return_value="next_id"
    ) as read_id:
        assert studio._studio_scroll_shorts_to_next(page, prev_video_id="old_id") == "next_id"
    page.wait_for_timeout.assert_called_once_with(1_000)
    read_id.assert_called_once()


def test_shorts_ad_waits_before_advancing():
    page = MagicMock()
    events = []
    with patch.object(studio, "_studio_wait_shorts_feed_ready"), patch.object(
        studio, "_studio_dismiss_shorts_player_overlays"
    ), patch.object(studio, "_studio_read_active_short_video_id", return_value="old_id"), patch.object(
        studio, "_studio_is_current_short_an_ad", side_effect=[True, False]
    ), patch.object(
        studio, "_studio_wait_ms_with_stop",
        side_effect=lambda page, ms, **kwargs: events.append(("wait", ms)) or False,
    ), patch.object(
        studio, "_studio_advance_shorts_feed",
        side_effect=lambda page, **kwargs: events.append(("advance",)) or "next_id",
    ), patch.object(studio, "_studio_ensure_shorts_playing"):
        studio._studio_browse_youtube_shorts(page, count=1, min_watch_s=1, max_watch_s=1)
    assert events[:2] == [("wait", 1_000), ("advance",)]


def test_comment_button_supports_number_in_accessible_label():
    assert studio._SHORTS_COMMENTS_BTN_RE.search("View 1 comment")
    assert studio._SHORTS_COMMENTS_BTN_RE.search("View 123 comments")
