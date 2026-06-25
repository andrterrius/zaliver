"""Поиск и вырезание аватарок из спрайт-листа по пикселям."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage


@dataclass(frozen=True)
class AvatarBox:
    """Прямоугольник обрезки (включительно по правому/нижнему краю)."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left + 1

    @property
    def height(self) -> int:
        return self.bottom - self.top + 1

    def with_padding(self, pad: int, max_w: int, max_h: int) -> AvatarBox:
        return AvatarBox(
            left=max(0, self.left - pad),
            top=max(0, self.top - pad),
            right=min(max_w - 1, self.right + pad),
            bottom=min(max_h - 1, self.bottom + pad),
        )

    def as_pil_crop(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right + 1, self.bottom + 1)


def load_rgba_image(path: Path) -> Image.Image:
    with Image.open(path) as img:
        return img.convert("RGBA")


def _estimate_background_color(rgba: np.ndarray, border: int = 8) -> np.ndarray:
    h, w = rgba.shape[:2]
    b = min(border, h // 4, w // 4, max(h, w))
    if b < 1:
        return np.median(rgba.reshape(-1, 4), axis=0)[:3]

    strips = [
        rgba[:b, :, :3].reshape(-1, 3),
        rgba[-b:, :, :3].reshape(-1, 3),
        rgba[:, :b, :3].reshape(-1, 3),
        rgba[:, -b:, :3].reshape(-1, 3),
    ]
    border_pixels = np.vstack(strips)
    return np.median(border_pixels, axis=0)


def _foreground_mask(
    rgba: np.ndarray,
    bg_rgb: np.ndarray,
    *,
    color_threshold: float,
    alpha_threshold: int,
) -> np.ndarray:
    rgb = rgba[:, :, :3].astype(np.float32)
    dist = np.linalg.norm(rgb - bg_rgb.astype(np.float32), axis=2)
    mask = dist > color_threshold
    if rgba.shape[2] == 4:
        mask &= rgba[:, :, 3] > alpha_threshold
    return mask


def _filter_component(
    bbox: tuple[int, int, int, int],
    area: int,
    image_area: int,
    *,
    min_side: int,
    max_side: int,
    min_area: int,
    max_area_ratio: float,
    max_aspect_ratio: float,
) -> bool:
    top, left, bottom, right = bbox
    width = right - left + 1
    height = bottom - top + 1
    if width < min_side or height < min_side:
        return False
    if width > max_side or height > max_side:
        return False
    if area < min_area:
        return False
    if area > image_area * max_area_ratio:
        return False
    aspect = max(width, height) / max(1, min(width, height))
    if aspect > max_aspect_ratio:
        return False
    return True


def _filter_by_dominant_size(
    boxes: list[AvatarBox],
    *,
    tolerance: float = 0.22,
    bin_size: int = 8,
    min_count: int = 2,
) -> list[AvatarBox]:
    if len(boxes) < min_count:
        return boxes

    sides = np.array([max(box.width, box.height) for box in boxes], dtype=np.float32)
    noise_ceiling = float(np.percentile(sides, 30))
    candidates = sides[sides >= max(20.0, noise_ceiling * 0.55)]
    if len(candidates) < min_count:
        candidates = sides

    bins = np.round(candidates / bin_size).astype(int)
    unique, counts = np.unique(bins, return_counts=True)
    scores = counts.astype(np.float64) * (unique.astype(np.float64) * bin_size) ** 2
    target = float(unique[np.argmax(scores)] * bin_size)

    lo = target * (1.0 - tolerance)
    hi = target * (1.0 + tolerance)
    return [box for box in boxes if lo <= max(box.width, box.height) <= hi]


def _merge_overlapping_boxes(boxes: list[AvatarBox], gap: int) -> list[AvatarBox]:
    if not boxes:
        return []

    merged: list[AvatarBox] = []
    used = [False] * len(boxes)

    for i, box in enumerate(boxes):
        if used[i]:
            continue
        left, top, right, bottom = box.left, box.top, box.right, box.bottom
        changed = True
        while changed:
            changed = False
            for j, other in enumerate(boxes):
                if used[j] or i == j:
                    continue
                if (
                    other.left <= right + gap
                    and other.right >= left - gap
                    and other.top <= bottom + gap
                    and other.bottom >= top - gap
                ):
                    left = min(left, other.left)
                    top = min(top, other.top)
                    right = max(right, other.right)
                    bottom = max(bottom, other.bottom)
                    used[j] = True
                    changed = True
        used[i] = True
        merged.append(AvatarBox(left, top, right, bottom))

    return merged


def detect_avatars(
    rgba: np.ndarray,
    *,
    color_threshold: float = 28.0,
    alpha_threshold: int = 128,
    min_side: int = 16,
    max_side: int = 0,
    min_area: int = 200,
    max_area_ratio: float = 0.25,
    max_aspect_ratio: float = 1.35,
    morph_size: int = 0,
    merge_gap: int = 0,
    adaptive_size: bool = True,
    size_tolerance: float = 0.22,
    square: bool = False,
) -> list[AvatarBox]:
    h, w = rgba.shape[:2]
    if max_side <= 0:
        max_side = max(h, w)

    bg = _estimate_background_color(rgba)
    mask = _foreground_mask(
        rgba,
        bg,
        color_threshold=color_threshold,
        alpha_threshold=alpha_threshold,
    )

    if morph_size > 1:
        structure = np.ones((morph_size, morph_size), dtype=bool)
        mask = ndimage.binary_closing(mask, structure=structure)
        mask = ndimage.binary_opening(mask, structure=structure)

    labeled, count = ndimage.label(mask)
    if count == 0:
        return []

    image_area = h * w
    boxes: list[AvatarBox] = []
    for _label_id, slices in enumerate(ndimage.find_objects(labeled), start=1):
        if slices is None:
            continue
        component = labeled == _label_id
        area = int(component.sum())
        row_slice, col_slice = slices
        top, bottom = row_slice.start, row_slice.stop - 1
        left, right = col_slice.start, col_slice.stop - 1
        if not _filter_component(
            (top, left, bottom, right),
            area,
            image_area,
            min_side=min_side,
            max_side=max_side,
            min_area=min_area,
            max_area_ratio=max_area_ratio,
            max_aspect_ratio=max_aspect_ratio,
        ):
            continue
        boxes.append(AvatarBox(left, top, right, bottom))

    if adaptive_size:
        boxes = _filter_by_dominant_size(boxes, tolerance=size_tolerance)
    if merge_gap > 0:
        boxes = _merge_overlapping_boxes(boxes, merge_gap)

    if square:
        squared: list[AvatarBox] = []
        for box in boxes:
            side = max(box.width, box.height)
            cx = (box.left + box.right) // 2
            cy = (box.top + box.bottom) // 2
            half = side // 2
            left = max(0, cx - half)
            top = max(0, cy - half)
            right = min(w - 1, left + side - 1)
            bottom = min(h - 1, top + side - 1)
            if right - left + 1 < side:
                left = max(0, right - side + 1)
            if bottom - top + 1 < side:
                top = max(0, bottom - side + 1)
            squared.append(AvatarBox(left, top, right, bottom))
        boxes = squared

    boxes.sort(key=lambda b: (b.top, b.left))
    return boxes


def draw_preview(
    image: Image.Image,
    boxes: list[AvatarBox],
    *,
    line_width: int = 2,
) -> Image.Image:
    preview = image.convert("RGBA").copy()
    draw = ImageDraw.Draw(preview)
    palette = [
        (255, 64, 64, 255),
        (64, 200, 120, 255),
        (64, 140, 255, 255),
        (255, 180, 40, 255),
        (200, 90, 255, 255),
        (40, 220, 220, 255),
    ]

    try:
        font = ImageFont.truetype("arial.ttf", max(12, min(image.width, image.height) // 40))
    except OSError:
        font = ImageFont.load_default()

    for idx, box in enumerate(boxes, start=1):
        color = palette[(idx - 1) % len(palette)]
        for offset in range(line_width):
            draw.rectangle(
                (
                    box.left - offset,
                    box.top - offset,
                    box.right + offset,
                    box.bottom + offset,
                ),
                outline=color,
            )
        label = str(idx)
        tx, ty = box.left + 4, box.top + 4
        draw.rectangle((tx - 2, ty - 2, tx + 18, ty + 16), fill=(0, 0, 0, 160))
        draw.text((tx, ty), label, fill=(255, 255, 255, 255), font=font)

    return preview


def _image_to_png_bytes(image: Image.Image) -> bytes:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def extract_avatar_pngs(
    image: Image.Image,
    *,
    padding: int = 2,
    square: bool = True,
    **detect_kwargs,
) -> tuple[list[bytes], list[AvatarBox], Image.Image]:
    """Вырезает аватарки; возвращает PNG-байты, bbox и превью с рамками."""
    rgba = np.array(image.convert("RGBA"))
    h, w = rgba.shape[:2]
    boxes = detect_avatars(rgba, square=square, **detect_kwargs)
    if padding > 0:
        boxes = [box.with_padding(padding, w, h) for box in boxes]

    pngs: list[bytes] = []
    for box in boxes:
        crop = image.convert("RGBA").crop(box.as_pil_crop())
        pngs.append(_image_to_png_bytes(crop))

    preview = draw_preview(image, boxes)
    return pngs, boxes, preview


def extract_avatar_pngs_from_path(
    path: Path,
    *,
    padding: int = 2,
    square: bool = True,
    **detect_kwargs,
) -> tuple[list[bytes], list[AvatarBox], Image.Image]:
    image = load_rgba_image(path)
    return extract_avatar_pngs(image, padding=padding, square=square, **detect_kwargs)


def load_image_file_as_png(path: Path) -> bytes:
    """Загружает файл целиком как PNG без нарезки спрайт-листа."""
    return _image_to_png_bytes(load_rgba_image(path))

