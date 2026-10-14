import argparse
import asyncio
import csv
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

import feishu_tool


DEFAULT_SECURITY_EMAIL = os.environ.get("BATCHSIGNUP_SECURITY_EMAIL", "security@example.com")
DEFAULT_FEISHU_SHEET_URL = "https://ru8q9fv50i.feishu.cn/wiki/IM3jw5X9Sialj4k5OupckZK5nBh?sheet=xUJBaF"
OUTLOOK_MAIL_URL = "https://outlook.live.com/mail/"
LOGIN_URL = "https://login.live.com/"
LOGOUT_URL = "https://login.live.com/logout.srf"

WEBAUTHN_CANCEL_SCRIPT = r"""
(() => {
  const proto = globalThis.CredentialsContainer && globalThis.CredentialsContainer.prototype;
  if (!proto || proto.__batchSignupFidoCancelInstalled) return;
  const originalGet = proto.get;
  Object.defineProperty(proto, "get", {
    configurable: true,
    value: function(options) {
      if (options && options.publicKey) {
        return Promise.reject(new DOMException("The operation was cancelled.", "NotAllowedError"));
      }
      return originalGet.apply(this, arguments);
    }
  });
  Object.defineProperty(proto, "__batchSignupFidoCancelInstalled", { value: true });
})();
"""


async def install_webauthn_cancel(context: BrowserContext) -> None:
    """Make WebAuthn behave exactly like a user cancellation before native Windows UI opens."""
    await context.add_init_script(script=WEBAUTHN_CANCEL_SCRIPT)


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


