"""Фоновое обновление статистики залитых Instagram Reels (instagrapi)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from zaliver.instagram_upload.instagrapi_session import (
    InstagrapiSessionError,
    ensure_instagrapi_client,
    invalidate_instagrapi_session,
)
from zaliver.instagram_upload.reel_stats import (
    DEFAULT_STATS_WORKERS,
    fetch_reel_stats_many,
)


class UploadedInstagramStatsRefreshWorker(QObject):
    """
    Запросы к private API Instagram через instagrapi в потоке Qt.
    Формат результатов совместим с YouTube-воркером:
      success: (video_id, views, likes, comments, age_restricted=False)
      failure: (video_id, message, is_api_error=False)
    """

    progress = pyqtSignal(int, int, str)
    batch_done = pyqtSignal(object, object)
    finished = pyqtSignal(object, object)
    log_line = pyqtSignal(str)

    def __init__(
        self,
        video_ids: list[str],
        *,
        profile_id: str,
        username: str = "",
        password: str = "",
        twofa_secret: str = "",
        sessionid_provider: Callable[[], str] | None = None,
    ) -> None:
        super().__init__()
        self._video_ids = list(video_ids)
        self._profile_id = (profile_id or "").strip()
        self._username = (username or "").strip()
        self._password = (password or "").strip()
        self._twofa_secret = (twofa_secret or "").strip()
        self._sessionid_provider = sessionid_provider

    def _log(self, msg: str) -> None:
        line = f"[ig-stats] {msg}"
        try:
            self.log_line.emit(line)
        except Exception:
            pass

    def run(self) -> None:
        successes: list[tuple[str, int, int | None, int | None, bool]] = []
        failures: list[tuple[str, str, bool]] = []
        ids = [(vid or "").strip() for vid in self._video_ids if (vid or "").strip()]
        total = len(ids)
        if total <= 0:
            self.finished.emit(successes, failures)
            return
        self.progress.emit(0, total, ids[0])
        self._log(
            f"Старт: {total} рилсов, profile_id={self._profile_id!r}, "
            f"creds={'yes' if self._username and self._password else 'no'}, "
            f"workers={DEFAULT_STATS_WORKERS}"
        )

        try:
            cl, source = ensure_instagrapi_client(
                self._profile_id,
                username=self._username,
                password=self._password,
                twofa_secret=self._twofa_secret,
                sessionid_provider=self._sessionid_provider,
            )
            uname = getattr(cl, "username", None) or "?"
            self._log(f"Сессия OK (@{uname}, source={source})")
        except InstagrapiSessionError as e:
            self._log(f"Сессия FAIL: {e}")
            for v in ids:
                failures.append((v, str(e), False))
            self.batch_done.emit([], list(failures))
            self.progress.emit(total, total, ids[-1])
            self.finished.emit(successes, failures)
            return
        except Exception as e:
            msg = f"Сессия Instagram: {e}"
            self._log(msg)
            for v in ids:
                failures.append((v, msg, False))
            self.batch_done.emit([], list(failures))
            self.progress.emit(total, total, ids[-1])
            self.finished.emit(successes, failures)
            return

        # Ускорить мастер-клиент на случай serial fallback.
        try:
            cl.delay_range = [0.0, 0.05]
        except Exception:
            pass

        emit_lock = threading.Lock()

        def _on_progress(done: int, tot: int, code: str) -> None:
            self.progress.emit(done, tot, code)

        def _on_item(st: Any, code: str, err: str | None) -> None:
            with emit_lock:
                if err is None and st is not None:
                    row = (
                        st.video_id,
                        int(st.view_count),
                        st.like_count,
                        st.comment_count,
                        False,
                    )
                    successes.append(row)
                    self.batch_done.emit([row], [])
                else:
                    self._log(f"{code}: {err}")
                    row_f = (code, err or "unknown error", False)
                    failures.append(row_f)
                    self.batch_done.emit([], [row_f])

        try:
            fetch_reel_stats_many(
                cl,
                ids,
                workers=DEFAULT_STATS_WORKERS,
                on_progress=_on_progress,
                on_item=_on_item,
            )
        except Exception as e:
            self._log(f"batch crash: {e}")
            left = [
                v
                for v in ids
                if v not in {s[0] for s in successes}
                and v not in {f[0] for f in failures}
            ]
            batch_fail = [(v, str(e), False) for v in left]
            failures.extend(batch_fail)
            if batch_fail:
                self.batch_done.emit([], batch_fail)

        # Если почти всё упало с login/session — dump протух, сбросим на следующий раз.
        if successes and failures:
            pass
        elif not successes and failures:
            sample = " ".join(
                str(f[1]) for f in failures[:3] if isinstance(f, (list, tuple)) and len(f) > 1
            ).lower()
            if any(
                m in sample
                for m in (
                    "login_required",
                    "login required",
                    "session",
                    "unauthorized",
                    "challenge",
                )
            ):
                self._log("Похоже, dump сессии протух — удаляю для перелогина.")
                invalidate_instagrapi_session(self._profile_id)

        self._log(f"Готово: ok={len(successes)}, fail={len(failures)}")
        self.finished.emit(successes, failures)
