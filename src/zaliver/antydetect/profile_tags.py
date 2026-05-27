"""Теги, которые Zaliver проставляет в профилях локального антидетекта (HTTP API)."""

from __future__ import annotations

# Неуспешная проверка доступности YouTube Studio.
STUDIO_AVAILABILITY_ERROR_TAG = "ОШИБКА ПРОВЕРКИ ДОСТУПНОСТИ"

# Результат последней попытки залива (взаимоисключающие).
UPLOAD_PREVIOUS_SUCCESS_TAG = "УСПЕШНЫЙ ПРОШЛЫЙ ЗАЛИВ"
UPLOAD_PREVIOUS_ERROR_TAG = "ОШИБКА ПРОШЛОГО ЗАЛИВА"
PREVIOUS_UPLOAD_RESULT_TAGS: tuple[str, ...] = (
    UPLOAD_PREVIOUS_SUCCESS_TAG,
    UPLOAD_PREVIOUS_ERROR_TAG,
)

# Три подряд неудачных залива на профиль.
UPLOAD_ERROR_3X_TAG = "upload_error_3x"

# Все названия тегов, которые программа может добавить в профиль.
ZALIVER_PROFILE_TAGS: tuple[str, ...] = (
    STUDIO_AVAILABILITY_ERROR_TAG,
    UPLOAD_PREVIOUS_SUCCESS_TAG,
    UPLOAD_PREVIOUS_ERROR_TAG,
    UPLOAD_ERROR_3X_TAG,
)


def clear_zaliver_tags_on_profile(api: object, profile_id: str) -> int:
    """
    Снимает с профиля все теги из ZALIVER_PROFILE_TAGS.
    Возвращает число успешных DELETE (отсутствующий тег — без ошибки).
    """
    from zaliver.antydetect.local_antidetect_api import LocalAntidetectHttpAPI

    if not isinstance(api, LocalAntidetectHttpAPI):
        raise TypeError("api must be LocalAntidetectHttpAPI")
    pid = (profile_id or "").strip()
    if not pid:
        return 0
    removed = 0
    for tag in ZALIVER_PROFILE_TAGS:
        try:
            api.remove_profile_tag(pid, tag)
            removed += 1
        except Exception:
            pass
    return removed
