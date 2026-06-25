"""Фоновая нарезка аватарок из одного или нескольких спрайт-листов (Qt thread)."""

from __future__ import annotations

import os
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from zaliver.ui.avatar_detection import extract_avatar_pngs_from_path, load_image_file_as_png


class AvatarDetectionCancel:
    """Флаг отмены; проверяется между файлами."""

    def __init__(self) -> None:
        self.cancelled = False


@dataclass(frozen=True)
class _FileProcessResult:
    index: int
    path: Path
    pngs: list[bytes]
    preview: bytes
    error: str | None = None


def _parallel_workers(file_count: int) -> int:
    if file_count <= 1:
        return 1
    cpus = os.cpu_count() or 4
    return min(file_count, max(2, cpus))


def _process_avatar_file(
    index: int,
    path: Path,
    *,
    crop_sprites: bool,
) -> _FileProcessResult:
    try:
        if crop_sprites:
            pngs, _boxes, preview_image = extract_avatar_pngs_from_path(
                path,
                padding=2,
                square=True,
            )
            if not pngs:
                return _FileProcessResult(
                    index,
                    path,
                    [],
                    b"",
                    f"{path.name}: аватарки не найдены",
                )
            buf = BytesIO()
            preview_image.save(buf, format="PNG")
            return _FileProcessResult(
                index,
                path,
                [bytes(p) for p in pngs if p],
                buf.getvalue(),
                None,
            )

        png_bytes = load_image_file_as_png(path)
        if not png_bytes:
            return _FileProcessResult(
                index,
                path,
                [],
                b"",
                f"{path.name}: пустой файл",
            )
        return _FileProcessResult(index, path, [png_bytes], png_bytes, None)
    except OSError as e:
        return _FileProcessResult(
            index,
            path,
            [],
            b"",
            f"{path.name}: не удалось прочитать ({e})",
        )
    except Exception as e:
        return _FileProcessResult(
            index,
            path,
            [],
            b"",
            f"{path.name}: ошибка обработки ({e})",
        )


class AvatarDetectionWorker(QObject):
    """Тяжёлую обработку (numpy/scipy) выполняет вне UI-потока."""

    progress = pyqtSignal(int, int, str)
    file_preview = pyqtSignal(bytes, str)
    finished = pyqtSignal(object, bytes, str, object, object)
    failed = pyqtSignal(str)
    aborted = pyqtSignal()

    def __init__(
        self,
        paths: list[Path],
        *,
        crop_sprites: bool = True,
        cancel: AvatarDetectionCancel | None = None,
    ) -> None:
        super().__init__()
        self._paths = list(paths)
        self._crop_sprites = crop_sprites
        self._cancel = cancel or AvatarDetectionCancel()

    def _cancelled(self) -> bool:
        return self._cancel.cancelled

    def _emit_file_result(
        self,
        result: _FileProcessResult,
        *,
        total: int,
        completed: int,
    ) -> None:
        self.progress.emit(completed, total, result.path.name)
        if result.preview:
            self.file_preview.emit(result.preview, str(result.path))

    def _collect_results(
        self,
        results: list[_FileProcessResult],
    ) -> tuple[list[bytes], bytes, str, list[str]]:
        all_pngs: list[bytes] = []
        errors: list[str] = []
        last_preview = b""
        last_path = ""

        for result in sorted(results, key=lambda item: item.index):
            if result.error:
                errors.append(result.error)
            if result.pngs:
                all_pngs.extend(result.pngs)
            if result.preview:
                last_preview = result.preview
                last_path = str(result.path)
            elif result.pngs and not last_path:
                last_path = str(result.path)

        return all_pngs, last_preview, last_path, errors

    def _run_sequential(self) -> tuple[list[bytes], bytes, str, list[str]] | None:
        total = len(self._paths)
        results: list[_FileProcessResult] = []

        for index, path in enumerate(self._paths):
            if self._cancelled():
                self.aborted.emit()
                return None

            result = _process_avatar_file(
                index,
                path,
                crop_sprites=self._crop_sprites,
            )
            results.append(result)
            self._emit_file_result(result, total=total, completed=len(results))

        return self._collect_results(results)

    def _run_parallel(self) -> tuple[list[bytes], bytes, str, list[str]] | None:
        total = len(self._paths)
        workers = _parallel_workers(total)
        results: list[_FileProcessResult] = []
        pending = list(enumerate(self._paths))

        self.progress.emit(0, total, self._paths[0].name)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            fut_map: dict[Future[_FileProcessResult], int] = {}

            while pending or fut_map:
                if self._cancelled():
                    for fut in fut_map:
                        fut.cancel()
                    self.aborted.emit()
                    return None

                while pending and len(fut_map) < workers:
                    index, path = pending.pop(0)
                    fut = pool.submit(
                        _process_avatar_file,
                        index,
                        path,
                        crop_sprites=self._crop_sprites,
                    )
                    fut_map[fut] = index

                if not fut_map:
                    break

                done, _ = wait(fut_map.keys(), return_when=FIRST_COMPLETED)
                for fut in done:
                    index = fut_map.pop(fut)
                    path = self._paths[index]
                    try:
                        result = fut.result()
                    except Exception as e:
                        result = _FileProcessResult(
                            index,
                            path,
                            [],
                            b"",
                            f"{path.name}: ошибка обработки ({e})",
                        )
                    results.append(result)
                    self._emit_file_result(result, total=total, completed=len(results))

        return self._collect_results(results)

    def run(self) -> None:
        if not self._paths:
            self.failed.emit("Не выбрано ни одного файла.")
            return

        total = len(self._paths)
        if total <= 1 or _parallel_workers(total) <= 1:
            collected = self._run_sequential()
        else:
            collected = self._run_parallel()

        if collected is None:
            return

        all_pngs, last_preview, last_path, errors = collected

        if self._cancelled():
            self.aborted.emit()
            return

        self.progress.emit(total, total, "")

        if not all_pngs:
            if errors:
                self.failed.emit("\n".join(errors))
            else:
                msg = (
                    "Аватарки не найдены ни в одном из выбранных файлов."
                    if self._crop_sprites
                    else "Не удалось загрузить ни один из выбранных файлов."
                )
                self.failed.emit(msg)
            return

        self.finished.emit(
            all_pngs,
            last_preview,
            last_path,
            [str(p) for p in self._paths],
            errors,
        )
