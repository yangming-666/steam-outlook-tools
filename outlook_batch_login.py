import argparse
import asyncio
import csv
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

import feishu_tool


DEFAULT_SECURITY_EMAIL = os.environ.get("BATCHSIGNUP_SECURITY_EMAIL", "security@example.com")
DEFAULT_FEISHU_SHEET_URL = "https://ru8q9fv50i.feishu.cn/wiki/IM3jw5X9Sialj4k5OupckZK5nBh?sheet=xUJBaF"
OUTLOOK_MAIL_URL = "https://outlook.live.com/mail/"
LOGIN_URL = "https://login.live.com/"
LOGOUT_URL = "https://login.live.com/logout.srf"


@dataclass
class Account:
    row_number: int
    steam_account: str
    email: str
    password: str
    steam_id: str


def read_pending_accounts(csv_path: Path) -> list[Account]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))

    if not rows:
        raise SystemExit(f"CSV is empty: {csv_path}")

    header = rows[0]
    required = ["steam账号", "邮箱账号", "steam id"]
    missing = [name for name in required if name not in header]
    if missing:
        raise SystemExit(f"CSV missing columns: {', '.join(missing)}")

    steam_account_idx = header.index("steam账号")
    email_idx = header.index("邮箱账号")
    steam_id_idx = header.index("steam id")

    # The file has two columns named "密码". The second one is the email password.
    password_indices = [idx for idx, name in enumerate(header) if name == "密码"]
    if len(password_indices) < 2:
        raise SystemExit('CSV must contain two "密码" columns; the second is used as email password.')
    email_password_idx = password_indices[1]

    accounts: list[Account] = []
    for zero_based_idx, row in enumerate(rows[1:], start=2):
        padded = row + [""] * (len(header) - len(row))
        steam_id = padded[steam_id_idx].strip()
        email = padded[email_idx].strip()
        password = padded[email_password_idx].strip()
        if steam_id or not email or not password:
            continue
        accounts.append(
            Account(
                row_number=zero_based_idx,
                steam_account=padded[steam_account_idx].strip(),
                email=email,
                password=password,
                steam_id=steam_id,
            )
        )

    accounts.reverse()
    return accounts


def cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value).strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("link") or ""))
            else:
                parts.append(cell_text(item))
        return "".join(parts).strip()
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or "").strip()
    return str(value).strip()


def read_pending_accounts_from_feishu(args) -> list[Account]:
    config = feishu_tool.load_config(Path(args.feishu_config))
    feishu_tool.start_mcp(Path(args.feishu_start_script))
    resolved = feishu_tool.resolve_spreadsheet(config, args.sheet_url, args.sheet_id)
    sheet_id = resolved.get("sheet_id")
    spreadsheet_token = resolved.get("spreadsheet_token")
    if not sheet_id or not spreadsheet_token:
        raise SystemExit("Feishu sheet id/token could not be resolved. Pass --sheet-id or use a URL with ?sheet=...")

    response = feishu_tool.read_sheet(
        config,
        spreadsheet_token,
        sheet_id,
        args.sheet_range,
        use_uat=args.feishu_auth == "user",
        package_root=Path(args.lark_mcp_root),
    )
    if response.get("code") != 0:
        raise SystemExit(f"Feishu read failed: {response}")

    values = response.get("data", {}).get("valueRange", {}).get("values", [])
    if not values:
        raise SystemExit("Feishu sheet range returned no rows.")

    header = [cell_text(cell) for cell in values[0]]
    required = ["steam账号", "邮箱账号", "steam id"]
    missing = [name for name in required if name not in header]
    if missing:
        raise SystemExit(f"Feishu sheet missing columns: {', '.join(missing)}")

    steam_account_idx = header.index("steam账号")
    email_idx = header.index("邮箱账号")
    steam_id_idx = header.index("steam id")
    password_indices = [idx for idx, name in enumerate(header) if name == "密码"]
    if len(password_indices) < 2:
        raise SystemExit('Feishu sheet must contain two "密码" columns; the second is used as email password.')
    email_password_idx = password_indices[1]

    accounts: list[Account] = []
    for sheet_row_number, row in enumerate(values[1:], start=2):
        padded = list(row) + [""] * (len(header) - len(row))
        steam_id = cell_text(padded[steam_id_idx])
        email = cell_text(padded[email_idx])
        password = cell_text(padded[email_password_idx])
        if steam_id or not email or not password:
            continue
        accounts.append(
            Account(
                row_number=sheet_row_number,
                steam_account=cell_text(padded[steam_account_idx]),
                email=email,
                password=password,
                steam_id=steam_id,
            )
        )

    accounts.reverse()
    return accounts


