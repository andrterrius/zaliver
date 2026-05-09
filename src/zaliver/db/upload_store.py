from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional


def _app_data_dir() -> Path:
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
        if root:
            return Path(root) / "Zaliver"
    return Path.home() / ".zaliver"


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class UploadSession:
    id: int
    started_at: str
    planned_videos: int
    processed_videos: int
    uploaded_ok: int
    ended_at: str | None
    status: str


@dataclass(frozen=True, slots=True)
class UploadedVideo:
    id: int
    session_id: int
    session_started_at: str | None
    uploaded_at: str
    title: str
    description: str
    url: str
    video_id: str
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    stats_updated_at: str | None


class UploadStore:
    """
    Stores info about successfully uploaded YouTube videos.
    Uses the same SQLite file as the main app (`zaliver.sqlite`) by default.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        base = _app_data_dir()
        self._db_path = (db_path or (base / "zaliver.sqlite")).expanduser()
        self._use_memory = False
        self._mem_con: sqlite3.Connection | None = None
        try:
            self._init()
        except (OSError, sqlite3.Error):
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
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self._db_path))
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        return con

    def _init(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    planned_videos INTEGER NOT NULL DEFAULT 0,
                    processed_videos INTEGER NOT NULL DEFAULT 0,
                    uploaded_ok INTEGER NOT NULL DEFAULT 0,
                    ended_at TEXT,
                    status TEXT NOT NULL DEFAULT 'running'
                );
                """
            )
            # Lightweight migration for existing DBs.
            for stmt in (
                "ALTER TABLE upload_sessions ADD COLUMN planned_videos INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE upload_sessions ADD COLUMN processed_videos INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE upload_sessions ADD COLUMN uploaded_ok INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE upload_sessions ADD COLUMN ended_at TEXT;",
                "ALTER TABLE upload_sessions ADD COLUMN status TEXT NOT NULL DEFAULT 'running';",
            ):
                try:
                    con.execute(stmt)
                except sqlite3.Error:
                    pass

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS uploaded_videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    video_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL DEFAULT '',
                    view_count INTEGER,
                    like_count INTEGER,
                    comment_count INTEGER,
                    stats_updated_at TEXT,
                    UNIQUE(video_id),
                    FOREIGN KEY(session_id) REFERENCES upload_sessions(id) ON DELETE CASCADE
                );
                """
            )
            # Lightweight migration for existing DBs (add profile_id).
            for stmt in (
                "ALTER TABLE uploaded_videos ADD COLUMN profile_id TEXT NOT NULL DEFAULT '';",
            ):
                try:
                    con.execute(stmt)
                except sqlite3.Error:
                    pass
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_uploaded_videos_session ON uploaded_videos(session_id, uploaded_at DESC);"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_uploaded_videos_uploaded_at ON uploaded_videos(uploaded_at DESC);"
            )

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_profile_state (
                    profile_id TEXT PRIMARY KEY,
                    consecutive_upload_errors INTEGER NOT NULL DEFAULT 0,
                    flagged INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT
                );
                """
            )
            for stmt in (
                "ALTER TABLE upload_profile_state ADD COLUMN consecutive_upload_errors INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE upload_profile_state ADD COLUMN flagged INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE upload_profile_state ADD COLUMN last_error TEXT NOT NULL DEFAULT '';",
                "ALTER TABLE upload_profile_state ADD COLUMN updated_at TEXT;",
            ):
                try:
                    con.execute(stmt)
                except sqlite3.Error:
                    pass

    def start_session(self, *, planned_videos: int) -> UploadSession:
        started_at = _utc_now_iso()
        planned = max(0, int(planned_videos))
        with self._connect() as con:
            cur = con.execute(
                """
                INSERT INTO upload_sessions(started_at, planned_videos, status)
                VALUES(?, ?, 'running');
                """,
                (started_at, planned),
            )
            sid = int(cur.lastrowid)
        return UploadSession(
            id=sid,
            started_at=started_at,
            planned_videos=planned,
            processed_videos=0,
            uploaded_ok=0,
            ended_at=None,
            status="running",
        )

    def inc_processed(self, *, session_id: int, delta: int = 1) -> None:
        d = max(0, int(delta))
        if d <= 0:
            return
        with self._connect() as con:
            con.execute(
                """
                UPDATE upload_sessions
                SET processed_videos = processed_videos + ?
                WHERE id=?;
                """,
                (d, int(session_id)),
            )

    def inc_uploaded_ok(self, *, session_id: int, delta: int = 1) -> None:
        d = max(0, int(delta))
        if d <= 0:
            return
        with self._connect() as con:
            con.execute(
                """
                UPDATE upload_sessions
                SET uploaded_ok = uploaded_ok + ?
                WHERE id=?;
                """,
                (d, int(session_id)),
            )

    def finish_session(self, *, session_id: int, status: str) -> None:
        st = (status or "").strip() or "done"
        ended_at = _utc_now_iso()
        with self._connect() as con:
            con.execute(
                """
                UPDATE upload_sessions
                SET ended_at=?, status=?
                WHERE id=?;
                """,
                (ended_at, st, int(session_id)),
            )

    def add_uploaded_video(
        self,
        *,
        session_id: int,
        title: str,
        description: str,
        url: str,
        video_id: str,
        profile_id: str = "",
        uploaded_at: str | None = None,
    ) -> int:
        ua = (uploaded_at or _utc_now_iso()).strip() or _utc_now_iso()
        with self._connect() as con:
            cur = con.execute(
                """
                INSERT INTO uploaded_videos(
                    session_id, uploaded_at, title, description, url, video_id, profile_id
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    uploaded_at=excluded.uploaded_at,
                    title=excluded.title,
                    description=excluded.description,
                    url=excluded.url,
                    profile_id=excluded.profile_id;
                """,
                (
                    int(session_id),
                    ua,
                    (title or "").strip(),
                    (description or "").strip(),
                    (url or "").strip(),
                    (video_id or "").strip(),
                    (profile_id or "").strip(),
                ),
            )
            return int(cur.lastrowid or 0)

    def update_video_stats(
        self,
        *,
        video_id: str,
        view_count: int,
        like_count: int | None,
        comment_count: int | None,
        stats_updated_at: str | None = None,
    ) -> None:
        ts = (stats_updated_at or _utc_now_iso()).strip() or _utc_now_iso()
        with self._connect() as con:
            con.execute(
                """
                UPDATE uploaded_videos
                SET view_count=?, like_count=?, comment_count=?, stats_updated_at=?
                WHERE video_id=?;
                """,
                (
                    int(view_count),
                    int(like_count) if like_count is not None else None,
                    int(comment_count) if comment_count is not None else None,
                    ts,
                    (video_id or "").strip(),
                ),
            )

    def list_sessions(self, limit: int = 200) -> list[UploadSession]:
        lim = max(1, int(limit))
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT id, started_at, planned_videos, processed_videos, uploaded_ok, ended_at, status
                FROM upload_sessions
                ORDER BY started_at DESC, id DESC
                LIMIT ?;
                """,
                (lim,),
            ).fetchall()
        out: list[UploadSession] = []
        for r in rows:
            out.append(
                UploadSession(
                    id=int(r["id"]),
                    started_at=str(r["started_at"]),
                    planned_videos=int(r["planned_videos"] or 0),
                    processed_videos=int(r["processed_videos"] or 0),
                    uploaded_ok=int(r["uploaded_ok"] or 0),
                    ended_at=str(r["ended_at"]) if r["ended_at"] else None,
                    status=str(r["status"] or "running"),
                )
            )
        return out

    def list_uploaded_videos_for_sessions(
        self, session_ids: Iterable[int]
    ) -> dict[int, list[UploadedVideo]]:
        ids = [int(x) for x in session_ids]
        if not ids:
            return {}
        ph = ",".join("?" for _ in ids)
        with self._connect() as con:
            rows = con.execute(
                f"""
                SELECT
                    v.id, v.session_id, s.started_at AS session_started_at,
                    v.uploaded_at, v.title, v.description, v.url, v.video_id,
                    view_count, like_count, comment_count, stats_updated_at
                FROM uploaded_videos v
                LEFT JOIN upload_sessions s ON s.id = v.session_id
                WHERE v.session_id IN ({ph})
                ORDER BY v.uploaded_at DESC, v.id DESC;
                """,
                tuple(ids),
            ).fetchall()
        out: dict[int, list[UploadedVideo]] = {sid: [] for sid in ids}
        for r in rows:
            v = UploadedVideo(
                id=int(r["id"]),
                session_id=int(r["session_id"]),
                session_started_at=str(r["session_started_at"])
                if r["session_started_at"]
                else None,
                uploaded_at=str(r["uploaded_at"]),
                title=str(r["title"] or ""),
                description=str(r["description"] or ""),
                url=str(r["url"] or ""),
                video_id=str(r["video_id"] or ""),
                view_count=int(r["view_count"]) if r["view_count"] is not None else None,
                like_count=int(r["like_count"]) if r["like_count"] is not None else None,
                comment_count=int(r["comment_count"]) if r["comment_count"] is not None else None,
                stats_updated_at=str(r["stats_updated_at"]) if r["stats_updated_at"] else None,
            )
            out.setdefault(v.session_id, []).append(v)
        return out

    def list_uploaded_videos(self, limit: int = 500) -> list[UploadedVideo]:
        lim = max(1, int(limit))
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT
                    v.id, v.session_id, s.started_at AS session_started_at,
                    v.uploaded_at, v.title, v.description, v.url, v.video_id,
                    v.view_count, v.like_count, v.comment_count, v.stats_updated_at
                FROM uploaded_videos v
                LEFT JOIN upload_sessions s ON s.id = v.session_id
                ORDER BY v.uploaded_at DESC, v.id DESC
                LIMIT ?;
                """,
                (lim,),
            ).fetchall()
        out: list[UploadedVideo] = []
        for r in rows:
            out.append(
                UploadedVideo(
                    id=int(r["id"]),
                    session_id=int(r["session_id"]),
                    session_started_at=str(r["session_started_at"])
                    if r["session_started_at"]
                    else None,
                    uploaded_at=str(r["uploaded_at"]),
                    title=str(r["title"] or ""),
                    description=str(r["description"] or ""),
                    url=str(r["url"] or ""),
                    video_id=str(r["video_id"] or ""),
                    view_count=int(r["view_count"]) if r["view_count"] is not None else None,
                    like_count=int(r["like_count"]) if r["like_count"] is not None else None,
                    comment_count=int(r["comment_count"]) if r["comment_count"] is not None else None,
                    stats_updated_at=str(r["stats_updated_at"]) if r["stats_updated_at"] else None,
                )
            )
        return out

    def is_profile_upload_error_flagged(self, *, profile_id: str) -> bool:
        pid = (profile_id or "").strip()
        if not pid:
            return False
        with self._connect() as con:
            row = con.execute(
                "SELECT flagged FROM upload_profile_state WHERE profile_id=?;",
                (pid,),
            ).fetchone()
        if row is None:
            return False
        try:
            return int(row["flagged"]) != 0
        except (TypeError, ValueError, KeyError):
            return False

    def reset_profile_upload_errors(self, *, profile_id: str) -> None:
        pid = (profile_id or "").strip()
        if not pid:
            return
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO upload_profile_state(profile_id, consecutive_upload_errors, flagged, last_error, updated_at)
                VALUES(?, 0, 0, '', ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    consecutive_upload_errors=0,
                    flagged=0,
                    last_error='',
                    updated_at=excluded.updated_at;
                """,
                (pid, _utc_now_iso()),
            )

    def inc_profile_upload_error(self, *, profile_id: str, error_text: str) -> int:
        """
        Increment consecutive upload errors for this profile and return the new value.
        """
        pid = (profile_id or "").strip()
        if not pid:
            return 0
        et = (error_text or "").strip()
        now = _utc_now_iso()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO upload_profile_state(profile_id, consecutive_upload_errors, flagged, last_error, updated_at)
                VALUES(?, 1, 0, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    consecutive_upload_errors = consecutive_upload_errors + 1,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at;
                """,
                (pid, et, now),
            )
            row = con.execute(
                "SELECT consecutive_upload_errors FROM upload_profile_state WHERE profile_id=?;",
                (pid,),
            ).fetchone()
        try:
            return int(row["consecutive_upload_errors"]) if row else 0
        except Exception:
            return 0

    def last_uploaded_at_by_profiles(self, profile_ids: Iterable[str]) -> dict[str, str]:
        """
        For each non-empty profile_id, return the latest uploaded_at (ISO) from uploaded_videos.
        Profiles with no uploads are omitted from the dict.
        """
        ids = sorted({(x or "").strip() for x in profile_ids if (x or "").strip()})
        if not ids:
            return {}
        ph = ",".join("?" for _ in ids)
        with self._connect() as con:
            rows = con.execute(
                f"""
                SELECT profile_id, MAX(uploaded_at) AS last_at
                FROM uploaded_videos
                WHERE profile_id IN ({ph})
                GROUP BY profile_id;
                """,
                tuple(ids),
            ).fetchall()
        out: dict[str, str] = {}
        for r in rows:
            pid = str(r["profile_id"] or "").strip()
            la = str(r["last_at"] or "").strip()
            if pid and la:
                out[pid] = la
        return out

    def reset_latest_upload_time_for_profile(self, *, profile_id: str) -> int:
        """
        Сдвигает время последнего залива для profile_id на >1 ч назад (по одной последней записи),
        чтобы в UI пауза 1 ч считалась пройденной. Возвращает число обновлённых строк (0 если записей нет).
        """
        pid = (profile_id or "").strip()
        if not pid:
            return 0
        old = (datetime.now(tz=timezone.utc) - timedelta(hours=2)).isoformat()
        with self._connect() as con:
            con.execute(
                """
                UPDATE uploaded_videos
                SET uploaded_at = ?
                WHERE profile_id = ?
                  AND id = (
                    SELECT id FROM uploaded_videos
                    WHERE profile_id = ?
                    ORDER BY uploaded_at DESC, id DESC
                    LIMIT 1
                  );
                """,
                (old, pid, pid),
            )
            row = con.execute("SELECT changes() AS n;").fetchone()
            try:
                return int(row["n"]) if row is not None else 0
            except (TypeError, ValueError, KeyError):
                return 0

    def flag_profile_after_upload_errors(self, *, profile_id: str, flagged: bool, error_text: str = "") -> None:
        pid = (profile_id or "").strip()
        if not pid:
            return
        now = _utc_now_iso()
        et = (error_text or "").strip()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO upload_profile_state(profile_id, consecutive_upload_errors, flagged, last_error, updated_at)
                VALUES(?, 0, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    flagged=excluded.flagged,
                    last_error=CASE WHEN excluded.last_error <> '' THEN excluded.last_error ELSE last_error END,
                    updated_at=excluded.updated_at;
                """,
                (pid, 1 if flagged else 0, et, now),
            )

