from unittest.mock import MagicMock, patch

import pytest

from zaliver.tiktok_upload import reels_upload


def test_caption_combines_title_and_description_without_repeating_title():
    build = reels_upload._build_tiktok_caption
    assert build("Title", "Title") == "Title"
    assert build("Title", "Title\n\nDetails") == "Title\n\nDetails"
    assert build("Title", "Details") == "Title\n\nDetails"
    assert build("Title", "Title extended") == "Title\n\nTitle extended"
    assert build("", "Details") == "Details"


def test_caption_check_rejects_duplicates_and_extra_text():
    assert reels_upload._caption_write_ok("hello world", "hello world")
    assert not reels_upload._caption_write_ok("hello world", "hello worldhello world")
    assert not reels_upload._caption_write_ok("hello world", "prefix hello world")


def test_caption_insert_uses_one_input_method():
    page = MagicMock()
    area = MagicMock()
    reels_upload._paste_caption_text(page, area, "hello")
    area.click.assert_called_once()
    page.keyboard.insert_text.assert_called_once_with("hello")
    area.evaluate.assert_not_called()


def test_caption_verification_catches_delayed_duplicate():
    page = MagicMock()
    area = MagicMock()
    with patch.object(reels_upload, "_pick_caption_area", return_value=area), patch.object(
        reels_upload, "_read_caption_text", side_effect=["hello", "hellohello"]
    ):
        assert not reels_upload._caption_remains_correct(page, "hello")


def test_failed_caption_read_is_not_treated_as_empty():
    area = MagicMock()
    area.evaluate.side_effect = RuntimeError("editor unavailable")
    with pytest.raises(RuntimeError, match="editor unavailable"):
        reels_upload._read_caption_text(area)
