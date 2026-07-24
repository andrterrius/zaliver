"""Фоновое обновление статистики залитых Instagram Reels (instagrapi)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from zaliver.instagram_upload.instagrapi_session import (
    InstagrapiSessionError,
    client_username,
    ensure_instagrapi_client,
    invalidate_instagrapi_session,
    is_instagrapi_session_error,
)
from zaliver.instagram_upload.reel_stats import (
    DEFAULT_REQUEST_PAUSE_S,
    DEFAULT_STATS_WORKERS,
    fetch_reel_stats_many,
)


class UploadedInstagramStatsRefreshWorker(QObject):
    """
    Запросы к private API Instagram через instagrapi в потоке Qt.

    По умолчанию до 3 воркеров, у каждого свой clone Client + тот же прокси
    профиля. Агрессивный refresh всё ещё может убить sessionid.
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
        proxy: str = "",
    ) -> None:
        super().__init__()
        self._video_ids = list(video_ids)
        self._profile_id = (profile_id or "").strip()
        self._username = (username or "").strip()
        self._password = (password or "").strip()
        self._twofa_secret = (twofa_secret or "").strip()
        self._sessionid_provider = sessionid_provider
        self._proxy = (proxy or "").strip()

    def _log(self, msg: str) -> None:
        line = f"[ig-stats] {msg}"
        try:
            self.log_line.emit(line)
        except Exception:
            pass

    def _open_session(self) -> tuple[Any, str]:
        return ensure_instagrapi_client(
            self._profile_id,
            username=self._username,
            password=self._password,
            twofa_secret=self._twofa_secret,
            sessionid_provider=self._sessionid_provider,
            allow_dump=True,
            proxy=self._proxy,
        )

    def run(self) -> None:
        successes: list[tuple[str, int, int | None, int | None, bool]] = []
        failures: list[tuple[str, str, bool]] = []
        ids = [(vid or "").strip() for vid in self._video_ids if (vid or "").strip()]
        total = len(ids)
        if total <= 0:
            self.finished.emit(successes, failures)
            return
        self.progress.emit(0, total, ids[0])
        from zaliver.antydetect.proxy_dsn import mask_proxy_dsn, proxy_dsn_has_auth

        proxy_label = mask_proxy_dsn(self._proxy) if self._proxy else "none"
        auth = (
            "yes"
            if self._proxy and proxy_dsn_has_auth(self._proxy)
            else ("no" if self._proxy else "n/a")
        )
        self._log(
            f"Старт: {total} рилсов, profile_id={self._profile_id!r}, "
            f"creds={'yes' if self._username and self._password else 'no'}, "
            f"proxy={proxy_label!r}, proxy_auth={auth}, "
            f"mode=parallel pause≈{DEFAULT_REQUEST_PAUSE_S}s "
            f"workers={DEFAULT_STATS_WORKERS}"
        )
        if self._proxy and not proxy_dsn_has_auth(self._proxy):
            msg = (
                "Прокси без логина/пароля — будет 407. "
                "Перезапустите Zaliver после обновления и повторите чек."
            )
            self._log(msg)
            for v in ids:
                failures.append((v, msg, False))
            self.batch_done.emit([], list(failures))
            self.progress.emit(total, total, ids[-1])
            self.finished.emit(successes, failures)
            return
        if not self._proxy:
            self._log(
                "Прокси профиля не найден — запросы пойдут с IP машины. "
                "Это часто ломает sessionid в антидетекте."
            )

        try:
            cl, source = self._open_session()
            uname = client_username(cl)
            self._log(f"Сессия OK (@{uname or '?'}, source={source})")
            if not uname and source == "dump":
                self._log(
                    "Dump без username — не делаю auto-refresh "
                    "(он часто валит живую сессию в браузере)."
                )
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

        try:
            cl.delay_range = [0.05, 0.25]
        except Exception:
            pass

        emit_lock = threading.Lock()
        settled: set[str] = set()
        session_dead = False

        def _emit_ok(st: Any) -> None:
            code = st.video_id
            with emit_lock:
                if code in settled:
                    return
                settled.add(code)
                row = (
                    code,
                    int(st.view_count),
                    st.like_count,
                    st.comment_count,
                    False,
                )
                successes.append(row)
                self.batch_done.emit([row], [])
                self.progress.emit(len(settled), total, code)

        def _emit_fail(code: str, msg: str) -> None:
            with emit_lock:
                if code in settled:
                    return
                settled.add(code)
                self._log(f"{code}: {msg}")
                row_f = (code, msg, False)
                failures.append(row_f)
                self.batch_done.emit([], [row_f])
                self.progress.emit(len(settled), total, code)

        def _on_item(st: Any, code: str, err: str | None) -> None:
            nonlocal session_dead
            if err is None and st is not None:
                _emit_ok(st)
                return
            msg = err or "unknown error"
            _emit_fail(code, msg)
            if is_instagrapi_session_error(msg):
                session_dead = True

        try:
            fetch_reel_stats_many(
                cl,
                ids,
                workers=DEFAULT_STATS_WORKERS,
                request_pause_s=DEFAULT_REQUEST_PAUSE_S,
                abort_on_session_error=True,
                on_progress=None,
                on_item=_on_item,
            )
        except Exception as e:
            self._log(f"batch crash: {e}")
            for v in ids:
                if v not in settled:
                    _emit_fail(v, str(e))
            if is_instagrapi_session_error(e):
                session_dead = True

        for v in ids:
            if v not in settled:
                _emit_fail(v, "не удалось получить метрики")

        if session_dead:
            self._log(
                "Сессия Instagram умерла (redirects/login_required). "
                "Dump не трогаю агрессивным re-login — зайдите в Instagram "
                "в антидетект-профиле вручную. Не используйте тот же профиль "
                "для чека и для залива одновременно."
            )
            # Не удаляем dump автоматически при redirects: invalidate + login_by_sessionid
            # с IP без прокси доламывает cookies в браузере.
            if not successes and failures:
                invalidate_instagrapi_session(self._profile_id)
                self._log("Удалён локальный dump (сессия уже мёртвая).")

        self.progress.emit(total, total, ids[-1])
        self._log(f"Готово: ok={len(successes)}, fail={len(failures)}")
        self.finished.emit(successes, failures)