async def click_first(page: Page, patterns: list[str], timeout_ms: int = 1500) -> bool:
    for pattern in patterns:
        locator = page.get_by_text(re.compile(pattern, re.I)).first
        try:
            await locator.click(timeout=timeout_ms)
            return True
        except PlaywrightTimeoutError:
            pass
    return False


async def fill_first(page: Page, selectors: list[str], value: str, timeout_ms: int = 5000) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.fill(value, timeout=timeout_ms)
            return True
        except PlaywrightTimeoutError:
            pass
    return False


async def press_enter_first(page: Page, selectors: list[str], timeout_ms: int = 5000) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.press("Enter", timeout=timeout_ms)
            return True
        except PlaywrightTimeoutError:
            pass
    return False


async def click_submit(page: Page) -> None:
    selectors = [
        "button[type=submit]",
        "input[type=submit]",
        "button:has-text('Next')",
        "button:has-text('下一步')",
        "button:has-text('Sign in')",
        "button:has-text('登录')",
        "button:has-text('Yes')",
        "button:has-text('是')",
        "button:has-text('Verify')",
        "button:has-text('验证')",
        "button:has-text('Continue')",
        "button:has-text('继续')",
    ]
    for selector in selectors:
        try:
            await page.locator(selector).first.click(timeout=2500)
            return
        except PlaywrightTimeoutError:
            pass
    await page.keyboard.press("Enter")


async def logout_current_account(page: Page) -> None:
    await page.goto(LOGOUT_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)


async def maybe_handle_stay_signed_in(page: Page) -> None:
    await click_first(page, [r"^Yes$", r"^是$", r"保持登录", r"Stay signed in"], timeout_ms=2500)
    await click_first(page, [r"^No$", r"^否$", r"Skip", r"跳过"], timeout_ms=1000)


async def fill_login(page: Page, account: Account) -> None:
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")

    email_filled = await fill_first(
        page,
        [
            "input[type=email]",
            "input[name=loginfmt]",
            "input[name=email]",
            "input[aria-label*='email' i]",
            "input[aria-label*='邮箱' i]",
        ],
        account.email,
    )
    if not email_filled:
        raise RuntimeError("Could not find Microsoft email input.")
    await click_submit(page)

    password_filled = await fill_first(
        page,
        [
            "input[type=password]",
            "input[name=passwd]",
            "input[aria-label*='password' i]",
            "input[aria-label*='密码' i]",
        ],
        account.password,
        timeout_ms=15000,
    )
    if not password_filled:
        raise RuntimeError("Could not find Microsoft password input.")
    await click_submit(page)
    await page.wait_for_timeout(3000)


async def security_challenge_visible(page: Page) -> bool:
    body = await page.locator("body").inner_text(timeout=5000)
    markers = [
        "security code",
        "verify your identity",
        "alternate email",
        "email address",
        "安全代码",
        "验证你的身份",
        "备用电子邮件",
    ]
    return any(marker.lower() in body.lower() for marker in markers)


