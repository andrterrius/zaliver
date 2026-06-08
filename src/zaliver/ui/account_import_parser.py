"""Parse bulk YouTube account credentials from text files."""

from __future__ import annotations

import re

from zaliver.ui.antic_profile_row import _profile_id, _profile_name
from zaliver.ui.profile_account_data_dialog import YT_2FA_KEY, YT_LOGIN_KEY, YT_PASSWORD_KEY

_GMAIL_RE = re.compile(r"([\w.+-]+@gmail\.com)", re.I)
_LINE_NUM_RE = re.compile(r"^\d+\.\s*")


def parse_accounts_text(data_text: str) -> list[dict[str, str]]:
    """
    Parse lines like:
    email@gmail.com:password:forward@mail.com::2FA_SECRET

    Returns dicts with yt_login, yt_password, yt_2fa keys.
    """
    accounts: list[dict[str, str]] = []
    seen_logins: set[str] = set()

    for raw_line in data_text.splitlines():
        line = _LINE_NUM_RE.sub("", raw_line.strip())
        if not line:
            continue

        match = _GMAIL_RE.search(line)
        if not match:
            continue

        rest = line[match.start() :]
        parts = rest.split(":")
        if len(parts) < 5:
            continue

        email = parts[0].strip()
        if not email.lower().endswith("@gmail.com"):
            continue

        login_key = email.lower()
        if login_key in seen_logins:
            continue
        seen_logins.add(login_key)

        accounts.append(
            {
                YT_LOGIN_KEY: email,
                YT_PASSWORD_KEY: parts[1],
                YT_2FA_KEY: parts[4].strip(),
            }
        )

    return accounts


def find_profile_for_account(
    account: dict[str, str],
    profiles: list[dict[str, object]],
) -> dict[str, object] | None:
    """Match imported account to a loaded antidetect profile."""
    login = (account.get(YT_LOGIN_KEY) or "").strip().lower()
    if not login:
        return None

    local_part = login.split("@", 1)[0]

    for profile in profiles:
        name = _profile_name(profile).strip().lower()
        if name == login:
            return profile

    for profile in profiles:
        custom_data = profile.get("custom_data")
        if not isinstance(custom_data, dict):
            continue
        existing = str(custom_data.get(YT_LOGIN_KEY) or "").strip().lower()
        if existing == login:
            return profile

    for profile in profiles:
        name = _profile_name(profile).strip().lower()
        if name == local_part:
            return profile

    return None


def build_import_rows(
    accounts: list[dict[str, str]],
    profiles: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Preview rows: account payload + matched profile metadata."""
    rows: list[dict[str, object]] = []
    for account in accounts:
        profile = find_profile_for_account(account, profiles)
        if profile is None:
            rows.append(
                {
                    "account": account,
                    "profile_id": "",
                    "profile_name": "",
                    "status": "Профиль не найден",
                    "can_save": False,
                }
            )
            continue
        rows.append(
            {
                "account": account,
                "profile_id": _profile_id(profile),
                "profile_name": _profile_name(profile),
                "status": "Готово к сохранению",
                "can_save": True,
            }
        )
    return rows


def build_selected_profile_rows(
    profiles: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Placeholder rows for profiles marked in the list before a file is chosen."""
    rows: list[dict[str, object]] = []
    for profile in profiles:
        rows.append(
            {
                "account": {},
                "profile_id": _profile_id(profile),
                "profile_name": _profile_name(profile),
                "status": "Выберите файл",
                "can_save": False,
            }
        )
    return rows


def assign_accounts_to_selected_profiles(
    profiles: list[dict[str, object]],
    accounts: list[dict[str, str]],
) -> list[dict[str, object]]:
    """Map parsed accounts to selected profiles in list order."""
    rows: list[dict[str, object]] = []
    for i, profile in enumerate(profiles):
        account = accounts[i] if i < len(accounts) else {}
        has_data = bool(account)
        rows.append(
            {
                "account": account,
                "profile_id": _profile_id(profile),
                "profile_name": _profile_name(profile),
                "status": "Готово к сохранению" if has_data else "Нет учётки в файле",
                "can_save": has_data,
            }
        )
    for account in accounts[len(profiles) :]:
        rows.append(
            {
                "account": account,
                "profile_id": "",
                "profile_name": "",
                "status": "Лишняя учётка (нет профиля)",
                "can_save": False,
            }
        )
    return rows
