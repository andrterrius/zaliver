"""Custom neon text overlay: layout, word wrap, ffmpeg drawtext filters."""

from __future__ import annotations

import os
import re
import sys
import math
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REF_VERTICAL = (1080, 1920)
REF_HORIZONTAL = (1920, 1080)

# Wave animation (preview + ffmpeg drawtext expressions)
NEON_WAVE_AMP_FRAC = 0.14
NEON_WAVE_CHAR_PHASE = 0.62
NEON_WAVE_FRAME_SPEED = 0.09


@dataclass
class TextOverlaySettings:
    enabled: bool = True
    text: str = "GAME IN BIO"
    font_size: int = 95
    glow_color: str = "#00FFFF"
    text_color: str = "#FFFFFF"
    preview_orientation: str = "vertical"  # vertical | horizontal
    anchor_x: float = 0.5
    anchor_y: float = 0.15
    max_width_frac: float = 0.85
    wave_amp_frac: float = NEON_WAVE_AMP_FRAC
    wave_char_phase: float = NEON_WAVE_CHAR_PHASE
    wave_frame_speed: float = NEON_WAVE_FRAME_SPEED
    from_middle: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "TextOverlaySettings":
        allowed = {f.name for f in fields(TextOverlaySettings)}
        raw = {k: v for k, v in d.items() if k in allowed}
        s = TextOverlaySettings(**raw)
        s.preview_orientation = (
            "horizontal" if str(s.preview_orientation).lower() == "horizontal" else "vertical"
        )
        s.anchor_x = max(0.0, min(1.0, float(s.anchor_x)))
        s.anchor_y = max(0.0, min(1.0, float(s.anchor_y)))
        s.max_width_frac = max(0.2, min(1.0, float(s.max_width_frac)))
        s.font_size = max(8, min(400, int(s.font_size)))
        s.glow_color = _normalize_hex_color(str(s.glow_color))
        s.text_color = _normalize_hex_color(str(s.text_color), default="#FFFFFF")
        s.wave_amp_frac = max(0.0, min(0.35, float(s.wave_amp_frac)))
        s.wave_char_phase = NEON_WAVE_CHAR_PHASE
        s.wave_frame_speed = max(0.0, min(0.25, float(s.wave_frame_speed)))
        return s

    def reference_size(self) -> Tuple[int, int]:
        if self.preview_orientation == "horizontal":
            return REF_HORIZONTAL
        return REF_VERTICAL


@dataclass
class ScaledTextOverlay:
    lines: List[str]
    x: int
    y: int
    font_size: int
    line_height: int
    glow_color: str
    text_color: str
    font_path: str
    char_lines: List[List[Tuple[str, int]]]
    wave_amp: int
    wave_char_phase: float
    wave_frame_speed: float
    from_middle: bool = True

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["char_lines"] = [[list(pair) for pair in line] for line in self.char_lines]
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ScaledTextOverlay":
        raw_chars = d.get("char_lines") or []
        char_lines: List[List[Tuple[str, int]]] = []
        for line in raw_chars:
            char_lines.append([(str(c), int(x)) for c, x in line])
        lines = [str(x) for x in (d.get("lines") or [])]
        font_size = int(d.get("font_size", 24))
        font_path = str(d.get("font_path", ""))
        if not char_lines and lines:
            char_lines = [layout_line_chars(ln, font_size, font_path) for ln in lines if ln]
        return ScaledTextOverlay(
            lines=lines,
            x=int(d.get("x", 0)),
            y=int(d.get("y", 0)),
            font_size=font_size,
            line_height=int(d.get("line_height", 24)),
            glow_color=str(d.get("glow_color", "#00FFFF")),
            text_color=str(d.get("text_color", "#FFFFFF")),
            font_path=font_path,
            char_lines=char_lines,
            wave_amp=int(d.get("wave_amp", 4)),
            wave_char_phase=float(d.get("wave_char_phase", NEON_WAVE_CHAR_PHASE)),
            wave_frame_speed=float(d.get("wave_frame_speed", NEON_WAVE_FRAME_SPEED)),
            from_middle=bool(d.get("from_middle", True)),
        )


