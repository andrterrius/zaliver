"""Экспорт / импорт настроек «Текст на видео» в JSON-файл."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtWidgets import QFileDialog, QMessageBox, QPushButton, QWidget

from zaliver.processing.text_overlay import TextOverlaySettings

_EXPORT_VERSION = 1
_EXPORT_KIND = "zaliver_text_overlay"
_EXPORT_FILTER = "Настройки текста (*.json);;Все файлы (*.*)"
_DEFAULT_FILENAME = "zaliver_text_overlay.json"


def normalize_text_overlay_export_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Проверить и нормализовать словарь настроек текста (включая диапазоны волны)."""
    toc = TextOverlaySettings.from_dict(raw)
    out = toc.to_dict()

    def _f(key: str, fallback: float) -> float:
        try:
            return float(raw.get(key, fallback))
        except (TypeError, ValueError):
            return float(fallback)

    waf = float(toc.wave_amp_frac)
    wfs = float(toc.wave_frame_speed)
    waf_lo = _f("wave_amp_frac_min", waf)
    waf_hi = _f("wave_amp_frac_max", waf)
    wfs_lo = _f("wave_frame_speed_min", wfs)
    wfs_hi = _f("wave_frame_speed_max", wfs)
    waf_lo = max(0.0, min(0.35, waf_lo))
    waf_hi = max(0.0, min(0.35, waf_hi))
    wfs_lo = max(0.0, min(0.25, wfs_lo))
    wfs_hi = max(0.0, min(0.25, wfs_hi))
    if waf_hi < waf_lo:
        waf_lo, waf_hi = waf_hi, waf_lo
    if wfs_hi < wfs_lo:
        wfs_lo, wfs_hi = wfs_hi, wfs_lo
    out["wave_amp_frac_min"] = waf_lo
    out["wave_amp_frac_max"] = waf_hi
    out["wave_frame_speed_min"] = wfs_lo
    out["wave_frame_speed_max"] = wfs_hi
    out["wave_amp_frac"] = (waf_lo + waf_hi) * 0.5
    out["wave_frame_speed"] = (wfs_lo + wfs_hi) * 0.5
    return out


def parse_text_overlay_file_payload(data: object) -> dict[str, Any]:
    """Разобрать JSON экспорта; вернуть нормализованный dict настроек."""
    if not isinstance(data, dict):
        raise ValueError("Корень JSON должен быть объектом.")
    kind = str(data.get("kind") or "").strip()
    if kind and kind != _EXPORT_KIND:
        raise ValueError(
            f"Неизвестный тип файла: {kind!r}. Ожидается {_EXPORT_KIND!r}."
        )
    raw = data.get("settings")
    if raw is None and any(k in data for k in ("text", "font_size", "enabled")):
        raw = data
    if not isinstance(raw, dict):
        raise ValueError("В файле нет объекта «settings» с настройками текста.")
    return normalize_text_overlay_export_dict(raw)


def export_text_overlay_settings(
    parent: QWidget,
    settings: dict[str, Any],
    *,
    default_filename: str = _DEFAULT_FILENAME,
) -> bool:
    """Сохранить настройки в JSON. True если файл записан."""
    path, _ = QFileDialog.getSaveFileName(
        parent,
        "Экспорт настроек текста",
        default_filename,
        _EXPORT_FILTER,
    )
    if not path:
        return False
    out = Path(path)
    if out.suffix.lower() != ".json":
        out = out.with_suffix(".json")
    payload = {
        "version": _EXPORT_VERSION,
        "kind": _EXPORT_KIND,
        "settings": normalize_text_overlay_export_dict(settings),
    }
    try:
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        QMessageBox.warning(
            parent,
            "Экспорт настроек текста",
            f"Не удалось сохранить файл:\n{e}",
        )
        return False
    QMessageBox.information(
        parent,
        "Экспорт настроек текста",
        f"Сохранено: {out.name}",
    )
    return True


def import_text_overlay_settings(parent: QWidget) -> dict[str, Any] | None:
    """Открыть JSON и вернуть нормализованные настройки, либо None при отмене/ошибке."""
    path, _ = QFileDialog.getOpenFileName(
        parent,
        "Импорт настроек текста",
        "",
        _EXPORT_FILTER,
    )
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
        data = json.loads(text)
        return parse_text_overlay_file_payload(data)
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
        QMessageBox.warning(
            parent,
            "Импорт настроек текста",
            f"Не удалось загрузить файл:\n{e}",
        )
        return None


def make_text_overlay_io_buttons(
    parent: QWidget,
    *,
    get_settings: Callable[[], dict[str, Any]],
    apply_settings: Callable[[dict[str, Any]], None],
) -> tuple[QPushButton, QPushButton]:
    """Кнопки «Экспорт» / «Импорт» для секции текста."""

    def _export() -> None:
        export_text_overlay_settings(parent, get_settings())

    def _import() -> None:
        settings = import_text_overlay_settings(parent)
        if settings is None:
            return
        apply_settings(settings)
        QMessageBox.information(
            parent,
            "Импорт настроек текста",
            "Настройки текста загружены из файла.",
        )

    btn_export = QPushButton("Экспорт")
    btn_export.setObjectName("secondary")
    btn_export.setAutoDefault(False)
    btn_export.setDefault(False)
    btn_export.setToolTip("Сохранить настройки текста в JSON-файл")
    btn_export.clicked.connect(_export)

    btn_import = QPushButton("Импорт")
    btn_import.setObjectName("secondary")
    btn_import.setAutoDefault(False)
    btn_import.setDefault(False)
    btn_import.setToolTip("Загрузить настройки текста из JSON-файла")
    btn_import.clicked.connect(_import)

    return btn_export, btn_import
