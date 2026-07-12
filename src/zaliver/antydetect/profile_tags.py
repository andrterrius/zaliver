"""Теги, которые Zaliver проставляет в профилях локального антидетекта (HTTP API)."""

from __future__ import annotations

# Проверка доступности YouTube Studio (взаимоисключающие).
STUDIO_AVAILABILITY_ERROR_TAG = "ОШИБКА ПРОВЕРКИ ДОСТУПНОСТИ"
STUDIO_AVAILABILITY_SUCCESS_TAG = "УСПЕШНАЯ ПРОВЕРКА ДОСТУПНОСТИ"
STUDIO_AVAILABILITY_RESULT_TAGS: tuple[str, ...] = (
    STUDIO_AVAILABILITY_ERROR_TAG,
    STUDIO_AVAILABILITY_SUCCESS_TAG,
)

# Смена аватарки канала.
AVATAR_CHANGE_ERROR_TAG = "ОШИБКА СМЕНЫ АВАТАРКИ"
AVATAR_CHANGE_SUCCESS_TAG = "УСПЕШНАЯ СМЕНА АВАТАРКИ"
AVATAR_CHANGE_RESULT_TAGS: tuple[str, ...] = (
    AVATAR_CHANGE_ERROR_TAG,
    AVATAR_CHANGE_SUCCESS_TAG,
)

# Смена названия канала.
NAME_CHANGE_ERROR_TAG = "ОШИБКА СМЕНЫ НАЗВАНИЯ"
NAME_CHANGE_SUCCESS_TAG = "УСПЕШНАЯ СМЕНА НАЗВАНИЯ"
NAME_CHANGE_RESULT_TAGS: tuple[str, ...] = (
    NAME_CHANGE_ERROR_TAG,
    NAME_CHANGE_SUCCESS_TAG,
)

# Прогрев Shorts.
WARMUP_ERROR_TAG = "ОШИБКА ПРОГРЕВА"
WARMUP_SUCCESS_TAG = "УСПЕШНЫЙ ПРОГРЕВ"
WARMUP_RESULT_TAGS: tuple[str, ...] = (
    WARMUP_ERROR_TAG,
    WARMUP_SUCCESS_TAG,
)

# Смена языка интерфейса YouTube.
LANGUAGE_CHANGE_ERROR_TAG = "ОШИБКА СМЕНЫ ЯЗЫКА"
LANGUAGE_CHANGE_SUCCESS_TAG = "УСПЕШНАЯ СМЕНА ЯЗЫКА"
LANGUAGE_CHANGE_RESULT_TAGS: tuple[str, ...] = (
    LANGUAGE_CHANGE_ERROR_TAG,
    LANGUAGE_CHANGE_SUCCESS_TAG,
)

# Фарм Cookie.
COOKIE_FARM_ERROR_TAG = "ОШИБКА ФАРМА КУКИ"
COOKIE_FARM_SUCCESS_TAG = "УСПЕШНО ЗАФАРМИЛ КУКИ"
COOKIE_FARM_RESULT_TAGS: tuple[str, ...] = (
    COOKIE_FARM_ERROR_TAG,
    COOKIE_FARM_SUCCESS_TAG,
)

# Заполнение описания канала.
DESCRIPTION_FILL_ERROR_TAG = "ОШИБКА ЗАПОЛНЕНИЯ ОПИСАНИЯ"
DESCRIPTION_FILL_SUCCESS_TAG = "УСПЕШНОЕ ЗАПОЛНЕНИЕ ОПИСАНИЯ"
DESCRIPTION_FILL_RESULT_TAGS: tuple[str, ...] = (
    DESCRIPTION_FILL_ERROR_TAG,
    DESCRIPTION_FILL_SUCCESS_TAG,
)

