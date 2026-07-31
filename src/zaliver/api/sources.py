"""Browse and upload under the managed sources root (path-sandboxed)."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS

# Upload / stored filenames only.
_SAFE_NAME = re.compile(r"[^\w.\- ()\[\]]+", re.UNICODE)
# Relative path segments: no separators, drives, or traversal.
_SAFE_SEGMENT = re.compile(r"^[\w.\- ()\[\]]+$", re.UNICODE)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB per file
MAX_UPLOAD_FILES = 50
MAX_DELETE_PATHS = 200
MAX_DOWNLOAD_PATHS = 100


def sanitize_filename(name: str) -> str:
    base = Path(name or "").name.strip() or "file"
    # Drop any directory components smuggled in the filename.
    base = base.replace("\\", "/").split("/")[-1]
    if "\x00" in base:
        raise ValueError("Invalid filename")
    cleaned = _SAFE_NAME.sub("_", base).strip(" ._")
    if (
        cleaned in {".", ".."}
        or cleaned.strip(".") == ""
        or cleaned.upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            "COM1",
            "LPT1",
        }
    ):
        cleaned = f"file_{cleaned}" if cleaned.strip(".") else "file_dots"
    return cleaned or "file"


def assert_under_root(root: Path, candidate: Path) -> Path:
    """Ensure candidate is the root or a real descendant (after resolve)."""
    root_res = root.resolve()
    cand_res = candidate.resolve()
    try:
        cand_res.relative_to(root_res)
    except ValueError as e:
        raise ValueError("Path outside sources root") from e
    # Extra belt-and-suspenders on platforms where relative_to is quirky.
    try:
        common = os.path.commonpath([str(root_res), str(cand_res)])
    except ValueError as e:
        raise ValueError("Path outside sources root") from e
    if Path(common).resolve() != root_res:
        raise ValueError("Path outside sources root")
    return cand_res


def _assert_safe_segment(raw: str, *, kind: str = "path segment") -> None:
    """Reject traversal, all-dots names, trailing dots/spaces, drives, bad chars."""
    if not raw:
        raise ValueError(f"Invalid {kind}")
    if raw in {".", ".."} or raw.strip(".") == "":
        # ".", "..", "...", "...." — never useful as a real folder/file name here.
        raise ValueError(f"Invalid {kind}: {raw!r}")
    if raw.endswith(".") or raw.endswith(" "):
        # Windows strips trailing dots/spaces; reject to avoid surprising renames.
        raise ValueError(f"Invalid {kind}: {raw!r}")
    if raw.startswith("/") or ":" in raw:
        raise ValueError("Absolute path segments are not allowed")
    if not _SAFE_SEGMENT.match(raw):
        raise ValueError(f"Invalid {kind}: {raw!r}")


def _validate_rel_segments(rel: str) -> list[str]:
    if "\x00" in (rel or ""):
        raise ValueError("Invalid path")
    raw_in = (rel or "").replace("\\", "/")
    # Do not silently reinterpret absolute / UNC-looking inputs as relative.
    if raw_in.startswith("/") or raw_in.startswith("//"):
        raise ValueError("Absolute path segments are not allowed")
    if len(raw_in) >= 2 and raw_in[1] == ":":
        raise ValueError("Absolute path segments are not allowed")
    rel_norm = raw_in.strip("/")
    if not rel_norm:
        return []
    parts: list[str] = []
    for raw in rel_norm.split("/"):
        # Do not skip empty / "." — treat as invalid (blocks a//b and .... tricks).
        if not raw:
            raise ValueError("Invalid path segment")
        if raw == ".":
            raise ValueError("Invalid path segment: '.'")
        if raw == "..":
            raise ValueError("Path escapes sources root")
        _assert_safe_segment(raw)
        parts.append(raw)
    return parts


def resolve_sources_rel(sources_root: Path, rel: str = "") -> Path:
    """Resolve a relative path under sources_root; raise ValueError if escapes."""
    root = sources_root.resolve()
    parts = _validate_rel_segments(rel)
    candidate = root.joinpath(*parts) if parts else root
    return assert_under_root(root, candidate)


def _entry_created_ts(path: Path) -> float:
    """Best-effort creation timestamp (birth time / Windows ctime / mtime)."""
    try:
        st = path.stat()
    except OSError:
        return 0.0
    birth = getattr(st, "st_birthtime", None)
    if birth is not None:
        return float(birth)
    if os.name == "nt":
        return float(st.st_ctime)
    return float(st.st_mtime)


def _format_created_at(ts: float) -> str | None:
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
    except (OverflowError, OSError, ValueError):
        return None


def list_sources(
    sources_root: Path,
    *,
    rel: str = "",
    kind: str = "media",
) -> dict:
    """
    List directory entries under sources_root/rel.

    kind: "media" | "video" | "audio" | "all"
    Entries are sorted newest-first (directories before files).
    """
    root = sources_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    cur = resolve_sources_rel(root, rel)
    if not cur.is_dir():
        raise FileNotFoundError(f"Not a directory: {rel}")
    # Do not list through a symlink that escaped (resolve already checked).
    if cur.is_symlink():
        raise ValueError("Symlinked directories are not browsable")

    if kind == "video":
        allow = VIDEO_EXTS
    elif kind == "audio":
        allow = AUDIO_EXTS
    elif kind == "all":
        allow = None
    else:
        allow = MEDIA_EXTS

    entries: list[dict] = []
    try:
        children = list(cur.iterdir())
    except OSError as e:
        raise OSError(f"Cannot list directory: {e}") from e

    for child in children:
        name = child.name
        if name.startswith("."):
            continue
        # Skip symlinks entirely — prevents leaking / deleting outside targets.
        try:
            if child.is_symlink():
                continue
        except OSError:
            continue
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        try:
            child_rel = child.relative_to(root).as_posix()
            assert_under_root(root, child)
        except ValueError:
            continue
        created_ts = _entry_created_ts(child)
        created_at = _format_created_at(created_ts)
        if is_dir:
            entries.append(
                {
                    "name": name,
                    "path": child_rel,
                    "is_dir": True,
                    "size": None,
                    "abs_path": None,
                    "created_at": created_at,
                    "_sort_ts": created_ts,
                }
            )
            continue
        ext = child.suffix.lower()
        if allow is not None and ext not in allow:
            continue
        try:
            size = int(child.stat().st_size)
        except OSError:
            size = None
        entries.append(
            {
                "name": name,
                "path": child_rel,
                "is_dir": False,
                "size": size,
                "abs_path": str(child.resolve()),
                "created_at": created_at,
                "_sort_ts": created_ts,
            }
        )

    entries.sort(
        key=lambda e: (
            0 if e["is_dir"] else 1,
            -float(e.get("_sort_ts") or 0),
            str(e["name"]).lower(),
        )
    )
    for e in entries:
        e.pop("_sort_ts", None)

    cur_rel = "" if cur == root else cur.relative_to(root).as_posix()
    parent = None
    if cur_rel:
        parent_path = Path(cur_rel).parent
        parent = "" if str(parent_path) == "." else parent_path.as_posix()

    disk_total = disk_used = disk_free = None
    try:
        usage = shutil.disk_usage(cur)
        disk_total = int(usage.total)
        disk_used = int(usage.used)
        disk_free = int(usage.free)
    except OSError:
        pass

    return {
        "root": str(root),
        "path": cur_rel,
        "parent": parent,
        "entries": entries,
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_free": disk_free,
    }


def abs_paths_for_rels(sources_root: Path, rels: list[str]) -> list[str]:
    out: list[str] = []
    for rel in rels:
        p = resolve_sources_rel(sources_root, rel)
        if p.is_symlink() or not p.is_file():
            raise FileNotFoundError(f"File not found: {rel}")
        out.append(str(p))
    return out


def save_upload(
    sources_root: Path,
    *,
    filename: str,
    data: bytes,
    subdir: str = "uploads",
) -> tuple[str, str]:
    """
    Save bytes under sources_root/subdir/.
    Returns (absolute_path, relative_posix_path).
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("File too large")
    root = sources_root.resolve()
    dest_dir = resolve_sources_rel(root, subdir)
    if dest_dir.is_symlink():
        raise ValueError("Cannot upload into a symlink")
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = sanitize_filename(filename)
    ext = Path(safe).suffix.lower()
    if ext not in MEDIA_EXTS:
        raise ValueError(f"File type not allowed: {ext or '(none)'}")
    stem = Path(safe).stem
    unique = f"{stem}_{uuid.uuid4().hex[:10]}{ext}"
    dest = dest_dir / unique
    dest = assert_under_root(root, dest)
    # Exclusive create — do not follow/truncate an existing symlink or file.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(dest, flags, 0o644)
    except FileExistsError as e:
        raise ValueError("Upload target already exists") from e
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    rel = dest.relative_to(root).as_posix()
    return str(dest.resolve()), rel


