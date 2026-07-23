"""Теги, которые Zaliver проставляет в профилях локального антидетекта (HTTP API)."""

from __future__ import annotations

# Проверка доступности YouTube Studio (взаимоисключающие).
STUDIO_AVAILABILITY_ERROR_TAG = "ОШИБКА ПРОВЕРКИ ДОСТУПНОСТИ YouTube"
STUDIO_AVAILABILITY_SUCCESS_TAG = "УСПЕШНАЯ ПРОВЕРКА ДОСТУПНОСТИ YouTube"
STUDIO_AVAILABILITY_RESULT_TAGS: tuple[str, ...] = (
    STUDIO_AVAILABILITY_ERROR_TAG,
    STUDIO_AVAILABILITY_SUCCESS_TAG,
)

# Проверка доступности Gmail (Instagram, взаимоисключающие).
GMAIL_AVAILABILITY_ERROR_TAG = "ОШИБКА ПРОВЕРКИ GMAIL Instagram"
GMAIL_AVAILABILITY_SUCCESS_TAG = "УСПЕШНАЯ ПРОВЕРКА GMAIL Instagram"
GMAIL_AVAILABILITY_RESULT_TAGS: tuple[str, ...] = (
    GMAIL_AVAILABILITY_ERROR_TAG,
    GMAIL_AVAILABILITY_SUCCESS_TAG,
)

# Проверка доступности Instagram (взаимоисключающие).
INSTAGRAM_AVAILABILITY_ERROR_TAG = "ОШИБКА ПРОВЕРКИ ДОСТУПНОСТИ Instagram"
INSTAGRAM_AVAILABILITY_SUCCESS_TAG = "УСПЕШНАЯ ПРОВЕРКА ДОСТУПНОСТИ Instagram"
INSTAGRAM_AVAILABILITY_RESULT_TAGS: tuple[str, ...] = (
    INSTAGRAM_AVAILABILITY_ERROR_TAG,
    INSTAGRAM_AVAILABILITY_SUCCESS_TAG,
)

# Регистрация аккаунта Instagram (взаимоисключающие).
IG_REGISTER_ERROR_TAG = "ОШИБКА РЕГИСТРАЦИИ Instagram"
IG_REGISTER_SMS_ERROR_TAG = "ОШИБКА АВТОРЕГА - SMS Instagram"
IG_REGISTER_SUCCESS_TAG = "УСПЕШНАЯ РЕГИСТРАЦИЯ Instagram"
IG_REGISTER_RESULT_TAGS: tuple[str, ...] = (
    IG_REGISTER_ERROR_TAG,
    IG_REGISTER_SMS_ERROR_TAG,
    IG_REGISTER_SUCCESS_TAG,
    # legacy
    "ОШИБКА РЕГИСТРАЦИИ INSTAGRAM",
    "ОШИБКА АВТОРЕГА INSTAGRAM - SMS",
    "УСПЕШНАЯ РЕГИСТРАЦИЯ INSTAGRAM",
)

# Подключение 2FA Instagram (взаимоисключающие).
IG_2FA_ERROR_TAG = "ОШИБКА ПОДКЛЮЧЕНИЯ 2FA Instagram"
IG_2FA_SUCCESS_TAG = "УСПЕШНОЕ ПОДКЛЮЧЕНИЕ 2FA Instagram"
IG_2FA_RESULT_TAGS: tuple[str, ...] = (
    IG_2FA_ERROR_TAG,
    IG_2FA_SUCCESS_TAG,
    # legacy
    "ОШИБКА ПОДКЛЮЧЕНИЯ 2FA INSTAGRAM",
    "УСПЕШНОЕ ПОДКЛЮЧЕНИЕ 2FA INSTAGRAM",
)

# Смена аватарки канала.
AVATAR_CHANGE_ERROR_TAG = "ОШИБКА СМЕНЫ АВАТАРКИ YouTube"
AVATAR_CHANGE_SUCCESS_TAG = "УСПЕШНАЯ СМЕНА АВАТАРКИ YouTube"
AVATAR_CHANGE_RESULT_TAGS: tuple[str, ...] = (
    AVATAR_CHANGE_ERROR_TAG,
    AVATAR_CHANGE_SUCCESS_TAG,
)

# Смена названия канала.
NAME_CHANGE_ERROR_TAG = "ОШИБКА СМЕНЫ НАЗВАНИЯ YouTube"
NAME_CHANGE_SUCCESS_TAG = "УСПЕШНАЯ СМЕНА НАЗВАНИЯ YouTube"
NAME_CHANGE_RESULT_TAGS: tuple[str, ...] = (
    NAME_CHANGE_ERROR_TAG,
    NAME_CHANGE_SUCCESS_TAG,
)

# Прогрев Shorts (YouTube).
WARMUP_ERROR_TAG = "ОШИБКА ПРОГРЕВА YouTube"
WARMUP_SUCCESS_TAG = "УСПЕШНЫЙ ПРОГРЕВ YouTube"
WARMUP_RESULT_TAGS: tuple[str, ...] = (
    WARMUP_ERROR_TAG,
    WARMUP_SUCCESS_TAG,
)

