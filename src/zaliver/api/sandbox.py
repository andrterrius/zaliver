"""Path sandbox: only allow reads/writes under configured roots."""

from __future__ import annotations

from pathlib import Path

# Absolute paths that must never be reachable via library / job path APIs.
_DENIED_ROOTS: list[Path] = []


class PathNotAllowedError(ValueError):
    pass


def register_denied_root(path: Path) -> None:
    """Mark a directory (e.g. private credentials) as unreachable via sandbox."""
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except OSError:
        resolved = Path(path).expanduser()
    if resolved not in _DENIED_ROOTS:
        _DENIED_ROOTS.append(resolved)


def _is_denied(resolved: Path) -> bool:
    for denied in _DENIED_ROOTS:
        try:
            resolved.relative_to(denied)
            return True
        except ValueError:
            continue
        except OSError:
            continue
    return False


def resolve_under_roots(path: str | Path, roots: list[Path]) -> Path:
    """Resolve path and ensure it is inside one of the allowed roots."""
    raw = Path(str(path)).expanduser()
    try:
        resolved = raw.resolve(strict=False)
    except OSError as e:
        raise PathNotAllowedError(f"Invalid path: {path}") from e

    if _is_denied(resolved):
        raise PathNotAllowedError("Path is in a protected private directory")

    if not roots:
        raise PathNotAllowedError("No allowed roots configured (ZALIVER_ALLOWED_ROOTS).")

    for root in roots:
        try:
            root_res = root.resolve(strict=False)
        except OSError:
            root_res = root
        try:
            resolved.relative_to(root_res)
            return resolved
        except ValueError:
            continue
    raise PathNotAllowedError(
        f"Path outside allowed roots: {resolved}. "
        f"Allowed: {', '.join(str(r) for r in roots)}"
    )


def resolve_existing_file(path: str | Path, roots: list[Path]) -> Path:
    p = resolve_under_roots(path, roots)
    if not p.is_file():
        raise PathNotAllowedError(f"File not found (or not a file): {p}")
    return p


def resolve_dir(path: str | Path, roots: list[Path], *, create: bool = False) -> Path:
    p = resolve_under_roots(path, roots)
    if create:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise PathNotAllowedError(f"Cannot create directory: {p}") from e
    if not p.is_dir():
        raise PathNotAllowedError(f"Directory not found: {p}")
    return p


def resolve_path_list(paths: list[str], roots: list[Path]) -> list[str]:
    return [str(resolve_existing_file(p, roots)) for p in paths]
