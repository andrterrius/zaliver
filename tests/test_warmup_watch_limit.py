from unittest.mock import Mock, patch

import pytest

from zaliver.instagram_upload import reels_warmup as instagram
from zaliver.tiktok_upload import reels_warmup as tiktok
from zaliver.youtube_upload import studio


@pytest.mark.parametrize('module', [instagram, tiktok])
@pytest.mark.parametrize('duration,expected', [(30, 30000), (300, 180000), (0, 180000)])
def test_full_reel_watch_is_capped(module, duration, expected):
    page = Mock()
    page.evaluate.return_value = duration
    with patch.object(module, '_try_unmute_or_play'):
        module._watch_current_reel(page, watch_min_s=200, watch_max_s=300, watch_full=True)
    page.wait_for_timeout.assert_called_once_with(expected)


@pytest.mark.parametrize('module', [instagram, tiktok])
def test_reel_custom_range_is_capped(module):
    page = Mock()
    with patch.object(module, '_try_unmute_or_play'):
        module._watch_current_reel(page, watch_min_s=200, watch_max_s=300, watch_full=False)
    page.wait_for_timeout.assert_called_once_with(180000)


def test_youtube_full_watch_caps_caller_timeout():
    page = Mock()
    with patch.object(studio, '_studio_dismiss_shorts_player_overlays'), patch.object(
        studio, '_studio_is_shorts_paused', return_value=False
    ), patch.object(studio.time, 'monotonic', side_effect=[0, 181]):
        assert not studio._studio_wait_for_short_full_watch(page, expect_video_id='id', max_wait_s=999)
    page.wait_for_timeout.assert_not_called()
