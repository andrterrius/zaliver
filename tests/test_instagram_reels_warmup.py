from unittest.mock import MagicMock, patch

from zaliver.instagram_upload import reels_warmup as warmup


def test_advance_requires_confirmed_change_and_uses_wheel_fallback():
    page = MagicMock()
    page.evaluate.return_value = {"x": 200, "y": 300, "delta": 600}
    with patch.object(warmup, "_current_post_key", return_value="first"), patch.object(
        warmup, "_wait_for_post_change", side_effect=[False, True]
    ):
        assert warmup._advance_to_next_reel(page, prev_src="first")
    page.keyboard.press.assert_called_once_with("ArrowDown")
    page.mouse.wheel.assert_called_once_with(0, 600)


def test_advance_stops_when_post_does_not_change():
    page = MagicMock()
    page.evaluate.return_value = {"x": 200, "y": 300, "delta": 600}
    with patch.object(warmup, "_current_post_key", return_value="first"), patch.object(
        warmup, "_wait_for_post_change", return_value=False
    ):
        assert not warmup._advance_to_next_reel(page, prev_src="first")


def test_actions_do_not_fall_back_to_first_page_button():
    page = MagicMock()
    with patch.object(warmup, "_active_reel_root_handle", return_value=None):
        assert not warmup._try_like_current_reel(page)
        assert not warmup._try_follow_current_reel(page)
    page.locator.assert_not_called()
    page.get_by_role.assert_not_called()
