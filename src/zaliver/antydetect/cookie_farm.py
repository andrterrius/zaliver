"""Последовательный обход сайтов с медленной прокруткой для фарма Cookie."""

from __future__ import annotations

import random
import time

from zaliver.antydetect.cookie_farm_domains import domain_to_url

_log_sink: object | None = None


def set_log_sink(sink) -> None:
    global _log_sink
    _log_sink = sink


def _log(message: str) -> None:
    line = f"[cookie_farm] {message}"
    sink = _log_sink
    if sink is not None:
        try:
            sink(line)
            return
        except Exception:
            pass
    print(line, flush=True)


def _slow_scroll_for_duration(page, duration_s: float) -> None:
    end = time.monotonic() + max(0.5, float(duration_s))
    while time.monotonic() < end:
        remaining_ms = int((end - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        step_ms = min(random.randint(500, 1200), remaining_ms)
        try:
            page.evaluate(
                """() => {
                    const h = window.innerHeight || 800;
                    const step = Math.max(30, Math.floor(h * (0.05 + Math.random() * 0.07)));
                    const maxY = Math.max(0, (document.body?.scrollHeight || 0) - h);
                    if (window.scrollY >= maxY - 5) {
                        window.scrollTo({ top: 0, behavior: 'smooth' });
                    } else {
                        window.scrollBy({ top: step, behavior: 'smooth' });
                    }
                }"""
            )
        except Exception:
            pass
        page.wait_for_timeout(step_ms)


def run_cookie_farm(
    page,
    *,
    domains: list[str],
    sites_count: int,
    watch_min_s: float,
    watch_max_s: float,
) -> None:
    """Открывает сайты по очереди и медленно прокручивает страницу до истечения времени."""
    available = [d for d in domains if (d or "").strip()]
    if not available:
        raise ValueError("Список доменов для фарма Cookie пуст.")
    n = max(1, min(int(sites_count), len(available)))
    sites = available[:n]
    watch_min = max(10.0, float(watch_min_s))
    watch_max = max(watch_min, float(watch_max_s))
    _log(
        f"Старт: {len(sites)} сайтов из {len(available)}, "
        f"просмотр {watch_min:.0f}–{watch_max:.0f} с на каждом…"
    )
    for i, domain in enumerate(sites, 1):
        url = domain_to_url(domain)
        _log(f"Сайт {i}/{len(sites)}: {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(1_500)
        except Exception as e:
            _log(f"Не удалось открыть {url}: {type(e).__name__}")
            continue
        watch_s = random.uniform(watch_min, watch_max)
        _log(f"Прокрутка ~{watch_s:.0f} с…")
        _slow_scroll_for_duration(page, watch_s)
    _log(f"Завершено ({len(sites)} сайтов).")
