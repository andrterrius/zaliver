from __future__ import annotations

import argparse
import sys
import time

from zaliver.antydetect.api import (
    DolphinAntyError,
    DolphinAntyLocalAPI,
    DolphinAntyPublicAPI,
)


def run_google_search(
    profile_id: str,
    query: str,
    *,
    token: str | None,
    headless: bool,
    keep_open_s: float,
) -> int:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    api = DolphinAntyLocalAPI()
    try:
        if token:
            api.login_with_token(token)

        conn = api.start_profile(profile_id, headless=headless)

        with sync_playwright() as p:
            browser = None
            last_err: Exception | None = None
            for endpoint in (conn.ws_url(), conn.http_url()):
                try:
                    browser = p.chromium.connect_over_cdp(endpoint)
                    last_err = None
                    break
                except PlaywrightError as e:
                    last_err = e

            if browser is None:
                raise DolphinAntyError(f"CDP connect failed for both endpoints. Last error: {last_err!r}")

            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()

            page.goto("https://www.google.com/", wait_until="domcontentloaded")

            page.fill('textarea[name="q"], input[name="q"]', query)
            page.keyboard.press("Enter")
            page.wait_for_load_state("domcontentloaded")

            if keep_open_s > 0:
                time.sleep(keep_open_s)

            browser.close()

        return 0
    except DolphinAntyError as e:
        sys.stderr.write(f"[dolphin] {e}\n")
        return 2
    except PlaywrightError as e:
        sys.stderr.write(f"[playwright] {e}\n")
        return 3
    finally:
        try:
            api.stop_profile(profile_id)
        except Exception:
            pass
        api.close()

def run_public_list_profiles(*, token: str, limit: int, query: str | None) -> int:
    try:
        api = DolphinAntyPublicAPI(token=token)
        try:
            profiles = api.list_profiles(limit=limit, query=query)
        finally:
            api.close()

        for p in profiles:
            pid = str(p.get("id") or "").strip()
            name = str(p.get("name") or "").strip()
            sys.stdout.write(f"{pid}\t{name}\n")
        return 0
    except DolphinAntyError as e:
        sys.stderr.write(f"[dolphin] {e}\n")
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dolphin{anty} smoke tests (Local API + Playwright; Public API list profiles).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list-profiles", help="List browser profiles via Public API (Bearer token).")
    p_list.add_argument(
        "--token",
        required=True,
        help="Public API JWT (raw JWT, or already prefixed as 'Bearer <jwt>').",
    )
    p_list.add_argument("--limit", type=int, default=50, help="Profiles per page (API limit param)")
    p_list.add_argument("--query", default=None, help="Optional query filter")

    p_google = sub.add_parser("google", help="Start local profile and perform Google search via Playwright CDP.")
    p_google.add_argument("--profile-id", required=True, help="Dolphin browser profile ID (local)")
    p_google.add_argument("--query", default="zaliver test", help="Google query to type")
    p_google.add_argument("--token", default=None, help="Local API token (optional)")
    p_google.add_argument("--headless", action="store_true", help="Start profile in headless mode")
    p_google.add_argument("--keep-open-s", type=float, default=5.0, help="Seconds to keep browser open after search")
    args = parser.parse_args(argv)

    if args.cmd == "list-profiles":
        return run_public_list_profiles(
            token=args.token,
            limit=args.limit,
            query=(args.query.strip() if isinstance(args.query, str) and args.query.strip() else None),
        )
    if args.cmd == "google":
        return run_google_search(
            profile_id=args.profile_id,
            query=args.query,
            token=args.token,
            headless=args.headless,
            keep_open_s=args.keep_open_s,
        )
    sys.stderr.write("Unknown command.\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

