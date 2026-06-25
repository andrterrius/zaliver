#!/usr/bin/env python3
"""CLI-обёртка над детекцией аватарок в zaliver."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from zaliver.ui.avatar_detection import extract_avatar_pngs_from_path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Поиск и вырезание аватарок из большой картинки по пикселям.",
    )
    p.add_argument("image", type=Path, help="Путь к исходной картинке")
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("avatars_out"),
        help="Папка для вырезанных аватарок (по умолчанию: avatars_out)",
    )
    p.add_argument(
        "--preview",
        type=Path,
        default=None,
        help="Куда сохранить превью с рамками (по умолчанию: <output>/<имя>_preview.png)",
    )
    p.add_argument("--padding", type=int, default=2, help="Отступ вокруг bbox при обрезке")
    p.add_argument("--square", action="store_true", help="Делать квадратные bbox")
    p.add_argument(
        "--no-crops",
        action="store_true",
        help="Только превью с рамками, без сохранения вырезанных файлов",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    image_path: Path = args.image
    if not image_path.is_file():
        print(f"Файл не найден: {image_path}", file=sys.stderr)
        return 1

    pngs, boxes, preview = extract_avatar_pngs_from_path(
        image_path,
        padding=args.padding,
        square=args.square,
    )

    preview_path = args.preview or (args.output / f"{image_path.stem}_preview.png")
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview.save(preview_path, format="PNG")

    if not args.no_crops and pngs:
        args.output.mkdir(parents=True, exist_ok=True)
        stem = image_path.stem
        for idx, png in enumerate(pngs, start=1):
            (args.output / f"{stem}_avatar_{idx:03d}.png").write_bytes(png)

    print(f"Найдено аватарок: {len(boxes)}")
    for idx, box in enumerate(boxes, start=1):
        print(f"  #{idx:02d}: x={box.left} y={box.top} w={box.width} h={box.height}")
    print(f"Превью с рамками: {preview_path}")
    if not args.no_crops:
        print(f"Вырезанные файлы: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
