"""Managed output directories: {root}/{platform}/{kind}/."""

from __future__ import annotations

from pathlib import Path

from zaliver.config.platform_settings import normalize_platform

# Folder names under the platform directory.
OUTPUT_KIND_UNIQUIFY = "uniquify"
OUTPUT_KIND_SLICING = "slicing"
OUTPUT_KIND_GLUING = "gluing"  # stitching / склейка

OUTPUT_KINDS: tuple[str, ...] = (
    OUTPUT_KIND_UNIQUIFY,
    OUTPUT_KIND_SLICING,
    OUTPUT_KIND_GLUING,
)


def resolve_managed_output_dir(
    output_root: Path,
    *,
    platform: str,
    kind: str,
    create: bool = True,
) -> Path:
    """Return ``output_root / <platform> / <kind>`` and optionally create it."""
    plat = normalize_platform(platform)
    k = (kind or "").strip().lower()
    if k not in OUTPUT_KINDS:
        raise ValueError(f"Unknown output kind: {kind!r}")
    path = Path(output_root) / plat / k
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def list_managed_output_dirs(output_root: Path, *, platform: str) -> dict[str, str]:
    """Map kind -> absolute path for the current platform (dirs created)."""
    plat = normalize_platform(platform)
    out: dict[str, str] = {}
    for kind in OUTPUT_KINDS:
        out[kind] = str(
            resolve_managed_output_dir(
                output_root, platform=plat, kind=kind, create=True
            )
        )
    return out
