"""Custom neon text overlay: layout, word wrap, ffmpeg drawtext filters."""

from __future__ import annotations

import math
import os
import re
import sys
import urllib.error
import urllib.request
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
    glow_enabled: bool = True
    glow_color: str = "#00FFFF"
    text_color: str = "#FFFFFF"
    letter_spacing: int = 0
    custom_font_path: str = ""
    font_bold: bool = True
    preview_orientation: str = "vertical"  # vertical | horizontal
    anchor_x: float = 0.5
    anchor_y: float = 0.15
    max_width_frac: float = 0.85
    wave_amp_frac: float = NEON_WAVE_AMP_FRAC
    wave_char_phase: float = NEON_WAVE_CHAR_PHASE
    wave_frame_speed: float = NEON_WAVE_FRAME_SPEED
    from_middle: bool = True
    after_frame_change: bool = False

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
        s.glow_enabled = bool(s.glow_enabled)
        s.glow_color = _normalize_hex_color(str(s.glow_color))
        s.text_color = _normalize_hex_color(str(s.text_color), default="#FFFFFF")
        s.letter_spacing = max(-50, min(120, int(s.letter_spacing)))
        s.custom_font_path = str(s.custom_font_path or "").strip()
        s.font_bold = bool(s.font_bold)
        s.wave_amp_frac = max(0.0, min(0.35, float(s.wave_amp_frac)))
        s.wave_char_phase = NEON_WAVE_CHAR_PHASE
        s.wave_frame_speed = max(0.0, min(0.25, float(s.wave_frame_speed)))
        s.after_frame_change = bool(getattr(s, "after_frame_change", False))
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
    glow_enabled: bool
    glow_color: str
    text_color: str
    font_path: str
    letter_spacing: int
    font_bold: bool
    char_lines: List[List[Tuple[str, int]]]
    wave_amp: int
    wave_char_phase: float
    wave_frame_speed: float
    from_middle: bool = True
    enable_after_sec: float | None = None

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
        letter_spacing = int(d.get("letter_spacing", 0))
        font_bold = bool(d.get("font_bold", True))
        if not char_lines and lines:
            char_lines = [
                layout_line_chars(
                    ln, font_size, font_path, letter_spacing, bold=font_bold
                )
                for ln in lines
                if ln
            ]
        return ScaledTextOverlay(
            lines=lines,
            x=int(d.get("x", 0)),
            y=int(d.get("y", 0)),
            font_size=font_size,
            line_height=int(d.get("line_height", 24)),
            glow_enabled=bool(d.get("glow_enabled", True)),
            glow_color=str(d.get("glow_color", "#00FFFF")),
            text_color=str(d.get("text_color", "#FFFFFF")),
            font_path=font_path,
            letter_spacing=letter_spacing,
            font_bold=font_bold,
            char_lines=char_lines,
            wave_amp=int(d.get("wave_amp", 4)),
            wave_char_phase=float(d.get("wave_char_phase", NEON_WAVE_CHAR_PHASE)),
            wave_frame_speed=float(d.get("wave_frame_speed", NEON_WAVE_FRAME_SPEED)),
            from_middle=bool(d.get("from_middle", True)),
            enable_after_sec=(
                float(d["enable_after_sec"])
                if d.get("enable_after_sec") is not None
                else None
            ),
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


_BUNDLED_FONT_BOLD = "Montserrat-Bold.ttf"


def _bundled_font_names(*, bold: bool) -> list[str]:
    if bold:
        return [_BUNDLED_FONT_BOLD]
    return ["Montserrat-Regular.ttf", "Montserrat-Medium.ttf"]


def _system_font_candidates(*, bold: bool) -> list[Path]:
    candidates: list[Path] = []
    if sys.platform == "win32":
        root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        if bold:
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
        else:
            candidates.extend(
                [
                    root / "segoeui.ttf",
                    root / "Segoe UI.ttf",
                    root / "calibri.ttf",
                    root / "arial.ttf",
                    root / "bahnschrift.ttf",
                ]
            )
    elif sys.platform == "darwin":
        if bold:
            candidates.extend(
                [
                    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
                    Path("/Library/Fonts/Arial Bold.ttf"),
                ]
            )
        else:
            candidates.extend(
                [
                    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
                    Path("/Library/Fonts/Arial.ttf"),
                    Path("/System/Library/Fonts/Helvetica.ttc"),
                ]
            )
    else:
        if bold:
            candidates.extend(
                [
                    Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
                    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
                ]
            )
        else:
            candidates.extend(
                [
                    Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
                    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
                ]
            )
    return candidates


def resolve_font_path() -> str:
    """Bold overlay font: bundled Montserrat Bold, then system fallbacks."""
    return effective_font_path("", bold=True)


_BUNDLED_EMOJI_FONT = "NotoEmoji.ttf"


def _system_emoji_font_candidates(*, color_preferred: bool = True) -> list[Path]:
    """System emoji fonts. Color fonts first when color_preferred."""
    if sys.platform == "win32":
        root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        return [
            root / "seguiemj.ttf",
            root / "Segoe UI Emoji.ttf",
        ]
    if sys.platform == "darwin":
        return [
            Path("/System/Library/Fonts/Apple Color Emoji.ttc"),
            Path("/Library/Fonts/Apple Color Emoji.ttc"),
        ]
    # Linux: Noto Color is CBDT (Qt may draw color); mono NotoEmoji last.
    color = [
        Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
        Path("/usr/share/fonts/noto/NotoColorEmoji.ttf"),
    ]
    mono = [
        Path("/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf"),
    ]
    return color + mono if color_preferred else mono + color


_color_emoji_font_cache: Optional[str] = None
_mono_emoji_font_cache: Optional[str] = None
_emoji_font_path_cache: Optional[str] = None


def resolve_color_emoji_font_path() -> str:
    """System color emoji font for Qt rasterization (Windows/macOS/Linux)."""
    global _color_emoji_font_cache
    if _color_emoji_font_cache is not None:
        return _color_emoji_font_cache
    for p in _system_emoji_font_candidates(color_preferred=True):
        try:
            if not p.is_file():
                continue
            # Skip clearly mono-only names on Linux list order
            name = p.name.lower()
            if name.startswith("notoemoji") and "color" not in name:
                continue
            _color_emoji_font_cache = str(p.resolve())
            return _color_emoji_font_cache
        except OSError:
            continue
    _color_emoji_font_cache = ""
    return ""


def resolve_mono_emoji_font_path() -> str:
    """Bundled monochrome Noto Emoji (ffmpeg drawtext fallback)."""
    global _mono_emoji_font_cache
    if _mono_emoji_font_cache is not None:
        return _mono_emoji_font_cache
    for root in _overlay_font_dirs():
        bundled = root / _BUNDLED_EMOJI_FONT
        try:
            if bundled.is_file():
                _mono_emoji_font_cache = str(bundled.resolve())
                return _mono_emoji_font_cache
        except OSError:
            continue
    for p in _system_emoji_font_candidates(color_preferred=False):
        try:
            name = p.name.lower()
            if p.is_file() and (
                "notoemoji" in name or name.startswith("seguiemj")
            ):
                _mono_emoji_font_cache = str(p.resolve())
                return _mono_emoji_font_cache
        except OSError:
            continue
    _mono_emoji_font_cache = ""
    return ""


def resolve_emoji_font_path() -> str:
    """Layout/metrics for emoji: bundled mono (matches ffmpeg drawtext)."""
    global _emoji_font_path_cache
    if _emoji_font_path_cache is not None:
        return _emoji_font_path_cache
    # Prefer mono for width parity with drawtext; color font only for UI preview paint.
    path = resolve_mono_emoji_font_path() or resolve_color_emoji_font_path()
    _emoji_font_path_cache = path
    return path


def is_emoji_unit(unit: str) -> bool:
    """True if the grapheme is (or contains) an emoji / pictograph codepoint."""
    if not unit:
        return False
    for ch in unit:
        cp = ord(ch)
        if (
            0x1F300 <= cp <= 0x1FAFF
            or 0x1F1E0 <= cp <= 0x1F1FF
            or 0x2600 <= cp <= 0x27BF
            or 0x2300 <= cp <= 0x23FF
            or 0x2B50 <= cp <= 0x2B55
            or cp in (0x200D, 0xFE0F, 0x20E3)
            or 0x1F000 <= cp <= 0x1F02F
        ):
            return True
    return False


def iter_text_units(text: str) -> List[str]:
    """Split into drawable units; keep ZWJ / VS16 / skin-tone / flag sequences together.

    Pure Python (not Qt): QTextBoundaryFinder returns UTF-16 offsets which break
    on non-BMP emoji when slicing Python str.
    """
    if not text:
        return []
    out: list[str] = []
    buf = ""

    def _is_skin_tone(cp: int) -> bool:
        return 0x1F3FB <= cp <= 0x1F3FF

    def _is_regional(cp: int) -> bool:
        return 0x1F1E6 <= cp <= 0x1F1FF

    def _continues(buf: str, ch: str) -> bool:
        if not buf:
            return False
        cp = ord(ch)
        prev = ord(buf[-1])
        if cp in (0x200D, 0xFE0F, 0x20E3):
            return True
        if _is_skin_tone(cp):
            return True
        if 0xE0020 <= cp <= 0xE007F:  # tag sequences
            return True
        if prev == 0x200D:
            return True
        if _is_regional(prev) and _is_regional(cp) and len(buf) == 1:
            return True
        return False

    for ch in text:
        if _continues(buf, ch):
            buf += ch
        else:
            if buf:
                out.append(buf)
            buf = ch
    if buf:
        out.append(buf)
    return out


def effective_font_path(custom_path: str = "", *, bold: bool = True) -> str:
    """User font file if valid, else bundled / system font for the chosen weight."""
    custom = (custom_path or "").strip()
    if custom:
        try:
            p = Path(custom)
            if p.is_file() and p.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                return str(p.resolve())
        except OSError:
            pass

    for name in _bundled_font_names(bold=bold):
        for root in _overlay_font_dirs():
            bundled = root / name
            try:
                if bundled.is_file():
                    return str(bundled.resolve())
            except OSError:
                continue

    for p in _system_font_candidates(bold=bold):
        if p.suffix.lower() == ".ttc":
            continue
        try:
            if p.is_file():
                return str(p.resolve())
        except OSError:
            continue

    if not bold:
        return effective_font_path("", bold=True)
    return ""


def font_path_for_unit(
    unit: str,
    main_font_path: str = "",
    *,
    bold: bool = True,
) -> str:
    """Main overlay font, or system emoji font when the unit is an emoji."""
    if is_emoji_unit(unit):
        emoji = resolve_emoji_font_path()
        if emoji:
            return emoji
    return main_font_path or effective_font_path("", bold=bold)


def list_bundled_overlay_fonts() -> List[Tuple[str, str]]:
    """(display label, absolute path) for fonts shipped with the app."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for root in _overlay_font_dirs():
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        for pattern in ("*.ttf", "*.otf"):
            try:
                paths = sorted(root.glob(pattern))
            except OSError:
                continue
            for p in paths:
                try:
                    if not p.is_file():
                        continue
                    if _is_emoji_font_path(str(p)):
                        continue
                    key = str(p.resolve())
                except OSError:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                label = p.stem.replace("-", " ").replace("_", " ")
                out.append((label, key))
    return out


def _is_emoji_font_path(font_path: str) -> bool:
    if not font_path:
        return False
    name_l = Path(font_path).name.lower()
    return any(
        key in name_l
        for key in ("seguiemj", "emoji", "notocoloremoji", "notoemoji")
    )


def _make_qfont(font_path: str, font_size: int, *, bold: bool = True):
    from PyQt6.QtGui import QFont, QFontDatabase

    px = max(8, int(font_size))
    # Emoji fonts have no bold face — forcing bold breaks glyph metrics.
    use_bold = bool(bold) and not _is_emoji_font_path(font_path)
    if font_path:
        p = Path(font_path)
        if p.is_file():
            fid = QFontDatabase.addApplicationFont(str(p.resolve()))
            if fid >= 0:
                families = QFontDatabase.applicationFontFamilies(fid)
                if families:
                    font = QFont(families[0])
                    font.setPixelSize(px)
                    if use_bold:
                        font.setWeight(QFont.Weight.Bold)
                        font.setBold(True)
                    else:
                        font.setWeight(QFont.Weight.Normal)
                        font.setBold(False)
                    return font
    font = QFont("Montserrat")
    font.setFamilies(["Montserrat", "Segoe UI", "Bahnschrift", "Calibri", "Arial"])
    font.setPixelSize(px)
    if use_bold:
        font.setWeight(QFont.Weight.Bold)
        font.setBold(True)
    else:
        font.setWeight(QFont.Weight.Normal)
        font.setBold(False)
    return font


def layout_line_chars(
    line: str,
    font_size: int,
    font_path: str = "",
    letter_spacing: int = 0,
    *,
    bold: bool = True,
) -> List[Tuple[str, int]]:
    if not line:
        return []
    from PyQt6.QtGui import QFontMetrics

    out: list[tuple[str, int]] = []
    x = 0
    units = iter_text_units(line)
    for i, unit in enumerate(units):
        unit_font_path = font_path_for_unit(unit, font_path, bold=bold)
        font = _make_qfont(
            unit_font_path,
            font_size,
            bold=bold,
        )
        fm = QFontMetrics(font)
        out.append((unit, x))
        x += fm.horizontalAdvance(unit)
        if i < len(units) - 1:
            x += int(letter_spacing)
    return out


def line_pixel_width(
    line: str,
    font_size: int,
    font_path: str = "",
    letter_spacing: int = 0,
    *,
    bold: bool = True,
) -> int:
    chars = layout_line_chars(
        line, font_size, font_path, letter_spacing, bold=bold
    )
    if not chars:
        return 0
    from PyQt6.QtGui import QFontMetrics

    last_ch, last_x = chars[-1]
    unit_font_path = font_path_for_unit(last_ch, font_path, bold=bold)
    font = _make_qfont(
        unit_font_path,
        font_size,
        bold=bold,
    )
    fm = QFontMetrics(font)
    return int(last_x + fm.horizontalAdvance(last_ch))


def wrap_text_lines(
    text: str,
    font_size: int,
    max_width_px: int,
    font_path: str = "",
    letter_spacing: int = 0,
    *,
    bold: bool = True,
) -> List[str]:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return []
    max_w = max(20, int(max_width_px))
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
            if line_pixel_width(trial, font_size, font_path, letter_spacing, bold=bold) <= max_w:
                line = trial
            else:
                out.append(line)
                line = word
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return out


def measure_text_block(
    lines: List[str],
    font_size: int,
    font_path: str = "",
    letter_spacing: int = 0,
    *,
    bold: bool = True,
) -> Tuple[int, int, int]:
    if not lines:
        return 0, 0, font_size
    from PyQt6.QtGui import QFontMetrics

    font = _make_qfont(font_path, font_size, bold=bold)
    fm = QFontMetrics(font)
    line_h = max(font_size, fm.height())
    emoji_path = resolve_emoji_font_path()
    if emoji_path:
        efm = QFontMetrics(_make_qfont(emoji_path, font_size, bold=False))
        line_h = max(line_h, efm.height())
    width = max(
        (
            line_pixel_width(line, font_size, font_path, letter_spacing, bold=bold)
            for line in lines
            if line
        ),
        default=0,
    )
    height = line_h * max(1, len(lines))
    return int(width), int(height), int(line_h)


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
    font_bold = bool(settings.font_bold)
    font_path = effective_font_path(settings.custom_font_path, bold=font_bold)
    # Перенос — как в предпросмотре (референс 9:16/16:9 + font_size из UI).
    # Иначе при другом аспекте исходника шрифт масштабируется по высоте, а
    # max_width по ширине кадра → лишние переносы на финале.
    max_w_ref = max(20, int(round(settings.max_width_frac * ref_w)))
    lines = wrap_text_lines(
        text,
        int(settings.font_size),
        max_w_ref,
        font_path,
        int(settings.letter_spacing),
        bold=font_bold,
    )
    if not lines:
        return None
    scale = vh / float(ref_h)
    font_size = max(8, int(round(settings.font_size * scale)))
    ref_font = max(1, int(settings.font_size))
    letter_spacing = int(
        round(settings.letter_spacing * font_size / ref_font)
    )
    char_lines = [
        layout_line_chars(ln, font_size, font_path, letter_spacing, bold=font_bold)
        for ln in lines
    ]
    amp_frac = max(0.0, min(0.35, float(settings.wave_amp_frac)))
    wave_amp = max(0, int(round(font_size * amp_frac)))
    block_w, block_h, line_h = measure_text_block(
        lines, font_size, font_path, letter_spacing, bold=font_bold
    )
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
        glow_enabled=bool(settings.glow_enabled),
        glow_color=settings.glow_color,
        text_color=settings.text_color,
        font_path=font_path,
        letter_spacing=letter_spacing,
        font_bold=font_bold,
        char_lines=char_lines,
        wave_amp=wave_amp,
        wave_char_phase=float(NEON_WAVE_CHAR_PHASE),
        wave_frame_speed=float(settings.wave_frame_speed),
        from_middle=bool(settings.from_middle),
        enable_after_sec=None,
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


def text_overlay_filter_has_content(overlay_part: str) -> bool:
    """True if filter graph actually draws overlay text/emoji."""
    s = overlay_part or ""
    return "drawtext" in s or "overlay=" in s


# Twemoji 72×72 PNGs (CC-BY 4.0) — color emoji without ffmpeg color-font / movie hacks.
_TWEMOJI_CDN = (
    "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72"
)


def _twemoji_cache_dir() -> Path:
    d = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "zaliver" / "twemoji_72"
    if sys.platform != "win32":
        d = Path.home() / ".cache" / "zaliver" / "twemoji_72"
    d.mkdir(parents=True, exist_ok=True)
    return d


def twemoji_codepoint_keys(unit: str) -> list[str]:
    """Candidate Twemoji filenames (hex codepoints) for a grapheme."""
    if not unit:
        return []
    full = "-".join(f"{ord(ch):x}" for ch in unit)
    no_vs = "-".join(f"{ord(ch):x}" for ch in unit if ord(ch) != 0xFE0F)
    keys: list[str] = []
    for k in (full, no_vs, full.lower(), no_vs.lower()):
        if k and k not in keys:
            keys.append(k)
    return keys


def ensure_twemoji_png(unit: str) -> Optional[Path]:
    """Download/cached Twemoji PNG for unit; None if unavailable."""
    if not unit or not is_emoji_unit(unit):
        return None
    cache = _twemoji_cache_dir()
    for key in twemoji_codepoint_keys(unit):
        dest = cache / f"{key}.png"
        try:
            if dest.is_file() and dest.stat().st_size > 32:
                return dest
        except OSError:
            continue
        url = f"{_TWEMOJI_CDN}/{key}.png"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "zaliver-emoji/1.0"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read()
            if not data or len(data) < 32:
                continue
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return dest
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            continue
    return None


@dataclass
class TextOverlayFilterBuild:
    """Filter graph fragment plus optional emoji still inputs (-loop 1 -i)."""

    graph: str
    emoji_input_argv: list[str]
    emoji_count: int = 0

    @property
    def has_content(self) -> bool:
        return text_overlay_filter_has_content(self.graph)


def _emoji_still_input_argv(
    pngs: list[Path],
    *,
    duration_sec: float,
    fps: float = 30.0,
) -> list[str]:
    """Finite looped PNG inputs covering the whole clip (avoids early EOF at the end)."""
    argv: list[str] = []
    dur = max(0.25, float(duration_sec))
    fps_v = max(1.0, float(fps))
    for p in pngs:
        # Disable hwaccel so AMF/d3d11va from the video input does not swallow PNGs
        # (wrong stream → tiny copy of the main frame instead of the emoji).
        argv.extend(
            [
                "-hwaccel",
                "none",
                "-loop",
                "1",
                "-t",
                f"{dur:.6f}",
                "-r",
                f"{fps_v:.6f}",
                "-i",
                str(p),
            ]
        )
    return argv


def _emoji_stream_duration_sec(
    *,
    start_frame: int,
    frame_count: int,
    total_frames: int,
    fps: float,
    total_duration_sec: float | None,
) -> float:
    fps_v = max(float(fps), 1e-6)
    chunk_dur = max(1, int(frame_count)) / fps_v
    if total_duration_sec is not None and total_duration_sec > 0:
        full_dur = float(total_duration_sec)
    elif int(total_frames) > 0:
        full_dur = int(total_frames) / fps_v
    else:
        full_dur = chunk_dur
    # Whole-clip overlay: cover full timeline; chunked encode: cover this chunk.
    if int(start_frame) == 0 and int(frame_count) >= max(1, int(total_frames)):
        return full_dur + 0.5
    return chunk_dur + 0.5


def build_text_overlay_filters(
    overlay: ScaledTextOverlay,
    input_label: str = "v0",
    *,
    start_frame: int = 0,
    frame_count: int = 0,
    total_frames: int = 0,
    fps: float = 30.0,
    total_duration_sec: float | None = None,
    emoji_input_start: int = 1,
) -> TextOverlayFilterBuild:
    """Build [input_label]…[outv] plus optional -i argv for color emoji PNGs.

    Color emoji use Twemoji PNGs overlaid via normal still inputs (fast, no movie/loop).
    Letters stay on drawtext (neon/wave). emoji_input_start = first ffmpeg input index
    reserved for emoji stills (1 when video is input 0).
    """
    empty = TextOverlayFilterBuild(
        graph=f"[{input_label}]null[outv]", emoji_input_argv=[], emoji_count=0
    )
    if not overlay.lines or not overlay.char_lines:
        return empty
    enable = overlay_enable_expr(
        from_middle=overlay.from_middle,
        chunk_start_frame=start_frame,
        chunk_frame_count=frame_count,
        total_frames=total_frames,
        fps=fps,
        total_duration_sec=total_duration_sec,
        enable_after_sec=overlay.enable_after_sec,
    )
    if enable == "__skip__":
        return empty
    enable_part = f":enable='{enable}'" if enable else ""
    fill = _hex_to_ffmpeg_color(overlay.text_color, 1.0)
    emoji_fill = _hex_to_ffmpeg_color("#FFFFFF", 1.0)
    glow_layers: list[tuple[int, int, float]] = []
    if overlay.glow_enabled:
        glow_layers = neon_glow_layers(
            overlay.font_size, max_layers=FFMPEG_GLOW_MAX_LAYERS
        )
    mono_emoji_font = resolve_mono_emoji_font_path()
    emoji_font = mono_emoji_font or resolve_emoji_font_path()
    em_size = max(8, int(overlay.font_size))
    em_dur = _emoji_stream_duration_sec(
        start_frame=start_frame,
        frame_count=frame_count,
        total_frames=total_frames,
        fps=fps,
        total_duration_sec=total_duration_sec,
    )
    def _font_part_for(unit: str) -> str:
        path = overlay.font_path
        if emoji_font and is_emoji_unit(unit):
            path = emoji_font
        if not path:
            return ""
        return f"fontfile='{_ffmpeg_escape_path(path)}':"

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
        return empty

    # One ffmpeg input + label per emoji use (never reuse a pad — that overlays
    # a tiny copy of the main frame instead of the PNG on the 2nd+ emoji).
    png_list: list[Path] = []
    glyph_em: list[Optional[int]] = []
    for _base_y, ch, _cx, _cg in glyphs:
        if not is_emoji_unit(ch):
            glyph_em.append(None)
            continue
        png = ensure_twemoji_png(ch)
        if png is None:
            glyph_em.append(None)
            continue
        glyph_em.append(len(png_list))
        png_list.append(png)

    prep: list[str] = []
    for i, _png in enumerate(png_list):
        inp = emoji_input_start + i
        # Twemoji is pal8 — convert before scale; force exact WxH box.
        prep.append(
            f"[{inp}:v]format=rgba,scale={em_size}:{em_size}:flags=lanczos,"
            f"setsar=1,format=rgba[__em{i}]"
        )

    parts: list[str] = []
    cur = input_label
    for step, ((base_y, ch, cx, cg), em_i) in enumerate(zip(glyphs, glyph_em)):
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

        if em_i is not None:
            y_ov = _y_expr_with_offset(y_expr, 0)
            y_inner = y_ov[1:-1] if y_ov.startswith("'") and y_ov.endswith("'") else y_ov
            parts.append(
                f"[{label}][__em{em_i}]overlay=x={cx}:y='{y_inner}':"
                f"format=auto:eof_action=repeat{enable_part}[{nxt}]"
            )
            cur = nxt
            continue

        esc = _ffmpeg_escape_text(ch)
        is_emoji = bool(emoji_font and is_emoji_unit(ch))
        font_part = _font_part_for(ch)
        glyph_fill = emoji_fill if is_emoji else fill
        glyph_glow = [] if is_emoji else glow_layers
        for gi, (dx, dy, alpha) in enumerate(glyph_glow):
            col = _hex_to_ffmpeg_color(overlay.glow_color, alpha)
            gl = f"tg{step}_{gi}"
            layer = (
                f"drawtext={font_part}text='{esc}':fontsize={overlay.font_size}:"
                f"y_align=font:x={cx + dx}:y={_y_expr_with_offset(y_expr, dy)}:fontcolor={col}:borderw=0"
                f"{enable_part}"
            )
            parts.append(f"[{label}]{layer}[{gl}]")
            label = gl
        core = (
            f"drawtext={font_part}text='{esc}':fontsize={overlay.font_size}:"
            f"y_align=font:x={cx}:y={_y_expr_with_offset(y_expr, 0)}:fontcolor={glyph_fill}:borderw=0"
            f"{enable_part}"
        )
        parts.append(f"[{label}]{core}[{nxt}]")
        cur = nxt

    graph_body = ";".join(parts)
    graph = ";".join([*prep, graph_body]) if prep else graph_body
    return TextOverlayFilterBuild(
        graph=graph,
        emoji_input_argv=_emoji_still_input_argv(
            png_list, duration_sec=em_dur, fps=float(fps)
        ),
        emoji_count=len(png_list),
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
    fps: float = 30.0,
    total_duration_sec: float | None = None,
    enable_after_sec: float | None = None,
) -> Optional[str]:
    """None = always on; '__skip__' = no text in this chunk."""
    fps_v = max(float(fps), 1e-6)
    if total_duration_sec is not None and total_duration_sec > 0:
        total_t = float(total_duration_sec)
    else:
        total_t = int(total_frames) / fps_v if total_frames > 0 else 0.0

    start_t: float | None = None
    if enable_after_sec is not None:
        start_t = max(0.0, float(enable_after_sec))
    elif from_middle and total_t > 0:
        start_t = total_t / 2.0
    else:
        return None

    chunk_start_t = int(chunk_start_frame) / fps_v
    chunk_end_t = (int(chunk_start_frame) + int(chunk_frame_count)) / fps_v
    if (
        total_duration_sec is not None
        and total_duration_sec > 0
        and int(chunk_start_frame) == 0
        and int(chunk_frame_count) >= int(total_frames)
    ):
        chunk_end_t = float(total_duration_sec)
    if chunk_end_t <= start_t + 1e-9:
        return "__skip__"
    # После trim+setpts в чанке t=0 в начале фрагмента; для целого ролика — абсолютное время.
    local_start_t = start_t - chunk_start_t
    if local_start_t <= 1e-9:
        return None
    return f"gte(t\\,{local_start_t:.6f})"
