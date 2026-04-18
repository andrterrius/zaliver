"""Shared memory helpers for chunk-sized frame buffers (optional path)."""

from __future__ import annotations

from dataclasses import dataclass
from multiprocessing import shared_memory
from typing import Optional, Tuple

import numpy as np


def estimate_buffer_bytes(
    width: int, height: int, frame_count: int, channels: int = 3
) -> int:
    return int(width) * int(height) * int(channels) * int(frame_count)


@dataclass
class ShmArrayHandle:
    name: str
    shape: Tuple[int, ...]
    dtype: str


def create_shm_numpy(
    shape: Tuple[int, ...], dtype: np.dtype = np.uint8
) -> tuple[shared_memory.SharedMemory, np.ndarray]:
    size = int(np.prod(shape)) * np.dtype(dtype).itemsize
    shm = shared_memory.SharedMemory(create=True, size=size)
    arr = np.ndarray(shape, dtype=dtype, buffer=shm.buf)
    return shm, arr


def attach_shm_numpy(
    name: str, shape: Tuple[int, ...], dtype: np.dtype = np.uint8
) -> tuple[shared_memory.SharedMemory, np.ndarray]:
    shm = shared_memory.SharedMemory(name=name)
    arr = np.ndarray(shape, dtype=dtype, buffer=shm.buf)
    return shm, arr


def close_shm(shm: Optional[shared_memory.SharedMemory], unlink: bool) -> None:
    if shm is None:
        return
    try:
        shm.close()
    except FileNotFoundError:
        pass
    if unlink:
        try:
            shm.unlink()
        except FileNotFoundError:
            pass


def should_use_shm_for_chunk(
    width: int, height: int, frame_count: int, threshold_bytes: int
) -> bool:
    return estimate_buffer_bytes(width, height, frame_count) <= threshold_bytes
