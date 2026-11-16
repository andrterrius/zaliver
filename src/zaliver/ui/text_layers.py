"""Shared multi-text editing for slicing and stitching."""
from __future__ import annotations

import copy
import json

from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton


class TextLayersMixin:
    def _init_text_layers(self, key: str) -> None:
        self._text_layers_key = key
        self._text_layers_loading = True
        self._text_layer_index = 0
        self._text_layers = [self._current_text_overlay_options_dict()]
        try:
            saved = json.loads(self._settings.value(key, "[]", type=str) or "[]")
            if isinstance(saved, list) and saved and all(isinstance(x, dict) for x in saved):
                self._text_layers = saved
                enabled = self.text_overlay_enabled.isChecked()
                self._apply_single_text_overlay_options({**saved[0], "enabled": enabled})
        except (ValueError, TypeError):
            pass
        finally:
            self._text_layers_loading = False
        self._refresh_text_layers()
        self._sync_text_overlay_preview()

    def _add_text_layer_controls(self, layout) -> None:
        row = QHBoxLayout()
        self.text_layer_combo = QComboBox()
        self.text_layer_combo.currentIndexChanged.connect(self._select_text_layer)
        add = QPushButton("Добавить текст")
        add.clicked.connect(self._add_text_layer)
        self.text_layer_remove = QPushButton("Удалить текст")
        self.text_layer_remove.clicked.connect(self._remove_text_layer)
        row.addWidget(self.text_layer_combo, 1)
        row.addWidget(add)
        row.addWidget(self.text_layer_remove)
        layout.addLayout(row)
        hint = QLabel("Выбери текст на предпросмотре, чтобы переместить или изменить его размер.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def _refresh_text_layers(self) -> None:
        self.text_layer_combo.blockSignals(True)
        self.text_layer_combo.clear()
        for index in range(len(self._text_layers)):
            self.text_layer_combo.addItem(f"Текст {index + 1}")
        self.text_layer_combo.setCurrentIndex(self._text_layer_index)
        self.text_layer_combo.blockSignals(False)
        self.text_layer_remove.setEnabled(len(self._text_layers) > 1)

    def _save_text_layers(self) -> None:
        if not hasattr(self, "_text_layers") or self._text_layers_loading:
            return
        self._text_layers[self._text_layer_index] = self._current_text_overlay_options_dict()
        self._settings.setValue(self._text_layers_key, json.dumps(self._text_layers, ensure_ascii=False))

    def text_overlay_options_dict(self) -> dict:
        if not hasattr(self, "_text_layers"):
            return self._current_text_overlay_options_dict()
        if not self._text_layers_loading:
            self._text_layers[self._text_layer_index] = self._current_text_overlay_options_dict()
        layers = copy.deepcopy(self._text_layers)
        result = layers[0]
        result["enabled"] = self.text_overlay_enabled.isChecked()
        result["layers"] = layers[1:]
        return result

    def apply_text_overlay_options(self, raw: dict) -> None:
        from zaliver.ui.text_overlay_io import normalize_text_overlay_export_dict
        data = normalize_text_overlay_export_dict(raw if isinstance(raw, dict) else {})
        layers = [dict(data)] + [dict(x) for x in data.get("layers", []) if isinstance(x, dict)]
        layers[0].pop("layers", None)
        self._text_layers_loading = True
        try:
            self._text_layers = layers
            self._text_layer_index = 0
            self._refresh_text_layers()
            self._apply_single_text_overlay_options(layers[0])
        finally:
            self._text_layers_loading = False
        self._sync_text_overlay_preview()
        self.save_settings()
        if self._on_text_overlay_text_changed is not None:
            self._on_text_overlay_text_changed(self.text_overlay_text())

    def _select_text_layer(self, index: int) -> None:
        if getattr(self, "_text_layers_loading", True) or index < 0 or index == self._text_layer_index:
            return
        self._text_layers[self._text_layer_index] = self._current_text_overlay_options_dict()
        self._text_layer_index = index
        enabled = self.text_overlay_enabled.isChecked()
        self._text_layers_loading = True
        try:
            self._apply_single_text_overlay_options({**self._text_layers[index], "enabled": enabled})
        finally:
            self._text_layers_loading = False
        self._sync_text_overlay_preview()
        self.save_settings()

    def _add_text_layer(self) -> None:
        self._text_layers[self._text_layer_index] = self._current_text_overlay_options_dict()
        layer = copy.deepcopy(self._text_layers[self._text_layer_index])
        layer["text"] = ""
        layer["anchor_y"] = min(0.9, float(layer.get("anchor_y", 0.5)) + 0.15)
        self._text_layers.append(layer)
        self._refresh_text_layers()
        self.text_layer_combo.setCurrentIndex(len(self._text_layers) - 1)
        self.text_overlay_edit.setFocus()

    def _remove_text_layer(self) -> None:
        if len(self._text_layers) <= 1:
            return
        del self._text_layers[self._text_layer_index]
        self._text_layer_index = min(self._text_layer_index, len(self._text_layers) - 1)
        self._refresh_text_layers()
        enabled = self.text_overlay_enabled.isChecked()
        self._text_layers_loading = True
        try:
            self._apply_single_text_overlay_options({**self._text_layers[self._text_layer_index], "enabled": enabled})
        finally:
            self._text_layers_loading = False
        self._sync_text_overlay_preview()
        self.save_settings()

    def set_shared_text_overlay_text(self, text: str) -> None:
        value = text or ""
        if not hasattr(self, "_text_layers") or self._text_layer_index == 0:
            self.set_text_overlay_text(value)
        else:
            self._text_layers[0]["text"] = value
        if hasattr(self, "_text_layers"):
            self._save_text_layers()
            self._sync_text_overlay_preview()

    def _sync_other_text_layers(self, preview, *, hide_after_frame: bool = False) -> None:
        layers = getattr(self, "_text_layers", [])
        selected = getattr(self, "_text_layer_index", 0)
        preview.set_other_layers([(i, x) for i, x in enumerate(layers) if i != selected and not (hide_after_frame and x.get("after_frame_change", False))], selected)
