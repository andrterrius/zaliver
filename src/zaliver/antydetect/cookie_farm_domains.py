"""Заготовленный список доменов и разбор пользовательского источника для фарма Cookie."""

from __future__ import annotations

from urllib.parse import urlparse

DEFAULT_COOKIE_FARM_DOMAINS: tuple[str, ...] = (
    "google.com",
    "youtube.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "reddit.com",
    "amazon.com",
    "ebay.com",
    "walmart.com",
    "aliexpress.com",
    "booking.com",
    "airbnb.com",
    "microsoft.com",
    "apple.com",
    "github.com",
    "stackoverflow.com",
    "wikipedia.org",
    "bbc.com",
    "cnn.com",
    "nytimes.com",
    "theguardian.com",
    "reuters.com",
    "forbes.com",
    "bloomberg.com",
    "yahoo.com",
    "bing.com",
    "duckduckgo.com",
    "linkedin.com",
    "pinterest.com",
    "tumblr.com",
    "medium.com",
    "spotify.com",
    "netflix.com",
    "twitch.tv",
    "weather.com",
    "accuweather.com",
    "craigslist.org",
    "target.com",
    "bestbuy.com",
    "etsy.com",
    "paypal.com",
    "stripe.com",
    "adobe.com",
    "dropbox.com",
    "zoom.us",
    "slack.com",
    "discord.com",
    "whatsapp.com",
    "tiktok.com",
)

DEFAULT_COOKIE_FARM_RU_DOMAINS: tuple[str, ...] = (
    "ok.ru",
    "ozon.ru",
    "wildberries.ru",
    "avito.ru",
    "kinopoisk.ru",
    "rutube.ru",
    "rbc.ru",
    "lenta.ru",
    "ria.ru",
    "tass.ru",
    "drom.ru",
    "telegram.org",
    "yandex.ru",
    "mail.ru",
    "vk.com",
    "dzen.ru",
    "gosuslugi.ru",
    "sberbank.ru",
    "tinkoff.ru",
    "citilink.ru",
    "dns-shop.ru",
    "mvideo.ru",
    "leroymerlin.ru",
    "hh.ru",
    "auto.ru",
    "cian.ru",
    "2gis.ru",
    "gazeta.ru",
    "kommersant.ru",
    "meduza.io",
    "championat.com",
    "sport-express.ru",
    "afisha.ru",
    "banki.ru",
    "pikabu.ru",
)

PRESET_COOKIE_FARM_LISTS: dict[str, tuple[str, ...]] = {
    "intl": DEFAULT_COOKIE_FARM_DOMAINS,
    "ru": DEFAULT_COOKIE_FARM_RU_DOMAINS,
}


def normalize_domain_line(line: str) -> str | None:
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return None
    if "#" in s:
        s = s.split("#", 1)[0].strip()
    if not s:
        return None
    if "://" in s:
        parsed = urlparse(s)
        host = (parsed.netloc or parsed.path or "").strip()
    else:
        host = s.split("/", 1)[0].strip()
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    return host


def parse_domains_text(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in (text or "").splitlines():
        domain = normalize_domain_line(line)
        if domain and domain not in seen:
            seen.add(domain)
            out.append(domain)
    return out


def domain_to_url(domain: str) -> str:
    d = (domain or "").strip()
    if not d:
        return "https://"
    if "://" in d:
        return d
    return f"https://{d}/"


def default_cookie_farm_domains(*, preset: str = "intl") -> list[str]:
    key = (preset or "intl").strip().lower()
    domains = PRESET_COOKIE_FARM_LISTS.get(key)
    if domains is None:
        domains = DEFAULT_COOKIE_FARM_DOMAINS
    return list(domains)
