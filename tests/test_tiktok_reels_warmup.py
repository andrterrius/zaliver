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
