"""Фоновая нарезка аватарок из одного или нескольких спрайт-листов (Qt thread)."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from zaliver.ui.avatar_detection import extract_avatar_pngs_from_path, load_image_file_as_png


class AvatarDetectionCancel:
    """Флаг отмены; проверяется между файлами."""

    def __init__(self) -> None:
        self.cancelled = False


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

    def run(self) -> None:
        if not self._paths:
            self.failed.emit("Не выбрано ни одного файла.")
            return

        all_pngs: list[bytes] = []
        last_preview = b""
        last_path = ""
        errors: list[str] = []
        total = len(self._paths)

        for idx, path in enumerate(self._paths):
            if self._cancel.cancelled:
                self.aborted.emit()
                return

            self.progress.emit(idx, total, path.name)
            try:
                if self._crop_sprites:
                    pngs, _boxes, preview_image = extract_avatar_pngs_from_path(
                        path,
                        padding=2,
                        square=True,
                    )
                    if not pngs:
                        errors.append(f"{path.name}: аватарки не найдены")
                        continue
                    all_pngs.extend(bytes(p) for p in pngs if p)
                    buf = BytesIO()
                    preview_image.save(buf, format="PNG")
                    last_preview = buf.getvalue()
                else:
                    png_bytes = load_image_file_as_png(path)
                    if not png_bytes:
                        errors.append(f"{path.name}: пустой файл")
                        continue
                    all_pngs.append(png_bytes)
                    last_preview = png_bytes
            except OSError as e:
                errors.append(f"{path.name}: не удалось прочитать ({e})")
                continue
            except Exception as e:
                errors.append(f"{path.name}: ошибка обработки ({e})")
                continue

            last_path = str(path)
            self.file_preview.emit(last_preview, last_path)

        if self._cancel.cancelled:
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
