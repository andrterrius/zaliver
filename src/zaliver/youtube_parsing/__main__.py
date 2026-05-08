from __future__ import annotations

from zaliver.youtube_parsing.video_stats import (
    YoutubeDataApiError,
    YoutubeNoKeyParseError,
    extract_video_id,
    fetch_video_stats_by_id,
    fetch_video_stats_no_key,
)


def main() -> int:
    test_url = "https://www.youtube.com/shorts/LF_4PVNvXF8"
    vid = extract_video_id(test_url)
    if not vid:
        print(f"Could not extract video id from: {test_url!r}")
        return 2

    try:
        stats = fetch_video_stats_by_id(vid)
    except YoutubeDataApiError as e:
        print(f"API error: {e}")
        print("Trying no-key HTML parsing…")
        try:
            stats = fetch_video_stats_no_key(test_url)
        except YoutubeNoKeyParseError as e2:
            print(f"No-key parse error: {e2}")
            print("Hint: set environment variable YOUTUBE_API_KEY for stable results.")
            return 3

    print(f"video_id: {stats.video_id}")
    print(f"views: {stats.view_count}")
    print(f"likes: {stats.like_count}")
    print(f"comments: {stats.comment_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

