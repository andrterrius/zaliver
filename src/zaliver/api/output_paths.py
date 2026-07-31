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


def managed_output_rel(*, platform: str, kind: str) -> str:
    """Relative path under output root: ``<platform>/<kind>``."""
    plat = normalize_platform(platform)
    k = (kind or "").strip().lower()
    if k not in OUTPUT_KINDS:
        raise ValueError(f"Unknown output kind: {kind!r}")
    return f"{plat}/{k}"


def resolve_job_output_dir(
    output_root: Path,
    *,
    platform: str,
    kind: str,
    requested: str | None = None,
    create: bool = True,
) -> Path:
    """
    Resolve job output directory.

    Empty ``requested`` → managed ``<platform>/<kind>``.
    Otherwise path must be that directory or a subdirectory of it
    (absolute path, or relative under the output root).
    """
    from zaliver.api.sources import assert_under_root, resolve_sources_rel

    base = resolve_managed_output_dir(
        output_root, platform=platform, kind=kind, create=True
    )
    raw = (requested or "").strip()
    if not raw:
        return base

    root = Path(output_root).resolve()
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        cand = assert_under_root(root, candidate)
    else:
        cand = resolve_sources_rel(root, raw)

    try:
        cand.relative_to(base)
    except ValueError as e:
        raise ValueError(
            f"output_dir must be under the results folder for this job: {base}"
        ) from e

    if create:
        cand.mkdir(parents=True, exist_ok=True)
    if not cand.is_dir():
        raise ValueError(f"output_dir is not a directory: {cand}")
    return cand.resolve()


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