# Заполнение ссылки канала.
LINK_FILL_ERROR_TAG = "ОШИБКА ЗАПОЛНЕНИЯ ССЫЛКИ"
LINK_FILL_SUCCESS_TAG = "УСПЕШНОЕ ЗАПОЛНЕНИЕ ССЫЛКИ"
LINK_FILL_RESULT_TAGS: tuple[str, ...] = (
    LINK_FILL_ERROR_TAG,
    LINK_FILL_SUCCESS_TAG,
)

# Смена названия по умолчанию для загрузки видео.
VIDEO_TITLE_CHANGE_ERROR_TAG = "ОШИБКА СМЕНЫ НАЗВАНИЯ ВИДЕО"
VIDEO_TITLE_CHANGE_SUCCESS_TAG = "УСПЕШНАЯ СМЕНА НАЗВАНИЯ ВИДЕО"
VIDEO_TITLE_CHANGE_RESULT_TAGS: tuple[str, ...] = (
    VIDEO_TITLE_CHANGE_ERROR_TAG,
    VIDEO_TITLE_CHANGE_SUCCESS_TAG,
)

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
    STUDIO_AVAILABILITY_SUCCESS_TAG,
    AVATAR_CHANGE_ERROR_TAG,
    AVATAR_CHANGE_SUCCESS_TAG,
    NAME_CHANGE_ERROR_TAG,
    NAME_CHANGE_SUCCESS_TAG,
    WARMUP_ERROR_TAG,
    WARMUP_SUCCESS_TAG,
    LANGUAGE_CHANGE_ERROR_TAG,
    LANGUAGE_CHANGE_SUCCESS_TAG,
    COOKIE_FARM_ERROR_TAG,
    COOKIE_FARM_SUCCESS_TAG,
    DESCRIPTION_FILL_ERROR_TAG,
    DESCRIPTION_FILL_SUCCESS_TAG,
    LINK_FILL_ERROR_TAG,
    LINK_FILL_SUCCESS_TAG,
    VIDEO_TITLE_CHANGE_ERROR_TAG,
    VIDEO_TITLE_CHANGE_SUCCESS_TAG,
    UPLOAD_PREVIOUS_SUCCESS_TAG,
    UPLOAD_PREVIOUS_ERROR_TAG,
    UPLOAD_ERROR_3X_TAG,
)


def apply_mutually_exclusive_profile_tag(
    api: object,
    profile_id: str,
    *,
    success: bool,
    success_tag: str,
    error_tag: str,
) -> None:
    """Ставит success_tag или error_tag, снимая противоположный."""
    from zaliver.antydetect.local_antidetect_api import LocalAntidetectHttpAPI

    if not isinstance(api, LocalAntidetectHttpAPI):
        raise TypeError("api must be LocalAntidetectHttpAPI")
    pid = (profile_id or "").strip()
    if not pid:
        return
    tag = success_tag if success else error_tag
    other = error_tag if success else success_tag
    try:
        api.remove_profile_tag(pid, other)
    except Exception:
        pass
    api.add_profile_tag(pid, tag)


def clear_zaliver_tags_on_profile(
    api: object,
    profile_id: str,
    tags: tuple[str, ...] | None = None,
) -> int:
    """
    Снимает с профиля теги из tags (по умолчанию — все ZALIVER_PROFILE_TAGS).
    Возвращает число успешных DELETE (отсутствующий тег — без ошибки).
    """
    from zaliver.antydetect.local_antidetect_api import LocalAntidetectHttpAPI

    if not isinstance(api, LocalAntidetectHttpAPI):
        raise TypeError("api must be LocalAntidetectHttpAPI")
    pid = (profile_id or "").strip()
    if not pid:
        return 0
    tag_list = tags if tags is not None else ZALIVER_PROFILE_TAGS
    removed = 0
    for tag in tag_list:
        t = (tag or "").strip()
        if not t:
            continue
        try:
            api.remove_profile_tag(pid, t)
            removed += 1
        except Exception:
            pass
    return removed