def _normalize_hex_color(value: str, *, default: str = "#00FFFF") -> str:
    v = value.strip()
    if not v:
        return default
    if not v.startswith("#"):
        v = f"#{v}"
    if len(v) == 4:
        v = "#" + "".join(ch * 2 for ch in v[1:])
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
        return v.upper()
    return default


def _hex_to_ffmpeg_color(hex_color: str, alpha: float = 1.0) -> str:
    h = _normalize_hex_color(hex_color).lstrip("#")
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    a = max(0.0, min(1.0, float(alpha)))
    if abs(a - 1.0) < 1e-6:
        return f"0x{h}"
    return f"0x{h}@{a:.3f}"


def _overlay_font_dirs() -> list[Path]:
    """Dirs with bundled overlay fonts (dev tree + PyInstaller/Nuitka bundle)."""
    dirs: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass) / "zaliver" / "assets" / "fonts")
        dirs.append(Path(meipass) / "assets" / "fonts")
    dirs.append(Path(__file__).resolve().parent.parent / "assets" / "fonts")
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        try:
            key = str(d.resolve())
        except OSError:
            key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


_BUNDLED_FONT = "Montserrat-Bold.ttf"


def resolve_font_path() -> str:
    """Bold overlay font: bundled Montserrat Bold, then system fallbacks."""
    for root in _overlay_font_dirs():
        bundled = root / _BUNDLED_FONT
        try:
            if bundled.is_file():
                return str(bundled.resolve())
        except OSError:
            continue

    candidates: list[Path] = []
    if sys.platform == "win32":
        root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates.extend(
            [
                root / "segoeuib.ttf",
                root / "Segoe UI Bold.ttf",
                root / "bahnschrift.ttf",
                root / "calibrib.ttf",
                root / "arialbd.ttf",
                root / "Arialbd.ttf",
            ]
        )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
                Path("/Library/Fonts/Arial Bold.ttf"),
                Path("/System/Library/Fonts/Helvetica.ttc"),
            ]
        )
    else:
        candidates.extend(
            [
                Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
            ]
        )
    for p in candidates:
        if p.suffix.lower() == ".ttc":
            continue
        try:
            if p.is_file():
                return str(p.resolve())
        except OSError:
            continue
    return ""


def _make_qfont(font_path: str, font_size: int):
    from PyQt6.QtGui import QFont, QFontDatabase

    px = max(8, int(font_size))
    if font_path:
        p = Path(font_path)
        if p.is_file():
            fid = QFontDatabase.addApplicationFont(str(p.resolve()))
            if fid >= 0:
                families = QFontDatabase.applicationFontFamilies(fid)
                if families:
                    font = QFont(families[0])
                    font.setPixelSize(px)
                    font.setWeight(QFont.Weight.Bold)
                    font.setBold(True)
                    return font
    font = QFont("Montserrat")
    font.setFamilies(["Montserrat", "Segoe UI", "Bahnschrift", "Calibri", "Arial"])
    font.setPixelSize(px)
    font.setWeight(QFont.Weight.Bold)
    font.setBold(True)
    return font


def wrap_text_lines(text: str, font_size: int, max_width_px: int, font_path: str = "") -> List[str]:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return []
    max_w = max(20, int(max_width_px))
    font = _make_qfont(font_path, font_size)
    from PyQt6.QtGui import QFontMetrics

    fm = QFontMetrics(font)
    out: list[str] = []
    for paragraph in raw.split("\n"):
        p = paragraph.strip()
        if not p:
            out.append("")
            continue
        words = p.split()
        if not words:
            continue
        line = words[0]
        for word in words[1:]:
            trial = f"{line} {word}"
            if fm.horizontalAdvance(trial) <= max_w:
                line = trial
            else:
                out.append(line)
                line = word
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return out


def measure_text_block(
    lines: List[str], font_size: int, font_path: str = ""
) -> Tuple[int, int, int]:
    if not lines:
        return 0, 0, font_size
    font = _make_qfont(font_path, font_size)
    from PyQt6.QtGui import QFontMetrics

    fm = QFontMetrics(font)
    line_h = max(font_size, fm.height())
    width = max((fm.horizontalAdvance(line) for line in lines if line), default=0)
    height = line_h * max(1, len(lines))
    return int(width), int(height), int(line_h)