# Прогрев Reels (Instagram).
IG_WARMUP_ERROR_TAG = "ОШИБКА ПРОГРЕВА Instagram"
IG_WARMUP_SUCCESS_TAG = "УСПЕШНЫЙ ПРОГРЕВ Instagram"
IG_WARMUP_RESULT_TAGS: tuple[str, ...] = (
    IG_WARMUP_ERROR_TAG,
    IG_WARMUP_SUCCESS_TAG,
)

# Продвижение (открытие чужих видео / подписка).
PROMOTE_ERROR_TAG = "ОШИБКА ПРОДВИЖЕНИЯ YouTube"
PROMOTE_SUCCESS_TAG = "УСПЕШНОЕ ПРОДВИЖЕНИЕ YouTube"
PROMOTE_RESULT_TAGS: tuple[str, ...] = (
    PROMOTE_ERROR_TAG,
    PROMOTE_SUCCESS_TAG,
)

# Смена языка интерфейса YouTube.
LANGUAGE_CHANGE_ERROR_TAG = "ОШИБКА СМЕНЫ ЯЗЫКА YouTube"
LANGUAGE_CHANGE_SUCCESS_TAG = "УСПЕШНАЯ СМЕНА ЯЗЫКА YouTube"
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
DESCRIPTION_FILL_ERROR_TAG = "ОШИБКА ЗАПОЛНЕНИЯ ОПИСАНИЯ YouTube"
DESCRIPTION_FILL_SUCCESS_TAG = "УСПЕШНОЕ ЗАПОЛНЕНИЕ ОПИСАНИЯ YouTube"
DESCRIPTION_FILL_RESULT_TAGS: tuple[str, ...] = (
    DESCRIPTION_FILL_ERROR_TAG,
    DESCRIPTION_FILL_SUCCESS_TAG,
)

# Заполнение ссылки канала.
LINK_FILL_ERROR_TAG = "ОШИБКА ЗАПОЛНЕНИЯ ССЫЛКИ YouTube"
LINK_FILL_SUCCESS_TAG = "УСПЕШНОЕ ЗАПОЛНЕНИЕ ССЫЛКИ YouTube"
LINK_FILL_RESULT_TAGS: tuple[str, ...] = (
    LINK_FILL_ERROR_TAG,
    LINK_FILL_SUCCESS_TAG,
)

# Смена названия по умолчанию для загрузки видео.
VIDEO_TITLE_CHANGE_ERROR_TAG = "ОШИБКА СМЕНЫ НАЗВАНИЯ ВИДЕО YouTube"
VIDEO_TITLE_CHANGE_SUCCESS_TAG = "УСПЕШНАЯ СМЕНА НАЗВАНИЯ ВИДЕО YouTube"
VIDEO_TITLE_CHANGE_RESULT_TAGS: tuple[str, ...] = (
    VIDEO_TITLE_CHANGE_ERROR_TAG,
    VIDEO_TITLE_CHANGE_SUCCESS_TAG,
)

# Результат последней попытки залива YouTube (взаимоисключающие).
UPLOAD_PREVIOUS_SUCCESS_TAG = "УСПЕШНЫЙ ПРОШЛЫЙ ЗАЛИВ YouTube"
UPLOAD_PREVIOUS_ERROR_TAG = "ОШИБКА ПРОШЛОГО ЗАЛИВА YouTube"
# Результат последней попытки залива Instagram (взаимоисключающие).
IG_UPLOAD_PREVIOUS_SUCCESS_TAG = "УСПЕШНЫЙ ПРОШЛЫЙ ЗАЛИВ Instagram"
IG_UPLOAD_PREVIOUS_ERROR_TAG = "ОШИБКА ПРОШЛОГО ЗАЛИВА Instagram"
PREVIOUS_UPLOAD_RESULT_TAGS: tuple[str, ...] = (
    UPLOAD_PREVIOUS_SUCCESS_TAG,
    UPLOAD_PREVIOUS_ERROR_TAG,
    IG_UPLOAD_PREVIOUS_SUCCESS_TAG,
    IG_UPLOAD_PREVIOUS_ERROR_TAG,
)

# Три подряд неудачных залива на профиль.
UPLOAD_ERROR_3X_TAG = "upload_error_3x"

