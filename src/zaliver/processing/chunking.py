"""Video metadata and chunk boundaries (streaming-friendly)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from zaliver.processing.ffmpeg_probe import probe_video_stream

# Reserved for optical-flow / temporal filters that need boundary context
OVERLAP_FRAMES_DEFAULT = 0


@dataclass(frozen=True)
class VideoInfo:
    path: str
    width: int
    height: int
    fps: float
    frame_count: int
    fourcc: int


@dataclass(frozen=True)
class ChunkSpec:
    index: int
    start_frame: int
    frame_count: int


def probe_video(path: str) -> VideoInfo:
    w, h, fps, n, fourcc = probe_video_stream(path)
    if n <= 0:
        raise RuntimeError(f"Не удалось определить длительность/кадры: {path}")
    if w <= 0 or h <= 0:
        raise RuntimeError("Некорректный размер кадра")
    return VideoInfo(
        path=path,
        width=w,
        height=h,
        fps=fps,
        frame_count=n,
        fourcc=fourcc,
    )


def build_n_even_chunks(total_frames: int, n_chunks: int) -> List[ChunkSpec]:
    """
    Split [0, total_frames) into contiguous ranges without overlap.
    Used to parallelize one video across workers; concat must follow chunk order.
    """
    total = int(total_frames)
    if total <= 0:
        return []
    n = max(1, min(int(n_chunks), total))
    if n == 1:
        return [ChunkSpec(index=0, start_frame=0, frame_count=total)]
    chunks: List[ChunkSpec] = []
    base = total // n
    rem = total % n
    start = 0
    for i in range(n):
        sz = base + (1 if i < rem else 0)
        if sz <= 0:
            continue
        chunks.append(
            ChunkSpec(index=len(chunks), start_frame=start, frame_count=sz)
        )
        start += sz
    return chunks


def build_chunks(
    info: VideoInfo,
    chunk_seconds: float,
    max_frames_per_chunk: int = 300,
    overlap_frames: int = OVERLAP_FRAMES_DEFAULT,
) -> List[ChunkSpec]:
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be positive")
    if max_frames_per_chunk <= 0:
        raise ValueError("max_frames_per_chunk must be positive")
    if overlap_frames < 0:
        raise ValueError("overlap_frames must be non-negative")
    if overlap_frames >= max_frames_per_chunk:
        raise ValueError("overlap_frames must be smaller than max_frames_per_chunk")

    by_time = max(1, int(info.fps * chunk_seconds))

    chunks: List[ChunkSpec] = []
    idx = 0
    start = 0
    total = info.frame_count
    while start < total:
        end = min(start + min(max_frames_per_chunk, by_time), total)
        count = end - start
        if count <= 0:
            break
        chunks.append(ChunkSpec(index=idx, start_frame=start, frame_count=count))
        idx += 1
        if end >= total:
            break
        start = end - overlap_frames if overlap_frames else end
    return chunks


def estimate_chunk_buffer_bytes(
    width: int, height: int, frame_count: int, channels: int = 3
) -> int:
    return width * height * channels * frame_count
