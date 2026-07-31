"""Allowlisted settings keys + secret masking for the HTTP API."""

from __future__ import annotations

from typing import Any

# Keys clients may read/write via API (platform-scoped or shared).
SETTINGS_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Uniquify / shared processing
        "output_folder",
        "input_files",
        "background_music_files",
        "background_music_enabled",
        "background_music_mix_with_source",
        "background_music_volume_pct",
        "background_music_volume_pct_min",
        "background_music_volume_pct_max",
        "num_workers",
        "copies_per_file",
        "one_copy_no_effects",
        "slice/fps_mode",
  "delete_after_upload",
  "upload_as_ready",
  "upload_title",
  "upload_description",
        "upload_title",
        "upload_description",
        "use_gpu_enabled",
        "use_gpu_finalize_enabled",
        "fx_brightness_enabled",
        "fx_contrast_enabled",
        "fx_saturation_enabled",
        "fx_scale_enabled",
        "fx_noise_enabled",
        "playback_speed_enabled",
        "fx_brightness_min",
        "fx_brightness_max",
        "fx_contrast_min",
        "fx_contrast_max",
        "fx_saturation_min",
        "fx_saturation_max",
        "fx_scale_min",
        "fx_scale_max",
        "fx_noise_min",
        "fx_noise_max",
        "fx_speed_min",
        "fx_speed_max",
        "text_overlay_enabled",
        "text_overlay_text",
        "text_overlay_from_middle",
        "text_overlay_after_frame_change",
        "text_overlay_font_size",
        "text_overlay_orientation",
        "text_overlay_glow_color",
        "text_overlay_text_color",
        "text_overlay_glow_enabled",
        "text_overlay_letter_spacing",
        "text_overlay_font_path",
        "text_overlay_font_bold",
        "text_overlay_anchor_x",
        "text_overlay_anchor_y",
        "text_overlay_wave_amp_frac",
        "text_overlay_wave_amp_frac_min",
        "text_overlay_wave_amp_frac_max",
        "text_overlay_wave_frame_speed",
        "text_overlay_wave_frame_speed_min",
        "text_overlay_wave_frame_speed_max",
        # Slicing
        "slice/output_folder",
        "slice/clip_files",
        "slice/music_files",
        "slice/copies_per_track",
        "slice/delete_after_upload",
        "slice/auto_scene_durations",
        "slice/min_scene_duration",
        "slice/max_scene_duration",
        "slice/min_scenes",
        "slice/max_scenes",
        "slice/text_overlay_enabled",
        "slice/text_overlay_text",
        "slice/text_overlay_from_middle",
        "slice/text_overlay_after_frame_change",
        "slice/text_overlay_font_size",
        "slice/text_overlay_orientation",
        "slice/text_overlay_glow_color",
        "slice/text_overlay_text_color",
        "slice/text_overlay_glow_enabled",
        "slice/text_overlay_letter_spacing",
        "slice/text_overlay_font_path",
        "slice/text_overlay_font_bold",
        "slice/text_overlay_anchor_x",
        "slice/text_overlay_anchor_y",
        "slice/text_overlay_wave_amp_frac",
        "slice/text_overlay_wave_amp_frac_min",
        "slice/text_overlay_wave_amp_frac_max",
        "slice/text_overlay_wave_frame_speed",
        "slice/text_overlay_wave_frame_speed_min",
        "slice/text_overlay_wave_frame_speed_max",
        # Stitching
        "stitch/output_folder",
        "stitch/part1_files",
        "stitch/part2_files",
        "stitch/music_files",
        "stitch/copies_per_track",
        "stitch/delete_after_upload",
        "stitch/transition",
        "stitch/transition_random",
        "stitch/min_part_duration",
        "stitch/max_part_duration",
        "stitch/text_overlay_enabled",
        "stitch/text_overlay_text",
        "stitch/text_overlay_from_middle",
        "stitch/text_overlay_after_frame_change",
        "stitch/text_overlay_font_size",
        "stitch/text_overlay_orientation",
        "stitch/text_overlay_glow_color",
        "stitch/text_overlay_text_color",
        "stitch/text_overlay_glow_enabled",
        "stitch/text_overlay_letter_spacing",
        "stitch/text_overlay_font_path",
        "stitch/text_overlay_font_bold",
        "stitch/text_overlay_anchor_x",
        "stitch/text_overlay_anchor_y",
        "stitch/text_overlay_wave_amp_frac",
        "stitch/text_overlay_wave_amp_frac_min",
        "stitch/text_overlay_wave_amp_frac_max",
        "stitch/text_overlay_wave_frame_speed",
        "stitch/text_overlay_wave_frame_speed_min",
        "stitch/text_overlay_wave_frame_speed_max",
        # Antidetect / upload / AI
        "antydetect/dolphin_headless",
        "antydetect/max_concurrent_browsers",
        "antydetect/default_browser",
        "antydetect/local_api_base_url",
        "antydetect/own_base_url",
        "antydetect/remote_api_base_url",
        "antydetect/remote_cdp_public_host",
        "antydetect/own_remote_cdp_host",
        "antydetect/own_remote_cdp_port",
        "antydetect/local_api_token",
        "instagram/tabs_per_profile",
        "instagram/stats_checker_profile_id",
        "upload_pause_hours",
        "ai/base_url",
        "ai/api_key",
        "ai/model",
        "stats_server_username",
        "stats_server/username",
        "youtube/api_key",
        "youtube/search_oldest_channel",
    }
)

# Stored as path lists under allowed roots (existence optional on save).
SETTINGS_PATH_LIST_KEYS: frozenset[str] = frozenset(
    {
        "input_files",
        "background_music_files",
        "slice/clip_files",
        "slice/music_files",
        "stitch/part1_files",
        "stitch/part2_files",
        "stitch/music_files",
    }
)

SETTINGS_OPTIONAL_FILE_KEYS: frozenset[str] = frozenset(
    {
        "text_overlay_font_path",
        "slice/text_overlay_font_path",
        "stitch/text_overlay_font_path",
    }
)

SECRET_KEYS: frozenset[str] = frozenset(
    {
        "ai/api_key",
        "youtube/api_key",
        "antydetect/local_api_token",
    }
)


def is_allowed_settings_key(key: str) -> bool:
    return (key or "").strip() in SETTINGS_ALLOWLIST


def mask_secret(value: Any) -> Any:
    if value is None:
        return None
    s = str(value)
    if not s:
        return ""
    if len(s) <= 4:
        return "****"
    return f"{s[:2]}…{s[-2:]} ({len(s)} chars)"


def public_settings_value(key: str, value: Any) -> Any:
    if key in SECRET_KEYS:
        return mask_secret(value)
    return value
