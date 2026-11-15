from zaliver.processing import text_overlay as overlay
from zaliver.processing.pipeline import materialize_text_overlay_ranges
from zaliver.ui.text_overlay_io import normalize_text_overlay_export_dict


def test_multiple_texts_roundtrip_and_filter_graph(monkeypatch):
    monkeypatch.setattr(overlay, "_qt_metrics_available", lambda: False)
    monkeypatch.setattr("zaliver.processing.ffmpeg_merge.ffmpeg_drawtext_has_y_align", lambda: False)
    raw = {
        "text": "FIRST",
        "from_middle": False,
        "glow_enabled": False,
        "layers": [{"text": "SECOND", "anchor_y": 0.7, "opacity": 0.5,
                    "outline_width": 4, "outline_color": "#FF0000",
                    "from_middle": False, "glow_enabled": False}],
    }
    normalized = normalize_text_overlay_export_dict(raw)
    settings = overlay.TextOverlaySettings.from_dict(materialize_text_overlay_ranges(normalized))
    scaled = overlay.compute_scaled_overlay_for_worker(settings, 1080, 1920)
    assert scaled is not None and len(scaled.layers) == 1
    restored = overlay.ScaledTextOverlay.from_dict(scaled.to_dict())
    assert restored.layers[0].opacity == 0.5
    assert restored.layers[0].outline_width == 4
    graph = overlay.build_text_overlay_filters(restored, frame_count=30, total_frames=30).graph
    assert "[overlay_layer_0]" in graph
    assert "borderw=4" in graph
    assert "0xFF0000@0.500" in graph
    assert graph.endswith("[outv]")


def test_empty_first_text_does_not_hide_other_texts(monkeypatch):
    monkeypatch.setattr(overlay, "_qt_metrics_available", lambda: False)
    settings = overlay.TextOverlaySettings.from_dict({
        "text": "", "layers": [{"text": "Only second", "enabled": False}]
    })
    scaled = overlay.compute_scaled_overlay_for_worker(settings, 320, 568)
    assert scaled is not None
    assert scaled.lines == ["Only second"]
    assert not scaled.layers
