import argparse
import asyncio
import re
import secrets
import string
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

import feishu_tool
import graph_mail
import dota2_arcade
from outlook_batch_login import (
    DEFAULT_FEISHU_SHEET_URL,
    DEFAULT_SECURITY_EMAIL,
    LOGIN_URL,
    LOGOUT_URL,
    OUTLOOK_MAIL_URL,
    click_first,
    fill_login,
    security_challenge_visible,
    submit_security_email,
    submit_code,
    ensure_mailbox_loaded,
    cell_text,
)


STEAM_ID_COL = "I"
BIG_ACCOUNT_COL = "G"
EMAIL_PASSWORD_COL = "D"
STEAM_GUARD_CODE_RE = re.compile(r"\b[A-Z0-9]{5}\b")


@dataclass
class SteamAccount:
    row_number: int
    steam_username: str
    steam_password: str
    email: str
    email_password: str
    steam_id: str

    @property
    def password(self) -> str:
        # Compatibility with outlook_batch_login.fill_login().
        return self.email_password


def read_pending_accounts(args) -> tuple[list[SteamAccount], dict[str, Any], str, str]:
    config = feishu_tool.load_config(Path(args.feishu_config))
    feishu_tool.start_mcp(Path(args.feishu_start_script))
    resolved = feishu_tool.resolve_spreadsheet(config, args.sheet_url, args.sheet_id)
    spreadsheet_token = resolved.get("spreadsheet_token")
    sheet_id = resolved.get("sheet_id")
    if not spreadsheet_token or not sheet_id:
        raise SystemExit("Could not resolve Feishu spreadsheet token/sheet id.")

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

    rows = response.get("data", {}).get("valueRange", {}).get("values", [])
    if not rows:
        raise SystemExit("Feishu sheet range returned no rows.")

    header = [cell_text(cell) for cell in rows[0]]
    required = ["steam账号", "邮箱账号", "steam id"]
    missing = [name for name in required if name not in header]
    if missing:
        raise SystemExit(f"Feishu sheet missing columns: {', '.join(missing)}")

    steam_username_idx = header.index("steam账号")
    email_idx = header.index("邮箱账号")
    steam_id_idx = header.index("steam id")
    password_indices = [idx for idx, name in enumerate(header) if name == "密码"]
    if len(password_indices) < 2:
        raise SystemExit('Feishu sheet must contain two "密码" columns: Steam password and email password.')

    steam_password_idx = password_indices[0]
    email_password_idx = password_indices[1]

    accounts: list[SteamAccount] = []
    for row_number, row in enumerate(rows[1:], start=2):
        padded = list(row) + [""] * (len(header) - len(row))
        steam_id = cell_text(padded[steam_id_idx])
        steam_username = cell_text(padded[steam_username_idx])
        steam_password = cell_text(padded[steam_password_idx])
        email = cell_text(padded[email_idx])
        email_password = cell_text(padded[email_password_idx])
        if steam_id or not steam_username or not steam_password or not email or not email_password:
            continue
        accounts.append(
            SteamAccount(
                row_number=row_number,
                steam_username=steam_username,
                steam_password=steam_password,
                email=email,
                email_password=email_password,
                steam_id=steam_id,
            )
        )

    start_row = getattr(args, "start_row", None)
    if start_row:
        accounts = [account for account in accounts if account.row_number >= int(start_row)]
    target_email = (getattr(args, "target_email", "") or "").strip().casefold()
    if target_email:
        accounts = [account for account in accounts if account.email.casefold() == target_email]
    return accounts, config, spreadsheet_token, sheet_id


def find_steam_exe(explicit_path: str | None) -> Path:
    if explicit_path:
        path = Path(explicit_path)
        if path.exists():
            return path
        raise SystemExit(f"Steam exe not found: {path}")

    candidates: list[Path] = []
    try:
        output = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$p=(Get-Process steam -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Path); if($p){$p}",
            ],
            text=True,
        ).strip()
        if output:
            candidates.append(Path(output))
    except subprocess.SubprocessError:
        pass

    candidates.extend([Path("D:/Steam/steam.exe"), Path("C:/Program Files (x86)/Steam/steam.exe")])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit("Steam exe not found. Pass --steam-exe D:\\Steam\\steam.exe")