def resolve_download_files(
    sources_root: Path, rels: list[str]
) -> list[tuple[Path, str]]:
    """
    Resolve relative paths to regular files under sources_root.
    Returns list of (absolute_path, relative_posix). Directories are skipped.
    """
    import stat as stat_mod

    if len(rels) > MAX_DOWNLOAD_PATHS:
        raise ValueError(f"Too many paths (max {MAX_DOWNLOAD_PATHS})")
    root = sources_root.resolve()
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for rel in rels:
        rel_s = (rel or "").strip()
        if not rel_s:
            continue
        target = resolve_sources_rel(root, rel_s)
        if target == root:
            raise ValueError("Cannot download sources root")
        try:
            st = os.lstat(target)
        except FileNotFoundError as e:
            raise FileNotFoundError(f"Not found: {rel_s}") from e
        except OSError as e:
            raise OSError(f"Cannot stat {rel_s}: {e}") from e
        if stat_mod.S_ISLNK(st.st_mode):
            raise ValueError(f"Refusing to download symlink: {rel_s}")
        if stat_mod.S_ISDIR(st.st_mode):
            continue
        if not stat_mod.S_ISREG(st.st_mode):
            raise ValueError(f"Refusing to download special file: {rel_s}")
        assert_under_root(root, target)
        arc = target.relative_to(root).as_posix()
        if arc in seen:
            continue
        seen.add(arc)
        out.append((target, arc))
    if not out:
        raise ValueError("No files to download (select files, not only folders)")
    return out


