"""Persistent job logs + metadata on disk with retention cleanup."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

_META_SUFFIX = ".meta.json"
_LOG_SUFFIX = ".log"


class JobLogStore:
    """Append-only log files and JSON meta under ``root``."""

    def __init__(
        self,
        root: Path,
        *,
        retention_days: int = 14,
        max_jobs: int = 500,
    ) -> None:
        self.root = Path(root)
        self.retention_days = max(1, int(retention_days))
        self.max_jobs = max(1, int(max_jobs))
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def _log_path(self, job_id: str) -> Path:
        return self.root / f"{job_id}{_LOG_SUFFIX}"

    def _meta_path(self, job_id: str) -> Path:
        return self.root / f"{job_id}{_META_SUFFIX}"

    def append_log(self, job_id: str, line: str) -> None:
        text = str(line).rstrip("\r\n") + "\n"
        path = self._log_path(job_id)
        with self._lock:
            try:
                with path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
            except OSError as e:
                _LOG.warning("job log append failed %s: %s", job_id, e)

    def read_tail(self, job_id: str, n: int) -> list[str]:
        if n <= 0:
            return []
        path = self._log_path(job_id)
        if not path.is_file():
            return []
        try:
            # Efficient-ish tail for typical job log sizes.
            data = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            _LOG.warning("job log read failed %s: %s", job_id, e)
            return []
        lines = [ln for ln in data.splitlines() if ln != ""]
        return lines[-n:]

    def save_meta(self, job_id: str, meta: dict[str, Any]) -> None:
        payload = dict(meta)
        payload["id"] = job_id
        path = self._meta_path(job_id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with self._lock:
            try:
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=0),
                    encoding="utf-8",
                )
                tmp.replace(path)
            except OSError as e:
                _LOG.warning("job meta save failed %s: %s", job_id, e)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def load_meta(self, job_id: str) -> dict[str, Any] | None:
        path = self._meta_path(job_id)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _LOG.warning("job meta load failed %s: %s", job_id, e)
            return None
        if not isinstance(raw, dict):
            return None
        return raw

    def list_metas(self, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        items: list[tuple[float, dict[str, Any]]] = []
        try:
            paths = list(self.root.glob(f"*{_META_SUFFIX}"))
        except OSError:
            return []
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            created = float(raw.get("created_at") or 0.0)
            if created <= 0:
                try:
                    created = path.stat().st_mtime
                except OSError:
                    created = 0.0
            items.append((created, raw))
        items.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in items[:limit]]

    def exists(self, job_id: str) -> bool:
        return self._meta_path(job_id).is_file() or self._log_path(job_id).is_file()

    def cleanup(self) -> int:
        """Delete jobs older than retention and enforce max_jobs. Returns removed count."""
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        try:
            meta_paths = list(self.root.glob(f"*{_META_SUFFIX}"))
        except OSError:
            return 0

        scored: list[tuple[float, str]] = []
        for meta_path in meta_paths:
            job_id = meta_path.name[: -len(_META_SUFFIX)]
            created = 0.0
            try:
                raw = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    created = float(raw.get("created_at") or 0.0)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                created = 0.0
            if created <= 0:
                try:
                    created = meta_path.stat().st_mtime
                except OSError:
                    created = 0.0
            scored.append((created, job_id))

        # Orphan .log without meta
        try:
            for log_path in self.root.glob(f"*{_LOG_SUFFIX}"):
                job_id = log_path.name[: -len(_LOG_SUFFIX)]
                if not self._meta_path(job_id).is_file():
                    try:
                        mtime = log_path.stat().st_mtime
                    except OSError:
                        mtime = 0.0
                    scored.append((mtime, job_id))
        except OSError:
            pass

        # Deduplicate by job_id keeping newest stamp
        by_id: dict[str, float] = {}
        for created, job_id in scored:
            prev = by_id.get(job_id)
            if prev is None or created > prev:
                by_id[job_id] = created

        to_delete: set[str] = set()
        for job_id, created in by_id.items():
            if created < cutoff:
                to_delete.add(job_id)

        survivors = sorted(
            ((created, job_id) for job_id, created in by_id.items() if job_id not in to_delete),
            key=lambda x: x[0],
            reverse=True,
        )
        if len(survivors) > self.max_jobs:
            for _, job_id in survivors[self.max_jobs :]:
                to_delete.add(job_id)

        for job_id in to_delete:
            if self._delete_job(job_id):
                removed += 1
        if removed:
            _LOG.info("job log cleanup removed %s job(s)", removed)
        return removed

    def _delete_job(self, job_id: str) -> bool:
        ok = False
        with self._lock:
            for path in (self._meta_path(job_id), self._log_path(job_id)):
                try:
                    if path.is_file():
                        path.unlink()
                        ok = True
                except OSError as e:
                    _LOG.warning("job log delete failed %s: %s", path, e)
        return ok
