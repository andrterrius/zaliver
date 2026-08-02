"""Standalone job worker process: python -m zaliver.api.job_worker --job-file …"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(path)


def _append_log(log_path: Path, line: str) -> None:
    text = str(line).rstrip("\r\n") + "\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()


def _patch_meta(
    meta_path: Path,
    *,
    progress: tuple[int, int, str] | None = None,
    output: str | None = None,
) -> None:
    """Merge live progress/outputs into job meta so the API can stream mid-run."""
    try:
        if meta_path.is_file():
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        else:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if progress is not None:
            cur, total, msg = progress
            data["progress"] = {
                "current": int(cur),
                "total": int(total),
                "message": str(msg or ""),
            }
        if output:
            outs = data.get("outputs")
            if not isinstance(outs, list):
                outs = []
            path = str(output)
            if path and path not in outs:
                outs.append(path)
            data["outputs"] = outs
        _write_json(meta_path, data)
    except Exception:
        pass


def _patch_progress_meta(meta_path: Path, cur: int, total: int, msg: str) -> None:
    _patch_meta(meta_path, progress=(cur, total, msg))


def _patch_output_meta(meta_path: Path, path: str) -> None:
    _patch_meta(meta_path, output=path)


def run_job_file(job_file: Path) -> int:
    for key in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ.setdefault(key, "1")

    spec = json.loads(job_file.read_text(encoding="utf-8"))
    kind = str(spec.get("kind") or "uniquify")
    options = spec.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("options must be an object")
    job_id = str(spec.get("job_id") or "")
    log_path = Path(str(spec.get("log_path") or ""))
    meta_path = Path(str(spec.get("meta_path") or ""))
    result_path = Path(str(spec.get("result_path") or ""))

    os.environ.setdefault("ZALIVER_API_SERVER", "1")
    if log_path.parent:
        os.environ["ZALIVER_JOB_LOG_DIR"] = str(log_path.parent)

    from zaliver.core.sinks import JobProgressSink

    finished = {"ok": False, "message": "", "done": False}
    outputs: list[str] = []
    last_prog = 0.0

    def on_log(msg: str) -> None:
        if log_path:
            _append_log(log_path, msg)

    def on_progress(cur: int, total: int, msg: str) -> None:
        nonlocal last_prog
        now = time.monotonic()
        if meta_path and now - last_prog >= 0.35:
            last_prog = now
            _patch_progress_meta(meta_path, cur, total, msg)

    def on_output(path: str, _skip: bool) -> None:
        p = str(path)
        if p and p not in outputs:
            outputs.append(p)
        if meta_path and p:
            _patch_output_meta(meta_path, p)

    def on_finished(ok: bool, message: str) -> None:
        finished["ok"] = bool(ok)
        finished["message"] = str(message or "")
        finished["done"] = True

    sink = JobProgressSink(
        on_progress=on_progress,
        on_finished=on_finished,
        on_log=on_log,
        on_output_saved=on_output,
    )

    if kind == "uniquify":
        from zaliver.api.uniquify_lite import run_uniquify_lite

        cancel_path = Path(str(spec.get("cancel_path") or ""))
        cancelled = {"v": False}

        def watch_cancel() -> None:
            while not finished["done"]:
                if cancel_path and cancel_path.is_file():
                    cancelled["v"] = True
                    return
                time.sleep(0.4)

        threading.Thread(
            target=watch_cancel, daemon=True, name="zaliver-cancel"
        ).start()
        on_log("Обработка в отдельном процессе (uniquify_lite, без multiprocessing).")
        def _lite_output(p: str) -> None:
            on_output(str(p), False)

        try:
            ok, message = run_uniquify_lite(
                options,
                log=on_log,
                on_output=_lite_output,
                cancel_check=lambda: cancelled["v"],
            )
            finished["ok"] = bool(ok)
            finished["message"] = str(message or "")
            finished["done"] = True
        except Exception as e:
            finished["ok"] = False
            finished["message"] = str(e)
            finished["done"] = True
            on_log(f"worker exception: {e!r}")
            on_log(traceback.format_exc())
    else:
        if kind == "slicing":
            from zaliver.processing.slicing_worker import SlicingService as Svc
        elif kind == "stitching":
            from zaliver.processing.stitching_worker import StitchingService as Svc
        else:
            raise ValueError(f"Unknown kind: {kind}")

        svc = Svc(sink)
        cancel_path = Path(str(spec.get("cancel_path") or ""))

        def watch_cancel() -> None:
            while not finished["done"]:
                if cancel_path and cancel_path.is_file():
                    try:
                        svc.cancel()
                    except Exception:
                        pass
                    return
                time.sleep(0.4)

        threading.Thread(
            target=watch_cancel, daemon=True, name="zaliver-cancel"
        ).start()
        on_log("Обработка в отдельном процессе (изоляция от API-сервера).")
        try:
            svc.run(options)
        except Exception as e:
            if not finished["done"]:
                finished["ok"] = False
                finished["message"] = str(e)
                finished["done"] = True
                on_log(f"worker exception: {e!r}")
                on_log(traceback.format_exc())
        if not finished["done"]:
            finished["ok"] = True
            finished["message"] = finished["message"] or "done"
            finished["done"] = True

    if result_path:
        _write_json(
            result_path,
            {
                "ok": bool(finished["ok"]),
                "message": str(finished["message"] or ""),
                "outputs": outputs,
                "job_id": job_id,
            },
        )
    return 0 if finished["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zaliver.api.job_worker")
    parser.add_argument("--job-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        return run_job_file(args.job_file)
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
