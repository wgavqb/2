"""Automate the PayFacile "Share by email" flow and capture every network request.

Flow:
  1. Open the product page (login first if credentials are provided).
  2. Click the "Share by email" button.
  3. Fill the email field (and optional note).
  4. Click "Confirm".
  5. Dump every request/response that passed through the page to:
       - requests_log.json  (full structured log)
       - network.har        (standard HAR file, importable in Chrome DevTools)
       - the console        (live stream of every request; pass --quiet for xhr/fetch only)

Environment variables:
  PF_EMAIL       PayFacile login email (optional; skip login if unset)
  PF_PASSWORD    PayFacile login password
  PRODUCT_URL    Product page URL
                 (default: https://www.payfacile.com/products/monthly-subscription)
  SHARE_EMAIL    Email address to put into the share dialog
                 (default: test@example.com)
  SHARE_NOTE     Optional note text added to the email

CLI flags:
  --headful      Launch a visible browser (default: headless).
  --slow-mo MS   Add a per-action delay to make the run watchable.
  --storage FILE Load a saved storage_state JSON for a pre-authenticated session.
  --save-storage After login, dump storage_state to this file so you can reuse it
                 with --storage next time without re-logging in.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Request,
    Response,
    async_playwright,
)

CHROMIUM_PATH = "/opt/pw-browsers/chromium"

OUTPUT_DIR = Path(__file__).parent / "output"
REQUESTS_JSON = OUTPUT_DIR / "requests_log.json"
HAR_PATH = OUTPUT_DIR / "network.har"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--headful", action="store_true", help="Run with a visible browser")
    p.add_argument("--slow-mo", type=int, default=0, help="Slow each action by N ms")
    p.add_argument("--storage", help="Load storage_state JSON from this file")
    p.add_argument("--save-storage", help="After login, write storage_state to this file")
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Print only xhr/fetch/document in the console (default: print every request)",
    )
    return p.parse_args()


async def record_request(request: Request, log: list[dict[str, Any]], quiet: bool) -> None:
    try:
        headers = await request.all_headers()
    except Exception:
        headers = dict(request.headers)
    entry: dict[str, Any] = {
        "phase": "request",
        "method": request.method,
        "url": request.url,
        "resource_type": request.resource_type,
        "headers": headers,
        "post_data": request.post_data,
    }
    log.append(entry)
    if not quiet or request.resource_type in ("xhr", "fetch", "document"):
        print(f"[REQ] {request.method:6s} {request.url}")
        if request.post_data:
            body = request.post_data
            if len(body) > 1000:
                body = body[:1000] + f"...<truncated {len(request.post_data) - 1000} bytes>"
            print(f"       body: {body}")


async def record_response(response: Response, log: list[dict[str, Any]], quiet: bool) -> None:
    req = response.request
    body_preview: str | None = None
    if req.resource_type in ("xhr", "fetch"):
        try:
            body_preview = await response.text()
        except Exception as exc:
            body_preview = f"<could not read body: {exc}>"
    try:
        headers = await response.all_headers()
    except Exception:
        headers = dict(response.headers)
    log.append(
        {
            "phase": "response",
            "status": response.status,
            "url": response.url,
            "method": req.method,
            "resource_type": req.resource_type,
            "headers": headers,
            "body": body_preview,
        }
    )
    if not quiet or req.resource_type in ("xhr", "fetch", "document"):
        print(f"[RES] {response.status:3d}    {response.url}")
        if body_preview:
            preview = body_preview if len(body_preview) <= 1000 else body_preview[:1000] + "..."
            print(f"       body: {preview}")


async def maybe_login(page: Page, email: str | None, password: str | None) -> None:
    if not (email and password):
        print("[INFO] No PF_EMAIL/PF_PASSWORD set — skipping login step.")
        return
    print(f"[INFO] Logging in as {email} …")
    await page.goto("https://www.payfacile.com/login", wait_until="domcontentloaded")
    # The login page uses standard email/password inputs.
    await page.fill('input[type="email"]', email)
    await page.fill('input[type="password"]', password)
    # Try several submit selectors — PayFacile has changed this a few times.
    for selector in [
        'button[type="submit"]',
        'button:has-text("Log in")',
        'button:has-text("Sign in")',
        'button:has-text("Se connecter")',
    ]:
        btn = page.locator(selector).first
        if await btn.count() > 0:
            await btn.click()
            break
    await page.wait_for_load_state("networkidle")
    print("[INFO] Login submitted.")


async def run_share_flow(page: Page, product_url: str, share_email: str, note: str) -> None:
    print(f"[INFO] Opening product page: {product_url}")
    await page.goto(product_url, wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")

    # Make sure the Share & Embed panel is open (vs the Preview panel).
    share_tab = page.locator('button:has-text("Share & Embed"), :text("Share & Embed")').first
    if await share_tab.count() > 0:
        try:
            await share_tab.click(timeout=3000)
        except Exception:
            pass

    print('[INFO] Clicking "Share by email" …')
    share_btn = page.locator(
        'button:has-text("Share by email"), [role="button"]:has-text("Share by email"), :text("Share by email")'
    ).first
    await share_btn.click()

    # Modal opens — wait for the email field.
    print("[INFO] Waiting for the Share-by-email modal …")
    dialog = page.locator('div:has(:text("Share by email")):has(:text("Confirm"))').last
    await dialog.wait_for(state="visible", timeout=10000)

    print(f"[INFO] Filling email: {share_email}")
    # The email input inside the dialog — try several shapes.
    email_input = dialog.locator('input[type="email"], input[type="text"], input').first
    await email_input.fill(share_email)

    if note:
        print(f"[INFO] Filling note ({len(note)} chars)")
        textarea = dialog.locator("textarea").first
        if await textarea.count() > 0:
            await textarea.fill(note)

    print('[INFO] Clicking "Confirm" …')
    confirm_btn = dialog.locator('button:has-text("Confirm")').first
    await confirm_btn.click()

    # Give the share request time to fly out and the response to come back.
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(2)
    print("[INFO] Share flow finished.")


async def main() -> int:
    args = parse_args()

    product_url = os.environ.get(
        "PRODUCT_URL", "https://www.payfacile.com/products/monthly-subscription"
    )
    share_email = os.environ.get("SHARE_EMAIL", "test@example.com")
    note = os.environ.get("SHARE_NOTE", "")
    login_email = os.environ.get("PF_EMAIL")
    login_password = os.environ.get("PF_PASSWORD")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log: list[dict[str, Any]] = []

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=not args.headful,
            slow_mo=args.slow_mo,
            executable_path=CHROMIUM_PATH,
        )

        context_kwargs: dict[str, Any] = {
            "record_har_path": str(HAR_PATH),
            "record_har_mode": "full",
            "viewport": {"width": 1440, "height": 900},
        }
        if args.storage and Path(args.storage).exists():
            context_kwargs["storage_state"] = args.storage

        context: BrowserContext = await browser.new_context(**context_kwargs)
        page: Page = await context.new_page()

        page.on("request", lambda req: asyncio.create_task(record_request(req, log, args.quiet)))
        page.on("response", lambda res: asyncio.create_task(record_response(res, log, args.quiet)))

        try:
            if not args.storage:
                await maybe_login(page, login_email, login_password)
            await run_share_flow(page, product_url, share_email, note)
        except Exception as exc:
            print(f"[ERROR] Flow failed: {exc}", file=sys.stderr)
            # Save a screenshot to help debug selector drift.
            try:
                shot = OUTPUT_DIR / "error.png"
                await page.screenshot(path=str(shot), full_page=True)
                print(f"[ERROR] Screenshot saved to {shot}")
            except Exception:
                pass
            raise
        finally:
            if args.save_storage:
                try:
                    await context.storage_state(path=args.save_storage)
                    print(f"[INFO] Saved storage_state to {args.save_storage}")
                except Exception as exc:
                    print(f"[WARN] Could not save storage_state: {exc}")

            REQUESTS_JSON.write_text(json.dumps(log, indent=2, default=str))
            print(f"\n[DONE] {len(log)} entries saved to {REQUESTS_JSON}")

            await context.close()  # flushes the HAR
            print(f"[DONE] HAR saved to {HAR_PATH}")
            await browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
