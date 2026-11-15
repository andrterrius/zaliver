import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from zaliver.ui.text_overlay_preview import TextOverlayPreviewWidget


def test_preview_shows_all_texts_when_selected_text_is_empty():
    app = QApplication.instance() or QApplication([])
    widget = TextOverlayPreviewWidget()
    widget.resize(360, 320)
    widget.set_text("AAAA")
    widget.set_anchor(0.5, 0.25)
    widget.set_text_color("#FF0000")
    widget.set_glow_enabled(False)
    widget.set_font_size(200)
    widget.set_wave_settings(0, 0)
    widget.set_other_layers([(1, {
        "text": "BBBB", "text_color": "#00FF00", "anchor_x": 0.5,
        "anchor_y": 0.7, "font_size": 200, "glow_enabled": False,
        "wave_amp_frac": 0, "wave_frame_speed": 0,
    })], 0)
    widget.show()
    app.processEvents()

    image = widget.grab().toImage()
    pixels = [image.pixelColor(x, y) for y in range(image.height())
              for x in range(image.width())]
    assert any(c.red() > 180 and c.green() < 80 and c.blue() < 80 for c in pixels)
    assert any(c.green() > 180 and c.red() < 80 and c.blue() < 80 for c in pixels)

    widget.set_text("")
    app.processEvents()
    image = widget.grab().toImage()
    assert any(image.pixelColor(x, y).green() > 180 and image.pixelColor(x, y).red() < 80
               for y in range(image.height()) for x in range(image.width()))
    assert widget._anim_timer.isActive()
    widget.close()