def mkdir_sources(
    sources_root: Path,
    *,
    parent: str = "",
    name: str,
) -> str:
    """
    Create a directory under sources_root/parent/name.
    Returns relative posix path of the new directory.
    """
    raw_name = (name or "").strip()
    if not raw_name:
        raise ValueError("Folder name is required")
    # Single segment only — no nested paths in the name.
    if "/" in raw_name or "\\" in raw_name or "\x00" in raw_name:
        raise ValueError("Invalid folder name")
    _assert_safe_segment(raw_name, kind="folder name")

    root = sources_root.resolve()
    parent_dir = resolve_sources_rel(root, parent)
    if not parent_dir.is_dir():
        raise FileNotFoundError(f"Parent folder not found: {parent or '/'}")
    if parent_dir.is_symlink():
        raise ValueError("Cannot create folder under a symlink")

    dest = assert_under_root(root, parent_dir / raw_name)
    if dest.exists():
        raise ValueError(f"Already exists: {raw_name}")
    try:
        dest.mkdir(parents=False, exist_ok=False)
    except FileExistsError as e:
        raise ValueError(f"Already exists: {raw_name}") from e
    except OSError as e:
        raise OSError(f"Cannot create folder: {e}") from e
    return dest.relative_to(root).as_posix()


def delete_sources(sources_root: Path, rels: list[str]) -> int:
    """Delete files/dirs by relative paths under sources_root. Returns count removed."""
    import stat as stat_mod

    if len(rels) > MAX_DELETE_PATHS:
        raise ValueError(f"Too many paths (max {MAX_DELETE_PATHS})")
    root = sources_root.resolve()
    removed = 0
    for rel in rels:
        rel_s = (rel or "").strip()
        if not rel_s:
            continue
        # Resolve containment using the path as given (may follow final link for
        # location check), then re-check with lstat before mutating.
        target = resolve_sources_rel(root, rel_s)
        if target == root:
            raise ValueError("Cannot delete sources root")
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            continue
        except OSError as e:
            raise OSError(f"Cannot stat {rel_s}: {e}") from e

        # Symlink / junction: remove the link only, never the target tree.
        if stat_mod.S_ISLNK(st.st_mode):
            os.unlink(target)
            removed += 1
            continue

        # Contained real path check (non-symlink).
        assert_under_root(root, target)

        if stat_mod.S_ISDIR(st.st_mode):
            shutil.rmtree(target)
        elif stat_mod.S_ISREG(st.st_mode):
            os.unlink(target)
        else:
            # Refuse special files (devices, fifos, etc.)
            raise ValueError(f"Refusing to delete special file: {rel_s}")
        removed += 1
    return removed
