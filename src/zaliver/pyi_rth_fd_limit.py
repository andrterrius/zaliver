# PyInstaller runtime hook: поднять ulimit до импорта основного приложения (.app из Finder).
import sys


def _bump_fd_limit() -> None:
    if sys.platform == "win32":
        return
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        for target in (65536, 16384, 4096, 2048, 1024):
            want = min(max(int(target), int(soft)), int(hard))
            if want <= soft:
                continue
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
                soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            except OSError:
                break
    except Exception:
        pass


_bump_fd_limit()