def shutdown_steam(steam_exe: Path, timeout_seconds: int = 30) -> None:
    subprocess.run([str(steam_exe), "-shutdown"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "if(Get-Process steam -ErrorAction SilentlyContinue){exit 1}else{exit 0}"],
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)

    subprocess.run(["taskkill", "/IM", "steam.exe", "/T", "/F"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)


def start_steam_login(steam_exe: Path, account: SteamAccount) -> None:
    subprocess.Popen(
        [str(steam_exe), "-login", account.steam_username, account.steam_password],
        cwd=str(steam_exe.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def send_guard_code_to_steam(code: str, timeout_seconds: int) -> bool:
    try:
        from pywinauto import Desktop
        from pywinauto.keyboard import send_keys
    except ImportError as exc:
        raise RuntimeError("pywinauto is not installed. Run: python -m pip install -r requirements.txt") from exc

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            windows = Desktop(backend="uia").windows(title_re=r".*(Steam|Sign in|登录|验证码|Guard).*")
            for window in windows:
                title = window.window_text()
                if not title:
                    continue
                try:
                    window.set_focus()
                    time.sleep(0.3)
                    edits = window.descendants(control_type="Edit")
                    if edits:
                        edits[-1].set_focus()
                    send_keys("^a")
                    send_keys(code, with_spaces=True)
                    send_keys("{ENTER}")
                    print(f"Submitted Steam Guard code in window: {title}")
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(2)
    return False


def parse_loginusers(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    users: list[dict[str, str]] = []
    idx = 0
    while idx < len(lines):
        match = re.match(r'\s*"(\d{17})"\s*$', lines[idx])
        if not match:
            idx += 1
            continue
        steam_id = match.group(1)
        block_lines = []
        depth = 0
        idx += 1
        while idx < len(lines):
            line = lines[idx]
            depth += line.count("{")
            depth -= line.count("}")
            block_lines.append(line)
            idx += 1
            if depth <= 0 and "}" in line:
                break
        block = "\n".join(block_lines)
        account = re.search(r'"AccountName"\s*"([^"]*)"', block)
        most_recent = re.search(r'"MostRecent"\s*"([^"]*)"', block)
        users.append(
            {
                "steam_id": steam_id,
                "account_name": account.group(1) if account else "",
                "most_recent": most_recent.group(1) if most_recent else "",
            }
        )
    return users


def wait_for_steam_id(steam_exe: Path, steam_username: str, timeout_seconds: int) -> str:
    loginusers_path = steam_exe.parent / "config" / "loginusers.vdf"
    deadline = time.monotonic() + timeout_seconds
    target = steam_username.casefold()
    while time.monotonic() < deadline:
        users = parse_loginusers(loginusers_path)
        for user in users:
            if user["account_name"].casefold() == target and user["most_recent"] == "1":
                return user["steam_id"]
        recent = [user for user in users if user["most_recent"] == "1" and user["steam_id"]]
        if recent:
            print(f"Most recent Steam account in loginusers.vdf: {recent[0]['account_name']} {recent[0]['steam_id']}")
        time.sleep(3)
    raise TimeoutError(f"Could not confirm Steam ID for {steam_username} from {loginusers_path}")


def extract_steam_guard_code(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text).upper()
    patterns = [
        r"STEAM\s+GUARD\s+CODE[^A-Z0-9]{0,80}([A-Z0-9]{5})",
        r"CODE\s+YOU\s+NEED[^A-Z0-9]{0,80}([A-Z0-9]{5})",
        r"YOUR\s+STEAM\s+CODE[^A-Z0-9]{0,80}([A-Z0-9]{5})",
        r"验证码[^A-Z0-9]{0,80}([A-Z0-9]{5})",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            return match.group(1)
    candidates = [match.group(0) for match in STEAM_GUARD_CODE_RE.finditer(normalized)]
    ignored = {"STEAM", "GUARD", "LOGIN", "EMAIL", "HTTPS"}
    for candidate in candidates:
        if candidate not in ignored:
            return candidate
    return None


async def search_steam_guard_mail(page: Page, steam_username: str, timeout_seconds: int) -> str:
    deadline = time.monotonic() + timeout_seconds
    queries = [
        '"Steam Guard"',
        '"Steam" "code"',
        f'"{steam_username}" "Steam"',
        "Steam",
    ]
    while time.monotonic() < deadline:
        for query in queries:
            await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            for selector in [
                "input[aria-label*='Search' i]",
                "input[aria-label*='搜索' i]",
                "input[placeholder*='Search' i]",
                "input[placeholder*='搜索' i]",
                "[role=searchbox]",
            ]:
                try:
                    search = page.locator(selector).first
                    await search.fill(query, timeout=3000)
                    await search.press("Enter")
                    await page.wait_for_timeout(5000)
                    break
                except PlaywrightTimeoutError:
                    pass

            body_text = await page.locator("body").inner_text(timeout=10000)
            code = extract_steam_guard_code(body_text)
            if code:
                return code
            opened = await click_first(page, [r"Steam", r"Steam Guard", r"验证码"], timeout_ms=1500)
            if opened:
                await page.wait_for_timeout(2500)
                body_text = await page.locator("body").inner_text(timeout=10000)
                code = extract_steam_guard_code(body_text)
                if code:
                    return code
        await page.wait_for_timeout(5000)
    raise TimeoutError("Timed out waiting for Steam Guard email code.")


async def logout_outlook(page: Page) -> None:
    await page.goto(LOGOUT_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)


async def show_window_instruction(page: Page, title: str, lines: list[str]) -> None:
    items = "".join(f"<li>{line}</li>" for line in lines)
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{ margin: 0; font-family: "Segoe UI", Arial, sans-serif; background: #f6f8fb; color: #172033; }}
    main {{ max-width: 760px; margin: 72px auto; background: white; border: 1px solid #d8dee9; border-radius: 8px; padding: 28px 32px; }}
    h1 {{ font-size: 24px; margin: 0 0 16px; }}
    li {{ margin: 10px 0; line-height: 1.6; }}
    .hint {{ margin-top: 18px; padding: 12px; background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; color: #92400e; }}
  </style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <ul>{items}</ul>
    <div class="hint">主要操作提示会同步显示在 localhost 控制台，请按提示完成后点击“已完成人工操作 / 继续”。</div>
  </main>
</body>
</html>"""
    await page.set_content(html, wait_until="domcontentloaded")


async def cancel_passkey_prompt(page: Page, log=print, timeout_seconds: int = 30) -> None:
    deadline = time.monotonic() + timeout_seconds
    prompt_patterns = [
        r"通行密钥",
        r"passkey",
        r"正在设置密码",
        r"setting.*pass",
        r"创建.*login\.microsoft\.com",
    ]
    cancel_patterns = [
        r"^取消$",
        r"^Cancel$",
        r"暂不",
        r"Skip",
        r"Not now",
    ]
    while time.monotonic() < deadline:
        try:
            body = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            body = ""
        if not any(re.search(pattern, body, re.I) for pattern in prompt_patterns):
            return
        log("Microsoft passkey prompt detected; clicking Cancel.")
        if await click_first(page, cancel_patterns, timeout_ms=2500):
            await page.wait_for_timeout(3000)
            continue
        for selector in [
            "button:has-text('取消')",
            "button:has-text('Cancel')",
            "input[type=button][value='取消']",
            "input[type=button][value='Cancel']",
        ]:
            try:
                await page.locator(selector).first.click(timeout=1500)
                await page.wait_for_timeout(3000)
                break
            except Exception:
                pass
        await page.wait_for_timeout(1000)


def dismiss_windows_security_passkey(log=print, timeout_seconds: int = 12) -> bool:
    saw_possible_dialog = False
    try:
        from pywinauto import Desktop
    except ImportError:
        Desktop = None

    deadline = time.monotonic() + timeout_seconds
    title_patterns = [r"Windows 安全", r"Windows Security"]
    cancel_patterns = [r"取消", r"Cancel"]
    other_method_patterns = [r"选择其他通行密钥", r"Use a different passkey", r"其他"]
    while time.monotonic() < deadline:
        if Desktop:
            desktop = Desktop(backend="uia")
            for window in desktop.windows():
                try:
                    title = window.window_text()
                    text = " ".join(item.window_text() for item in window.descendants()[:40])
                    combined = f"{title} {text}"
                    if not any(re.search(pattern, combined, re.I) for pattern in title_patterns):
                        continue
                    if not any(marker in combined for marker in ["密钥", "passkey", "PIN", "QR", "Windows Security"]):
                        continue
                    saw_possible_dialog = True
                    for pattern in cancel_patterns:
                        try:
                            window.child_window(title_re=pattern, control_type="Button").click_input()
                            log("Windows security passkey dialog cancelled.")
                            return True
                        except Exception:
                            pass
                    for pattern in other_method_patterns:
                        try:
                            window.child_window(title_re=pattern).click_input()
                            log("Windows security passkey dialog switched to other method.")
                            return True
                        except Exception:
                            pass
                    try:
                        window.close()
                        log("Windows security passkey dialog closed.")
                        return True
                    except Exception:
                        pass
                except Exception:
                    pass
        time.sleep(0.5)
    if saw_possible_dialog:
        try:
            import pyautogui

            pyautogui.press("esc")
            log("Sent Escape to dismiss Windows security passkey dialog.")
            return True
        except Exception:
            pass
    return False


async def wait_for_outlook_inbox(page: Page, log=print, timeout_seconds: int = 90) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        dismiss_windows_security_passkey(log, timeout_seconds=2)
        await cancel_passkey_prompt(page, log, timeout_seconds=5)
        try:
            url = page.url
            body = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            url = ""
            body = ""
        markers = [
            "New mail",
            "新建邮件",
            "Inbox",
            "收件箱",
            "Focused",
            "重点",
            "Outlook",
        ]
        if "outlook.live.com" in url and any(marker.casefold() in body.casefold() for marker in markers):
            log("Outlook inbox confirmed.")
            return
        await page.wait_for_timeout(3000)
    raise TimeoutError("Timed out waiting for Outlook inbox after Microsoft login.")


async def connect_security_context(playwright, args) -> tuple[BrowserContext, Page]:
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=args.security_profile,
        headless=False,
        viewport={"width": 1280, "height": 900},
        args=[f"--window-position={args.security_x},{args.security_y}"],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    await show_window_instruction(
        page,
        "窗口2：安全邮箱",
        [
            f"请确认这个窗口登录的是安全邮箱：{args.security_email}",
            "程序会在目标账号触发 Microsoft 安全验证时，从这里收取验证码。",
            "如果没有登录，请手动登录；登录完成后回到 localhost 控制台点击继续。",
        ],
    )
    await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
    return context, page


def get_microsoft_security_code(args, account: SteamAccount) -> str:
    if args.security_code_source == "graph":
        if args.graph_all_accounts:
            accounts = graph_mail.load_accounts_config(Path(args.graph_accounts_config))
            code, mailbox = graph_mail.find_security_code_across_accounts(accounts, account.email, args.microsoft_code_timeout)
            print(f"Microsoft security code found in {mailbox}")
            return code
        client_id = graph_mail.load_client_id(Path(args.graph_config), args.graph_client_id)
        return graph_mail.find_security_code(client_id, Path(args.graph_cache), account.email, args.microsoft_code_timeout)
    raise RuntimeError("Browser security code source must use get_security_code with security_page.")


def generate_email_password(length: int = 18) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    required = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*"),
    ]
    required.extend(secrets.choice(alphabet) for _ in range(max(length, 12) - len(required)))
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


async def wait_for_manual_continue(
    message: str,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
) -> None:
    log(message)
    on_manual_wait(message)
    if pause_controller:
        if not pause_controller.wait():
            raise RuntimeError("Stopped while waiting for manual confirmation.")
    else:
        input(f"{message}\nPress Enter to continue...")
    on_manual_resume(message)


async def try_change_microsoft_password(page: Page, current_password: str, new_password: str, log) -> bool:
    await page.goto("https://account.live.com/password/Change", wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)
    await cancel_passkey_prompt(page, log, timeout_seconds=8)

    password_inputs = page.locator("input[type='password']")
    try:
        count = await password_inputs.count()
        if count < 3:
            return False
        await password_inputs.nth(0).fill(current_password, timeout=5000)
        await password_inputs.nth(1).fill(new_password, timeout=5000)
        await password_inputs.nth(2).fill(new_password, timeout=5000)
    except Exception:
        return False

    for pattern in [r"^保存$", r"^Save$", r"^下一步$", r"^Next$", r"^提交$", r"^Submit$"]:
        if await click_first(page, [pattern], timeout_ms=1500):
            await page.wait_for_timeout(5000)
            log("Password change form submitted; waiting for manual confirmation before writing Feishu.")
            return True
    return False


async def run_mail_security_maintenance(
    page: Page,
    args,
    account: SteamAccount,
    feishu_config: dict[str, Any],
    spreadsheet_token: str,
    sheet_id: str,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
) -> None:
    bind_email = (getattr(args, "bind_security_email", "") or "").strip()
    new_password = (getattr(args, "new_email_password", "") or "").strip() or generate_email_password()

    if bind_email:
        await page.goto("https://account.live.com/proofs/manage/additional", wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        await cancel_passkey_prompt(page, log, timeout_seconds=8)
        await wait_for_manual_continue(
            f"请在目标邮箱账号 {account.email} 的 Microsoft 页面完成绑定安全邮箱：{bind_email}。完成后点击 Web UI 的“已完成人工操作 / 继续”。",
            log,
            pause_controller,
            on_manual_wait,
            on_manual_resume,
        )

    submitted = await try_change_microsoft_password(page, account.email_password, new_password, log)
    if submitted:
        message = (
            f"请确认 {account.email} 的邮箱密码已成功改为：{new_password}。"
            "确认后点击“已完成人工操作 / 继续”，程序才会写回飞书 D 列。"
        )
    else:
        message = (
            f"请手动打开/停留在 Microsoft 改密页面，把 {account.email} 的邮箱密码改为：{new_password}。"
            "改密成功后点击“已完成人工操作 / 继续”，程序才会写回飞书 D 列。"
        )
    await wait_for_manual_continue(message, log, pause_controller, on_manual_wait, on_manual_resume)

    account.email_password = new_password
    feishu_tool.write_sheet(
        feishu_config,
        spreadsheet_token,
        sheet_id,
        f"{EMAIL_PASSWORD_COL}{account.row_number}:{EMAIL_PASSWORD_COL}{account.row_number}",
        [[new_password]],
        use_uat=args.feishu_auth == "user",
        package_root=Path(args.lark_mcp_root),
    )
    log(f"Wrote new email password to row {account.row_number} column {EMAIL_PASSWORD_COL}.")


async def run_graph_api_login(
    args,
    email: str,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
) -> None:
    client_id = graph_mail.load_client_id(Path(args.graph_config), args.graph_client_id)
    cache_path = Path(graph_mail.safe_cache_name(email))
    app, cache, flow = graph_mail.begin_login_flow(client_id, cache_path)
    message = (
        f"正在为 {email} 开启 Microsoft Graph API 登录。\n"
        f"{flow.get('message') or ''}\n"
        "请按上面的链接/代码完成 Microsoft 授权。授权完成后程序会自动继续；如果长时间无响应，可点停止。"
    )
    log(message)
    on_manual_wait(message)
    try:
        result = await asyncio.to_thread(graph_mail.complete_login_flow, app, cache, flow, cache_path)
    finally:
        on_manual_resume(f"Graph API login finished for {email}")
    account_email = result.get("id_token_claims", {}).get("preferred_username") or email
    graph_mail.save_account_config(Path(args.graph_accounts_config), account_email, client_id, cache_path)
    log(f"Graph API login saved: {account_email} -> {cache_path}")


async def verify_security_mailbox(page: Page, security_email: str) -> bool:
    expected = security_email.casefold()
    try:
        body = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        body = ""
    if expected in body.casefold():
        return True
    try:
        url = page.url.casefold()
    except Exception:
        url = ""
    return expected in url


async def wait_for_security_mailbox(
    page: Page,
    security_email: str,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
) -> None:
    log(f"Checking security mailbox login: {security_email}")
    while True:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError("Stopped while waiting for security mailbox login.")
        if await verify_security_mailbox(page, security_email):
            log(f"Security mailbox confirmed: {security_email}")
            on_manual_resume(f"Security mailbox confirmed: {security_email}")
            return
        try:
            body = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            body = ""
        if any(marker in body for marker in ["验证你的电子邮件", "Verify your email", "无法发送验证码", "can't send a code", "其他登录方法"]):
            message = (
                f"窗口2正在登录安全邮箱 {security_email}，但这个安全邮箱本身触发了 Microsoft 二次验证。"
                "请手动完成它的验证；如果页面提示“无法发送验证码”，这个安全邮箱当前不能用于网页收码，"
                "建议换一个已能正常进入收件箱的安全邮箱，或在 Web UI 把“安全验证码来源”改成 Microsoft Graph 多邮箱。"
                "处理完成并看到安全邮箱收件箱后，点击“已完成人工操作 / 继续”。"
            )
        else:
            message = f"请在窗口2登录/确认安全邮箱 {security_email}，完成后回到 localhost 控制台点击“已完成人工操作 / 继续”。"
        log(message)
        on_manual_wait(message)
        if pause_controller:
            if not pause_controller.wait():
                raise RuntimeError("Stopped while waiting for security mailbox login.")
        else:
            input("Log in security mailbox manually, then press Enter to re-check...")


class PauseController:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._decision_event = threading.Event()
        self._stop = threading.Event()
        self._decision = None

    def wait(self) -> bool:
        self._event.clear()
        while not self._stop.is_set():
            if self._event.wait(0.5):
                return True
        return False

    def continue_next(self) -> None:
        self._event.set()
        self.decide("continue")

    def decide(self, value: str) -> None:
        self._decision = value
        self._decision_event.set()
        self._event.set()

    def wait_decision(self, allowed: set[str]) -> str:
        self._decision = None
        self._decision_event.clear()
        while not self._stop.is_set():
            if self._decision_event.wait(0.5):
                if self._decision in allowed:
                    return self._decision
                self._decision_event.clear()
        return "stop"

    def stop(self) -> None:
        self._stop.set()
        self._event.set()

    def should_stop(self) -> bool:
        return self._stop.is_set()


async def run_with_callbacks(args, callbacks: dict[str, Any] | None = None) -> None:
    callbacks = callbacks or {}
    log = callbacks.get("log", print)
    on_pending = callbacks.get("on_pending", lambda accounts: None)
    on_account_start = callbacks.get("on_account_start", lambda account, index, total: None)
    on_account_done = callbacks.get("on_account_done", lambda account, steam_id: None)
    on_account_error = callbacks.get("on_account_error", lambda account, exc: None)
    on_manual_wait = callbacks.get("on_manual_wait", lambda message: None)
    on_manual_resume = callbacks.get("on_manual_resume", lambda message: None)
    pause_controller = callbacks.get("pause_controller")

    accounts, feishu_config, spreadsheet_token, sheet_id = read_pending_accounts(args)
    if args.limit:
        accounts = accounts[: args.limit]
    on_pending(accounts)
    if not accounts:
        log("No pending accounts with empty steam id.")
        return

    log(f"Pending accounts: {len(accounts)}")
    if args.list_pending:
        for account in accounts:
            log(f"row {account.row_number}: {account.steam_username} / {account.email}")
        return

    steam_exe = None
    if args.enable_steam_login or args.enable_dota_check:
        steam_exe = find_steam_exe(args.steam_exe)
        log(f"Steam exe: {steam_exe}")
    if args.enable_dota_check and not args.enable_steam_login:
        log("Dota check requires Steam login; Dota check will be skipped because Steam login is disabled.")
    log(f"Security mailbox window should be logged in as: {args.security_email}")

    async with async_playwright() as playwright:
        outlook_browser = await playwright.chromium.launch(
            headless=False,
            args=[f"--window-position={args.outlook_x},{args.outlook_y}"],
        )
        outlook_context = await outlook_browser.new_context(
            viewport={"width": 1280, "height": 900},
        )
        outlook_page = await outlook_context.new_page()
        await show_window_instruction(
            outlook_page,
            "窗口1：目标 Outlook / Steam 验证码",
            [
                "这个窗口由程序自动退出当前 Microsoft 账号，然后登录飞书当前行的 Hotmail/Outlook 邮箱。",
                "登录成功后，程序会在这里收取 Steam Guard 邮件验证码。",
                "如果页面要求人工验证、绑定安全邮箱或改邮箱密码，请按 localhost 控制台提示操作。",
            ],
        )
        security_context = None
        security_page = None
        if args.security_code_source == "browser":
            security_context, security_page = await connect_security_context(playwright, args)
            await wait_for_security_mailbox(security_page, args.security_email, log, pause_controller, on_manual_wait, on_manual_resume)
        elif args.security_code_source == "graph":
            if args.graph_all_accounts:
                graph_accounts = graph_mail.load_accounts_config(Path(args.graph_accounts_config))
                log(f"Graph security mailboxes loaded: {len(graph_accounts)}")
            else:
                client_id = graph_mail.load_client_id(Path(args.graph_config), args.graph_client_id)
                me = graph_mail.get_me(client_id, Path(args.graph_cache))
                user_name = me.get("userPrincipalName") or me.get("mail") or ""
                log(f"Graph security mailbox confirmed: {user_name}")
        else:
            log("Security verification source: manual. Security mailbox window will not be opened.")

        for idx, account in enumerate(accounts, start=1):
            if pause_controller and pause_controller.should_stop():
                log("Stopped before next account.")
                break

            on_account_start(account, idx, len(accounts))
            log(f"[{idx}/{len(accounts)}] row {account.row_number}: steam={account.steam_username}, email={account.email}")
            try:
                await logout_outlook(outlook_page)
                await fill_login(outlook_page, account)
                dismiss_windows_security_passkey(log, timeout_seconds=5)
                if await security_challenge_visible(outlook_page):
                    if args.security_code_source == "manual":
                        await wait_for_manual_continue(
                            f"窗口1正在登录 {account.email}，页面需要安全验证。请你在窗口1手动完成验证，进入 Outlook 收件箱后回到 Web UI 点击“已完成人工操作 / 继续”。",
                            log,
                            pause_controller,
                            on_manual_wait,
                            on_manual_resume,
                        )
                    else:
                        await submit_security_email(outlook_page, args.security_email)
                    if args.security_code_source == "graph":
                        microsoft_code = get_microsoft_security_code(args, account)
                    elif args.security_code_source == "browser":
                        from outlook_batch_login import get_security_code

                        microsoft_code = await get_security_code(security_page, account.email, args.microsoft_code_timeout)
                    else:
                        microsoft_code = ""
                    if microsoft_code:
                        await submit_code(outlook_page, microsoft_code)
                await cancel_passkey_prompt(outlook_page, log)
                dismiss_windows_security_passkey(log, timeout_seconds=5)
                await ensure_mailbox_loaded(outlook_page)
                await wait_for_outlook_inbox(outlook_page, log)

                if args.enable_mail_maintenance:
                    await run_mail_security_maintenance(
                        outlook_page,
                        args,
                        account,
                        feishu_config,
                        spreadsheet_token,
                        sheet_id,
                        log,
                        pause_controller,
                        on_manual_wait,
                        on_manual_resume,
                    )
                    if args.enable_target_graph_login:
                        await run_graph_api_login(
                            args,
                            account.email,
                            log,
                            pause_controller,
                            on_manual_wait,
                            on_manual_resume,
                        )

                steam_id = ""
                if args.enable_steam_login:
                    shutdown_steam(steam_exe)
                    start_steam_login(steam_exe, account)
                    steam_code = await search_steam_guard_mail(outlook_page, account.steam_username, args.steam_code_timeout)
                    log(f"Steam Guard code found: {steam_code}")
                    if not send_guard_code_to_steam(steam_code, args.steam_ui_timeout):
                        raise TimeoutError("Could not find Steam Guard code input window.")

                    steam_id = wait_for_steam_id(steam_exe, account.steam_username, args.steam_login_timeout)
                    log(f"Steam login confirmed: {account.steam_username} -> {steam_id}")

                    feishu_tool.write_sheet(
                        feishu_config,
                        spreadsheet_token,
                        sheet_id,
                        f"{STEAM_ID_COL}{account.row_number}:{STEAM_ID_COL}{account.row_number}",
                        [[steam_id]],
                        use_uat=args.feishu_auth == "user",
                        package_root=Path(args.lark_mcp_root),
                    )
                    log(f"Wrote Steam ID to row {account.row_number}: {steam_id}")

                if args.enable_dota_check and args.enable_steam_login:
                    description = f"{account.steam_username}-{int(time.time())}"
                    checker = dota2_arcade.Dota2Automation(log=log, pause_controller=pause_controller, manual_wait=on_manual_wait, manual_resume=on_manual_resume)
                    if checker.run_check(steam_exe, description, max_attempts=args.dota_max_attempts):
                        feishu_tool.write_sheet(
                            feishu_config,
                            spreadsheet_token,
                            sheet_id,
                            f"{BIG_ACCOUNT_COL}{account.row_number}:{BIG_ACCOUNT_COL}{account.row_number}",
                            [["是"]],
                            use_uat=args.feishu_auth == "user",
                            package_root=Path(args.lark_mcp_root),
                        )
                        log(f"Wrote big-account flag to row {account.row_number}: 是")
                on_account_done(account, steam_id)
            except Exception as exc:
                screenshot = Path(args.error_dir) / f"row_{account.row_number}_{account.steam_username}.png"
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                try:
                    await outlook_page.screenshot(path=str(screenshot), full_page=True)
                    log(f"Screenshot saved: {screenshot}")
                except Exception:
                    pass
                log(f"ERROR on row {account.row_number}: {exc}")
                on_account_error(account, exc)
                if not args.continue_on_error:
                    break

            if args.pause_between:
                if pause_controller:
                    log("Waiting for web Continue button.")
                    if not pause_controller.wait():
                        log("Stopped while waiting.")
                        break
                else:
                    answer = input("Press Enter for next account, or type q then Enter to stop: ").strip().lower()
                    if answer == "q":
                        break

        await outlook_context.close()
        await outlook_browser.close()
        if security_context:
            await security_context.close()


async def run(args) -> None:
    await run_with_callbacks(args)


def parse_args():
    parser = argparse.ArgumentParser(description="Login Outlook/Steam accounts, fetch verification codes, and write results to Feishu sheet.")
    parser.add_argument("--sheet-url", default=DEFAULT_FEISHU_SHEET_URL)
    parser.add_argument("--sheet-id")
    parser.add_argument("--sheet-range", default="A1:I1000")
    parser.add_argument("--feishu-auth", choices=["user", "app"], default="user")
    parser.add_argument("--feishu-config", default=str(feishu_tool.DEFAULT_CONFIG))
    parser.add_argument("--feishu-start-script", default=str(feishu_tool.DEFAULT_START_SCRIPT))
    parser.add_argument("--lark-mcp-root", default=str(feishu_tool.DEFAULT_LARK_MCP_ROOT))
    parser.add_argument("--steam-exe", help="Path to steam.exe. Auto-detected by default.")
    parser.add_argument("--security-email", default=DEFAULT_SECURITY_EMAIL)
    parser.add_argument("--security-code-source", choices=["browser", "graph", "manual"], default="manual")
    parser.add_argument("--graph-client-id")
    parser.add_argument("--graph-config", default=str(graph_mail.DEFAULT_CONFIG))
    parser.add_argument("--graph-cache", default=str(graph_mail.DEFAULT_CACHE))
    parser.add_argument("--graph-accounts-config", default=str(graph_mail.DEFAULT_ACCOUNTS_CONFIG))
    parser.add_argument("--graph-all-accounts", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--outlook-profile", default=".playwright-steam-outlook-profile")
    parser.add_argument("--security-profile", default=".playwright-security-profile")
    parser.add_argument("--outlook-x", type=int, default=0)
    parser.add_argument("--outlook-y", type=int, default=0)
    parser.add_argument("--security-x", type=int, default=1280)
    parser.add_argument("--security-y", type=int, default=0)
    parser.add_argument("--microsoft-code-timeout", type=int, default=180)
    parser.add_argument("--steam-code-timeout", type=int, default=180)
    parser.add_argument("--steam-ui-timeout", type=int, default=90)
    parser.add_argument("--steam-login-timeout", type=int, default=180)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--start-row", type=int, help="Start from this sheet row and continue downward.")
    parser.add_argument("--target-email", default="", help="Only process this exact Outlook/Hotmail email address.")
    parser.add_argument("--list-pending", action="store_true")
    parser.add_argument("--pause-between", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--error-dir", default="errors")
    parser.add_argument("--enable-steam-login", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--enable-dota-check", action="store_true")
    parser.add_argument("--dota-max-attempts", type=int, default=3)
    parser.add_argument("--enable-mail-maintenance", action="store_true", help="Bind security email and change target email password after Outlook login.")
    parser.add_argument("--bind-security-email", default="", help="Security email to bind on the target Microsoft account. Empty skips binding prompt.")
    parser.add_argument("--new-email-password", default="", help="New target email password. Empty generates a strong password per account.")
    parser.add_argument("--enable-target-graph-login", action="store_true", help="After mail maintenance, run Microsoft Graph OAuth for the target mailbox and save it to graph_accounts.json.")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
