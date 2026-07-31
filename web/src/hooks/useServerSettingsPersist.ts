import { useEffect, useRef } from "react";
import { api } from "../api/client";
import type { TextOverlayState } from "../components/TextOverlayFields";
import type { RangeValue } from "../components/RangeSlider";

export function asStringList(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.map((x) => String(x || "").trim()).filter(Boolean);
}

export function asBool(v: unknown, fallback: boolean): boolean {
  if (v === undefined || v === null) return fallback;
  return Boolean(v);
}

export function asNumber(v: unknown, fallback: number): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

export function asRange(
  loRaw: unknown,
  hiRaw: unknown,
  fallback: RangeValue,
): RangeValue {
  const lo = asNumber(loRaw, fallback.lo);
  const hi = asNumber(hiRaw, fallback.hi);
  return { lo, hi: Math.max(lo, hi) };
}

/** Settings store wave as fraction (0..1); UI uses percent (0..100). */
export function textOverlayFromSettings(
  v: Record<string, unknown>,
  prefix: string,
  defaults: TextOverlayState,
): TextOverlayState {
  const k = (name: string) => `${prefix}${name}`;
  const ampFallback = defaults.waveAmp.lo / 100;
  const spdFallback = defaults.waveSpeed.lo / 100;
  const ampLo = asNumber(v[k("text_overlay_wave_amp_frac_min")], ampFallback);
  const ampHi = asNumber(
    v[k("text_overlay_wave_amp_frac_max")],
    asNumber(v[k("text_overlay_wave_amp_frac")], ampLo),
  );
  const spdLo = asNumber(v[k("text_overlay_wave_frame_speed_min")], spdFallback);
  const spdHi = asNumber(
    v[k("text_overlay_wave_frame_speed_max")],
    asNumber(v[k("text_overlay_wave_frame_speed")], spdLo),
  );
  return {
    enabled: asBool(v[k("text_overlay_enabled")], defaults.enabled),
    fromMiddle: asBool(v[k("text_overlay_from_middle")], defaults.fromMiddle),
    afterFrameChange: asBool(
      v[k("text_overlay_after_frame_change")],
      defaults.afterFrameChange,
    ),
    text: String(v[k("text_overlay_text")] ?? defaults.text),
    fontSize: Math.max(
      12,
      Math.min(240, Math.round(asNumber(v[k("text_overlay_font_size")], defaults.fontSize))),
    ),
    glowEnabled: asBool(v[k("text_overlay_glow_enabled")], defaults.glowEnabled),
    glowColor: String(v[k("text_overlay_glow_color")] ?? defaults.glowColor),
    textColor: String(v[k("text_overlay_text_color")] ?? defaults.textColor),
    letterSpacing: Math.round(
      asNumber(v[k("text_overlay_letter_spacing")], defaults.letterSpacing),
    ),
    fontBold: asBool(v[k("text_overlay_font_bold")], defaults.fontBold),
    fontPath: String(v[k("text_overlay_font_path")] ?? defaults.fontPath),
    waveAmp: {
      lo: Math.round(ampLo * 1000) / 10,
      hi: Math.round(ampHi * 1000) / 10,
    },
    waveSpeed: {
      lo: Math.round(spdLo * 1000) / 10,
      hi: Math.round(spdHi * 1000) / 10,
    },
    anchorX: asNumber(v[k("text_overlay_anchor_x")], defaults.anchorX),
    anchorY: asNumber(v[k("text_overlay_anchor_y")], defaults.anchorY),
  };
}

export function textOverlayToSettings(
  state: TextOverlayState,
  prefix: string,
): Record<string, unknown> {
  const k = (name: string) => `${prefix}${name}`;
  const ampLo = state.waveAmp.lo / 100;
  const ampHi = state.waveAmp.hi / 100;
  const spdLo = state.waveSpeed.lo / 100;
  const spdHi = state.waveSpeed.hi / 100;
  return {
    [k("text_overlay_enabled")]: state.enabled,
    [k("text_overlay_text")]: state.text,
    [k("text_overlay_from_middle")]: state.fromMiddle,
    [k("text_overlay_after_frame_change")]: state.afterFrameChange,
    [k("text_overlay_font_size")]: state.fontSize,
    [k("text_overlay_orientation")]: "vertical",
    [k("text_overlay_glow_color")]: state.glowColor,
    [k("text_overlay_text_color")]: state.textColor,
    [k("text_overlay_glow_enabled")]: state.glowEnabled,
    [k("text_overlay_letter_spacing")]: state.letterSpacing,
    [k("text_overlay_font_path")]: state.fontPath.trim(),
    [k("text_overlay_font_bold")]: state.fontBold,
    [k("text_overlay_anchor_x")]: state.anchorX,
    [k("text_overlay_anchor_y")]: state.anchorY,
    [k("text_overlay_wave_amp_frac")]: ampLo,
    [k("text_overlay_wave_amp_frac_min")]: ampLo,
    [k("text_overlay_wave_amp_frac_max")]: ampHi,
    [k("text_overlay_wave_frame_speed")]: spdLo,
    [k("text_overlay_wave_frame_speed_min")]: spdLo,
    [k("text_overlay_wave_frame_speed_max")]: spdHi,
  };
}

/** After hydrate, debounce-patch allowlisted values into server settings.json. */
export function useDebouncedSettingsPatch(
  values: Record<string, unknown> | null,
  delayMs = 450,
) {
  const first = useRef(true);
  useEffect(() => {
    if (!values) return;
    // Skip the immediate post-hydrate write (same data we just loaded).
    if (first.current) {
      first.current = false;
      return;
    }
    const timer = window.setTimeout(() => {
      void api.patchSettings(values).catch(() => {
        /* ignore transient network errors while typing */
      });
    }, delayMs);
    return () => window.clearTimeout(timer);
  }, [values, delayMs]);
}
