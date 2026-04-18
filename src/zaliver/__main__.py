import os
import sys
import traceback
from pathlib import Path


def _write_crash_log(exc: BaseException) -> None:
    try:
        log_path = Path(os.environ.get("TEMP", os.environ.get("TMP", "."))) / "zaliver_crash.log"
        log_path.write_text(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        pass


def _is_frozen_bundle() -> bool:
    return bool(
        getattr(sys, "frozen", False)
        or getattr(sys, "_MEIPASS", None)
        or globals().get("__compiled__")
    )


def _pause_console() -> None:
    try:
        sys.stderr.write("\nPress Enter to close this window...\n")
        sys.stderr.flush()
        input()
    except (EOFError, OSError):
        pass


def main() -> None:
    from PyQt6.QtWidgets import QApplication

    from zaliver.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Zaliver")
    w = MainWindow()
    w.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:
        _write_crash_log(exc)
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
        sys.stderr.flush()
        try:
            is_worker = multiprocessing.current_process().name != "MainProcess"
        except Exception:
            is_worker = False
        if is_worker:
            raise SystemExit(1) from exc
        if _is_frozen_bundle():
            _pause_console()
        raise SystemExit(1) from exc