async def wait_for_timeout_maybe_stop(
    page: Page,
    milliseconds: int,
    pause_controller=None,
    step: str = "page wait",
) -> None:
    deadline = time.monotonic() + max(0, milliseconds) / 1000
    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError(f"Stopped during {step}.")
        remaining_ms = int(max(1, min(500, (deadline - time.monotonic()) * 1000)))
        await page.wait_for_timeout(remaining_ms)


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
    if not patterns:
        return False
    combined = re.compile("(?:" + ")|(?:".join(patterns) + ")", re.I)
    try:
        await page.get_by_text(combined).first.click(timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


async def fill_first(page: Page, selectors: list[str], value: str, timeout_ms: int = 5000) -> bool:
    if not selectors:
        return False
    # Microsoft changes the verification form between builds.  A broad
    # selector such as input[name*="email"] can resolve to a proof-method
    # radio button before the real text box appears.  The callers exclude
    # non-editable controls, so combine the selectors into one bounded lookup:
    # do not wait once per selector and make the radio branch appear stuck.
    visible_selectors = ", ".join(f"{selector}:visible" for selector in selectors)
    try:
        await page.locator(visible_selectors).first.fill(value, timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False
    except Exception as exc:
        message = str(exc).casefold()
        if any(marker in message for marker in [
            "cannot be filled",
            "not editable",
            "element is not an input",
            "input of type \"radio\"",
            "input of type \"checkbox\"",
        ]):
            return False
        raise


async def press_enter_first(page: Page, selectors: list[str], timeout_ms: int = 5000) -> bool:
    if not selectors:
        return False
    visible_selectors = ", ".join(f"{selector}:visible" for selector in selectors)
    try:
        await page.locator(visible_selectors).first.press("Enter", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
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
        "button:has-text('Send code')",
        "button:has-text('发送代码')",
        "button:has-text('发送验证码')",
        "button:has-text('获取验证码')",
        "input[type=submit][value*='Send code' i]",
        "input[type=submit][value*='发送验证码']",
        "button:has-text('Continue')",
        "button:has-text('继续')",
    ]
    visible_selectors = ", ".join(f"{selector}:visible" for selector in selectors)
    try:
        # Dispatch the DOM click synchronously instead of using Playwright's click
        # action. Microsoft may open the native passkey chooser while navigation is
        # settling; fill_login must regain control immediately to follow cancelUrl.
        await page.locator(visible_selectors).first.evaluate(
            "element => element.click()",
            timeout=10000,
        )
    except PlaywrightTimeoutError:
        await page.keyboard.press("Enter")


async def prefer_password_sign_in(page: Page, pause_controller=None) -> bool:
    password_patterns = [
        r"Use your password",
        r"Use password",
        r"Sign in with password",
        r"Use a password instead",
        r"使用密码",
        r"改用密码",
        r"用密码登录",
        r"使用你的密码",
    ]
    if await click_first(page, password_patterns, timeout_ms=2500):
        await wait_for_timeout_maybe_stop(page, 2500, pause_controller, "Microsoft password option")
        return True
    alternate_method_patterns = [
        r"Sign in another way",
        r"Other ways to sign in",
        r"Use another method",
        r"Use a different verification option",
        r"其他登录方法",
        r"改用其他方式",
        r"使用其他方式",
        r"选择其他验证方式",
        r"换一种验证方式",
    ]
    if await click_first(page, alternate_method_patterns, timeout_ms=1500):
        await wait_for_timeout_maybe_stop(page, 1500, pause_controller, "Microsoft alternate sign-in options")
        if await click_first(page, password_patterns, timeout_ms=2500):
            await wait_for_timeout_maybe_stop(page, 2500, pause_controller, "Microsoft password option after alternate sign-in")
            return True
    for selector in [
        "a[href*='pwd']",
        "button[data-testid*='password' i]",
        "button[aria-label*='password' i]",
        "button[aria-label*='密码' i]",
    ]:
        try:
            await page.locator(selector).first.click(timeout=1500)
            await wait_for_timeout_maybe_stop(page, 2500, pause_controller, "Microsoft password selector fallback")
            return True
        except PlaywrightTimeoutError:
            pass
        except Exception:
            pass
    return False


async def cancel_microsoft_fido_bridge(page: Page, pause_controller=None, timeout_seconds: int = 8) -> bool:
    """Leave either Microsoft FIDO route before it can block password login."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError("Stopped while cancelling Microsoft FIDO bridge.")
        url = getattr(page, "url", "") or ""
        parsed = urlparse(url)
        if parsed.netloc.casefold() == "login.microsoft.com" and "/consumers/fido/" in parsed.path.casefold():
            # The current consumer FIDO page exposes password sign-in under the
            # bottom-right ellipsis menu. Use that real page route first; merely
            # going back returns to account entry and selects FIDO again.
            try:
                if await prefer_password_sign_in(page, pause_controller):
                    return True
            except Exception:
                pass
            try:
                more_options = page.locator(
                    "button[aria-label*='more' i]:visible, button[title*='more' i]:visible, "
                    "button[aria-label*='更多']:visible, button[title*='更多']:visible, "
                    "[role=button][aria-label*='more' i]:visible, [role=button][aria-label*='更多']:visible"
                )
                if await more_options.count():
                    await more_options.first.evaluate("element => element.click()")
                else:
                    visible_buttons = page.locator("button:visible")
                    if await visible_buttons.count() >= 2:
                        await visible_buttons.last.evaluate("element => element.click()")
                await wait_for_timeout_maybe_stop(page, 500, pause_controller, "Microsoft FIDO more options")
                if await prefer_password_sign_in(page, pause_controller):
                    return True
            except Exception:
                pass
            await page.go_back(wait_until="domcontentloaded")
            await wait_for_timeout_maybe_stop(page, 500, pause_controller, "Microsoft consumer FIDO cancel")
            return True
        if "/bridge/fido" in url.casefold():
            query = parse_qs(parsed.query)
            cancel_url = unquote((query.get("cancelUrl") or query.get("cancelurl") or [""])[0]).strip()
            if not cancel_url.startswith("https://login.live.com/"):
                await page.go_back(wait_until="domcontentloaded")
            else:
                await page.goto(cancel_url, wait_until="domcontentloaded")
            await wait_for_timeout_maybe_stop(page, 500, pause_controller, "Microsoft FIDO bridge cancel")
            return True
        try:
            if await page.locator("input[type=password]:visible").count():
                return False
        except Exception:
            pass
        await page.wait_for_timeout(200)
    return False


async def logout_current_account(page: Page, pause_controller=None) -> None:
    await page.goto(LOGOUT_URL, wait_until="domcontentloaded")
    await wait_for_timeout_maybe_stop(page, 2500, pause_controller, "Microsoft logout")


async def maybe_handle_stay_signed_in(page: Page) -> None:
    await click_first(page, [r"^Yes$", r"^是$", r"保持登录", r"Stay signed in"], timeout_ms=2500)
    await click_first(page, [r"^No$", r"^否$", r"Skip", r"跳过"], timeout_ms=1000)


async def fill_login(page: Page, account: Account, pause_controller=None) -> None:
    # Start authentication from Outlook so Microsoft carries an Outlook return URL.
    # Opening the generic login.live.com root makes the "Stay signed in" step fall
    # back to account.microsoft.com instead of the mailbox.
    await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")

    use_another_account_patterns = [
        r"Use another account",
        r"Sign in with another account",
        r"Add account",
        r"选择其他帐户",
        r"选择其他账户",
        r"使用其他帐户",
        r"使用其他账户",
        r"添加帐户",
        r"添加账户",
    ]
    email_selectors = [
        "input[type=email]",
        "input[name=loginfmt]",
        "input[autocomplete=username]",
        "input[inputmode=email]",
        "input[name=email]",
        "input[name=login]",
        "input[aria-label*='email' i]",
        "input[aria-label*='邮箱' i]",
    ]
    email_filled = await fill_first(
        page,
        email_selectors,
        account.email,
    )
    if not email_filled:
        # Outlook.com can redirect an unauthenticated /mail/ request to the
        # Microsoft 365 marketing page. That page has a 登录 link but no
        # credential form; enter the Microsoft sign-in endpoint explicitly.
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await wait_for_timeout_maybe_stop(page, 1000, pause_controller, "Microsoft login fallback navigation")
        email_filled = await fill_first(page, email_selectors, account.email, timeout_ms=10000)
    if not email_filled and await click_first(page, use_another_account_patterns, timeout_ms=2500):
        await wait_for_timeout_maybe_stop(page, 2000, pause_controller, "Microsoft account chooser use another account")
        email_filled = await fill_first(
            page,
            email_selectors,
            account.email,
            timeout_ms=10000,
        )
    if not email_filled:
        raise RuntimeError("Could not find Microsoft email input.")
    await click_submit(page)
    fido_cancelled = await cancel_microsoft_fido_bridge(page, pause_controller)
    if fido_cancelled:
        # Microsoft's FIDO cancel endpoint can return to the account entry page.
        # Re-submit the same account once; the cancelled method is then excluded
        # and the password option becomes available.
        email_filled_after_cancel = await fill_first(page, email_selectors, account.email, timeout_ms=5000)
        if email_filled_after_cancel:
            await click_submit(page)
            await cancel_microsoft_fido_bridge(page, pause_controller, timeout_seconds=3)
    if not fido_cancelled:
        await wait_for_timeout_maybe_stop(page, 1000, pause_controller, "Microsoft email submit")
    await prefer_password_sign_in(page, pause_controller)

    password_filled = await fill_first(
        page,
        [
            "input[type=password]",
            "input[name=passwd]",
            "input[autocomplete=current-password]",
            "input[aria-label*='password' i]",
            "input[aria-label*='密码' i]",
        ],
        account.password,
        timeout_ms=15000,
    )
    if not password_filled and await cancel_microsoft_fido_bridge(page, pause_controller):
        await prefer_password_sign_in(page, pause_controller)
        password_filled = await fill_first(
            page,
            [
                "input[type=password]",
                "input[name=passwd]",
                "input[autocomplete=current-password]",
                "input[aria-label*='password' i]",
                "input[aria-label*='密码' i]",
            ],
            account.password,
            timeout_ms=10000,
        )
    if not password_filled and await prefer_password_sign_in(page, pause_controller):
        password_filled = await fill_first(
            page,
            [
                "input[type=password]",
                "input[name=passwd]",
                "input[autocomplete=current-password]",
                "input[aria-label*='password' i]",
                "input[aria-label*='密码' i]",
            ],
            account.password,
            timeout_ms=10000,
        )
    if not password_filled:
        # FIDO cancellation and stale Microsoft account pages can return to
        # the localized account-entry screen. Re-detect the page state instead
        # of assuming that the next screen must already contain a password box.
        email_filled_retry = await fill_first(page, email_selectors, account.email, timeout_ms=5000)
        if email_filled_retry:
            await click_submit(page)
            await cancel_microsoft_fido_bridge(page, pause_controller, timeout_seconds=5)
            await wait_for_timeout_maybe_stop(page, 1000, pause_controller, "Microsoft email re-submit after FIDO return")
            await prefer_password_sign_in(page, pause_controller)
            password_filled = await fill_first(
                page,
                [
                    "input[type=password]",
                    "input[name=passwd]",
                    "input[autocomplete=current-password]",
                    "input[aria-label*='password' i]",
                    "input[aria-label*='密码' i]",
                ],
                account.password,
                timeout_ms=10000,
            )
    if not password_filled:
        raise RuntimeError("Could not find Microsoft password input.")
    await click_submit(page)
    await wait_for_timeout_maybe_stop(page, 3000, pause_controller, "Microsoft password submit")


async def security_challenge_visible(page: Page) -> bool:
    url = (getattr(page, "url", "") or "").casefold()
    # The Outlook mailbox/search shell can contain generic words such as
    # "security" and "code" in its navigation and message list. It is never
    # the Microsoft sign-in challenge surface, and must not be treated as one.
    if "outlook.live.com/mail" in url:
        return False
    if "account.microsoft.com/account" in url or "pagename=account" in url:
        return False
    body = await page.locator("body").inner_text(timeout=5000)
    lower = body.casefold()
    strong_markers = [
        "security code",
        "verify your identity",
        "alternate email",
        "verification method",
        "use a different verification option",
        "send code to",
        "already have a code",
        "we need to verify",
        "prove your identity",
        "安全代码",
        "验证你的身份",
        "备用电子邮件",
        "验证方式",
        "其他验证方式",
        "将代码发送到",
        "将验证码发送到",
        "已经拥有代码",
        "已有验证码",
        "证明你的身份",
    ]
    if any(marker.casefold() in lower for marker in strong_markers):
        return True
    email_address_markers = ["email address", "e-mail address", "邮箱地址", "电子邮件地址"]
    verification_context_markers = [
        "security",
        "verify",
        "verification",
        "identity",
        "code",
        "安全",
        "验证",
        "身份",
        "代码",
    ]
    return any(marker in lower for marker in email_address_markers) and any(
        marker in lower for marker in verification_context_markers
    )


async def submit_security_email(page: Page, security_email: str, pause_controller=None) -> bool:
    # Never let the broad email selectors below target Outlook's search box or
    # any other mailbox control. The recovery-email form lives on Microsoft's
    # sign-in/account surface, not on /mail/.
    if "outlook.live.com/mail" in (getattr(page, "url", "") or "").casefold():
        return False
    # Microsoft may first render a proof-method chooser. The email option is
    # a radio input, not a text field; select it and submit the chooser so the
    # next state can prove whether a code was actually sent.
    try:
        radios = page.locator("input[type=radio]:visible")
        radio_count = await radios.count()
        for index in range(radio_count):
            radio = radios.nth(index)
            value = (await radio.get_attribute("value", timeout=500) or "").casefold()
            title = (await radio.get_attribute("title", timeout=500) or "").casefold()
            aria_label = (await radio.get_attribute("aria-label", timeout=500) or "").casefold()
            try:
                label_text = (await radio.locator("xpath=..").inner_text(timeout=500)).casefold()
            except Exception:
                label_text = ""
            proof_text = f"{value} {title} {aria_label} {label_text}"
            if not any(marker in proof_text for marker in ["email", "e-mail", "电子邮件", "邮箱", "邮件"]):
                continue
            if any(marker in proof_text for marker in ["phone", "sms", "短信", "authenticator", "身份验证器", "电话"]):
                continue
            try:
                await radio.check(timeout=2000)
            except Exception:
                await radio.click(timeout=2000)
            # The Chinese Microsoft page shown in production renders a
            # separate text box after the radio is selected.  Its domain is
            # fixed to @outlook.com, so only the local part must be entered.
            local_part = security_email.rsplit("@", 1)[0].strip()
            await wait_for_timeout_maybe_stop(page, 400, pause_controller, "Microsoft proof address field render")
            await fill_first(
                page,
                [
                    "input[name=proof]:not([type=radio])",
                    "input[name*='proof' i]:not([type=radio])",
                    "input[id^='iProof']:not([type=radio])",
                    "input[id*='proof' i]:not([type=radio])",
                    "input[aria-describedby*='iProof' i]:not([type=radio])",
                ],
                local_part,
                timeout_ms=4000,
            )
            await click_submit(page)
            await wait_for_timeout_maybe_stop(page, 3000, pause_controller, "Microsoft email proof method submit")
            return True
    except Exception:
        pass

    filled = await fill_first(
        page,
        [
            "input[type=email]",
            "input[name=email]:not([type=radio])",
            "input[name=proof]:not([type=radio])",
            "input[name*='proof' i]:not([type=radio])",
            "input[name*='email' i]:not([type=radio]):not([type=checkbox])",
            "input[id*='proof' i]:not([type=radio])",
            "input[id*='email' i]:not([type=radio]):not([type=checkbox])",
            "input[aria-label*='email' i]:not([type=radio]):not([type=checkbox])",
            "input[aria-label*='邮箱' i]:not([type=radio]):not([type=checkbox])",
            "input[aria-label*='电子邮件' i]:not([type=radio]):not([type=checkbox])",
            "input[placeholder*='email' i]:not([type=radio]):not([type=checkbox])",
            "input[placeholder*='邮箱' i]:not([type=radio]):not([type=checkbox])",
            "input[placeholder*='电子邮件' i]:not([type=radio]):not([type=checkbox])",
        ],
        security_email,
        timeout_ms=4000,
    )
    if not filled:
        # Forward-rule reauthentication uses a localized generic text input
        # labelled only "电子邮件"/"Email".  Limit this fallback to the
        # Microsoft verification surface and its send-code copy so it can
        # never target Outlook's mailbox search box.
        try:
            body = (await page.locator("body").inner_text(timeout=1500)).casefold()
        except Exception:
            body = ""
        if any(marker in body for marker in [
            "验证你的电子邮件",
            "发送验证码",
            "verify your email",
            "send a code",
        ]):
            filled = await fill_first(
                page,
                [
                    "input:not([type=radio]):not([type=checkbox]):not([type=password]):not([type=hidden])",
                ],
                security_email,
                timeout_ms=4000,
            )
    if filled:
        await click_submit(page)
        await wait_for_timeout_maybe_stop(page, 3000, pause_controller, "Microsoft security email submit")
        return True
    return False


def extract_code(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text)
    patterns = [
        r"microsoft account security code[^0-9]{0,100}([0-9]{6,8})",
        r"microsoft security code[^0-9]{0,80}([0-9]{6,8})",
        r"account security code[^0-9]{0,80}([0-9]{6,8})",
        r"your microsoft account code[^0-9]{0,80}([0-9]{6,8})",
        r"your microsoft security code[^0-9]{0,80}([0-9]{6,8})",
        r"安全代码[^0-9]{0,80}([0-9]{6,8})",
        r"Microsoft.{0,80}?安全代码[^0-9]{0,80}([0-9]{6,8})",
        r"Microsoft.{0,80}?(?:验证码|代码)[^0-9]{0,80}([0-9]{6,8})",
        r"Microsoft\s*(?:帐户|账户).{0,160}?(?:安全代码|验证码|验证代码)[^0-9]{0,160}([0-9]{6,8})",
        r"(?:安全代码|验证码|验证代码)[^0-9]{0,80}([0-9]{6,8}).{0,160}?Microsoft",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            return match.group(1)
    return None


async def read_open_outlook_message_texts(page: Page) -> list[str]:
    """Read an opened Outlook message, including its isolated message-body frames."""
    texts: list[str] = []
    message_selectors = [
        "[data-testid*='message-body' i]",
        "[aria-label*='Message body' i]",
        "[aria-label*='邮件正文']",
        "[aria-label*='邮件内容']",
        "[role=document]",
    ]
    for selector in message_selectors:
        try:
            for value in await page.locator(selector).all_inner_texts():
                if value.strip():
                    texts.append(value)
        except Exception:
            pass
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            value = await frame.locator("body").inner_text(timeout=2000)
            if value.strip():
                texts.append(value)
        except Exception:
            pass
    try:
        texts.append(await page.locator("body").inner_text(timeout=3000))
    except Exception:
        pass
    return texts


async def extract_security_code_from_open_message(page: Page) -> str | None:
    """Extract a Microsoft code from the actual opened Outlook message content."""
    texts = await read_open_outlook_message_texts(page)
    for value in texts:
        code = extract_code(value)
        if code:
            return code
    return None


def parse_mail_row_time(text: str, now_epoch: float | None = None) -> float | None:
    """Parse the time/date Outlook exposes on a message row."""
    value = " ".join((text or "").split())
    if not value:
        return None
    now_epoch = now_epoch if now_epoch is not None else time.time()
    local_now = time.localtime(now_epoch)
    lower_value = value.casefold()
    # A numeric calendar date is authoritative.  Outlook otherwise labels
    # older rows as relative dates; keep 前天 distinct from 昨天 so an old
    # message cannot pass a request-time lower bound just because its clock
    # value is later than the current request.
    date_match = re.search(
        r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b|\b(\d{1,2})月(\d{1,2})日",
        value,
    )
    day_offset = 0
    if not date_match:
        if "前天" in value:
            day_offset = -2
        elif "yesterday" in lower_value or "昨天" in value:
            day_offset = -1
    year, month, day = local_now.tm_year, local_now.tm_mon, local_now.tm_mday
    if date_match:
        if date_match.group(1):
            month, day = int(date_match.group(1)), int(date_match.group(2))
            explicit_year = date_match.group(3)
        else:
            month, day = int(date_match.group(4)), int(date_match.group(5))
            explicit_year = None
        if explicit_year:
            year = int(explicit_year)
            if year < 100:
                year += 2000
    clock = re.search(r"\b(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\b", value)
    if not clock:
        return None
    hour, minute = int(clock.group(1)), int(clock.group(2))
    meridiem = (clock.group(3) or "").casefold()
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    try:
        return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1)) + day_offset * 86400
    except (OverflowError, ValueError):
        return None


def microsoft_security_mail_row_selector() -> str:
    return ", ".join(["[role=option]", "[data-convid]", "[data-itemid]", "[role=listitem]"])


def microsoft_masked_email(email: str) -> str:
    """Build the mask used by real Microsoft security-code messages."""
    value = (email or "").strip().casefold()
    if "@" not in value:
        return ""
    local, domain = value.split("@", 1)
    if len(local) <= 2:
        masked_local = local[:1] + "**"
    else:
        masked_local = local[:2] + "**" + local[-1]
    return f"{masked_local}@{domain}"


def is_microsoft_security_mail_row(text: str, target_email: str | None = None) -> bool:
    """Match the actual Microsoft code-mail sender/subject/body shape."""
    value = " ".join((text or "").split())
    lower = value.casefold()
    chinese_shape = (
        re.search(r"Microsoft\s*(?:帐户|账户)团队", value, re.I)
        and re.search(r"个人\s+Microsoft\s*(?:帐户|账户)\s*安全代码", value, re.I)
        and "安全代码" in value
    )
    english_shape = (
        re.search(r"Microsoft\s+account\s+team", value, re.I)
        and re.search(r"Microsoft\s+account\s+security\s+code", value, re.I)
    )
    if not (chinese_shape or english_shape):
        return False
    if target_email:
        masked = microsoft_masked_email(target_email)
        if masked and masked not in lower:
            return False
    return extract_code(value) is not None


def should_accept_microsoft_security_mail_row(
    text: str,
    row_epoch: float | None,
    message_ids: set[str],
    not_before_epoch: float | None,
    exclude_message_ids: set[str] | None,
    target_email: str | None = None,
) -> bool:
    """Pure decision gate for replay tests and the live Outlook row scanner."""
    if not is_microsoft_security_mail_row(text, target_email):
        return False
    if not_before_epoch is None:
        return True
    request_minute = not_before_epoch - (not_before_epoch % 60)
    if row_epoch is None or row_epoch < not_before_epoch:
        # A stable new ID can still legitimately display the same HH:MM as
        # the request; the UI has no seconds field. Rows from an earlier
        # displayed minute remain impossible to accept.
        if row_epoch is None or row_epoch < request_minute:
            return False
    if exclude_message_ids is not None and message_ids & exclude_message_ids:
        return False
    # Without a stable ID proving that the row was created after the request,
    # a same-minute Outlook row is ambiguous because the UI only shows HH:MM.
    if not message_ids or not exclude_message_ids:
        if row_epoch <= request_minute:
            return False
    return True


async def microsoft_security_mail_ids_from_row(row) -> set[str]:
    """Read conversation/item IDs from a row and its nested Outlook nodes."""
    try:
        values = await row.evaluate(
            """(element) => {
                const nodes = [element, ...element.querySelectorAll('[data-convid], [data-itemid]')];
                const ids = [];
                for (const node of nodes) {
                    for (const attribute of ['data-convid', 'data-itemid']) {
                        const value = node.getAttribute(attribute);
                        if (value) ids.push(value);
                    }
                }
                return ids;
            }"""
        )
        return {value for value in (values or []) if value}
    except Exception:
        ids: set[str] = set()
        for attribute in ["data-convid", "data-itemid"]:
            try:
                value = await row.get_attribute(attribute, timeout=500)
                if value:
                    ids.add(value)
            except Exception:
                pass
        return ids


async def collect_microsoft_security_mail_ids(page: Page) -> set[str]:
    """Capture existing message IDs before requesting a new security code."""
    # The security mailbox can be left on a stale /mail/ shell after a
    # persisted-session reuse. In that state Outlook may not have rendered
    # list-row IDs yet. Always return to the inbox and wait for real rows
    # before taking the pre-request snapshot. This refresh is isolated to the
    # security mailbox page and happens before the new code is sent.
    try:
        await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
    except Exception:
        pass
    ids: set[str] = set()
    rows = page.locator(microsoft_security_mail_row_selector())
    try:
        row_count = 0
        for _ in range(3):
            row_count = await rows.count()
            if row_count:
                break
            await page.wait_for_timeout(1000)
        for index in range(min(row_count, 50)):
            row = rows.nth(index)
            ids.update(await microsoft_security_mail_ids_from_row(row))
    except Exception:
        pass
    return ids


async def open_latest_microsoft_security_mail(
    page: Page,
    not_before_epoch: float | None = None,
    row_time_slack_seconds: int = 0,
    exclude_message_ids: set[str] | None = None,
    target_email: str | None = None,
) -> bool:
    """Open the newest real Microsoft security-code message row, preferring unread rows."""
    rows = page.locator(microsoft_security_mail_row_selector())
    candidates: list[tuple[bool, Any]] = []
    try:
        count = min(await rows.count(), 20)
        for index in range(count):
            row = rows.nth(index)
            try:
                text = " ".join(
                    [
                        await row.inner_text(timeout=1000),
                        (await row.get_attribute("aria-label")) or "",
                        (await row.get_attribute("title")) or "",
                    ]
                )
                message_ids = await microsoft_security_mail_ids_from_row(row)
                row_epoch = parse_mail_row_time(text) if not_before_epoch is not None else None
                if not should_accept_microsoft_security_mail_row(
                    text,
                    row_epoch,
                    message_ids,
                    not_before_epoch,
                    exclude_message_ids,
                    target_email,
                ):
                    continue
                unread = bool(re.search(r"\bunread\b|未读", text, re.I))
                candidates.append((unread, row))
            except Exception:
                pass
        candidates.sort(key=lambda item: not item[0])
        for _unread, row in candidates:
            try:
                await row.click(timeout=3000)
                return True
            except Exception:
                pass
    except Exception:
        pass
    return False


async def search_security_mail(page: Page, query: str, pause_controller=None) -> None:
    await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
    await wait_for_timeout_maybe_stop(page, 1500, pause_controller, "Outlook security-code mailbox load")

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
            await wait_for_timeout_maybe_stop(page, 2000, pause_controller, "Outlook security-code search")
            return
        except PlaywrightTimeoutError:
            pass


async def get_security_code(
    page: Page,
    target_email: str,
    timeout_seconds: int,
    pause_controller=None,
    not_before_epoch: float | None = None,
    exclude_message_ids: set[str] | None = None,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    # Do not inspect the mailbox immediately after Microsoft confirms the
    # request.  A previous unread message can be visible before the new mail
    # reaches the list, and Outlook often displays only minute precision.
    delivery_grace_seconds = 8
    earliest_search_epoch = (
        not_before_epoch + delivery_grace_seconds if not_before_epoch is not None else None
    )
    queries = [
        "isread:no from:accountprotection.microsoft.com",
        "from:accountprotection.microsoft.com",
    ]

    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError(f"Stopped while waiting for security code for {target_email}.")
        if earliest_search_epoch is not None and time.time() < earliest_search_epoch:
            await wait_for_timeout_maybe_stop(
                page,
                min(1000, max(100, int((earliest_search_epoch - time.time()) * 1000))),
                pause_controller,
                "Microsoft security-code delivery grace period",
            )
            continue
        await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
        await wait_for_timeout_maybe_stop(page, 1500, pause_controller, "Outlook latest security-code mail load")
        if await open_latest_microsoft_security_mail(
            page,
            not_before_epoch=not_before_epoch,
            exclude_message_ids=exclude_message_ids,
            target_email=target_email,
        ):
            await wait_for_timeout_maybe_stop(page, 1200, pause_controller, "Outlook latest security-code message open")
            code = await extract_security_code_from_open_message(page)
            if code:
                return code
        for query in queries:
            if pause_controller and pause_controller.should_stop():
                raise RuntimeError(f"Stopped while waiting for security code for {target_email}.")
            if time.monotonic() >= deadline:
                break
            await search_security_mail(page, query, pause_controller)
            opened = await open_latest_microsoft_security_mail(
                page,
                not_before_epoch=not_before_epoch,
                exclude_message_ids=exclude_message_ids,
                target_email=target_email,
            )
            if opened:
                await wait_for_timeout_maybe_stop(page, 1200, pause_controller, "Outlook security-code message open")
                code = await extract_security_code_from_open_message(page)
                if code:
                    return code

        await wait_for_timeout_maybe_stop(page, 5000, pause_controller, f"Outlook security-code retry for {target_email}")

    raise TimeoutError(f"Timed out waiting for security code for {target_email}.")


async def classify_security_code_input_layout(page: Page, expected_length: int = 6) -> tuple[str, object | None]:
    """Identify Microsoft's current OTP control shape before writing anything.

    Returns ``("segmented", locator)``, ``("single", locator)``, or
    ``("none", None)``. Microsoft has deployed both one six-character input
    and six independent one-character inputs, sometimes without a useful
    ``name`` attribute.
    """
    code_inputs = page.locator(
        "input[name=otc]:visible, input[name=code]:visible, input[type=tel]:visible, "
        "input[autocomplete=one-time-code]:visible, input[inputmode=numeric]:visible, "
        "input[aria-label*='code' i]:visible, input[id*='code' i]:visible, "
        "input[name*='otp' i]:visible, input[id*='otp' i]:visible"
    )
    code_count = await code_inputs.count()
    if code_count > 1:
        return "segmented", code_inputs
    if code_count == 1:
        return "single", code_inputs

    # Some Microsoft builds expose six bare text inputs, role=textbox nodes, or
    # contenteditable controls and distinguish them only through maxlength,
    # inputmode, or position. Inspect only visible controls and reject obvious
    # email/password controls.
    generic_inputs = page.locator(
        "input:visible, textarea:visible, [role='textbox']:visible, [contenteditable='true']:visible"
    )
    generic_count = await generic_inputs.count()
    candidates: list[int] = []
    for index in range(min(generic_count, 20)):
        item = generic_inputs.nth(index)
        attributes = {}
        for name in ["type", "name", "id", "aria-label", "placeholder", "inputmode", "maxlength", "autocomplete"]:
            try:
                attributes[name] = (await item.get_attribute(name, timeout=400) or "").casefold()
            except Exception:
                attributes[name] = ""
        descriptor = " ".join(attributes.values())
        if any(marker in descriptor for marker in ["email", "mail", "password", "passwd"]):
            continue
        if (
            attributes["inputmode"] == "numeric"
            or attributes["type"] == "tel"
            or attributes["maxlength"] in {"1", "2"}
            or any(marker in descriptor for marker in ["code", "otp", "verification", "验证码", "代码"])
        ):
            candidates.append(index)
    if len(candidates) > 1:
        return "segmented", (generic_inputs, candidates)
    if len(candidates) == 1:
        return "single", generic_inputs.nth(candidates[0])
    if generic_count >= expected_length and generic_count <= expected_length + 2:
        return "segmented", (generic_inputs, list(range(generic_count)))
    return "none", None


async def fill_security_code_control(control, value: str) -> None:
    """Fill either a native input or a Microsoft role=textbox control."""
    try:
        await control.fill(value, timeout=5000)
        return
    except Exception as first_error:
        try:
            await control.click(timeout=3000)
            await control.press("Control+A", timeout=3000)
            await control.type(value, timeout=5000)
            return
        except Exception:
            raise first_error


async def submit_code(page: Page, code: str, pause_controller=None) -> None:
    normalized_code = str(code or "").strip()
    if not normalized_code:
        raise RuntimeError("Microsoft security code is empty.")

    layout, inputs = await classify_security_code_input_layout(page, len(normalized_code))
    if layout == "segmented":
        if isinstance(inputs, tuple):
            segmented_inputs, segmented_indexes = inputs
            segmented_count = len(segmented_indexes)
        else:
            segmented_inputs = inputs
            segmented_count = await inputs.count()
            segmented_indexes = list(range(segmented_count))
        capacities: list[int] = []
        for segmented_index in segmented_indexes:
            item = segmented_inputs.nth(segmented_index)
            try:
                maxlength = (await item.get_attribute("maxlength", timeout=500) or "").strip()
                capacity = int(maxlength) if maxlength.isdigit() and int(maxlength) > 0 else 1
            except Exception:
                capacity = 1
            capacities.append(capacity)
        # Some Microsoft localized builds render three fields, each accepting
        # two characters, while the six-box build renders one character per
        # field.  Infer the equal-width capacity only when the code divides
        # evenly; otherwise keep the strict maxlength-based safety gate.
        if sum(capacities) < len(normalized_code) and segmented_count and len(normalized_code) % segmented_count == 0:
            inferred_capacity = len(normalized_code) // segmented_count
            if inferred_capacity > 1 and all(capacity == 1 for capacity in capacities):
                capacities = [inferred_capacity] * segmented_count
        if sum(capacities) < len(normalized_code):
            raise RuntimeError(
                f"Microsoft security code input has {segmented_count} boxes, "
                f"but the code has {len(normalized_code)} characters."
            )
        offset = 0
        for index, capacity in enumerate(capacities):
            chunk = normalized_code[offset : offset + capacity]
            if not chunk:
                break
            await fill_security_code_control(segmented_inputs.nth(segmented_indexes[index]), chunk)
            offset += len(chunk)
        # The six-box Microsoft control may auto-submit as soon as the last
        # digit is entered and close/navigate the OAuth popup. That is a
        # successful submission path, not a reason to refresh the mailbox.
        try:
            if page.is_closed():
                return
            if not await page.locator(
                "input[name=otc]:visible, input[name=code]:visible, input[type=tel]:visible, "
                "input[autocomplete=one-time-code]:visible, input[inputmode=numeric]:visible, "
                "input[aria-label*='code' i]:visible, input[id*='code' i]:visible"
            ).count():
                return
        except Exception as exc:
            if any(marker in str(exc).casefold() for marker in ["closed", "target page", "browser has been"]):
                return
            raise
    elif layout == "single":
        await fill_security_code_control(inputs, normalized_code)
    else:
        raise RuntimeError(
            "Could not identify a Microsoft security-code input layout; refusing to fill a generic field."
        )
    try:
        await click_submit(page)
        if page.is_closed():
            return
        await wait_for_timeout_maybe_stop(page, 4000, pause_controller, "Microsoft security code submit")
    except Exception as exc:
        if any(marker in str(exc).casefold() for marker in ["closed", "target page", "browser has been"]):
            return
        raise


async def ensure_mailbox_loaded(page: Page, pause_controller=None) -> None:
    await maybe_handle_stay_signed_in(page)
    if "outlook.live.com" not in page.url:
        await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
    await wait_for_timeout_maybe_stop(page, 5000, pause_controller, "Outlook mailbox load")


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
        args=[
            f"--window-position={args.security_x},{args.security_y}",
        ],
    )
    await install_webauthn_cancel(context)
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
            args=[
                f"--window-position={args.target_x},{args.target_y}",
            ],
        )
        await install_webauthn_cancel(target_context)
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
                    print(f"Code found for {account.email}.")
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


def main() -> int:
    print("This legacy Outlook-only entry point is disabled.")
    print("Use run_steam_web_ui.bat for the three supported hardened task flows.")
    print("Use run_steam_outlook_batch.bat only if you intentionally want the hardened CLI worker.")
    print("outlook_batch_login.py is kept as an imported helper module; direct execution skips current recovery/status handling.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
