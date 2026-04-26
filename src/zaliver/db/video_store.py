from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from zaliver.processing.ffmpeg_merge import resolve_ffmpeg_executable


def _app_data_dir() -> Path:
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
        if root:
            return Path(root) / "Zaliver"
    return Path.home() / ".zaliver"


def _iso_from_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def _file_created_iso(p: Path) -> str:
    st = p.stat()
    # On Windows, st_ctime is creation time. On Unix, it is metadata-change time,
    # so we fallback to mtime there as a more meaningful proxy.
    ts = st.st_ctime if sys.platform == "win32" else st.st_mtime
    return _iso_from_timestamp(ts)


def _popen_flags() -> int:
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _ensure_thumb(video_path: Path, thumbs_dir: Path) -> Optional[Path]:
    try:
        thumbs_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    exe = resolve_ffmpeg_executable()
    if not exe:
        return None

    try:
        key = hashlib.sha256(str(video_path.resolve()).encode("utf-8")).hexdigest()[:32]
    except OSError:
        key = hashlib.sha256(str(video_path).encode("utf-8")).hexdigest()[:32]
    out = thumbs_dir / f"{key}.jpg"
    if out.is_file():
        return out

    # Grab a frame around 1 second; scale to UI-friendly width.
    cmd = [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "00:00:01.000",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=320:-2",
        "-q:v",
        "4",
        str(out),
    ]
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            creationflags=_popen_flags(),
        )
    except Exception:
        return None
    if p.returncode != 0:
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return out if out.is_file() else None


@dataclass(frozen=True)
class StoredVideo:
    id: int
    path: str
    created_at: str
    added_at: str
    thumb_path: str | None


class VideoStore:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        base = _app_data_dir()
        self._db_path = (db_path or (base / "zaliver.sqlite")).expanduser()
        self._thumbs_dir = self._db_path.parent / "thumbs"
        self._use_memory = False
        self._mem_con: sqlite3.Connection | None = None
        try:
            self._init()
        except (OSError, sqlite3.Error):
            # If we cannot create/open a persistent DB (permissions, invalid path, etc.),
            # fall back to an in-memory DB so the UI can still start.
            self._use_memory = True
            self._init()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        if self._use_memory:
            if self._mem_con is None:
                self._mem_con = sqlite3.connect(":memory:")
                self._mem_con.row_factory = sqlite3.Row
                self._mem_con.execute("PRAGMA foreign_keys=ON;")
            return self._mem_con
        else:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(str(self._db_path))
        con.row_factory = sqlite3.Row
        # WAL is not available for in-memory databases.
        if not self._use_memory:
            con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        return con

    def _init(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    thumb_path TEXT
                );
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_videos_added_at ON videos(added_at DESC);"
            )

    def upsert_video(self, video_path: str) -> None:
        p = Path(str(video_path)).expanduser()
        try:
            if not p.is_file():
                return
        except OSError:
            return

        created_at = _file_created_iso(p)
        thumb = _ensure_thumb(p, self._thumbs_dir)
        thumb_s = str(thumb) if thumb is not None else None

        with self._connect() as con:
            con.execute(
                """
                INSERT INTO videos(path, created_at, thumb_path)
                VALUES(?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    created_at=excluded.created_at,
                    thumb_path=COALESCE(excluded.thumb_path, videos.thumb_path);
                """,
                (str(p), created_at, thumb_s),
            )

    def list_videos(self, limit: int = 500) -> list[StoredVideo]:
        lim = max(1, int(limit))
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT id, path, created_at, added_at, thumb_path
                FROM videos
                ORDER BY added_at DESC, id DESC
                LIMIT ?;
                """,
                (lim,),
            ).fetchall()
        out: list[StoredVideo] = []
        for r in rows:
            out.append(
                StoredVideo(
                    id=int(r["id"]),
                    path=str(r["path"]),
                    created_at=str(r["created_at"]),
                    added_at=str(r["added_at"]),
                    thumb_path=str(r["thumb_path"]) if r["thumb_path"] else None,
                )
            )
        return out

    def remove_video_record(self, video_id: int) -> bool:
        """Удалить запись из каталога. Файл видео на диске не трогаем; миниатюра в кэше — удаляется."""
        vid = int(video_id)
        with self._connect() as con:
            row = con.execute(
                "SELECT thumb_path FROM videos WHERE id=?;", (vid,)
            ).fetchone()
            if row is None:
                return False
            tp = row["thumb_path"]
            con.execute("DELETE FROM videos WHERE id=?;", (vid,))
        if tp:
            try:
                Path(str(tp)).unlink(missing_ok=True)
            except OSError:
                pass
        return True

    def remove_video_records(self, video_ids: Iterable[int]) -> int:
        """Удалить несколько записей за один проход. Файлы видео не трогаем; миниатюры из кэша — по возможности."""
        ids = sorted({int(x) for x in video_ids})
        if not ids:
            return 0
        removed = 0
        with self._connect() as con:
            for vid in ids:
                row = con.execute(
                    "SELECT thumb_path FROM videos WHERE id=?;", (vid,)
                ).fetchone()
                if row is None:
                    continue
                tp = row["thumb_path"]
                con.execute("DELETE FROM videos WHERE id=?;", (vid,))
                removed += 1
                if tp:
                    try:
                        Path(str(tp)).unlink(missing_ok=True)
                    except OSError:
                        pass
        return removed

    def prune_missing_files(self) -> int:
        removed = 0
        with self._connect() as con:
            rows = con.execute("SELECT id, path, thumb_path FROM videos;").fetchall()
            for r in rows:
                vid = Path(str(r["path"]))
                exists = False
                try:
                    exists = vid.is_file()
                except OSError:
                    exists = False
                if exists:
                    continue
                con.execute("DELETE FROM videos WHERE id=?;", (int(r["id"]),))
                removed += 1
                tp = r["thumb_path"]
                if tp:
                    try:
                        Path(str(tp)).unlink(missing_ok=True)
                    except OSError:
                        pass
        return removed

    def ensure_thumbs(self, paths: Iterable[str]) -> None:
        for p in paths:
            try:
                self.upsert_video(str(p))
            except Exception:
                continue

