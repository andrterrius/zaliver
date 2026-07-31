"""Single-chunk encode in a disposable process: python -m zaliver.api.encode_one."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    for key in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ.setdefault(key, "1")
    os.environ.setdefault("ZALIVER_API_SERVER", "1")

    parser = argparse.ArgumentParser(prog="zaliver.api.encode_one")
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--result-file", required=True, type=Path)
    args = parser.parse_args(argv)

    result: dict = {"ok": False, "error": "not started", "chunk_index": 0}
    try:
        task = json.loads(args.task_file.read_text(encoding="utf-8"))
        if not isinstance(task, dict):
            raise ValueError("task must be an object")
        from zaliver.processing.worker import init_worker, process_chunk_disk
        import queue
        import threading

        # Local progress queue discarded — parent polls job log / frames another way.
        q: queue.Queue = queue.Queue()
        ev = threading.Event()
        init_worker(q, ev)
        result = process_chunk_disk(task)
        if not isinstance(result, dict):
            result = {"ok": False, "error": "invalid encode result"}
    except Exception as e:
        result = {
            "ok": False,
            "error": str(e),
            "traceback": traceback.format_exc()[-1500:],
        }

    try:
        args.result_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.result_file.with_suffix(args.result_file.suffix + ".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        tmp.replace(args.result_file)
    except OSError as e:
        print(f"cannot write result: {e}", file=sys.stderr)
        return 2
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
