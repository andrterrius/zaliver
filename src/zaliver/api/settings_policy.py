"""Allowlisted settings keys + secret masking for the HTTP API."""

from __future__ import annotations

from typing import Any

# Keys clients may read/write via API (platform-scoped or shared).
SETTINGS_ALLOWLIST: frozenset[str] = frozenset(
    {
        "output_folder",
        "input_files",
        "background_music_files",
        "background_music_enabled",
        "background_music_mix_with_source",
        "background_music_volume_pct",
        "background_music_volume_pct_min",
        "background_music_volume_pct_max",
        "num_workers",
        "slice/fps_mode",
        "delete_after_upload",
        "upload_as_ready",
        "use_gpu_enabled",
        "use_gpu_finalize_enabled",
        "fx_brightness_enabled",
        "fx_contrast_enabled",
        "fx_saturation_enabled",
        "fx_scale_enabled",
        "fx_noise_enabled",
        "playback_speed_enabled",
        "text_overlay_enabled",
        "text_overlay_text",
        "text_overlay_from_middle",
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
        "text_overlay_wave_frame_speed",
        "antydetect/dolphin_headless",
        "antydetect/max_concurrent_browsers",
        "antydetect/default_browser",
        "antydetect/local_api_base_url",
        "antydetect/own_base_url",
        "antydetect/remote_api_base_url",
        "antydetect/remote_cdp_public_host",
        "antydetect/own_remote_cdp_host",
        "antydetect/own_remote_cdp_port",
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

SECRET_KEYS: frozenset[str] = frozenset(
    {
        "ai/api_key",
        "youtube/api_key",
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