# Старые имена тегов (без суффикса / старый INSTAGRAM) — для очистки с профилей.
_ZALIVER_PROFILE_TAGS_LEGACY: tuple[str, ...] = (
    "ОШИБКА ПРОВЕРКИ ДОСТУПНОСТИ",
    "УСПЕШНАЯ ПРОВЕРКА ДОСТУПНОСТИ",
    "ОШИБКА ПРОВЕРКИ GMAIL",
    "УСПЕШНАЯ ПРОВЕРКА GMAIL",
    "ОШИБКА ПРОВЕРКИ ДОСТУПНОСТИ INSTAGRAM",
    "УСПЕШНАЯ ПРОВЕРКА ДОСТУПНОСТИ INSTAGRAM",
    "ОШИБКА РЕГИСТРАЦИИ INSTAGRAM",
    "ОШИБКА АВТОРЕГА INSTAGRAM - SMS",
    "УСПЕШНАЯ РЕГИСТРАЦИЯ INSTAGRAM",
    "ОШИБКА ПОДКЛЮЧЕНИЯ 2FA INSTAGRAM",
    "УСПЕШНОЕ ПОДКЛЮЧЕНИЕ 2FA INSTAGRAM",
    "ОШИБКА СМЕНЫ АВАТАРКИ",
    "УСПЕШНАЯ СМЕНА АВАТАРКИ",
    "ОШИБКА СМЕНЫ НАЗВАНИЯ",
    "УСПЕШНАЯ СМЕНА НАЗВАНИЯ",
    "ОШИБКА ПРОГРЕВА",
    "УСПЕШНЫЙ ПРОГРЕВ",
    "ОШИБКА ПРОГРЕВА INSTAGRAM",
    "УСПЕШНЫЙ ПРОГРЕВ INSTAGRAM",
    "ОШИБКА ПРОДВИЖЕНИЯ",
    "УСПЕШНОЕ ПРОДВИЖЕНИЕ",
    "ОШИБКА СМЕНЫ ЯЗЫКА",
    "УСПЕШНАЯ СМЕНА ЯЗЫКА",
    "ОШИБКА ЗАПОЛНЕНИЯ ОПИСАНИЯ",
    "УСПЕШНОЕ ЗАПОЛНЕНИЕ ОПИСАНИЯ",
    "ОШИБКА ЗАПОЛНЕНИЯ ССЫЛКИ",
    "УСПЕШНОЕ ЗАПОЛНЕНИЕ ССЫЛКИ",
    "ОШИБКА СМЕНЫ НАЗВАНИЯ ВИДЕО",
    "УСПЕШНАЯ СМЕНА НАЗВАНИЯ ВИДЕО",
    "УСПЕШНЫЙ ПРОШЛЫЙ ЗАЛИВ",
    "ОШИБКА ПРОШЛОГО ЗАЛИВА",
)

# Все названия тегов, которые программа может добавить в профиль.
ZALIVER_PROFILE_TAGS: tuple[str, ...] = (
    STUDIO_AVAILABILITY_ERROR_TAG,
    STUDIO_AVAILABILITY_SUCCESS_TAG,
    GMAIL_AVAILABILITY_ERROR_TAG,
    GMAIL_AVAILABILITY_SUCCESS_TAG,
    INSTAGRAM_AVAILABILITY_ERROR_TAG,
    INSTAGRAM_AVAILABILITY_SUCCESS_TAG,
    IG_REGISTER_ERROR_TAG,
    IG_REGISTER_SMS_ERROR_TAG,
    IG_REGISTER_SUCCESS_TAG,
    IG_2FA_ERROR_TAG,
    IG_2FA_SUCCESS_TAG,
    AVATAR_CHANGE_ERROR_TAG,
    AVATAR_CHANGE_SUCCESS_TAG,
    NAME_CHANGE_ERROR_TAG,
    NAME_CHANGE_SUCCESS_TAG,
    WARMUP_ERROR_TAG,
    WARMUP_SUCCESS_TAG,
    IG_WARMUP_ERROR_TAG,
    IG_WARMUP_SUCCESS_TAG,
    PROMOTE_ERROR_TAG,
    PROMOTE_SUCCESS_TAG,
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
    IG_UPLOAD_PREVIOUS_SUCCESS_TAG,
    IG_UPLOAD_PREVIOUS_ERROR_TAG,
    UPLOAD_ERROR_3X_TAG,
    *_ZALIVER_PROFILE_TAGS_LEGACY,
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


def apply_ig_register_result_tag(
    api: object,
    profile_id: str,
    *,
    success: bool,
    sms_captcha: bool = False,
) -> str:
    """
    Результат авторега Instagram: успех / обычная ошибка / SMS-капча.
    Снимает все IG_REGISTER_RESULT_TAGS, ставит один актуальный.
    """
    from zaliver.antydetect.local_antidetect_api import LocalAntidetectHttpAPI

    if not isinstance(api, LocalAntidetectHttpAPI):
        raise TypeError("api must be LocalAntidetectHttpAPI")
    pid = (profile_id or "").strip()
    if not pid:
        return ""
    if success:
        tag = IG_REGISTER_SUCCESS_TAG
    elif sms_captcha:
        tag = IG_REGISTER_SMS_ERROR_TAG
    else:
        tag = IG_REGISTER_ERROR_TAG
    for other in IG_REGISTER_RESULT_TAGS:
        if other == tag:
            continue
        try:
            api.remove_profile_tag(pid, other)
        except Exception:
            pass
    api.add_profile_tag(pid, tag)
    return tag


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
