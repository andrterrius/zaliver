"""Per-user concurrency limits for the web API."""

from __future__ import annotations

from zaliver.api.jobs_registry import JobKind, JobRecord, JobRegistry, JobStatus

# File processing: strictly one worker thread per user job.
PROCESSING_WORKERS_PER_USER = 1

# Browser windows open at once across all jobs of one user.
MAX_BROWSERS_PER_USER = 10

PROCESSING_KINDS: frozenset[JobKind] = frozenset(
    {
        JobKind.UNIQUIFY,
        JobKind.SLICING,
        JobKind.STITCHING,
    }
)

BROWSER_KINDS: frozenset[JobKind] = frozenset(
    {
        JobKind.UPLOAD,
        JobKind.AVAILABILITY,
        JobKind.INSTAGRAM_REGISTER,
        JobKind.INSTAGRAM_2FA,
        JobKind.CHANNEL_SETUP,
        JobKind.WARMUP,
        JobKind.PROMOTE,
        JobKind.COOKIE_FARM,
        JobKind.STATS_REFRESH,
    }
)


def clamp_browsers_per_user(value: int | float | str | None) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = MAX_BROWSERS_PER_USER
    return max(1, min(MAX_BROWSERS_PER_USER, n))


def _active_jobs(registry: JobRegistry) -> list[JobRecord]:
    # Access via list_jobs would rebuild snapshots; walk in-memory records.
    with registry._lock:  # noqa: SLF001 — intentional for live concurrency checks
        jobs = list(registry._jobs.values())  # noqa: SLF001
    return [
        j
        for j in jobs
        if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)
    ]


def assert_can_start_processing(registry: JobRegistry, username: str) -> None:
    owner = (username or "").strip().lower()
    for job in _active_jobs(registry):
        if job.kind not in PROCESSING_KINDS:
            continue
        if (job.owner or "").strip().lower() == owner:
            raise RuntimeError(
                "У вас уже есть активная задача обработки. "
                "Дождитесь завершения или отмените её "
                "(лимит: 1 поток на пользователя)."
            )


def assert_browser_budget(
    registry: JobRegistry,
    username: str,
    *,
    requested_slots: int,
) -> int:
    """Return clamped slots; raise if the user would exceed the browser cap."""
    slots = clamp_browsers_per_user(requested_slots)
    owner = (username or "").strip().lower()
    used = 0
    for job in _active_jobs(registry):
        if job.kind not in BROWSER_KINDS:
            continue
        if (job.owner or "").strip().lower() != owner:
            continue
        used += max(0, int(job.browser_slots or 0))
    if used + slots > MAX_BROWSERS_PER_USER:
        remaining = max(0, MAX_BROWSERS_PER_USER - used)
        if remaining <= 0:
            raise RuntimeError(
                f"Лимит браузеров для пользователя исчерпан "
                f"(макс. {MAX_BROWSERS_PER_USER} одновременно)."
            )
        slots = remaining
    return slots
