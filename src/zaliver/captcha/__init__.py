"""Решение капч через внешние сервисы."""

from zaliver.captcha.rucaptcha import (
    RuCaptchaError,
    solve_image_to_text,
    solve_recaptcha_v2_proxyless,
)

__all__ = [
    "RuCaptchaError",
    "solve_image_to_text",
    "solve_recaptcha_v2_proxyless",
]
