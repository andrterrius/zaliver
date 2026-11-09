"""Build ffmpeg filter graphs for uniquification (no OpenCV)."""

from __future__ import annotations

import random
from typing import Optional, Tuple

from zaliver.processing.pipeline import UniquifySettings
from zaliver.processing.text_overlay import ScaledTextOverlay, build_text_overlay_filters


def _even_dim(x: int) -> int:
    return max(2, int(x) - (int(x) % 2))


def _generated_frame_pattern(w: int, h: int, seed: int) -> str:
    """Generate a repeatable, layered geometric pattern for one output video."""
    rng = random.Random(seed)
    cell = max(32, min(w, h) // rng.randint(6, 10))
    ox, oy = rng.randrange(cell), rng.randrange(cell)
    kind = rng.choice(("outline", "cross", "tiles"))
    filters = []
    for row, y in enumerate(range(oy - cell, h, cell)):
        for col, x in enumerate(range(ox - cell, w, cell)):
            if x < 0 or y < 0:
                continue
            size = max(4, int(cell * rng.uniform(.18, .5)))
            alpha = rng.uniform(.07, .22)
            thickness = max(1, cell // 35)
            if kind == "cross":
                bar = max(2, size // 5)
                filters.append(f"drawbox=x={x}:y={y + size // 2}:w={size}:h={bar}:color=white@{alpha:.3f}:t=fill")
                filters.append(f"drawbox=x={x + size // 2}:y={y}:w={bar}:h={size}:color=white@{alpha:.3f}:t=fill")
            else:
                fill = "fill" if kind == "tiles" and (row + col) % 2 else str(thickness)
                filters.append(f"drawbox=x={x}:y={y}:w={size}:h={size}:color=white@{alpha:.3f}:t={fill}")
    return ",".join(filters)


def _normalize_sar_block() -> str:
    """К square pixels до любых scale/crop (иначе SAR даёт «растянутое» видео)."""
    return "scale=iw*sar:ih,setsar=1"


def _fit_inside_scale(w: int, h: int) -> str:
    """Вписать в w×h с увеличением, если исходник мельче. Пропорции сохраняются."""
    return (
        f"scale=w='trunc(iw*min({int(w)}/iw\\,{int(h)}/ih)/2)*2'"
        f":h='trunc(ih*min({int(w)}/iw\\,{int(h)}/ih)/2)*2':flags=lanczos"
    )


def _final_scale_block(w_out: int, h_out: int) -> str:
    """Вписать в кадр. Незаполненная высота закрывается чёрным сверху и снизу."""
    return (
        f"{_fit_inside_scale(w_out, h_out)},"
        f"pad={w_out}:{h_out}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )


def _uniquify_final_scale_block(w_out: int, h_out: int) -> str:
    """Output dimensions already follow the source aspect ratio (within two pixels)."""
    return f"scale={w_out}:{h_out}:flags=lanczos,setsar=1"


def _fit_scale_pad(w: int, h: int) -> str:
    """Fit into w×h without stretching; letterbox/pillarbox with black if needed."""
    return (
        f"{_fit_inside_scale(w, h)},"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
    )


def _scale_pct_block(w: int, h: int, scale_pct: float) -> str:
    f = float(scale_pct) / 100.0
    if abs(f - 1.0) < 1e-6:
        return ""
    nw = max(2, _even_dim(int(round(w * f))))
    nh = max(2, _even_dim(int(round(h * f))))
    sc = f"scale={nw}:{nh}:flags=lanczos"
    if nw >= w and nh >= h:
        x0 = max(0, (nw - w) // 2)
        y0 = max(0, (nh - h) // 2)
        return f"{sc},crop={w}:{h}:{x0}:{y0}"
    x0 = max(0, (w - nw) // 2)
    y0 = max(0, (h - nh) // 2)
    return f"{sc},pad={w}:{h}:{x0}:{y0}:black"


def _crop_jitter_block(
    w: int, h: int, crop: Optional[Tuple[int, int, int, int]]
) -> str:
    if crop is None:
        return ""
    t, b, l, r = (int(crop[0]), int(crop[1]), int(crop[2]), int(crop[3]))
    iw = w - l - r
    ih = h - t - b
    if iw <= 2 or ih <= 2:
        return ""
    return f"crop={iw}:{ih}:{l}:{t},{_fit_scale_pad(w, h)}"


def _eq_block(settings: UniquifySettings) -> str:
    c = float(settings.contrast)
    b = max(-1.0, min(1.0, float(settings.brightness_delta) / 255.0))
    sat = float(settings.saturation_scale)
    parts = [f"eq=contrast={c:.6f}:brightness={b:.6f}:saturation={sat:.6f}"]
    ns = float(settings.noise_sigma)
    if ns > 1e-6:
        amt = int(min(90, max(1, round(ns * 6.0))))
        parts.append(f"noise=alls={amt}:allf=t+u")
    return ",".join(parts)


def build_uniquify_filtergraph(
    *,
    start_frame: int,
    frame_count: int,
    settings: UniquifySettings,
    crop: Optional[Tuple[int, int, int, int]],
    w: int,
    h: int,
    w_out: int,
    h_out: int,
    text_overlay: Optional[ScaledTextOverlay] = None,
    total_frames: int = 0,
    fps: float = 30.0,
) -> tuple[str, list[str]]:
    """
    Full -filter_complex graph: one video input [0:v] -> uniquified [outv].
    Returns (graph, extra_input_argv) for optional color emoji stills.
    """
    s = int(start_frame)
    fc = int(frame_count)
    e = s + fc
    head = (
        f"trim=start_frame={s}:end_frame={e},setpts=PTS-STARTPTS,"
        f"{_normalize_sar_block()}"
    )

    tail: list[str] = []
    cj = _crop_jitter_block(w, h, crop)
    if cj:
        tail.append(cj)
    tail.append(_eq_block(settings))
    random_scale = max(0.1, float(settings.scale_pct) / 100.0)
    sx = max(0.1, min(20.0, float(settings.scale_x) * random_scale))
    sy = max(0.1, min(20.0, float(settings.scale_y) * random_scale))
    px = max(-10.0, min(10.0, float(settings.position_x)))
    py = max(-10.0, min(10.0, float(settings.position_y)))
    left = max(0.0, min(.95, float(settings.crop_left)))
    top = max(0.0, min(.95, float(settings.crop_top)))
    right = max(0.0, min(.95 - left, float(settings.crop_right)))
    bottom = max(0.0, min(.95 - top, float(settings.crop_bottom)))
    # Keep the visible crop anchored when the random scale changes its size.
    # Positions in the editor refer to the unrandomized scale.
    base_sx = max(0.1, min(20.0, float(settings.scale_x)))
    base_sy = max(0.1, min(20.0, float(settings.scale_y)))
    px += (sx - base_sx) * (right - left) / 2
    py += (sy - base_sy) * (bottom - top) / 2
    background = settings.scale_background if settings.scale_background in ("black", "blur", "solid", "pattern") else "black"
    simple_black = background == "black" and abs(px) < 0.0001 and abs(py) < 0.0001 and not any((left, top, right, bottom))
    if simple_black and (abs(sx - 1.0) > 0.0001 or abs(sy - 1.0) > 0.0001):
        nw, nh = _even_dim(round(w * sx)), _even_dim(round(h * sy))
        pw, ph = max(w, nw), max(h, nh)
        tail.append(f"scale={nw}:{nh}:flags=lanczos,pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2:black,crop={w}:{h}:(iw-ow)/2:(ih-oh)/2")
    if simple_black:
        tail.append(f"format=yuv420p,{_uniquify_final_scale_block(w_out, h_out)}")
        base = f"[0:v]{head},{','.join(tail)}[v0]"
    else:
        nw, nh = _even_dim(round(w * sx)), _even_dim(round(h * sy))
        color = str(settings.frame_color)
        if len(color) != 6 or any(c not in '0123456789abcdefABCDEF' for c in color):
            color = '283957'
        if background == "black":
            bg = f"scale={w}:{h},drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill"
        elif background == "blur":
            radius = max(0, min(40, int(settings.blur_strength), (min(w, h) - 2) // 4))
            factor = 1.0 - max(0, min(80, int(settings.blur_darkness))) / 100.0
            bg = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
            if radius:
                bg += f",boxblur={radius}:2"
            if factor < 1.0:
                bg += f",colorchannelmixer=rr={factor:.2f}:gg={factor:.2f}:bb={factor:.2f}"
        else:
            if settings.frame_gradient:
                # Interpolate cyclically through all chosen colors.
                colors = [color]
                for item in settings.frame_gradient_colors:
                    item = str(item).lstrip("#")
                    if len(item) == 6 and all(c in "0123456789abcdefABCDEF" for c in item):
                        colors.append(item)
                speed = max(0, min(200, int(settings.frame_gradient_speed))) / 100
                count = len(colors)
                if settings.frame_gradient_type == "radial":
                    spatial = "sqrt(pow(X-W/2,2)+pow(Y-H/2,2))/sqrt(pow(W/2,2)+pow(H/2,2))"
                elif settings.frame_gradient_type == "conical":
                    spatial = "atan2(H/2-Y,X-W/2)/(2*PI)"
                else:
                    spatial = "(X/W+Y/H)/2"
                pos = f"mod(mod(({spatial})*{count}-T*{0.55 * speed * count / (2 * 3.141592654):.6f},{count})+{count},{count})"
                rgb = [[int(c[i:i+2], 16) for i in (0, 2, 4)] for c in colors]
                def gradient_channel(channel_index):
                    if count == 1:
                        return str(rgb[0][channel_index])
                    terms = []
                    for i, values in enumerate(rgb):
                        distance = f"min(abs({pos}-{i}),{count}-abs({pos}-{i}))"
                        terms.append(f"{values[channel_index]}*(1+cos(PI*min(1,{distance})))/2")
                    return "+".join(terms)
                # A smooth gradient needs far fewer samples than the finished video.
                # Keep the aspect ratio for circular and conical gradients.
                gradient_scale = min(1.0, 320.0 / max(w, h))
                gradient_w = _even_dim(max(2, round(w * gradient_scale)))
                gradient_h = _even_dim(max(2, round(h * gradient_scale)))
                gradient_animated = speed > 0 and count > 1
                source = "" if gradient_animated else "trim=end_frame=1,setpts=PTS-STARTPTS,"
                bg = f"{source}scale={gradient_w}:{gradient_h},format=gbrp,geq=" + ":".join(
                    f"{channel}='{gradient_channel(i)}'" for i, channel in enumerate(("r", "g", "b"))
                )
                bg += f",scale={w}:{h}:flags=bilinear"
            else:
                # Static backgrounds are rendered once and reused.
                bg = f"trim=end_frame=1,setpts=PTS-STARTPTS,scale={w}:{h},drawbox=x=0:y=0:w=iw:h=ih:color=0x{color}:t=fill"
            if background == "pattern":
                seed = int(settings.frame_seed)
                step = max(32, min(w, h) // (7 + seed % 4))
                offset = seed % step
                thickness = max(1, step // 32)
                if settings.frame_pattern == "curved_grid":
                    # Inverse of a radial quadratic warp, evaluated per pixel.
                    gx = "(X-W/2)/(1+0.35*pow((Y-H/2)/(H/2),2))+W/2"
                    gy = "(Y-H/2)/(1+0.35*pow((X-W/2)/(W/2),2))+H/2"
                    spacing_x, spacing_y = max(4, w / 8), max(4, h / 8)
                    line_x = f"lt(abs(mod({gx}+{spacing_x/2:.3f},{spacing_x:.3f})-{spacing_x/2:.3f}),{max(1,w/400):.3f})"
                    line_y = f"lt(abs(mod({gy}+{spacing_y/2:.3f},{spacing_y:.3f})-{spacing_y/2:.3f}),{max(1,h/400):.3f})"
                    mask = f"min(1,{line_x}+{line_y})"
                    if settings.frame_gradient and gradient_animated:
                        # The grid does not move: render it once, then blend it
                        # over each animated gradient frame.
                        grid = ":".join(f"{channel}='120*{mask}'" for channel in ("r", "g", "b"))
                        bg += (f"[gradient_base];color=c=black:s={w}x{h}:r={max(0.01, float(fps)):.6f},"
                               f"trim=end_frame=1,format=gbrp,geq={grid},"
                               f"loop=loop=-1:size=1:start=0,setpts=N/({max(0.01, float(fps)):.6f}*TB)[grid_mask];"
                               f"[gradient_base][grid_mask]blend=all_mode=addition:shortest=1")
                    else:
                        bg += ",format=gbrp,geq=" + ":".join(
                            f"{channel}='min(255,{channel}(X,Y)+120*{mask})'"
                            for channel in ("r", "g", "b")
                        )
                elif settings.frame_pattern == "random":
                    generated = _generated_frame_pattern(w, h, seed)
                    if generated:
                        bg += "," + generated
                elif settings.frame_pattern == "grid":
                    for x in range(offset, w, step):
                        bg += f",drawbox=x={x}:y=0:w={thickness}:h=ih:color=white@0.18:t=fill"
                    for y in range(offset, h, step):
                        bg += f",drawbox=x=0:y={y}:w=iw:h={thickness}:color=white@0.18:t=fill"
                elif settings.frame_pattern == "mosaic":
                    size = max(4, step // 2)
                    count_x = max(1, (w - size) // step + 1)
                    count_y = max(1, (h - size) // step + 1)
                    start_x = (w - (count_x - 1) * step - size) // 2
                    start_y = (h - (count_y - 1) * step - size) // 2
                    for ix in range(count_x):
                        for iy in range(count_y):
                            x, y = start_x + ix * step, start_y + iy * step
                            opacity = (10 + ((ix * 17 + iy * 31 + seed) % 18)) / 100
                            bg += f",drawbox=x={x}:y={y}:w={size}:h={size}:color=white@{opacity:.2f}:t=fill"
                elif settings.frame_pattern == "stripes":
                    for x in range(offset, w, step):
                        bg += f",drawbox=x={x}:y=0:w={max(2, step // 7)}:h=ih:color=white@0.12:t=fill"
                else:
                    for x in range(offset, w, step):
                        for y in range(offset, h, step):
                            if settings.frame_pattern == "dots":
                                size = max(3, step // 9)
                                bg += f",drawbox=x={x}:y={y}:w={size}:h={size}:color=white@0.28:t=fill"
                            elif settings.frame_pattern == "mosaic":
                                size = max(4, step // 2)
                                opacity = (10 + ((x // step * 17 + y // step * 31 + seed) % 18)) / 100
                                bg += f",drawbox=x={x}:y={y}:w={size}:h={size}:color=white@{opacity:.2f}:t=fill"
                            else:
                                size = max(4, step // 3)
                                bg += f",drawbox=x={x}:y={y}:w={size}:h={size}:color=white@0.2:t={thickness}"
            if not settings.frame_gradient or not gradient_animated:
                bg += f",loop=loop=-1:size=1:start=0,setpts=N/({max(0.01, float(fps)):.6f}*TB)"
        cl = min(nw - 2, int(nw * left) // 2 * 2)
        ct = min(nh - 2, int(nh * top) // 2 * 2)
        cw = max(2, int(nw * (1-left-right)) // 2 * 2)
        ch = max(2, int(nh * (1-top-bottom)) // 2 * 2)
        cw, ch = min(cw, nw-cl), min(ch, nh-ct)
        src_l = min(w - 2, int(w * left) // 2 * 2)
        src_t = min(h - 2, int(h * top) // 2 * 2)
        src_w = min(w - src_l, max(2, int(w * (1-left-right)) // 2 * 2))
        src_h = min(h - src_t, max(2, int(h * (1-top-bottom)) // 2 * 2))
        x = (w-cw)/2 + px*w + (left-right)*nw/2
        y = (h-ch)/2 + py*h + (top-bottom)*nh/2
        foreground = f"crop={src_w}:{src_h}:{src_l}:{src_t},scale={cw}:{ch}:flags=lanczos"
        base = (f"[0:v]{head},{','.join(tail)},split=2[fgsrc][bgsrc];"
                f"[bgsrc]{bg}[bg];[fgsrc]{foreground}[fg];"
                f"[bg][fg]overlay={x:.3f}:{y:.3f}:shortest=1,format=yuv420p,"
                f"{_uniquify_final_scale_block(w_out, h_out)}[v0]")
    if text_overlay and text_overlay.lines:
        built = build_text_overlay_filters(
            text_overlay,
            "v0",
            start_frame=s,
            frame_count=fc,
            total_frames=int(total_frames),
            fps=float(fps),
            emoji_input_start=1,
        )
        return f"{base};{built.graph}", list(built.emoji_input_argv)
    return f"{base};[v0]null[outv]", []