def layout_line_chars(line: str, font_size: int, font_path: str = "") -> List[Tuple[str, int]]:
    if not line:
        return []
    font = _make_qfont(font_path, font_size)
    from PyQt6.QtGui import QFontMetrics

    fm = QFontMetrics(font)
    out: list[tuple[str, int]] = []
    x = 0
    for ch in line:
        out.append((ch, x))
        x += fm.horizontalAdvance(ch)
    return out


def wave_offset_y(
    char_idx: int,
    frame: int,
    amp: float,
    *,
    char_phase: float = NEON_WAVE_CHAR_PHASE,
    frame_speed: float = NEON_WAVE_FRAME_SPEED,
) -> float:
    return float(amp) * math.sin(char_idx * char_phase + frame * frame_speed)


def wave_y_ffmpeg_expr(
    base_y: int,
    char_idx: int,
    amp: int,
    start_frame: int,
    *,
    char_phase: float = NEON_WAVE_CHAR_PHASE,
    frame_speed: float = NEON_WAVE_FRAME_SPEED,
) -> str:
    phase = f"{char_idx}*{char_phase}+(n+{int(start_frame)})*{frame_speed}"
    return f"{int(base_y)}+{int(amp)}*sin({phase})"


def compute_scaled_overlay(
    settings: TextOverlaySettings,
    video_w: int,
    video_h: int,
) -> Optional[ScaledTextOverlay]:
    if not settings.enabled:
        return None
    text = (settings.text or "").strip()
    if not text:
        return None
    vw = max(2, int(video_w))
    vh = max(2, int(video_h))
    ref_w, ref_h = settings.reference_size()
    font_path = resolve_font_path()
    font_size = max(8, int(round(settings.font_size * vh / ref_h)))
    max_w = max(20, int(round(settings.max_width_frac * vw)))
    lines = wrap_text_lines(text, font_size, max_w, font_path)
    if not lines:
        return None
    char_lines = [layout_line_chars(ln, font_size, font_path) for ln in lines]
    amp_frac = max(0.0, min(0.35, float(settings.wave_amp_frac)))
    wave_amp = max(0, int(round(font_size * amp_frac)))
    block_w, block_h, line_h = measure_text_block(lines, font_size, font_path)
    block_h += wave_amp * 2
    cx = settings.anchor_x * vw
    cy = settings.anchor_y * vh
    x = int(round(cx - block_w / 2))
    y = int(round(cy - block_h / 2))
    x = max(0, min(x, max(0, vw - block_w)))
    y = max(0, min(y, max(0, vh - block_h)))
    return ScaledTextOverlay(
        lines=lines,
        x=x,
        y=y,
        font_size=font_size,
        line_height=line_h,
        glow_color=settings.glow_color,
        text_color=settings.text_color,
        font_path=font_path,
        char_lines=char_lines,
        wave_amp=wave_amp,
        wave_char_phase=float(NEON_WAVE_CHAR_PHASE),
        wave_frame_speed=float(settings.wave_frame_speed),
        from_middle=bool(settings.from_middle),
    )

def _ffmpeg_escape_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _ffmpeg_escape_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


FFMPEG_GLOW_MAX_LAYERS = 6


def neon_glow_layers(
    font_size: int,
    *,
    max_layers: int = 14,
    alpha_scale: float = 1.0,
) -> List[Tuple[int, int, float]]:
    """Soft radial glow: (dx, dy, alpha) offsets — no outline borders."""
    scale = max(0.05, min(1.0, float(alpha_scale)))
    layers: list[tuple[int, int, float]] = []
    max_r = max(4, min(28, int(round(font_size * 0.26))))
    seen: set[tuple[int, int]] = set()
    for r in range(max_r, 0, -1):
        t = 1.0 - (r - 1) / max(max_r, 1)
        alpha = (0.12 + 0.58 * (t**1.45)) * scale
        steps = max(6, int(5 + r * 1.6))
        for i in range(steps):
            ang = 360.0 * i / steps
            dx = int(round(r * math.cos(math.radians(ang))))
            dy = int(round(r * math.sin(math.radians(ang))))
            key = (dx, dy)
            if key in seen:
                continue
            seen.add(key)
            layers.append((dx, dy, alpha))
            if len(layers) >= max_layers - 1:
                break
        if len(layers) >= max_layers - 1:
            break
    layers.append((0, 0, min(0.95, (0.48 + 0.038 * max_r) * scale)))
    return layers[:max_layers]


