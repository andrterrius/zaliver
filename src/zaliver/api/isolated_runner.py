"""Run processing jobs (optionally out-of-process on Windows)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from zaliver.core.sinks import JobProgressSink
from zaliver.processing.subprocess_flags import (
    resolve_python_executable,
    worker_creationflags,
)
from zaliver.processing.win_console import suppress_console_ctrl

RegisterCancel = Callable[[Callable[[], None]], None]
JobRunnerBody = Callable[[JobProgressSink, RegisterCancel, str], None]


def should_isolate_processing_jobs() -> bool:
    """
    Out-of-process by default on Windows API so an encode AV cannot kill uvicorn.

    Disable: ZALIVER_FORCE_INPROCESS_JOBS=1
    """
    force_in = (os.environ.get("ZALIVER_FORCE_INPROCESS_JOBS") or "").strip().lower()
    if force_in in {"1", "true", "yes", "on"}:
        return False
    force_iso = (os.environ.get("ZALIVER_ISOLATE_JOBS") or "").strip().lower()
    if force_iso in {"0", "false", "no", "off"}:
        return False
    if force_iso in {"1", "true", "yes", "on"}:
        return True
    api = (os.environ.get("ZALIVER_API_SERVER") or "").strip().lower()
    return api in {"1", "true", "yes", "on"} and sys.platform == "win32"


def _job_log_dir() -> Path:
    raw = (os.environ.get("ZALIVER_JOB_LOG_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    if local:
        return Path(local) / "Zaliver" / "api" / "job_logs"
    return Path.home() / ".zaliver" / "api" / "job_logs"


def _win_exit_message(code: int | None) -> str:
    if code is None:
        return "Worker process ended unexpectedly."
    u = code & 0xFFFFFFFF
    if u == 0xC0000005:
        return (
            f"Worker process crashed (access violation, exit 0x{u:08X})."
        )
    if u == 0xC000013A:
        return f"Worker process interrupted (exit 0x{u:08X})."
    if code != 0:
        return f"Worker process ended unexpectedly (exit code {code})."
    return "Worker process ended without a result file."


def _limit_blas_threads() -> None:
    for key in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ.setdefault(key, "1")


def _with_ready_consumed_path(options: dict[str, Any], job_id: str) -> dict[str, Any]:
    opts = dict(options or {})
    try:
        limit = int(opts.get("upload_ready_buffer_limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return opts
    if not str(opts.get("upload_ready_consumed_path") or "").strip():
        from zaliver.processing.ready_buffer import default_consumed_path

        consumed = default_consumed_path(job_id)
        if consumed:
            opts["upload_ready_consumed_path"] = consumed
    if job_id and not str(opts.get("job_id") or "").strip():
        opts["job_id"] = job_id
    return opts


def _run_inprocess(
    *,
    service_cls: type,
    options: dict[str, Any],
    sink: JobProgressSink,
    register_cancel: RegisterCancel,
    job_id: str = "",
) -> None:
    _limit_blas_threads()
    svc = service_cls(sink)
    register_cancel(svc.cancel)
    with suppress_console_ctrl(also_ctrl_c=False):
        svc.run(_with_ready_consumed_path(options, job_id))


def _run_via_subprocess(
    *,
    kind: str,
    options: dict[str, Any],
    sink: JobProgressSink,
    register_cancel: RegisterCancel,
    job_id: str,
) -> tuple[bool, str]:
    """Returns (finished_ok_or_false, message). finished False means hard failure."""
    log_dir = _job_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    work = log_dir / f".worker_{job_id or 'job'}"
    work.mkdir(parents=True, exist_ok=True)
    job_file = work / "job.json"
    result_path = work / "result.json"
    cancel_path = work / "cancel.flag"
    log_path = log_dir / f"{job_id}.log" if job_id else work / "job.log"
    meta_path = log_dir / f"{job_id}.meta.json" if job_id else work / "job.meta.json"

    for p in (result_path, cancel_path):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    spec = {
        "kind": kind,
        "job_id": job_id,
        "options": _with_ready_consumed_path(options, job_id),
        "log_path": str(log_path),
        "meta_path": str(meta_path),
        "result_path": str(result_path),
        "cancel_path": str(cancel_path),
    }
    job_file.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    cancelled = {"v": False}

    def _cancel() -> None:
        cancelled["v"] = True
        try:
            cancel_path.write_text("1", encoding="utf-8")
        except OSError:
            pass

    register_cancel(_cancel)

    cmd = [
        resolve_python_executable(),
        "-m",
        "zaliver.api.job_worker",
        "--job-file",
        str(job_file),
    ]
    env = os.environ.copy()
    env["ZALIVER_API_SERVER"] = "1"
    env["ZALIVER_JOB_LOG_DIR"] = str(log_dir)
    # Never fall back to in-process encode inside the worker's parent API thread.
    env["ZALIVER_FORCE_INPROCESS_JOBS"] = "0"
    _limit_blas_threads()
    for key in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        env.setdefault(key, "1")

    stderr_path = work / "worker.stderr.txt"
    try:
        stderr_f = stderr_path.open("wb")
    except OSError:
        stderr_f = subprocess.DEVNULL  # type: ignore[assignment]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_f,
            env=env,
            creationflags=worker_creationflags(),
        )
    except OSError as e:
        try:
            if hasattr(stderr_f, "close"):
                stderr_f.close()
        except Exception:
            pass
        return False, f"Cannot start worker process: {e}"

    stderr_tail = ""
    seen_outputs: set[str] = set()
    last_prog: tuple[int, int, str] | None = None

    def _forward_meta_live() -> None:
        nonlocal last_prog
        if not meta_path.is_file():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(meta, dict):
            return
        dp = meta.get("progress")
        if isinstance(dp, dict):
            try:
                cur = int(dp.get("current") or 0)
                total = int(dp.get("total") or 0)
            except (TypeError, ValueError):
                cur, total = 0, 0
            msg = str(dp.get("message") or "")
            prog = (cur, total, msg)
            if prog != last_prog:
                last_prog = prog
                try:
                    sink.on_progress(cur, total, msg)
                except Exception:
                    pass
        for raw in meta.get("outputs") or []:
            p = str(raw or "").strip()
            if not p or p in seen_outputs:
                continue
            seen_outputs.add(p)
            try:
                sink.on_output_saved(p, True)
            except Exception:
                pass

    try:
        while True:
            _forward_meta_live()
            rc = proc.poll()
            if result_path.is_file():
                break
            if rc is not None:
                break
            if cancelled["v"]:
                try:
                    proc.terminate()
                except OSError:
                    pass
            time.sleep(0.4)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        if not result_path.is_file():
            time.sleep(0.3)
    finally:
        try:
            if hasattr(stderr_f, "close"):
                stderr_f.close()
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            if stderr_path.is_file():
                stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace")[
                    -800:
                ]
        except OSError:
            pass

    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return False, f"Invalid worker result: {e}"
        _forward_meta_live()
        for out in result.get("outputs") or []:
            p = str(out or "").strip()
            if not p or p in seen_outputs:
                continue
            seen_outputs.add(p)
            try:
                sink.on_output_saved(p, True)
            except Exception:
                pass
        ok = bool(result.get("ok"))
        msg = str(result.get("message") or "")
        return ok, msg

    msg = _win_exit_message(proc.returncode)
    if stderr_tail.strip():
        msg = f"{msg}\n{stderr_tail.strip()}"
    return False, msg


def _make_kind_runner(kind: str, service_import: tuple[str, str]) -> Callable[[dict[str, Any]], JobRunnerBody]:
    mod_name, cls_name = service_import

    def factory(options: dict[str, Any]) -> JobRunnerBody:
        def runner(
            sink: JobProgressSink,
            register_cancel: RegisterCancel,
            job_id: str = "",
        ) -> None:
            mod = __import__(mod_name, fromlist=[cls_name])
            service_cls = getattr(mod, cls_name)

            if should_isolate_processing_jobs():
                ok, msg = _run_via_subprocess(
                    kind=kind,
                    options=options,
                    sink=sink,
                    register_cancel=register_cancel,
                    job_id=job_id,
                )
                # Never fall back to in-process on Windows — that kills uvicorn on AV.
                sink.on_finished(ok, msg)
                return

            _run_inprocess(
                service_cls=service_cls,
                options=options,
                sink=sink,
                register_cancel=register_cancel,
                job_id=job_id,
            )

        return runner

    return factory


uniquify_runner = _make_kind_runner(
    "uniquify", ("zaliver.processing.thread_worker", "ProcessingService")
)
slicing_runner = _make_kind_runner(
    "slicing", ("zaliver.processing.slicing_worker", "SlicingService")
)
stitching_runner = _make_kind_runner(
    "stitching", ("zaliver.processing.stitching_worker", "StitchingService")
)
