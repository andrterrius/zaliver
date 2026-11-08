from unittest.mock import MagicMock, patch
import pytest
from zaliver.tiktok_upload import reels_warmup as warmup


def test_advance_uses_next_button_and_requires_change():
    page = MagicMock()
    page.locator.return_value.first.is_disabled.return_value = False
    with patch.object(warmup, '_current_post_key', return_value='card:0'), patch.object(
        warmup, '_wait_for_post_change', return_value=True
    ):
        assert warmup._advance_to_next_reel(page, prev_src='video')
    page.locator.return_value.first.click.assert_called_once()
    page.keyboard.press.assert_not_called()


def test_feed_reports_incomplete_when_transition_fails():
    page = MagicMock()
    with patch.object(warmup, '_ensure_reels_feed'), patch.object(warmup, '_current_video_src', return_value='v'), patch.object(
        warmup, '_watch_current_reel'
    ), patch.object(warmup, '_advance_to_next_reel', return_value=False):
        with pytest.raises(RuntimeError, match='1 из 3'):
            warmup.browse_tiktok_reels(page, count=3, like_probability_pct=0, follow_probability_pct=0)


def test_search_advance_clicks_dialog_next_and_checks_transition():
    page = MagicMock()
    button = page.locator.return_value.first
    button.count.return_value = 1
    button.is_visible.return_value = True
    button.is_disabled.return_value = False
    with patch.object(warmup, '_current_post_key', return_value='post:1'), patch.object(
        warmup, '_wait_for_post_change', return_value=True
    ) as changed:
        assert warmup._advance_to_next_reel(page, prev_src='video', sideways=True)
    page.locator.assert_called_once_with('[role="dialog"] [data-e2e="arrow-right"]')
    button.click.assert_called_once()
    changed.assert_called_once()
    page.keyboard.press.assert_not_called()


def test_search_advance_falls_back_to_keyboard_when_button_missing():
    page = MagicMock()
    page.locator.return_value.first.count.return_value = 0
    with patch.object(warmup, '_current_post_key', return_value='post:1'), patch.object(
        warmup, '_wait_for_post_change', return_value=True
    ):
        assert warmup._advance_to_next_reel(page, prev_src='video', sideways=True)
    page.keyboard.press.assert_called_once_with('ArrowRight')


def test_search_advance_rejects_unchanged_post():
    page = MagicMock()
    with patch.object(warmup, '_current_post_key', return_value='post:1'), patch.object(
        warmup, '_wait_for_post_change', return_value=False
    ):
        assert not warmup._advance_to_next_reel(page, prev_src='video', sideways=True)
