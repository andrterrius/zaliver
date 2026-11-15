from __future__ import annotations

import os
import subprocess
import tempfile
import shutil
from pathlib import Path
from zaliver.processing.ffmpeg_merge import resolve_ffmpeg_executable
from zaliver.processing.ffmpeg_probe import probe_video_stream
from zaliver.processing.subprocess_flags import popen_creationflags

VIDEO_BANNER_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v', '.mpeg', '.mpg', '.ts'}
ANIMATED_BANNER_EXTENSIONS = VIDEO_BANNER_EXTENSIONS | {'.gif'}

def prepare_banner_sources(paths, banners_by_video, temp_dir, *, cancel_check=None, log=None):
    """Make temporary input copies with each source's banners before scene assembly."""
    prepared = {}
    result = []
    for raw_path in paths:
        key = os.path.normcase(os.path.abspath(str(raw_path)))
        banners = banners_by_video.get(key) or []
        if not banners:
            result.append(raw_path)
            continue
        if key not in prepared:
            if cancel_check and cancel_check():
                raise RuntimeError('cancelled')
            source = Path(raw_path)
            target = Path(temp_dir) / f'banner_source_{len(prepared):05d}{source.suffix}'
            if log:
                log(f'Наложение баннеров на исходное видео: {source.name}')
            shutil.copyfile(source, target)
            render_banners(target, banners, cancel_check=cancel_check)
            prepared[key] = str(target)
        result.append(prepared[key])
    return result


def render_banners(path, banners, *, cancel_check=None):
    if not banners: return
    exe = resolve_ffmpeg_executable()
    if not exe: raise RuntimeError('FFmpeg не найден для наложения баннеров')
    w, h, _, _, _ = probe_video_stream(str(path))
    args = [exe, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(path)]
    filters = []
    for i, b in enumerate(banners):
        image = str(b['path'])
        if not Path(image).is_file(): raise RuntimeError(f'Файл баннера не найден: {image}')
        if Path(image).suffix.lower() in ANIMATED_BANNER_EXTENSIONS:
            args += ['-stream_loop', '-1', '-i', image]
        else:
            args += ['-loop', '1', '-i', image]
        bw = max(1, round(w * float(b['w']))); bh = max(1, round(h * float(b['h'])))
        x = round(w * float(b['x'])); y = round(h * float(b['y']))
        filters.append(f'[{i+1}:v]setpts=PTS-STARTPTS,format=rgba,scale={bw}:{bh}[banner{i}]')
        prev = '[0:v]' if i == 0 else f'[v{i}]'
        filters.append(f'{prev}[banner{i}]overlay={x}:{y}:shortest=1[v{i+1}]')
    output = Path(path); tmp = output.with_name(output.stem + '._banners' + output.suffix)
    args += ['-filter_complex', ';'.join(filters), '-map', f'[v{len(banners)}]', '-map', '0:a?', '-map_metadata', '0', '-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-pix_fmt', 'yuv420p', '-c:a', 'copy', str(tmp)]
    try:
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=errors, creationflags=popen_creationflags())
            while proc.poll() is None:
                if cancel_check and cancel_check():
                    proc.terminate(); proc.wait(); raise RuntimeError('cancelled')
                try: proc.wait(timeout=.25)
                except subprocess.TimeoutExpired: pass
            if proc.returncode:
                errors.seek(0); raise RuntimeError('Ошибка наложения баннеров: ' + errors.read().decode('utf-8', 'replace')[-3000:])
        os.replace(tmp, output)
    finally:
        tmp.unlink(missing_ok=True)