def _y_expr_with_offset(y_expr: str, dy: int) -> str:
    if dy == 0:
        return f"'{y_expr}'"
    return f"'({y_expr})+{dy}'"


def overlay_enable_expr(
    *,
    from_middle: bool,
    chunk_start_frame: int,
    chunk_frame_count: int,
    total_frames: int,
) -> Optional[str]:
    """None = always on; '__skip__' = no text in this chunk."""
    if not from_middle or total_frames <= 0:
        return None
    mid = int(total_frames) // 2
    start = int(chunk_start_frame)
    count = int(chunk_frame_count)
    if start + count <= mid:
        return "__skip__"
    local_start = max(0, mid - start)
    if local_start <= 0:
        return None
    return f"gte(n\\,{local_start})"


def build_text_overlay_filters(
    overlay: ScaledTextOverlay,
    input_label: str = "v0",
    *,
    start_frame: int = 0,
    frame_count: int = 0,
    total_frames: int = 0,
) -> str:
    """Return filter chain fragment: [input_label]...filters...[outv]"""
    if not overlay.lines or not overlay.char_lines:
        return f"[{input_label}]null[outv]"
    enable = overlay_enable_expr(
        from_middle=overlay.from_middle,
        chunk_start_frame=start_frame,
        chunk_frame_count=frame_count,
        total_frames=total_frames,
    )
    if enable == "__skip__":
        return f"[{input_label}]null[outv]"
    enable_part = f":enable='{enable}'" if enable else ""
    fill = _hex_to_ffmpeg_color(overlay.text_color, 1.0)
    glow_layers = neon_glow_layers(
        overlay.font_size, max_layers=FFMPEG_GLOW_MAX_LAYERS
    )
    font_part = ""
    if overlay.font_path:
        font_part = f"fontfile='{_ffmpeg_escape_path(overlay.font_path)}':"

    glyphs: list[tuple[int, str, int, int]] = []
    char_global = 0
    for li, chars in enumerate(overlay.char_lines):
        if not chars:
            continue
        base_y = overlay.y + li * overlay.line_height + overlay.wave_amp
        for ch, x_off in chars:
            glyphs.append((base_y, ch, overlay.x + x_off, char_global))
            char_global += 1
    if not glyphs:
        return f"[{input_label}]null[outv]"

    parts: list[str] = []
    cur = input_label
    for step, (base_y, ch, cx, cg) in enumerate(glyphs):
        esc = _ffmpeg_escape_text(ch)
        y_expr = wave_y_ffmpeg_expr(
            base_y,
            cg,
            overlay.wave_amp,
            start_frame,
            char_phase=overlay.wave_char_phase,
            frame_speed=overlay.wave_frame_speed,
        )
        nxt = "outv" if step == len(glyphs) - 1 else f"tc{step}"
        label = cur
        for gi, (dx, dy, alpha) in enumerate(glow_layers):
            col = _hex_to_ffmpeg_color(overlay.glow_color, alpha)
            gl = f"tg{step}_{gi}"
            layer = (
                f"drawtext={font_part}text='{esc}':fontsize={overlay.font_size}:"
                f"x={cx + dx}:y={_y_expr_with_offset(y_expr, dy)}:fontcolor={col}:borderw=0"
                f"{enable_part}"
            )
            parts.append(f"[{label}]{layer}[{gl}]")
            label = gl
        core = (
            f"drawtext={font_part}text='{esc}':fontsize={overlay.font_size}:"
            f"x={cx}:y={_y_expr_with_offset(y_expr, 0)}:fontcolor={fill}:borderw=0"
            f"{enable_part}"
        )
        parts.append(f"[{label}]{core}[{nxt}]")
        cur = nxt
    return ";".join(parts)