async def submit_security_email(page: Page, security_email: str) -> bool:
    filled = await fill_first(
        page,
        [
            "input[type=email]",
            "input[name=email]",
            "input[name=proof]",
            "input[aria-label*='email' i]",
            "input[aria-label*='邮箱' i]",
            "input[type=text]",
        ],
        security_email,
        timeout_ms=4000,
    )
    if filled:
        await click_submit(page)
        await page.wait_for_timeout(3000)
    return filled


def extract_code(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text)
    patterns = [
        r"security code[^0-9]{0,80}([0-9]{6,8})",
        r"code[^0-9]{0,80}([0-9]{6,8})",
        r"安全代码[^0-9]{0,80}([0-9]{6,8})",
        r"验证码[^0-9]{0,80}([0-9]{6,8})",
        r"\b([0-9]{6,8})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            return match.group(1)
    return None


async def search_security_mail(page: Page, query: str) -> None:
    await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)

    search_selectors = [
        "input[aria-label*='Search' i]",
        "input[aria-label*='搜索' i]",
        "input[placeholder*='Search' i]",
        "input[placeholder*='搜索' i]",
        "[role=searchbox]",
    ]
    for selector in search_selectors:
        try:
            search = page.locator(selector).first
            await search.fill(query, timeout=3000)
            await search.press("Enter")
            await page.wait_for_timeout(5000)
            return
        except PlaywrightTimeoutError:
            pass


async def get_security_code(page: Page, target_email: str, timeout_seconds: int) -> str:
    deadline = time.monotonic() + timeout_seconds
    queries = [
        f'"{target_email}" "security code"',
        f'"{target_email}" "Microsoft"',
        "Microsoft account security code",
        "security code",
        "安全代码",
    ]

    while time.monotonic() < deadline:
        for query in queries:
            await search_security_mail(page, query)
            body_text = await page.locator("body").inner_text(timeout=10000)
            code = extract_code(body_text)
            if code:
                return code

            # If the list only shows a preview, open the newest visible Microsoft result and try again.
            opened = await click_first(page, [r"Microsoft", r"security code", r"安全代码"], timeout_ms=1500)
            if opened:
                await page.wait_for_timeout(2500)
                body_text = await page.locator("body").inner_text(timeout=10000)
                code = extract_code(body_text)
                if code:
                    return code

        await page.wait_for_timeout(5000)

    raise TimeoutError(f"Timed out waiting for security code for {target_email}.")


async def submit_code(page: Page, code: str) -> None:
    filled = await fill_first(
        page,
        [
            "input[name=otc]",
            "input[name=code]",
            "input[type=tel]",
            "input[autocomplete=one-time-code]",
            "input[type=text]",
        ],
        code,
        timeout_ms=10000,
    )
    if not filled:
        raise RuntimeError("Could not find security code input.")
    await click_submit(page)
    await page.wait_for_timeout(4000)


async def ensure_mailbox_loaded(page: Page) -> None:
    await maybe_handle_stay_signed_in(page)
    if "outlook.live.com" not in page.url:
        await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)


async def connect_security_context(playwright, args) -> tuple[BrowserContext, Page]:
    if args.security_cdp:
        browser = await playwright.chromium.connect_over_cdp(args.security_cdp)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
        return context, page

    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=args.security_profile,
        headless=False,
        viewport={"width": 1280, "height": 900},
        args=[f"--window-position={args.security_x},{args.security_y}"],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
    return context, page


async def run(args) -> None:
    if args.source == "csv":
        accounts = read_pending_accounts(Path(args.csv))
    else:
        accounts = read_pending_accounts_from_feishu(args)
    if args.limit:
        accounts = accounts[: args.limit]

    if not accounts:
        print("No pending accounts with empty steam id.")
        return

    print(f"Pending accounts: {len(accounts)}")
    if args.list_pending:
        for account in accounts:
            print(f"row {account.row_number}: {account.email}")
        return

    async with async_playwright() as playwright:
        target_context = await playwright.chromium.launch_persistent_context(
            user_data_dir=args.target_profile,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=[f"--window-position={args.target_x},{args.target_y}"],
        )
        target_page = target_context.pages[0] if target_context.pages else await target_context.new_page()

        security_context, security_page = await connect_security_context(playwright, args)

        for idx, account in enumerate(accounts, start=1):
            print(f"[{idx}/{len(accounts)}] row {account.row_number}: {account.email}")
            try:
                await logout_current_account(target_page)
                await fill_login(target_page, account)

                if await security_challenge_visible(target_page):
                    if not await submit_security_email(target_page, args.security_email):
                        print("Security email field was not found. Waiting for manual input.")
                    code = await get_security_code(security_page, account.email, args.code_timeout)
                    print(f"Code found for {account.email}: {code}")
                    await submit_code(target_page, code)

                await ensure_mailbox_loaded(target_page)
                print(f"Logged in: {account.email}")

                if args.pause_after_login:
                    input("Press Enter to continue to the next account...")
            except Exception as exc:
                screenshot = Path(args.error_dir) / f"row_{account.row_number}_{account.email.replace('@', '_')}.png"
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                await target_page.screenshot(path=str(screenshot), full_page=True)
                print(f"ERROR on {account.email}: {exc}")
                print(f"Screenshot saved: {screenshot}")
                if not args.continue_on_error:
                    break

        await target_context.close()
        if not args.security_cdp:
            await security_context.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Batch login pending Hotmail/Outlook accounts from Feishu sheet bottom-up.")
    parser.add_argument("--source", choices=["feishu", "csv"], default="feishu", help="Account source. Default: feishu")
    parser.add_argument("--csv", default="account.csv", help="CSV path. Default: account.csv")
    parser.add_argument("--sheet-url", default=DEFAULT_FEISHU_SHEET_URL, help="Feishu wiki/sheet URL.")
    parser.add_argument("--sheet-id", help="Feishu sheet id override.")
    parser.add_argument("--sheet-range", default="A1:I1000", help="Feishu range to read. Default: A1:I1000")
    parser.add_argument("--feishu-auth", choices=["user", "app"], default="user", help="Feishu auth mode. Default: user")
    parser.add_argument("--feishu-config", default=str(feishu_tool.DEFAULT_CONFIG), help="Feishu MCP config path.")
    parser.add_argument("--feishu-start-script", default=str(feishu_tool.DEFAULT_START_SCRIPT), help="Feishu MCP start script path.")
    parser.add_argument("--lark-mcp-root", default=str(feishu_tool.DEFAULT_LARK_MCP_ROOT), help="Local lark-mcp package root.")
    parser.add_argument("--security-email", default=DEFAULT_SECURITY_EMAIL, help="Security mailbox address.")
    parser.add_argument("--target-profile", default=".playwright-target-profile", help="Persistent profile for screen 1 target login.")
    parser.add_argument("--security-profile", default=".playwright-security-profile", help="Persistent profile for screen 2 security mailbox.")
    parser.add_argument("--security-cdp", help="CDP URL for an already-running Chrome, for example http://127.0.0.1:9222")
    parser.add_argument("--target-x", type=int, default=0, help="Target browser window X position.")
    parser.add_argument("--target-y", type=int, default=0, help="Target browser window Y position.")
    parser.add_argument("--security-x", type=int, default=1280, help="Security browser window X position.")
    parser.add_argument("--security-y", type=int, default=0, help="Security browser window Y position.")
    parser.add_argument("--code-timeout", type=int, default=180, help="Seconds to wait for each verification code.")
    parser.add_argument("--limit", type=int, help="Only process the first N pending accounts after bottom-up sorting.")
    parser.add_argument("--list-pending", action="store_true", help="Print pending accounts and exit without opening browsers.")
    parser.add_argument("--pause-after-login", action="store_true", help="Pause after each successful mailbox login.")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue with next account when one fails.")
    parser.add_argument("--error-dir", default="errors", help="Directory for failure screenshots.")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
