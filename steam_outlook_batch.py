import argparse
import asyncio
import hashlib
import json
import re
import secrets
import string
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

import feishu_tool
import dota2_arcade
from outlook_batch_login import (
    DEFAULT_FEISHU_SHEET_URL,
    DEFAULT_SECURITY_EMAIL,
    LOGIN_URL,
    LOGOUT_URL,
    OUTLOOK_MAIL_URL,
    cancel_microsoft_fido_bridge,
    click_first,
    fill_login,
    install_webauthn_cancel,
    security_challenge_visible,
    submit_security_email,
    submit_code,
    maybe_handle_stay_signed_in,
    prefer_password_sign_in,
    read_open_outlook_message_texts,
    cell_text,
)


STEAM_ID_COL = "I"
BIG_ACCOUNT_COL = "G"
EMAIL_PASSWORD_COL = "D"
REMARK_HEADER = "备注"
BACKUP_SECURITY_EMAIL_COL = "J"
BACKUP_SECURITY_EMAIL_HEADER = "备用安全邮箱"
FORWARD_RULE_COL = "K"
FORWARD_RULE_HEADER = "转发规则"
OUTLOOK_SESSION_ROOT = Path(".playwright-outlook-sessions")
INVALID_STEAM_GUARD_CODES = {
    "ABOUT",
    "CHROME",
    "CLICK",
    "CODE",
    "EMAIL",
    "EMPTY",
    "FOUND",
    "GUARD",
    "HTTPS",
    "LOGIN",
    "STORE",
    "POWER",
    "SHARE",
    "SUPPO",
    "OUTLO",
    "STEAM",
    "TODAY",
    "WHERE",
    "YOUR",
}


def text_snippet(text: str, limit: int = 220) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def safe_artifact_name(text: str, limit: int = 80) -> str:
    compact = re.sub(r"[^A-Za-z0-9_.-]+", "_", text or "").strip("._")
    return (compact or "page")[:limit]


def disable_password_manager_profile(profile: Path, log=print) -> None:
    """Disable Chromium's native save-password UI before a persistent launch."""
    profile.mkdir(parents=True, exist_ok=True)
    preference_paths = [profile / "Default" / "Preferences", profile / "Preferences"]
    for preference_path in preference_paths:
        try:
            preference_path.parent.mkdir(parents=True, exist_ok=True)
            if preference_path.exists():
                preferences = json.loads(preference_path.read_text(encoding="utf-8"))
            else:
                preferences = {}
            preferences["credentials_enable_service"] = False
            preferences["password_manager_enabled"] = False
            preferences.setdefault("profile", {})["password_manager_enabled"] = False
            preferences.setdefault("autofill", {})["profile_enabled"] = False
            temporary_path = preference_path.with_suffix(".codex.tmp")
            temporary_path.write_text(
                json.dumps(preferences, ensure_ascii=False),
                encoding="utf-8",
            )
            temporary_path.replace(preference_path)
        except Exception as exc:
            log(f"Could not disable native password-save preferences for {profile}: {exc}")


async def dismiss_password_save_prompt(page: Page, log=print) -> bool:
    """Close page-visible password-save prompts; native bubbles are disabled at launch."""
    try:
        body = await page.locator("body").inner_text(timeout=1000)
    except Exception:
        body = ""
    normalized = body.casefold()
    if not any(marker in normalized for marker in ["save password", "保存密码", "保存登录信息"]):
        return False
    for pattern in [r"^Never$", r"^No thanks$", r"^Not now$", r"^不保存$", r"^关闭$", r"^取消$"]:
        if await click_first(page, [pattern], timeout_ms=800):
            log("Closed browser save-password prompt.")
            return True
    try:
        await page.keyboard.press("Escape")
        log("Sent Escape to close browser save-password prompt.")
        return True
    except Exception:
        return False


async def wait_for_timeout_with_stop(
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


MICROSOFT_BLOCKING_PAGE_MARKERS = [
    (
        "密码错误或需要重新输入密码",
        [
            "your account or password is incorrect",
            "that microsoft account doesn't exist",
            "incorrect password",
            "密码不正确",
            "帐户或密码不正确",
            "账户或密码不正确",
            "该 microsoft 帐户不存在",
            "该 microsoft 账户不存在",
        ],
    ),
    (
        "Microsoft 账号选择器或需要切换账号",
        [
            "choose an account",
            "pick an account",
            "use another account",
            "sign in with a different account",
            "signed in with a different account",
            "选择帐户",
            "选择账户",
            "使用其他帐户",
            "使用其他账户",
            "切换帐户",
            "切换账户",
        ],
    ),
    (
        "账号被锁定、暂停或检测到异常活动",
        [
            "account has been locked",
            "account is temporarily locked",
            "temporarily locked",
            "temporarily suspended",
            "unusual activity",
            "unusual sign-in",
            "unusual sign in",
            "suspicious activity",
            "help us secure your account",
            "recover your account",
            "account recovery",
            "recover your microsoft account",
            "we detected something unusual",
            "protect your account",
            "帐户已被锁定",
            "账户已被锁定",
            "帐户暂时锁定",
            "账户暂时锁定",
            "恢复你的帐户",
            "恢复你的账户",
            "帐户恢复",
            "账户恢复",
            "暂时被阻止",
            "异常活动",
            "异常登录",
            "异常登陆",
            "可疑活动",
            "帮助我们保护你的帐户",
            "帮助我们保护你的账户",
            "保护你的帐户",
            "保护你的账户",
        ],
    ),
    (
        "需要 Microsoft Authenticator、passkey、Windows Hello 或二维码确认",
        [
            "microsoft authenticator",
            "approve sign in request",
            "open your authenticator app",
            "use a passkey",
            "windows hello",
            "scan the qr code",
            "authenticator 应用",
            "身份验证器",
            "通行密钥",
            "使用密钥",
            "扫描 qr",
            "扫描二维码",
        ],
    ),
    (
        "验证码发送方式不可用或需要选择其他验证方式",
        [
            "usage limit exceeded",
            "maximum number of codes",
            "we can't send a code",
            "can't send a code",
            "couldn't send a code",
            "we won't send codes",
            "won't send codes",
            "temporarily block where we won't send codes",
            "can't use this verification option right now",
            "cannot use this verification option right now",
            "try another verification method",
            "choose a different verification option",
            "use a different verification option",
            "无法发送验证码",
            "无法发送代码",
            "暂时无法发送",
            "使用次数已达上限",
            "验证码次数已达上限",
            "不能使用此验证选项",
            "无法使用此验证选项",
            "其他登录方法",
            "选择其他验证方式",
            "换一种验证方式",
        ],
    ),
    (
        "需要填写 Microsoft 验证码",
        [
            "enter code",
            "enter the code",
            "security code",
            "verification code",
            "输入代码",
            "输入验证码",
            "安全代码",
            "验证码",
        ],
    ),
    (
        "需要完成人机验证",
        [
            "captcha",
            "enter the characters you see",
            "prove you're not a robot",
            "证明你不是机器人",
            "输入你看到的字符",
            "人机验证",
        ],
    ),
    (
        "需要补充或确认安全信息",
        [
            "add security info",
            "add a new way to sign in",
            "manage how i sign in",
            "protect your account",
            "help us protect",
            "添加安全信息",
            "添加新的登录或验证方式",
            "管理我的登录方式",
            "保护你的帐户",
            "保护你的账户",
        ],
    ),
    (
        "安全信息变更等待期或当前无法访问",
        [
            "security info change is still pending",
            "your security info change is still pending",
            "you can't access this site right now",
            "you cannot access this site right now",
            "we'll send notifications for 30 days",
            "30 days",
            "安全信息更改仍在等待",
            "安全信息变更仍在等待",
            "当前无法访问此网站",
            "现在无法访问此网站",
            "30 天",
            "30天",
        ],
    ),
    (
        "请求过多或 Microsoft 服务暂时不可用",
        [
            "too many requests",
            "too many attempts",
            "usage limit exceeded",
            "maximum number of codes",
            "temporarily unavailable",
            "service is temporarily unavailable",
            "try again later",
            "something went wrong",
            "请求次数过多",
            "尝试次数过多",
            "使用次数已达上限",
            "验证码次数已达上限",
            "暂时不可用",
            "服务暂时不可用",
            "请稍后再试",
            "出现问题",
        ],
    ),
]


def classify_microsoft_page(url: str, body: str) -> tuple[str, str] | None:
    url = url or ""
    target = f"{url}\n{body}".casefold()
    if not any(host in url.casefold() for host in ["login.live.com", "account.live.com", "account.microsoft.com"]):
        return None
    for label, markers in MICROSOFT_BLOCKING_PAGE_MARKERS:
        if any(marker.casefold() in target for marker in markers):
            return label, text_snippet(body)
    return None


OUTLOOK_BLOCKING_PAGE_MARKERS = [
    (
        "Outlook 需要添加 Outlook/Hotmail/Live 第一方别名后才能使用邮箱",
        [
            "asked to add a 1st party microsoft alias",
            "add a 1st party microsoft alias",
            "add an outlook.com alias",
            "add a new email address",
            "create an outlook.com email address",
            "outlook.com and outlook apps require a 1st party alias",
            "添加 outlook.com 别名",
            "添加 outlook 别名",
            "添加新的电子邮件地址",
            "创建 outlook.com 电子邮件地址",
            "添加 microsoft 别名",
        ],
    ),
    (
        "Outlook 账号选择器或需要切换账号",
        [
            "choose an account",
            "pick an account",
            "use another account",
            "signed in to outlook with a different account",
            "选择帐户",
            "选择账户",
            "使用其他帐户",
            "使用其他账户",
            "切换帐户",
            "切换账户",
        ],
    ),
    (
        "Outlook 邮箱暂时无法加载或服务不可用",
        [
            "something went wrong",
            "please try again later",
            "couldn't load your mailbox",
            "can't load your mailbox",
            "service is unavailable",
            "too many requests",
            "temporarily unavailable",
            "we're having trouble opening your mailbox",
            "出现问题",
            "请稍后再试",
            "无法加载邮箱",
            "无法打开邮箱",
            "服务不可用",
            "请求次数过多",
            "暂时不可用",
        ],
    ),
    (
        "Outlook 存储空间已满或邮箱受限",
        [
            "storage is full",
            "mailbox is full",
            "quota exceeded",
            "not enough storage",
            "your mailbox is almost full",
            "存储空间已满",
            "邮箱已满",
            "配额已超过",
            "空间不足",
        ],
    ),
    (
        "Outlook 需要刷新、重新加载或网络恢复",
        [
            "refresh the page",
            "reload",
            "try refreshing",
            "working offline",
            "no internet connection",
            "刷新页面",
            "重新加载",
            "脱机工作",
            "没有 internet 连接",
            "网络连接",
        ],
    ),
]


def classify_outlook_page(url: str, body: str) -> tuple[str, str] | None:
    url = url or ""
    target = f"{url}\n{body}".casefold()
    if not any(host in url.casefold() for host in ["outlook.live.com", "outlook.office.com"]):
        return None
    for label, markers in OUTLOOK_BLOCKING_PAGE_MARKERS:
        if any(marker.casefold() in target for marker in markers):
            return label, text_snippet(body)
    return None


@dataclass
class SteamAccount:
    row_number: int
    steam_username: str
    steam_password: str
    email: str
    email_password: str
    steam_id: str
    backup_security_email: str = ""
    security_email_password: str = ""
    security_binding_required: bool = False
    external_target: bool = False

    @property
    def password(self) -> str:
        # Compatibility with outlook_batch_login.fill_login().
        return self.email_password


def parse_security_mailbox_pool(raw: str) -> list[dict[str, str]]:
    pool = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "," in line:
            email, password = line.split(",", 1)
        elif "----" in line:
            email, password = line.split("----", 1)
        elif "\t" in line:
            email, password = line.split("\t", 1)
        else:
            email, password = line, ""
        email = email.strip()
        if email:
            pool.append({"email": email, "password": password.strip()})
    return pool


SECURITY_EMAIL_DELIVERY_FAILURE_MARKERS = [
    "we can't send a code",
    "can't send a code",
    "couldn't send a code",
    "we couldn't send a code",
    "could not send a code",
    "failed to send a code",
    "send code failed",
    "we can't send a verification code",
    "can't send a verification code",
    "couldn't send a verification code",
    "we couldn't send a verification code",
    "could not send a verification code",
    "failed to send a verification code",
    "there was a problem sending",
    "we're having trouble sending",
    "we won't send codes",
    "won't send codes",
    "usage limit exceeded",
    "maximum number of codes",
    "temporarily block where we won't send codes",
    "can't use this verification option right now",
    "cannot use this verification option right now",
    "try again later",
    "too many requests",
    "too many attempts",
    "temporarily blocked",
    "temporarily unavailable",
    "something went wrong",
    "that didn't work",
    "use a different verification option",
    "choose a different verification option",
    "无法发送验证码",
    "无法发送代码",
    "无法发送安全代码",
    "发送验证码失败",
    "发送代码失败",
    "发送安全代码失败",
    "发送失败",
    "未能发送验证码",
    "未能发送代码",
    "未能发送安全代码",
    "暂时无法发送",
    "不能使用此验证选项",
    "无法使用此验证选项",
    "请稍后再试",
    "请求次数过多",
    "发送次数过多",
    "使用次数已达上限",
    "验证码次数已达上限",
    "暂时被阻止",
    "出现问题",
    "验证失败",
    "受限",
    "换一种验证方式",
    "选择其他验证方式",
]


MICROSOFT_CODE_REJECTED_MARKERS = [
    "that code didn't work",
    "code didn't work",
    "code is incorrect",
    "incorrect code",
    "wrong code",
    "code has expired",
    "expired code",
    "try again with a new code",
    "验证码不正确",
    "验证码错误",
    "代码不正确",
    "代码错误",
    "安全代码不正确",
    "安全代码错误",
    "验证码已过期",
    "代码已过期",
    "重新获取代码",
]


def normalize_email_set(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, str):
        chunks = re.split(r"[\s,;]+", value)
    else:
        chunks = [str(item) for item in value]
    return {chunk.strip().casefold() for chunk in chunks if chunk and chunk.strip()}


def normalize_task_args(args) -> None:
    task_mode = getattr(args, "task_mode", "full")
    security_pool = (getattr(args, "security_mailbox_pool", "") or "").strip()
    bind_security_email = (getattr(args, "bind_security_email", "") or "").strip()
    if security_pool:
        setattr(args, "bind_security_email", "")
    elif task_mode in {"full", "mail_password", "forward_rule"} and not bind_security_email:
        setattr(args, "bind_security_email", (getattr(args, "security_email", "") or DEFAULT_SECURITY_EMAIL).strip())
    if task_mode == "full":
        setattr(args, "enable_mail_maintenance", True)
    elif task_mode == "mail_password":
        setattr(args, "enable_mail_maintenance", True)
        setattr(args, "enable_steam_login", False)
        setattr(args, "enable_dota_check", False)
    elif task_mode == "forward_rule":
        setattr(args, "enable_mail_maintenance", True)
        setattr(args, "enable_steam_login", False)
        setattr(args, "enable_dota_check", False)


async def detect_security_email_delivery_failure(page: Page) -> str:
    try:
        body = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        body = ""
    lower = body.casefold()
    if any(marker.casefold() in lower for marker in SECURITY_EMAIL_DELIVERY_FAILURE_MARKERS):
        return text_snippet(body)
    return ""


async def detect_microsoft_code_rejected(page: Page) -> str:
    try:
        body = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        body = ""
    lower = body.casefold()
    if any(marker.casefold() in lower for marker in MICROSOFT_CODE_REJECTED_MARKERS):
        return text_snippet(body)
    return ""


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
    remark_indices = [
        index
        for index, name in enumerate(header)
        if "备注" in name.strip() or "remark" in name.strip().casefold()
    ]
    forward_rule_idx = header.index(FORWARD_RULE_HEADER) if FORWARD_RULE_HEADER in header else None
    task_mode = getattr(args, "task_mode", "full")
    backup_security_email_idx = header.index(BACKUP_SECURITY_EMAIL_HEADER) if BACKUP_SECURITY_EMAIL_HEADER in header else None
    password_indices = [idx for idx, name in enumerate(header) if name == "密码"]
    if len(password_indices) < 2:
        raise SystemExit('Feishu sheet must contain two "密码" columns: Steam password and email password.')

    steam_password_idx = password_indices[0]
    email_password_idx = password_indices[1]

    accounts: list[SteamAccount] = []
    skipped_with_remark = 0
    skipped_remark_rows: list[int] = []
    skipped_missing_security_email = 0
    skipped_missing_required_fields = 0
    skipped_security_bind_limit = 0
    needs_security_binding = task_mode in {"full", "mail_password", "forward_rule"}
    bind_email = (getattr(args, "bind_security_email", "") or "").strip().casefold()
    bind_limit = int(getattr(args, "security_email_bind_limit", 10) or 10)
    blocked_security_mailboxes = normalize_email_set(getattr(args, "blocked_security_mailboxes", None))
    raw_security_pool = parse_security_mailbox_pool(getattr(args, "security_mailbox_pool", "") or "")
    skipped_blocked_security_mailbox_pool = sum(
        1 for item in raw_security_pool if item["email"].casefold() in blocked_security_mailboxes
    )
    security_pool = [
        item for item in raw_security_pool if item["email"].casefold() not in blocked_security_mailboxes
    ]
    security_pool_by_email = {item["email"].casefold(): item for item in security_pool}
    security_pool_counts = {item["email"].casefold(): 0 for item in security_pool}
    security_pool_full_emails: set[str] = set()
    existing_bind_count = 0
    selected_new_bind_count = 0
    if backup_security_email_idx is not None:
        for row in rows[1:]:
            padded = list(row) + [""] * (len(header) - len(row))
            backup_security_email = (
                cell_text(padded[backup_security_email_idx])
                if backup_security_email_idx < len(padded)
                else ""
            )
            if backup_security_email.casefold() == bind_email:
                existing_bind_count += 1
            if backup_security_email.casefold() in security_pool_counts:
                security_pool_counts[backup_security_email.casefold()] += 1
    for item in security_pool:
        key = item["email"].casefold()
        if security_pool_counts.get(key, 0) >= bind_limit:
            security_pool_full_emails.add(key)
    start_row = getattr(args, "start_row", None)
    start_row_number = int(start_row) if start_row else None
    target_email = (getattr(args, "target_email", "") or "").strip().casefold()
    for row_number, row in enumerate(rows[1:], start=2):
        padded = list(row) + [""] * (len(header) - len(row))
        remarks = [
            cell_text(padded[index])
            for index in remark_indices
            if index < len(padded) and cell_text(padded[index])
        ]
        remark = " | ".join(remarks)
        steam_id = cell_text(padded[steam_id_idx])
        steam_username = cell_text(padded[steam_username_idx])
        steam_password = cell_text(padded[steam_password_idx])
        email = cell_text(padded[email_idx])
        email_password = cell_text(padded[email_password_idx])
        forward_rule_status = cell_text(padded[forward_rule_idx]) if forward_rule_idx is not None and forward_rule_idx < len(padded) else ""
        backup_security_email = (
            cell_text(padded[backup_security_email_idx])
            if backup_security_email_idx is not None and backup_security_email_idx < len(padded)
            else ""
        )
        # A previously written J value can become unusable later when
        # Microsoft rate-limits that security mailbox. Treat it as unbound so
        # the next refresh assigns a healthy mailbox from the pool and the row
        # re-verifies the replacement before continuing.
        if backup_security_email.casefold() in blocked_security_mailboxes:
            backup_security_email = ""
        if not email:
            continue
        if start_row_number and row_number < start_row_number:
            continue
        if target_email and email.casefold() != target_email:
            continue
        if remark:
            skipped_with_remark += 1
            skipped_remark_rows.append(row_number)
            continue
        if task_mode == "full":
            if steam_id or not steam_username or not steam_password or not email_password:
                if not steam_id and (not steam_username or not steam_password or not email_password):
                    skipped_missing_required_fields += 1
                continue
        elif task_mode == "mail_password":
            if not email_password:
                skipped_missing_required_fields += 1
                continue
        elif task_mode == "forward_rule":
            # K is the durable completion checkpoint.  Older rows may contain only
            # the forwarding address (often as a Feishu rich-link cell), while new
            # rows contain a descriptive "已设置 ..." status.  Any non-empty K
            # therefore means the row was already completed and must not be run
            # again.
            if not steam_id or not email_password or bool(forward_rule_status):
                if not steam_id or not email_password:
                    skipped_missing_required_fields += 1
                continue
        else:
            raise SystemExit(f"Unknown task mode: {task_mode}")

        security_binding_required = False
        if needs_security_binding:
            if security_pool and not backup_security_email:
                assigned = None
                for item in security_pool:
                    key = item["email"].casefold()
                    if security_pool_counts.get(key, 0) < bind_limit:
                        assigned = item
                        security_pool_counts[key] = security_pool_counts.get(key, 0) + 1
                        if security_pool_counts[key] >= bind_limit:
                            security_pool_full_emails.add(key)
                        break
                if not assigned:
                    skipped_security_bind_limit += 1
                    continue
                backup_security_email = assigned["email"]
                security_binding_required = True
            elif bind_email and backup_security_email.casefold() != bind_email:
                if existing_bind_count + selected_new_bind_count >= bind_limit:
                    skipped_security_bind_limit += 1
                    continue
                selected_new_bind_count += 1
                backup_security_email = bind_email
                security_binding_required = True
        accounts.append(
            SteamAccount(
                row_number=row_number,
                steam_username=steam_username,
                steam_password=steam_password,
                email=email,
                email_password=email_password,
                steam_id=steam_id,
                backup_security_email=backup_security_email,
                security_email_password=(security_pool_by_email.get(backup_security_email.casefold()) or {}).get("password", ""),
                security_binding_required=security_binding_required,
            )
        )

    setattr(args, "skipped_with_remark", skipped_with_remark)
    setattr(args, "skipped_remark_rows", skipped_remark_rows)
    setattr(args, "skipped_missing_security_email", skipped_missing_security_email)
    setattr(args, "skipped_missing_required_fields", skipped_missing_required_fields)
    setattr(args, "skipped_security_bind_limit", skipped_security_bind_limit)
    setattr(args, "skipped_blocked_security_mailbox_pool", skipped_blocked_security_mailbox_pool)
    setattr(args, "existing_security_bind_count", existing_bind_count)
    setattr(args, "security_pool_full_emails", sorted(security_pool_full_emails))
    setattr(args, "security_pool_available_slots", sum(max(0, bind_limit - count) for count in security_pool_counts.values()))
    setattr(
        args,
        "security_pool_usage",
        [
            {"email": item["email"], "count": security_pool_counts[item["email"].casefold()], "limit": bind_limit}
            for item in security_pool
        ],
    )
    return accounts, config, spreadsheet_token, sheet_id


def write_sheet_verified(
    config: dict[str, Any],
    spreadsheet_token: str,
    sheet_id: str,
    cell_range: str,
    values: list[list[Any]],
    use_uat: bool = False,
    package_root: Path = feishu_tool.DEFAULT_LARK_MCP_ROOT,
) -> dict[str, Any]:
    """Write a cell range and read it back before the workflow treats it as committed."""
    expected_normalized = [[cell_text(cell) for cell in row] for row in values]
    last_error = ""
    for attempt in range(1, 4):
        response = feishu_tool.write_sheet(
            config,
            spreadsheet_token,
            sheet_id,
            cell_range,
            values,
            use_uat=use_uat,
            package_root=package_root,
        )
        if response.get("code") != 0:
            last_error = f"Feishu write failed for {cell_range}: {response}"
        else:
            readback = feishu_tool.read_sheet(
                config,
                spreadsheet_token,
                sheet_id,
                cell_range,
                use_uat=use_uat,
                package_root=package_root,
            )
            if readback.get("code") != 0:
                last_error = f"Feishu readback failed for {cell_range}: {readback}"
            else:
                actual = readback.get("data", {}).get("valueRange", {}).get("values", [])
                actual_normalized = [
                    [cell_text(cell) for cell in row[: len(expected_normalized[index])]]
                    for index, row in enumerate(actual[: len(expected_normalized)])
                ]
                if actual_normalized == expected_normalized:
                    return response
                last_error = (
                    f"Feishu write verification failed for {cell_range}: "
                    f"expected={expected_normalized!r}, actual={actual_normalized!r}"
                )
        if attempt < 3:
            time.sleep(attempt)
    raise RuntimeError(last_error or f"Feishu write verification failed for {cell_range}.")


def ensure_forward_rule_header(args, feishu_config: dict[str, Any], spreadsheet_token: str, sheet_id: str) -> None:
    write_sheet_verified(
        feishu_config,
        spreadsheet_token,
        sheet_id,
        f"{FORWARD_RULE_COL}1:{FORWARD_RULE_COL}1",
        [[FORWARD_RULE_HEADER]],
        use_uat=args.feishu_auth == "user",
        package_root=Path(args.lark_mcp_root),
    )


def ensure_backup_security_email_header(args, feishu_config: dict[str, Any], spreadsheet_token: str, sheet_id: str) -> None:
    write_sheet_verified(
        feishu_config,
        spreadsheet_token,
        sheet_id,
        f"{BACKUP_SECURITY_EMAIL_COL}1:{BACKUP_SECURITY_EMAIL_COL}1",
        [[BACKUP_SECURITY_EMAIL_HEADER]],
        use_uat=args.feishu_auth == "user",
        package_root=Path(args.lark_mcp_root),
    )


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
            timeout=5,
        ).strip()
        if output:
            candidates.append(Path(output))
    except (subprocess.SubprocessError, subprocess.TimeoutExpired):
        pass

    candidates.extend([Path("D:/Steam/steam.exe"), Path("C:/Program Files (x86)/Steam/steam.exe")])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit("Steam exe not found. Pass --steam-exe D:\\Steam\\steam.exe")


def steam_process_running(log=print) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        log("Steam process check timed out; assuming Steam is still running.")
        return True
    except Exception as exc:
        log(f"Steam process check failed: {exc}; assuming Steam is still running.")
        return True
    output = (result.stdout or "") + (result.stderr or "")
    return "steam.exe" in output.casefold()


def shutdown_steam(steam_exe: Path, timeout_seconds: int = 30, log=print, pause_controller=None) -> None:
    if not steam_process_running(log):
        log("Steam is not running; skipping shutdown.")
        return
    log(f"Requesting Steam shutdown: {steam_exe}")
    try:
        subprocess.run(
            [str(steam_exe), "-shutdown"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except subprocess.TimeoutExpired:
        log("Steam -shutdown command timed out; continuing with process wait/kill.")
    except Exception as exc:
        log(f"Steam -shutdown command failed: {exc}; continuing with process wait/kill.")

    deadline = time.monotonic() + timeout_seconds
    next_log = 0.0
    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError("Stopped while shutting down Steam.")
        if not steam_process_running(log):
            log("Steam process exited.")
            return
        now = time.monotonic()
        if now >= next_log:
            remaining = max(0, int(deadline - now))
            log(f"Waiting for Steam to exit... {remaining}s before force kill.")
            next_log = now + 5
        time.sleep(1)

    log("Steam did not exit in time; force killing steam.exe.")
    if pause_controller and pause_controller.should_stop():
        raise RuntimeError("Stopped before force killing Steam.")
    try:
        subprocess.run(
            ["taskkill", "/IM", "steam.exe", "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        log("taskkill steam.exe timed out.")
    except Exception as exc:
        log(f"taskkill steam.exe failed: {exc}")
    time.sleep(2)
    if steam_process_running(log):
        raise TimeoutError("Steam process is still running after force kill.")
    log("Steam force kill completed.")


def start_steam_login(steam_exe: Path, account: SteamAccount, log=print) -> None:
    try:
        process = subprocess.Popen(
            [str(steam_exe)],
            cwd=str(steam_exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log(f"Failed to start Steam login process: {exc}")
        raise
    log(
        f"Steam client started for UI login: pid={process.pid}, account={account.steam_username}. "
        "Credentials are not placed in process command-line arguments."
    )


def run_steam_helper(payload: dict[str, Any], timeout_seconds: int, log=print, pause_controller=None) -> None:
    helper = Path(__file__).with_name("steam_login_helper.py")
    if not helper.exists():
        raise RuntimeError(f"Steam helper not found: {helper}")
    mode = payload.get("mode") or "unknown"
    log(f"Steam helper starting: mode={mode}, timeout={timeout_seconds}s")
    process = subprocess.Popen(
        [sys.executable, str(helper)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None

    output_lines: list[str] = []

    def forward_output() -> None:
        for line in process.stdout:
            line = line.rstrip()
            if line:
                output_lines.append(line)
                log(line)

    output_thread = threading.Thread(target=forward_output, daemon=True)
    output_thread.start()
    try:
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload, ensure_ascii=False))
        process.stdin.close()
    except Exception:
        process.kill()
        raise

    deadline = time.monotonic() + timeout_seconds
    return_code = None
    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            last_output = output_lines[-1] if output_lines else "no helper output"
            raise RuntimeError(f"Stopped Steam helper in mode={mode}; last output: {last_output}")
        return_code = process.poll()
        if return_code is not None:
            break
        time.sleep(0.25)
    if return_code is None:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        last_output = output_lines[-1] if output_lines else "no helper output"
        raise TimeoutError(f"Steam helper timed out after {timeout_seconds}s in mode={mode}; last output: {last_output}")
    output_thread.join(timeout=2)
    log(f"Steam helper exited: mode={mode}, code={return_code}")
    if return_code != 0:
        last_output = output_lines[-1] if output_lines else "no helper output"
        raise RuntimeError(f"Steam helper failed with exit code {return_code} in mode={mode}; last output: {last_output}")


def ensure_steam_login_submitted(
    account: SteamAccount,
    log=print,
    timeout_seconds: int = 60,
    pause_controller=None,
    screenshot_path: Path | None = None,
) -> None:
    run_steam_helper(
        {
            "mode": "login",
            "username": account.steam_username,
            "password": account.steam_password,
            "timeout_seconds": timeout_seconds,
            "screenshot_path": str(screenshot_path or ""),
        },
        timeout_seconds + 10,
        log,
        pause_controller,
    )


def ensure_steam_guard_prompt_visible(
    steam_username: str,
    log=print,
    timeout_seconds: int = 45,
    pause_controller=None,
    screenshot_path: Path | None = None,
) -> None:
    run_steam_helper(
        {
            "mode": "wait_guard",
            "username": steam_username,
            "timeout_seconds": timeout_seconds,
            "screenshot_path": str(screenshot_path or ""),
        },
        timeout_seconds + 10,
        log,
        pause_controller,
    )


def send_guard_code_to_steam(
    code: str,
    timeout_seconds: int,
    log=print,
    pause_controller=None,
    steam_username: str = "",
    screenshot_path: Path | None = None,
) -> bool:
    normalized_code = (code or "").strip().upper()
    if not is_valid_steam_guard_code(normalized_code):
        log(f"Steam Guard helper skipped invalid code: {code!r}")
        return False
    try:
        run_steam_helper(
            {
                "mode": "guard",
                "code": normalized_code,
                "username": steam_username,
                "timeout_seconds": timeout_seconds,
                "screenshot_path": str(screenshot_path or ""),
            },
            timeout_seconds + 10,
            log,
            pause_controller,
        )
        return True
    except Exception as exc:
        log(f"Steam Guard helper failed: {exc}")
        return False


def is_valid_steam_guard_code(code: str) -> bool:
    normalized_code = (code or "").strip().upper()
    return bool(re.fullmatch(r"[A-Z0-9]{5}", normalized_code)) and normalized_code not in INVALID_STEAM_GUARD_CODES


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


def wait_for_steam_id(steam_exe: Path, steam_username: str, timeout_seconds: int, log=print, pause_controller=None) -> str:
    loginusers_path = steam_exe.parent / "config" / "loginusers.vdf"
    deadline = time.monotonic() + timeout_seconds
    target = steam_username.casefold()
    next_log = 0.0
    last_users: list[dict[str, str]] = []
    last_recent = ""
    log(f"Waiting for Steam ID in {loginusers_path}; target account={steam_username}")
    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError(f"Stopped while waiting for Steam ID for {steam_username}.")
        users = parse_loginusers(loginusers_path)
        last_users = users
        for user in users:
            if user["account_name"].casefold() == target and user["most_recent"] == "1":
                return user["steam_id"]
        recent = [user for user in users if user["most_recent"] == "1" and user["steam_id"]]
        if recent:
            last_recent = f"{recent[0]['account_name']} {recent[0]['steam_id']}"
        now = time.monotonic()
        if now >= next_log:
            if recent:
                log(
                    f"Most recent Steam account in loginusers.vdf: {last_recent}; "
                    f"target={steam_username}; parsed accounts={len(users)}"
                )
            else:
                account_names = ", ".join(user["account_name"] or user["steam_id"] for user in users[:5]) or "none"
                log(f"Waiting for loginusers.vdf to mark {steam_username} as most recent; parsed accounts={account_names}.")
            next_log = now + 10
        sleep_deadline = min(time.monotonic() + 3, deadline)
        while time.monotonic() < sleep_deadline:
            if pause_controller and pause_controller.should_stop():
                raise RuntimeError(f"Stopped while waiting for Steam ID for {steam_username}.")
            time.sleep(min(0.5, max(0.0, sleep_deadline - time.monotonic())))
    parsed_accounts = ", ".join(
        f"{user['account_name'] or '?'}:{user['steam_id']}:{user['most_recent'] or '0'}" for user in last_users[:8]
    ) or "none"
    raise TimeoutError(
        f"Could not confirm Steam ID for {steam_username} from {loginusers_path}; "
        f"last_recent={last_recent or 'none'}; parsed_accounts={parsed_accounts}"
    )


def extract_steam_guard_code(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text).upper()
    if "STEAM" not in normalized or ("GUARD" not in normalized and "令牌" not in text and "验证码" not in text):
        return None
    patterns = [
        r"HERE'?S\s+THE\s+STEAM\s+GUARD\s+CODE\s+YOU\s+NEED\s+TO\s+LOGIN.{0,200}?\b([A-Z0-9]{5})\b",
        r"STEAM\s+GUARD\s+CODE\s+YOU\s+NEED\s+TO\s+LOGIN.{0,200}?\b([A-Z0-9]{5})\b",
        r"STEAM\s+GUARD\s+CODE.{0,80}?\b(?:LOGIN|SIGN\s*IN|登录|登入).{0,120}?\b([A-Z0-9]{5})\b",
        r"STEAM\s+GUARD.{0,80}?\b(?:CODE|验证码|令牌).{0,120}?\b([A-Z0-9]{5})\b",
        r"STEAM.{0,40}?(?:GUARD|令牌|验证码).{0,80}?(?:登录|登入|LOGIN|SIGN\s*IN).{0,120}?\b([A-Z0-9]{5})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, re.I)
        if match and is_valid_steam_guard_code(match.group(1)):
            return match.group(1)
    return None


def outlook_rules_surface_detected(url: str, body: str) -> bool:
    """Return True only when the current page is the actual Rules/Forwarding surface."""
    url_lower = (url or "").casefold()
    body_lower = (body or "").casefold()
    if "outlook.live.com" not in url_lower:
        return False
    settings_url = any(
        marker in url_lower
        for marker in [
            "/options/mail/rules",
            "/options/mail/forwarding",
            "/mail/options/mail/rules",
            "/mail/options/mail/forwarding",
        ]
    )
    page_marker = any(
        marker in body_lower
        for marker in [
            "rules",
            "forwarding",
            "enable forwarding",
            "add new rule",
            "redirect to",
            "forward to",
            "规则",
            "转发",
            "启用转发",
            "添加新规则",
            "新建规则",
            "重定向到",
        ]
    )
    strong_marker = any(
        marker in body_lower
        for marker in [
            "mail rules",
            "inbox rules",
            "rules settings",
            "forwarding settings",
            "邮件规则",
            "收件箱规则",
            "规则设置",
            "转发设置",
        ]
    )
    return page_marker and (settings_url or strong_marker)


async def open_latest_steam_guard_mail(page: Page) -> bool:
    """Open the newest real Steam Guard message row, preferring unread mail."""
    rows = page.locator("[role=option], [data-convid], [data-itemid], [role=listitem]").filter(
        has_text=re.compile(r"Steam\s*Guard|steampowered\.com|Steam.{0,80}(?:code|验证码|令牌)", re.I)
    )
    candidates: list[tuple[bool, Any]] = []
    try:
        for index in range(min(await rows.count(), 20)):
            row = rows.nth(index)
            try:
                text = " ".join(
                    [
                        await row.inner_text(timeout=1000),
                        (await row.get_attribute("aria-label")) or "",
                        (await row.get_attribute("title")) or "",
                    ]
                )
                candidates.append((bool(re.search(r"\bunread\b|未读", text, re.I)), row))
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


async def extract_steam_guard_code_from_open_message(page: Page) -> str | None:
    for text in await read_open_outlook_message_texts(page):
        code = extract_steam_guard_code(text)
        if code:
            return code
    return None


async def search_steam_guard_mail(
    page: Page,
    steam_username: str,
    timeout_seconds: int,
    log=print,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    queries = ["isread:no from:steampowered.com", "from:steampowered.com"]
    next_log = 0.0
    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError("Stopped while waiting for Steam Guard email.")
        now = time.monotonic()
        if now >= next_log:
            log(f"Waiting for Steam Guard email in Outlook for {steam_username}.")
            next_log = now + 10
        await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
        await wait_for_timeout_with_stop(page, 1500, pause_controller, "Steam Guard latest-mail load")
        if await open_latest_steam_guard_mail(page):
            await wait_for_timeout_with_stop(page, 1200, pause_controller, "Steam Guard latest message open")
            code = await extract_steam_guard_code_from_open_message(page)
            if code:
                log(f"Steam Guard code extracted from latest Outlook mail for {steam_username}.")
                return code
        for query in queries:
            if pause_controller and pause_controller.should_stop():
                raise RuntimeError("Stopped while waiting for Steam Guard email.")
            if time.monotonic() >= deadline:
                break
            await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
            await wait_for_timeout_with_stop(page, 1500, pause_controller, "Steam Guard mailbox load")
            if pause_controller and pause_controller.should_stop():
                raise RuntimeError("Stopped while waiting for Steam Guard email.")
            if await handle_microsoft_blocking_page(
                page,
                f"目标邮箱 Steam Guard 收件箱 {steam_username}",
                log,
                pause_controller,
                on_manual_wait,
                on_manual_resume,
            ):
                continue
            if await handle_outlook_blocking_page(
                page,
                f"目标邮箱 Steam Guard 收件箱 {steam_username}",
                log,
                pause_controller,
                on_manual_wait,
                on_manual_resume,
            ):
                continue
            log(f"Searching Outlook for Steam Guard mail: query={query!r}, url={page.url}")
            search_filled = False
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
                    await wait_for_timeout_with_stop(page, 2000, pause_controller, "Steam Guard Outlook search")
                    if pause_controller and pause_controller.should_stop():
                        raise RuntimeError("Stopped while waiting for Steam Guard email.")
                    search_filled = True
                    break
                except PlaywrightTimeoutError:
                    pass
            if not search_filled:
                log(f"Outlook search box not found for Steam Guard query={query!r}; current url={page.url}")

            opened = await open_latest_steam_guard_mail(page)
            if opened:
                await wait_for_timeout_with_stop(page, 1200, pause_controller, "Steam Guard message open")
                if pause_controller and pause_controller.should_stop():
                    raise RuntimeError("Stopped while waiting for Steam Guard email.")
                code = await extract_steam_guard_code_from_open_message(page)
                if code:
                    log(f"Steam Guard code extracted from opened Outlook mail for {steam_username}.")
                    return code
                log(f"Opened a Steam-related Outlook item for {steam_username}, but no fixed-format Guard code was found.")
            else:
                log(f"No Steam Guard mail opened for query={query!r}.")
        await wait_for_timeout_with_stop(page, 5000, pause_controller, "Steam Guard email retry")
    raise TimeoutError(
        "Timed out waiting for Steam Guard email code. "
        "Steam 官方说明邮件码可能延迟，需确认正在查看 Steam 账号绑定邮箱，并检查垃圾邮件/受信任发件人 noreply@steampowered.com、support@steampowered.com。"
    )


async def get_microsoft_security_code_from_mailbox(
    page: Page,
    target_email: str,
    timeout_seconds: int,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
    not_before_epoch: float | None = None,
    exclude_message_ids: set[str] | None = None,
) -> str:
    from outlook_batch_login import get_security_code

    deadline = time.monotonic() + timeout_seconds
    next_log = 0.0
    if exclude_message_ids is not None and not exclude_message_ids:
        log(
            f"Microsoft security-mail pre-request snapshot is empty for {target_email}; "
            "same-minute messages will be rejected until a later dated row appears."
        )
    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError(f"Stopped while waiting for Microsoft security code for {target_email}.")
        now = time.monotonic()
        if now >= next_log:
            log(f"Waiting for Microsoft security code for {target_email}: {await page_state_summary(page)}")
            next_log = now + 10
        if await handle_microsoft_blocking_page(
            page,
            f"安全邮箱窗口收取 {target_email} 的 Microsoft 安全码",
            log,
            pause_controller,
            on_manual_wait,
            on_manual_resume,
        ):
            continue
        if await handle_outlook_blocking_page(
            page,
            f"安全邮箱窗口收取 {target_email} 的 Microsoft 安全码",
            log,
            pause_controller,
            on_manual_wait,
            on_manual_resume,
        ):
            continue
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError(f"Stopped while waiting for Microsoft security code for {target_email}.")
        remaining = max(1, int(deadline - time.monotonic()))
        try:
            return await get_security_code(
                page,
                target_email,
                min(20, remaining),
                pause_controller,
                not_before_epoch=not_before_epoch,
                exclude_message_ids=exclude_message_ids,
            )
        except TimeoutError:
            log(f"Microsoft security code not found yet for {target_email}; retrying mailbox search.")
        except Exception as exc:
            log(f"Microsoft security code search failed for {target_email}: {exc}; {await page_state_summary(page)}")
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError(f"Stopped while waiting for Microsoft security code for {target_email}.")
        await wait_for_timeout_with_stop(page, 1000, pause_controller, f"Microsoft security code mailbox retry for {target_email}")
    raise TimeoutError(f"Timed out waiting for Microsoft security code for {target_email}.")


async def submit_microsoft_security_code_with_validation(page: Page, code: str, account_email: str, log, pause_controller=None) -> bool:
    if await outlook_inbox_ready(page):
        log(f"Microsoft verification already completed before code submission for {account_email}; Outlook inbox is ready.")
        return True
    log(f"Submitting Microsoft security code for {account_email}.")
    try:
        await submit_code(page, code, pause_controller)
    except RuntimeError as exc:
        if "security code input" in str(exc).casefold() and await outlook_inbox_ready(page):
            log(f"Microsoft verification completed while retrieving the code for {account_email}; Outlook inbox is ready.")
            return True
        raise
    try:
        if page.is_closed():
            log(f"Microsoft security code auto-submitted and the verification popup closed for {account_email}.")
            return True
    except Exception:
        pass
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError(f"Stopped while validating Microsoft security code for {account_email}.")
        rejected = await detect_microsoft_code_rejected(page)
        if rejected:
            log(f"Microsoft security code rejected for {account_email}; page={rejected}")
            return False
        delivery_failure = await detect_security_email_delivery_failure(page)
        if delivery_failure:
            log(f"Microsoft security code flow hit delivery failure for {account_email}; page={delivery_failure}")
            return False
        state = await detect_microsoft_blocking_page(page)
        if state and state[0] in MICROSOFT_WRITEBACK_BLOCKING_LABELS:
            log(f"Microsoft security code flow still blocked for {account_email}: {state[0]}; {state[1]}")
            return False
        try:
            body = await page.locator("body").inner_text(timeout=1000)
        except Exception:
            body = ""
        lower = body.casefold()
        if any(marker in lower for marker in ["incorrect", "expired", "try again", "didn't work"]) or any(
            marker in body for marker in ["不正确", "错误", "过期", "重新获取", "重试"]
        ):
            continue
        if not any(
            marker in lower
            for marker in ["enter code", "security code", "verification code", "we sent a code"]
        ) and not any(marker in body for marker in ["输入代码", "输入验证码", "安全代码", "验证码", "已发送代码"]):
            log(f"Microsoft security code submitted for {account_email}; {await page_state_summary(page)}")
            return True
        await wait_for_timeout_with_stop(page, 1000, pause_controller, f"Microsoft security code validation for {account_email}")
    rejected = await detect_microsoft_code_rejected(page)
    if rejected:
        log(f"Microsoft security code rejected for {account_email}; page={rejected}")
        return False
    log(f"Microsoft security code submission did not leave the code-entry page for {account_email}; {await page_state_summary(page)}")
    return False


async def microsoft_security_code_entry_visible(page: Page) -> bool:
    """Return whether Microsoft has actually reached the code-entry step."""
    try:
        inputs = page.locator(
            "input[name=otc]:visible, input[name=code]:visible, input[type=tel]:visible, "
            "input[autocomplete=one-time-code]:visible"
        )
        if await inputs.count():
            return True
    except Exception:
        pass
    try:
        body = await page.locator("body").inner_text(timeout=1500)
    except Exception:
        body = ""
    lower = body.casefold()
    if any(
        marker in lower
        for marker in [
            "enter code",
            "enter the code",
            "we sent a code",
            "check your email",
            "code we sent",
        ]
    ) or any(marker in body for marker in ["输入代码", "输入验证码", "我们已发送代码", "检查你的电子邮件"]):
        return True
    return False


async def request_microsoft_security_email_code(
    page: Page,
    bind_email: str,
    account_email: str,
    log,
    pause_controller=None,
    allow_add_security_method: bool = False,
    allow_redirect_without_code: bool = False,
    on_security_mailbox_blocked=lambda email, reason: None,
) -> float:
    """Select the recovery-email method, submit it, and prove code entry is ready.

    The forwarding OAuth popup is not consistent: an already-bound account can
    show an email field, a method chooser, or a code field after Microsoft has
    already sent the message.  Treat each state explicitly and never start
    reading the mailbox unless the target page confirms that a code was sent.
    """
    if "outlook.live.com/mail" in (getattr(page, "url", "") or "").casefold():
        log(
            f"Microsoft security-email request blocked for {account_email}: "
            "target page is an Outlook mailbox/search page, not a verification page."
        )
        return False
    if await microsoft_security_code_entry_visible(page):
        log(f"Microsoft already sent a security code for {account_email}; waiting for mailbox delivery.")
        return time.time() - 90

    log(f"Microsoft security verification method detected for {account_email}; selecting email delivery.")
    submitted = await submit_security_email(page, bind_email, pause_controller)
    if not submitted and allow_add_security_method:
        await click_first(
            page,
            [
                r"^立即添加$", r"^现在添加$", r"^Add now$", r"Add recovery email",
                r"Add a new way to sign in", r"Add security info",
                r"添加恢复电子邮件地址", r"添加新的登录或验证方式", r"添加安全信息",
            ],
            timeout_ms=5000,
        )
        await wait_for_timeout_with_stop(page, 1200, pause_controller, "Microsoft security-email method open")
        log(f"Microsoft email delivery method opened for {account_email}; submitting recovery address.")
        submitted = await submit_security_email(page, bind_email, pause_controller)
    if not submitted:
        await click_first(
            page,
            [
                r"^Email$", r"Email.*code", r"Email a code", r"^Use email$",
                r"^电子邮件$", r"电子邮件.*代码", r"备用电子邮件", r"使用电子邮件",
            ],
            timeout_ms=4000,
        )
        await wait_for_timeout_with_stop(page, 1200, pause_controller, "Microsoft email verification method select")
        submitted = await submit_security_email(page, bind_email, pause_controller)
    if not submitted:
        send_method_clicked = await click_first(
            page,
            [
                r"Send.*code", r"Send code to.*", r"already have a code",
                r"发送.*代码", r"发送验证码", r"将代码发送到.*", r"将验证码发送到.*",
                r"发送电子邮件", r"Send an email", r"Email me a code",
                r"Get code", r"获取代码",
            ],
            timeout_ms=3000,
        )
        if send_method_clicked:
            await wait_for_timeout_with_stop(page, 1200, pause_controller, "Microsoft security-email address confirmation")
            # Microsoft may use the masked-address option only as a method
            # chooser, then require the complete recovery address before it
            # sends the code. Re-check the page after the click and fill that
            # address before treating the request as submitted.
            submitted = await submit_security_email(page, bind_email, pause_controller)
            if not submitted:
                # Some builds send immediately from the method chooser and
                # land directly on the code page; preserve that successful
                # click and let the state check below validate it.
                submitted = True
    if not submitted:
        log(f"Microsoft security-email code method was not submitted for {account_email}.")
        return False

    code_requested_at = time.time()
    log(f"Microsoft security-email send control submitted for {account_email}; waiting for the six-digit code page.")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        failure = await detect_security_email_delivery_failure(page)
        if failure:
            reason = (
                f"Microsoft rejected security-email code delivery for target {account_email}: {failure}"
            )
            log(f"Security mailbox marked limited/risk-controlled: {bind_email}. {reason}")
            on_security_mailbox_blocked(bind_email, reason)
            raise RuntimeError(
                f"安全邮箱 {bind_email} 收码受限，Microsoft 未能发送验证码。"
                "已持久化记录并从安全邮箱池移除，请补充可用安全邮箱。"
            )
        if await microsoft_security_code_entry_visible(page):
            log(f"Microsoft security code request confirmed for {account_email}.")
            return code_requested_at
        if allow_redirect_without_code and not await outlook_forward_verification_required(page):
            # A successful OAuth redirect can finish without rendering the
            # code input. The rule-editor commit check remains the final gate.
            return time.time() - 90
        await wait_for_timeout_with_stop(page, 500, pause_controller, "Microsoft security code page render")
    raise RuntimeError(
        f"Microsoft security-email code request did not reach the code-entry page for {account_email}."
    )


async def save_page_screenshot(page: Page | None, path: Path, label: str, log) -> None:
    if page is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), full_page=True)
        log(f"{label} screenshot saved: {path}")
    except Exception as exc:
        log(f"{label} screenshot save failed: {exc}")


async def log_error_page_state(label: str, page: Page | None, log) -> None:
    if page is None:
        log(f"{label} page state unavailable: page not opened.")
        return
    try:
        log(f"{label} page state at failure: {await page_state_summary(page)}")
    except Exception as exc:
        log(f"{label} page state unavailable: {exc}")


async def logout_outlook(page: Page, pause_controller=None) -> None:
    # The consumer Outlook logout endpoint may leave account-picker/PRT state
    # available. Clear this context's cookies after the server-side logout so a
    # subsequent mailbox cannot silently inherit the previous row's session.
    await page.goto(LOGOUT_URL, wait_until="domcontentloaded")
    await wait_for_timeout_with_stop(page, 2500, pause_controller, "Outlook logout")
    try:
        await page.context.clear_cookies()
    except Exception as exc:
        raise RuntimeError(f"Could not clear Outlook session cookies after logout: {exc}") from exc


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


async def launch_persistent_context_with_fallback(
    playwright,
    profile: str,
    x: int,
    y: int,
    log,
    label: str,
) -> BrowserContext:
    disable_password_manager_profile(Path(profile), log)
    browser_args = [
        f"--window-position={x},{y}",
        "--password-store=basic",
        "--credentials-enable-service=false",
        "--disable-save-password-bubble",
        "--disable-features=PasswordManagerOnboarding,PasswordManager,PasswordLeakDetection,PasswordManagerSaveBrowser,PasswordManagerSigninIntercept",
    ]
    try:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=profile,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=browser_args,
        )
    except Exception as exc:
        temp_profile = f"{profile}-fallback-{int(time.time())}"
        log(f"{label} browser profile failed: {exc}; using fallback profile {temp_profile}")
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=temp_profile,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=browser_args,
        )
    await install_webauthn_cancel(context)
    return context


def email_scoped_outlook_profile(scope: str, email: str) -> Path:
    normalized = email.strip().casefold()
    safe_prefix = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:32] or "mailbox"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    profile = OUTLOOK_SESSION_ROOT / scope / f"{safe_prefix}-{digest}"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def email_scoped_session_marker(scope: str, email: str) -> Path:
    return email_scoped_outlook_profile(scope, email) / ".batchsignup-session-confirmed"


def email_scoped_storage_state(scope: str, email: str) -> Path:
    return email_scoped_outlook_profile(scope, email) / ".batchsignup-storage-state.json"


def mark_email_scoped_outlook_session(scope: str, email: str) -> None:
    email_scoped_session_marker(scope, email).write_text(email.strip().casefold(), encoding="utf-8")


def clear_email_scoped_outlook_session(scope: str, email: str) -> None:
    email_scoped_session_marker(scope, email).unlink(missing_ok=True)
    email_scoped_storage_state(scope, email).unlink(missing_ok=True)


async def persist_email_scoped_outlook_session(context: BrowserContext, scope: str, email: str) -> None:
    await context.storage_state(path=str(email_scoped_storage_state(scope, email)))
    mark_email_scoped_outlook_session(scope, email)


async def restore_email_scoped_outlook_session(context: BrowserContext, scope: str, email: str) -> bool:
    marker = email_scoped_session_marker(scope, email)
    state_path = email_scoped_storage_state(scope, email)
    try:
        if marker.read_text(encoding="utf-8").strip() != email.strip().casefold():
            return False
        state = json.loads(state_path.read_text(encoding="utf-8"))
        cookies = state.get("cookies") or []
        if not cookies:
            return False
        await context.add_cookies(cookies)
        return True
    except Exception:
        return False


async def launch_isolated_target_context(playwright, email: str, x: int, y: int, log) -> tuple[BrowserContext, Path]:
    """Open a persistent target-mailbox session that cannot inherit another email's cookies."""
    profile = email_scoped_outlook_profile("target", email)
    disable_password_manager_profile(profile, log)
    browser_args = [
        f"--window-position={x},{y}",
        "--password-store=basic",
        "--credentials-enable-service=false",
        "--disable-save-password-bubble",
        "--disable-features=PasswordManagerOnboarding,PasswordManager,PasswordLeakDetection,PasswordManagerSaveBrowser,PasswordManagerSigninIntercept",
    ]
    try:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=browser_args,
        )
    except Exception:
        raise
    await install_webauthn_cancel(context)
    await restore_email_scoped_outlook_session(context, "target", email)
    log(f"Target Outlook isolated browser context opened: {profile.name}")
    return context, profile


async def launch_isolated_security_context(playwright, email: str, x: int, y: int, log) -> tuple[BrowserContext, Path]:
    """Open a persistent security-mailbox session scoped to exactly one email."""
    profile = email_scoped_outlook_profile("security", email)
    disable_password_manager_profile(profile, log)
    browser_args = [
        f"--window-position={x},{y}",
        "--password-store=basic",
        "--credentials-enable-service=false",
        "--disable-save-password-bubble",
        "--disable-features=PasswordManagerOnboarding,PasswordManager,PasswordLeakDetection,PasswordManagerSaveBrowser,PasswordManagerSigninIntercept",
    ]
    try:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=browser_args,
        )
    except Exception:
        raise
    await install_webauthn_cancel(context)
    await restore_email_scoped_outlook_session(context, "security", email)
    log(f"Security Outlook isolated browser context opened: {profile.name}")
    return context, profile


def extract_email_addresses(text: str) -> set[str]:
    return {
        match.casefold()
        for match in re.findall(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text or "", re.I)
    }


async def read_active_outlook_mailbox(page: Page, log=print) -> str:
    """Read identity only from Outlook's account control/panel, never the whole mailbox body."""
    account_button_selectors = [
        "#O365_MeFlexPane_Button",
        "#mectrl_main_trigger",
        "[data-testid*='mectrl' i]",
        "[aria-label*='Account manager' i]",
        "[aria-label*='帐户管理器' i]",
        "[aria-label*='账户管理器' i]",
        "button[aria-label*='Account manager' i]",
        "button[aria-label*='帐户管理器' i]",
        "button[aria-label*='账户管理器' i]",
        "button[aria-label*='My account' i]",
        "button[aria-label*='我的帐户' i]",
        "button[aria-label*='我的账户' i]",
    ]
    button = page.locator(", ".join(f"{selector}:visible" for selector in account_button_selectors)).first
    button_text = ""
    try:
        if not await button.count():
            log("Outlook account manager control is not present; identity will rely on the email-scoped session gate.")
            return ""
        button_text = " ".join(
            filter(
                None,
                [
                    await button.get_attribute("aria-label", timeout=1000),
                    await button.get_attribute("title", timeout=1000),
                    await button.inner_text(timeout=1000),
                ],
            )
        )
    except Exception:
        pass
    direct_emails = extract_email_addresses(button_text)
    if len(direct_emails) == 1:
        return next(iter(direct_emails))

    try:
        await button.click(timeout=3000)
        await wait_for_timeout_with_stop(page, 750, None, "Outlook account panel open")
    except Exception as exc:
        log(f"Could not open Outlook account manager while verifying identity: {exc}")
        return ""

    panel_selectors = [
        "[role='dialog']:visible",
        "[role='menu']:visible",
        "[aria-modal='true']:visible",
        "[data-testid*='account' i]:visible",
    ]
    panel_texts: list[str] = []
    for selector in panel_selectors:
        try:
            locator = page.locator(selector)
            count = min(await locator.count(), 8)
            for index in range(count):
                text = await locator.nth(index).inner_text(timeout=1500)
                if text:
                    panel_texts.append(text)
        except Exception:
            continue
    emails = extract_email_addresses("\n".join(panel_texts))
    # Do not leave the account panel covering the inbox for the next step.
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    if len(emails) == 1:
        return next(iter(emails))
    if emails:
        log(f"Outlook account manager exposed multiple identities; refusing to guess active account: {sorted(emails)}")
    else:
        log("Outlook account manager did not expose an email address; active identity is unverified.")
    return ""


async def ensure_active_outlook_mailbox(
    page: Page,
    expected_email: str,
    log=print,
    timeout_seconds: int = 30,
    pause_controller=None,
) -> None:
    expected = expected_email.strip().casefold()
    deadline = time.monotonic() + timeout_seconds
    last_active = ""
    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError(f"Stopped while verifying active Outlook mailbox for {expected_email}.")
        last_active = await read_active_outlook_mailbox(page, log)
        if last_active == expected:
            log(f"Active Outlook mailbox verified: {expected_email}")
            return
        if last_active:
            raise RuntimeError(
                f"Outlook identity mismatch: expected {expected_email}, active mailbox is {last_active}."
            )
        await wait_for_timeout_with_stop(page, 1500, pause_controller, f"Outlook identity verification for {expected_email}")
    raise RuntimeError(
        f"Could not verify the active Outlook mailbox as {expected_email}; last detected identity={last_active or 'unknown'}."
    )


async def confirm_fresh_outlook_login_identity(page: Page, expected_email: str, log=print) -> None:
    """Validate a clean explicit login without discarding it solely for an unreadable account control."""
    expected = expected_email.strip().casefold()
    active = await read_active_outlook_mailbox(page, log)
    if active and active != expected:
        raise RuntimeError(
            f"Outlook identity mismatch after fresh login: expected {expected_email}, active mailbox is {active}."
        )
    if active == expected:
        log(f"Active Outlook mailbox verified after fresh login: {expected_email}")
    else:
        log(
            f"Outlook account control is unreadable after fresh isolated credential login for {expected_email}; "
            "keeping the confirmed inbox session instead of closing and logging in again."
        )
async def cancel_passkey_prompt(page: Page, log=print, timeout_seconds: int = 30, pause_controller=None) -> None:
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
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError("Stopped while cancelling Microsoft passkey prompt.")
        dialog_scope = None
        try:
            if "outlook.live.com" in (page.url or "").casefold():
                dialogs = page.locator("[role=dialog]:visible, [aria-modal=true]:visible")
                if not await dialogs.count():
                    return
                dialog_scope = dialogs.first
                body = await dialog_scope.inner_text(timeout=3000)
            else:
                body = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            body = ""
        if not any(re.search(pattern, body, re.I) for pattern in prompt_patterns):
            return
        log("Microsoft passkey prompt detected; clicking Cancel.")
        clicked = False
        if dialog_scope is not None:
            try:
                combined = re.compile("(?:" + ")|(?:".join(cancel_patterns) + ")", re.I)
                await dialog_scope.get_by_text(combined).first.click(timeout=2500)
                clicked = True
            except Exception:
                pass
        elif await click_first(page, cancel_patterns, timeout_ms=2500):
            clicked = True
        if clicked:
            await wait_for_timeout_with_stop(page, 3000, pause_controller, "Microsoft passkey prompt cancel")
            continue
        for selector in [
            "button:has-text('取消')",
            "button:has-text('Cancel')",
            "input[type=button][value='取消']",
            "input[type=button][value='Cancel']",
        ]:
            try:
                await page.locator(selector).first.click(timeout=1500)
                await wait_for_timeout_with_stop(page, 3000, pause_controller, "Microsoft passkey prompt fallback cancel")
                break
            except Exception:
                pass
        await wait_for_timeout_with_stop(page, 1000, pause_controller, "Microsoft passkey prompt retry")


async def wait_for_outlook_inbox(
    page: Page,
    log=print,
    timeout_seconds: int = 90,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
    account_label: str = "目标 Outlook",
) -> None:
    deadline = time.monotonic() + timeout_seconds
    next_log = 0.0
    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError(f"Stopped while waiting for Outlook inbox for {account_label}.")
        now = time.monotonic()
        if now >= next_log:
            log(f"Waiting for Outlook inbox for {account_label}: {await page_state_summary(page)}")
            next_log = now + 10
        if await cancel_microsoft_fido_bridge(page, pause_controller, timeout_seconds=1):
            log("Microsoft passkey request cancelled through the browser login flow.")
        await cancel_passkey_prompt(page, log, timeout_seconds=5, pause_controller=pause_controller)
        await handle_outlook_browser_prompts(page, log, timeout_seconds=2, pause_controller=pause_controller)
        await handle_microsoft_post_login_prompts(
            page,
            log,
            timeout_seconds=3,
            pause_controller=pause_controller,
            on_manual_wait=on_manual_wait,
            on_manual_resume=on_manual_resume,
        )
        if await redirect_microsoft_account_home_to_outlook(page, log, pause_controller):
            deadline = time.monotonic() + timeout_seconds
            continue
        if await handle_microsoft_blocking_page(page, account_label, log, pause_controller, on_manual_wait, on_manual_resume):
            deadline = time.monotonic() + timeout_seconds
            continue
        if await handle_outlook_blocking_page(page, account_label, log, pause_controller, on_manual_wait, on_manual_resume):
            deadline = time.monotonic() + timeout_seconds
            continue
        try:
            url = page.url
            body = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            url = ""
            body = ""
        markers = ["New mail", "新建邮件", "Inbox", "收件箱", "Focused", "重点"]
        if "outlook.live.com" in (url or "").casefold() and any(
            marker.casefold() in (body or "").casefold() for marker in markers
        ):
            log("Outlook inbox confirmed.")
            return
        await wait_for_timeout_with_stop(page, 3000, pause_controller, f"Outlook inbox poll for {account_label}")
    state = await detect_microsoft_blocking_page(page)
    if state:
        raise TimeoutError(f"Timed out waiting for Outlook inbox; Microsoft page still requires handling: {state[0]}")
    outlook_state = await detect_outlook_blocking_page(page)
    if outlook_state:
        raise TimeoutError(f"Timed out waiting for Outlook inbox; Outlook page still requires handling: {outlook_state[0]}")
    raise TimeoutError(f"Timed out waiting for Outlook inbox after Microsoft login. Last page: {await page_state_summary(page)}")


async def outlook_inbox_ready(page: Page, url: str | None = None, body: str | None = None) -> bool:
    try:
        current_url = url if url is not None else page.url
        current_body = body if body is not None else await page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    markers = ["New mail", "新建邮件", "Inbox", "收件箱", "Focused", "重点"]
    return "outlook.live.com" in (current_url or "").casefold() and any(
        marker.casefold() in (current_body or "").casefold() for marker in markers
    )


async def try_reuse_email_scoped_outlook_session(
    page: Page,
    scope: str,
    email: str,
    log,
    pause_controller=None,
) -> bool:
    """Reuse a persisted session whose profile directory is already scoped to this exact email."""
    marker = email_scoped_session_marker(scope, email)
    try:
        if marker.read_text(encoding="utf-8").strip() != email.strip().casefold():
            return False
    except OSError:
        return False
    try:
        await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded", timeout=30000)
        # A valid Microsoft session can briefly land on account.microsoft.com
        # (the user/account directory) or render Outlook's shell before the
        # mailbox controls appear.  Neither state means the login was lost.
        # Keep navigating back to Outlook and wait for the real inbox gate.
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if pause_controller and pause_controller.should_stop():
                raise RuntimeError("Stopped during persisted Outlook session check.")
            if await redirect_microsoft_account_home_to_outlook(page, log, pause_controller):
                continue
            url = (page.url or "").casefold()
            try:
                body = await page.locator("body").inner_text(timeout=5000)
            except Exception:
                body = ""
            if await outlook_inbox_ready(page, url=url, body=body):
                log(f"Reusing persisted Outlook session: {email}")
                return True
            await handle_microsoft_post_login_prompts(page, log, timeout_seconds=2, pause_controller=pause_controller)
            await wait_for_timeout_with_stop(page, 2000, pause_controller, "Persisted Outlook inbox recovery")
    except Exception as exc:
        if pause_controller and pause_controller.should_stop():
            raise
        log(f"Persisted Outlook session check did not reach the inbox for {email}: {text_snippet(str(exc), 160)}")
    return False


async def redirect_microsoft_account_home_to_outlook(page: Page, log=print, pause_controller=None) -> bool:
    """Recover when Microsoft login finishes on the account landing page instead of Outlook."""
    url = (page.url or "").casefold()
    if "account-checkup" in url:
        return False
    if "account.microsoft.com" not in url and "pagename=account" not in url:
        return False
    log("Microsoft login finished on the account home page; navigating to Outlook inbox without reloading the account page.")
    await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
    await wait_for_timeout_with_stop(
        page,
        2500,
        pause_controller,
        "Outlook inbox navigation after Microsoft account landing",
    )
    return True


async def handle_outlook_browser_prompts(page: Page, log=print, timeout_seconds: int = 5, pause_controller=None) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError("Stopped while handling Outlook browser prompt.")
        try:
            body = await page.locator("body").inner_text(timeout=1000)
        except Exception:
            body = ""
        if not any(marker in body for marker in ["打开邮箱链接", "outlook.live.com 想要", "Allow", "允许", "Block", "屏蔽"]):
            return
        for pattern in [r"^允许$", r"^Allow$"]:
            if await click_first(page, [pattern], timeout_ms=1000):
                log("Allowed Outlook mail link browser prompt.")
                await wait_for_timeout_with_stop(page, 1000, pause_controller, "Outlook browser prompt allow")
                return
        for pattern in [r"^屏蔽$", r"^Block$"]:
            if await click_first(page, [pattern], timeout_ms=1000):
                log("Blocked Outlook mail link browser prompt.")
                await wait_for_timeout_with_stop(page, 1000, pause_controller, "Outlook browser prompt block")
                return
        for selector in ["button[aria-label*='关闭' i]", "button[aria-label*='Close' i]"]:
            try:
                await page.locator(selector).first.click(timeout=1000)
                log("Closed Outlook mail link browser prompt.")
                await wait_for_timeout_with_stop(page, 1000, pause_controller, "Outlook browser prompt close")
                return
            except Exception:
                pass
        try:
            await page.keyboard.press("Escape")
            log("Sent Escape to close Outlook browser prompt.")
            await wait_for_timeout_with_stop(page, 1000, pause_controller, "Outlook browser prompt escape")
            return
        except Exception:
            pass
    return


async def dismiss_outlook_nonbusiness_popup(
    page: Page,
    log=print,
    pause_controller=None,
) -> bool:
    """Dismiss Outlook surveys/feedback overlays that can interrupt settings automation."""
    try:
        body = await page.locator("body").inner_text(timeout=2000)
    except Exception:
        return False
    lower = body.casefold()
    survey_markers = [
        "how are we doing?",
        "how likely are you to recommend outlook.com",
        "we'd love your feedback",
        "tell us what you think",
        "我们做得怎么样",
        "您向他人推荐 outlook",
        "提供反馈",
    ]
    if not any(marker in lower for marker in survey_markers):
        return False
    if pause_controller and pause_controller.should_stop():
        raise RuntimeError("Stopped while dismissing Outlook feedback popup.")
    if await click_first(
        page,
        [r"^Skip$", r"^跳过$", r"^Not now$", r"^暂不$", r"^以后再说$"],
        timeout_ms=1500,
    ):
        log("Dismissed Outlook feedback survey with Skip.")
        await wait_for_timeout_with_stop(page, 700, pause_controller, "Outlook feedback popup dismiss")
        return True
    try:
        await page.keyboard.press("Escape")
        await wait_for_timeout_with_stop(page, 700, pause_controller, "Outlook feedback popup escape")
        log("Dismissed Outlook feedback survey with Escape fallback.")
        return True
    except Exception:
        return False


async def current_outlook_mailbox_matches(
    page: Page,
    account: SteamAccount,
    log=print,
    timeout_seconds: int = 20,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
) -> bool:
    expected = account.email.casefold()
    try:
        if "outlook.live.com" not in page.url.casefold():
            await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
        else:
            await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
    except Exception as exc:
        log(f"Could not navigate to Outlook while checking session for {account.email}: {exc}")
        return False

    deadline = time.monotonic() + timeout_seconds
    next_log = 0.0
    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError(f"Stopped while checking Outlook session for {account.email}.")
        now = time.monotonic()
        if now >= next_log:
            log(f"Checking whether Outlook session matches {account.email}: {await page_state_summary(page)}")
            next_log = now + 5
        await handle_outlook_browser_prompts(page, log, timeout_seconds=1, pause_controller=pause_controller)
        await handle_microsoft_post_login_prompts(page, log, timeout_seconds=2, pause_controller=pause_controller)
        if await handle_microsoft_blocking_page(
            page,
            f"目标邮箱 {account.email}",
            log,
            pause_controller,
            on_manual_wait,
            on_manual_resume,
        ):
            return False
        if await handle_outlook_blocking_page(
            page,
            f"目标邮箱 {account.email}",
            log,
            pause_controller,
            on_manual_wait,
            on_manual_resume,
        ):
            return False
        try:
            url = page.url.casefold()
            body = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            url = ""
            body = ""
        lower = body.casefold()
        if "login.live.com" in url or any(marker in lower for marker in ["sign in", "登录", "输入密码", "enter password"]):
            return False
        active_mailbox = await read_active_outlook_mailbox(page, log)
        if active_mailbox == expected:
            try:
                await wait_for_outlook_inbox(
                    page,
                    log,
                    timeout_seconds=10,
                    pause_controller=pause_controller,
                    on_manual_wait=on_manual_wait,
                    on_manual_resume=on_manual_resume,
                    account_label=f"目标邮箱 {account.email}",
                )
            except Exception as exc:
                log(
                    f"Outlook session contains {account.email}, but inbox was not confirmed; "
                    f"will not reuse this session. Error: {exc}; {await page_state_summary(page)}"
                )
                return False
            log(f"Current Outlook session already matches {account.email}; reusing it.")
            return True
        if active_mailbox and active_mailbox != expected:
            log(f"Current active Outlook mailbox is {active_mailbox}, expected {account.email}; session will not be reused.")
            return False
        await wait_for_timeout_with_stop(page, 2000, pause_controller, f"Outlook session retry for {account.email}")
    log(f"Current Outlook session did not confirm {account.email}; {await page_state_summary(page)}")
    return False


async def handle_microsoft_post_login_prompts(
    page: Page,
    log=print,
    timeout_seconds: int = 12,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_prompt = ""
    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError("Stopped while handling Microsoft post-login prompts.")
        await dismiss_password_save_prompt(page, log)
        try:
            body = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            body = ""
        normalized = body.casefold()

        if any(marker in body for marker in ["保持登录状态", "保持登录", "无需每次都登录"]) or "stay signed in" in normalized:
            log("Microsoft stay-signed-in prompt detected; choosing Yes.")
            await maybe_handle_stay_signed_in(page)
            await wait_for_timeout_with_stop(page, 2500, pause_controller, "Microsoft stay-signed-in prompt")
            continue

        if any(marker in normalized for marker in ["protect your account", "help us protect", "add security info"]) or any(
            marker in body for marker in ["保护你的帐户", "保护你的账户", "添加安全信息", "帮助我们保护", "挂起的安全操作", "添加恢复电子邮件地址"]
        ):
            last_prompt = f"Microsoft account-protection prompt still visible: {text_snippet(body)}"
            if await click_first(page, [r"^Skip$", r"^跳过$", r"暂不", r"Not now", r"以后再说"], timeout_ms=1500):
                log("Skipped Microsoft optional account-protection prompt.")
                await wait_for_timeout_with_stop(page, 2500, pause_controller, "Microsoft optional account-protection prompt skip")
                continue
            for selector in [
                "button[aria-label*='Close' i]",
                "button[aria-label*='关闭' i]",
                "button[title*='Close' i]",
                "button[title*='关闭' i]",
            ]:
                try:
                    await page.locator(selector).first.click(timeout=1500)
                    log("Closed Microsoft pending security action prompt.")
                    await wait_for_timeout_with_stop(page, 2500, pause_controller, "Microsoft pending security action close")
                    break
                except Exception:
                    pass
            else:
                try:
                    await page.keyboard.press("Escape")
                    log("Sent Escape to close Microsoft pending security action prompt.")
                    await wait_for_timeout_with_stop(page, 2500, pause_controller, "Microsoft pending security action escape")
                    continue
                except Exception:
                    pass
            log(f"Microsoft account-protection prompt was not dismissible automatically; {await page_state_summary(page)}")
            continue

        if any(marker in normalized for marker in ["use windows hello", "passkey"]) or any(marker in body for marker in ["Windows Hello", "通行密钥"]):
            await cancel_passkey_prompt(page, log, timeout_seconds=3, pause_controller=pause_controller)
            await wait_for_timeout_with_stop(page, 1000, pause_controller, "Microsoft Windows Hello prompt")
            continue

        return
    if last_prompt:
        # A security-code/recovery-email page is an expected hand-off to the
        # verification state machine.  Every other prompt that survives the
        # bounded handler is an unhandled page and must fail this row rather
        # than silently letting the workflow continue on the wrong surface.
        if await security_challenge_visible(page) or await is_recovery_email_setup_page(page):
            log(f"{last_prompt}; handing the recognized verification page to the security flow.")
            return
        manual_message = (
            "Microsoft 登录后出现自动化未识别页面，已暂停当前账号供人工/ChatGPT 处理。"
            "请在对应浏览器窗口完成页面操作，回到控制台点击“确认完成，继续”；"
            "程序只会复核一次，仍未进入已知状态就停止当前批次，不会循环重试。"
        )
        await wait_for_manual_continue(
            manual_message,
            log,
            pause_controller,
            on_manual_wait,
            on_manual_resume,
            page=page,
            screenshot_path=Path("errors") / "microsoft_post_login_unhandled.png",
            screenshot_label="Microsoft post-login unhandled page",
        )
        if await security_challenge_visible(page) or await is_recovery_email_setup_page(page):
            return
        raise RuntimeError(
            f"Microsoft post-login prompt handling timed out; "
            f"Microsoft post-login page remained unhandled after one manual intervention: "
            f"{await page_state_summary(page)}"
        )


async def connect_security_context(playwright, args, log=print) -> tuple[BrowserContext, Page]:
    context = await launch_persistent_context_with_fallback(
        playwright,
        args.security_profile,
        args.security_x,
        args.security_y,
        log,
        "Security mailbox",
    )
    page = context.pages[0] if context.pages else await context.new_page()
    security_pool = parse_security_mailbox_pool(getattr(args, "security_mailbox_pool", "") or "")
    if security_pool:
        instruction_lines = [
            "当前任务使用安全邮箱池，窗口2会按当前飞书行切换或登录对应安全邮箱。",
            "程序会在目标账号触发 Microsoft 安全验证时，从当前行分配的安全邮箱收取验证码。",
            "如果页面要求登录或验证，请按 Web UI 状态区提示完成；不要固定停留在默认安全邮箱。",
        ]
    else:
        instruction_lines = [
            f"请确认这个窗口登录的是默认安全邮箱：{args.security_email}",
            "程序会在目标账号触发 Microsoft 安全验证时，从这里收取验证码。",
            "如果没有登录，请手动登录；登录完成后回到 localhost 控制台点击继续。",
        ]
    await show_window_instruction(
        page,
        "窗口2：安全邮箱",
        instruction_lines,
    )
    await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
    return context, page


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
    page: Page | None = None,
    screenshot_path: Path | None = None,
    screenshot_label: str = "Manual action",
) -> None:
    if page is not None:
        message = f"{message}\n当前页面：{await page_state_summary(page)}"
    if page is not None and screenshot_path is not None:
        await save_page_screenshot(page, screenshot_path, screenshot_label, log)
    log(message)
    on_manual_wait(message)
    if pause_controller:
        if not pause_controller.wait():
            raise RuntimeError("Stopped while waiting for manual confirmation.")
    else:
        input(f"{message}\nPress Enter to continue...")
    if page is not None:
        log(f"Manual action confirmed; current page after user action: {await page_state_summary(page)}")
    if page is not None and screenshot_path is not None:
        after_path = screenshot_path.with_name(f"{screenshot_path.stem}_after{screenshot_path.suffix}")
        await save_page_screenshot(page, after_path, f"{screenshot_label} after manual confirmation", log)
    on_manual_resume(message)


async def detect_microsoft_blocking_page(page: Page) -> tuple[str, str] | None:
    try:
        url = page.url
    except Exception:
        url = ""
    try:
        body = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        body = ""
    return classify_microsoft_page(url, body)


MICROSOFT_WRITEBACK_BLOCKING_LABELS = {
    "账号被锁定、暂停或检测到异常活动",
    "验证码发送方式不可用或需要选择其他验证方式",
    "安全信息变更等待期或当前无法访问",
    "请求过多或 Microsoft 服务暂时不可用",
}


async def ensure_microsoft_page_allows_writeback(page: Page, action_label: str, log) -> None:
    state = await detect_microsoft_blocking_page(page)
    if not state:
        return
    label, preview = state
    if label not in MICROSOFT_WRITEBACK_BLOCKING_LABELS:
        log(f"{action_label} writeback check saw Microsoft page: {label}; {preview}")
        return
    summary = await page_state_summary(page)
    raise RuntimeError(
        f"{action_label} 不能写回：Microsoft 页面仍处于“{label}”。"
        f" 页面摘要：{preview or summary}。请先处理该页面，再点击继续。"
    )


def masked_security_email_present(bind_email: str, body: str) -> bool:
    email = (bind_email or "").strip().casefold()
    if "@" not in email:
        return False
    local, domain = email.rsplit("@", 1)
    if not local or not domain:
        return False
    escaped_domain = re.escape(domain)
    first = re.escape(local[0])
    patterns = [
        rf"\b{first}[\*\u2022xX]{{2,}}@{escaped_domain}\b",
        rf"(?<![A-Za-z0-9_.-])[\*\u2022xX]{{2,}}@{escaped_domain}\b",
    ]
    return any(re.search(pattern, body.casefold()) for pattern in patterns)


async def ensure_security_binding_ready_for_writeback(page: Page, bind_email: str, log) -> None:
    await ensure_microsoft_page_allows_writeback(page, "绑定安全邮箱", log)
    try:
        url = page.url
    except Exception:
        url = ""
    try:
        body = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        body = ""
    lower = body.casefold()
    security_page_markers = [
        "security",
        "sign in",
        "verify",
        "proof",
        "ways to prove",
        "manage how i sign in",
        "add a new way",
        "安全",
        "登录方式",
        "验证",
        "证明你的身份",
        "备用电子邮件",
        "添加新的",
    ]
    if not any(host in url.casefold() for host in ["account.live.com", "account.microsoft.com", "login.live.com"]):
        raise RuntimeError(f"绑定安全邮箱不能写回：当前页面不是 Microsoft 账号安全页面。页面摘要：{await page_state_summary(page)}")
    if not any(marker.casefold() in lower for marker in security_page_markers):
        raise RuntimeError(f"绑定安全邮箱不能写回：当前页面没有安全信息/验证方式标记。页面摘要：{await page_state_summary(page)}")
    pending_confirmation_markers = [
        "enter code",
        "enter the code",
        "security code",
        "verification code",
        "we sent a code",
        "we'll send a code",
        "check your email",
        "输入代码",
        "输入验证码",
        "安全代码",
        "验证码",
        "我们已发送代码",
        "检查你的电子邮件",
        "查看你的电子邮件",
    ]
    if any(marker.casefold() in lower for marker in pending_confirmation_markers):
        raise RuntimeError(
            "绑定安全邮箱不能写回：当前页面仍在等待安全邮箱验证码确认。"
            "请提交验证码并确认安全邮箱出现在 Microsoft 安全信息列表后，再点击继续。"
            f" 页面摘要：{await page_state_summary(page)}"
        )
    if bind_email and bind_email.casefold() not in lower and not masked_security_email_present(bind_email, body):
        raise RuntimeError(
            f"绑定安全邮箱不能写回：当前页面未看到完整或掩码形式的安全邮箱 {bind_email}。"
            "请确认安全邮箱已经绑定成功并显示在 Microsoft 安全信息列表中，再点击继续。"
            f" 页面摘要：{await page_state_summary(page)}"
        )
    if bind_email and bind_email.casefold() not in lower:
        log(
            f"绑定安全邮箱写回检查没有在页面文本中看到完整邮箱 {bind_email}；"
            "Microsoft 可能只显示掩码邮箱，继续依赖人工确认和页面截图。"
        )


async def ensure_password_change_ready_for_writeback(page: Page, log) -> None:
    await ensure_microsoft_page_allows_writeback(page, "修改邮箱密码", log)
    try:
        url = page.url
    except Exception:
        url = ""
    try:
        body = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        body = ""
    lower = body.casefold()
    failure_or_form_markers = [
        "current password",
        "new password",
        "reenter password",
        "confirm password",
        "save password",
        "change password",
        "incorrect password",
        "password is incorrect",
        "password doesn't meet",
        "password does not meet",
        "password requirements",
        "password is too weak",
        "password is too common",
        "try a different password",
        "we couldn't update your password",
        "we could not update your password",
        "当前密码",
        "新密码",
        "确认密码",
        "再次输入密码",
        "保存密码",
        "更改密码",
        "修改密码",
        "密码不正确",
        "密码错误",
        "密码不符合",
        "密码要求",
        "密码太弱",
        "密码过于常见",
        "请使用其他密码",
        "无法更新你的密码",
        "无法更新您的密码",
    ]
    success_markers = [
        "password changed",
        "password has been changed",
        "password updated",
        "your password was changed",
        "your password has been changed",
        "密码已更改",
        "密码已更新",
        "密码更改成功",
        "密码修改成功",
    ]
    security_page_markers = [
        "security dashboard",
        "security basics",
        "advanced security options",
        "recent activity",
        "安全仪表板",
        "安全基础",
        "高级安全选项",
        "最近活动",
    ]
    pending_verification_markers = [
        "verify your identity",
        "enter code",
        "enter the code",
        "security code",
        "verification code",
        "we sent a code",
        "check your email",
        "证明你的身份",
        "验证你的身份",
        "输入代码",
        "输入验证码",
        "安全代码",
        "验证码",
        "我们已发送代码",
        "检查你的电子邮件",
        "查看你的电子邮件",
    ]
    if not any(host in url.casefold() for host in ["account.live.com", "account.microsoft.com", "login.live.com"]):
        raise RuntimeError(f"修改邮箱密码不能写回：当前页面不是 Microsoft 账号页面。页面摘要：{await page_state_summary(page)}")
    if any(marker.casefold() in lower for marker in pending_verification_markers):
        raise RuntimeError(
            "修改邮箱密码不能写回：当前页面仍在等待 Microsoft 身份验证或安全验证码。"
            "请完成验证并确认改密成功后，再点击继续。"
            f" 页面摘要：{await page_state_summary(page)}"
        )
    if any(marker.casefold() in lower for marker in failure_or_form_markers) and not any(
        marker.casefold() in lower for marker in success_markers
    ):
        raise RuntimeError(
            "修改邮箱密码不能写回：当前页面仍显示改密表单或改密失败提示。"
            f" 页面摘要：{await page_state_summary(page)}"
        )
    security_page = "security" in url.casefold() and any(marker.casefold() in lower for marker in security_page_markers)
    if not any(marker.casefold() in lower for marker in success_markers) and not security_page:
        raise RuntimeError(
            "修改邮箱密码不能写回：当前页面没有改密成功、账号安全或完成状态标记。"
            f" 页面摘要：{await page_state_summary(page)}"
        )
    log("Microsoft password change writeback check passed.")


async def page_state_summary(page: Page) -> str:
    try:
        url = page.url
    except Exception:
        url = ""
    try:
        body = await page.locator("body").inner_text(timeout=2000)
    except Exception:
        body = ""
    return f"url={url or 'unknown'} body={text_snippet(body, 160) or 'empty'}"


def is_browser_context_closed_error(exc: BaseException) -> bool:
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in [
            "target page, context or browser has been closed",
            "page has been closed",
            "browser has been closed",
            "context has been closed",
            "targetclosederror",
        ]
    )


def is_outlook_context_retryable_error(exc: BaseException) -> bool:
    message = str(exc).casefold()
    if any(
        marker in message
        for marker in [
            "security code was rejected",
            "security-email code request did not reach",
            "收码受限",
            "验证码被拒绝",
            "security mailbox marked",
        ]
    ):
        return False
    return is_browser_context_closed_error(exc) or any(
        marker in message
        for marker in [
            "could not verify the active outlook mailbox",
            "timed out waiting for outlook inbox after microsoft login",
            "could not find microsoft password input",
            "could not find microsoft email input",
            "navigation is interrupted by another navigation",
            "page.goto: navigation to",
            "规则编辑器未",
            "规则验证窗口未",
            "save/save button",
            "save/保存",
            "stop processing more rules",
            "input of type \"radio\" cannot be filled",
        ]
    )


def is_non_skippable_verification_error(exc: BaseException) -> bool:
    """Security-code failures must stop the batch instead of skipping an account."""
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in [
            "security code was rejected",
            "microsoft rejected the security code",
            "security code input",
            "no resend control",
            "安全码被拒绝",
            "验证码被拒绝",
            "安全验证码",
            "绑定安全邮箱不能写回",
            "安全邮箱不能写回",
            "feishu write verification failed",
            "feishu write failed",
            "feishu readback failed",
            "转发规则重新验证安全码被拒绝",
            "security-email code request did not reach",
            "http put failed",
            "http get failed",
            "401 unauthorized",
            "99991677",
            "token expired",
            "authentication token expired",
            "could not find microsoft password input",
            "microsoft password input",
            "security mailbox login",
        ]
    )


async def handle_microsoft_blocking_page(
    page: Page,
    account_label: str,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
) -> bool:
    state = await detect_microsoft_blocking_page(page)
    if not state:
        return False
    label, preview = state
    message = (
        f"{account_label} 遇到 Microsoft 页面需要人工处理：{label}。\n"
        f"页面摘要：{preview or '无可读取文本'}\n"
        "请在对应浏览器窗口处理到可以继续自动化的页面，然后回到 Web UI 点击“确认完成，继续流程”。"
    )
    screenshot_path = Path("errors") / f"microsoft_blocking_{safe_artifact_name(account_label)}.png"
    await wait_for_manual_continue(
        message,
        log,
        pause_controller,
        on_manual_wait,
        on_manual_resume,
        page=page,
        screenshot_path=screenshot_path,
        screenshot_label=f"Microsoft blocking page for {account_label}",
    )
    return True


async def is_recovery_email_setup_page(page: Page) -> bool:
    try:
        url = page.url.casefold()
    except Exception:
        url = ""
    try:
        body = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        body = ""
    lower = body.casefold()
    security_setup_url = (
        "account-checkup" in url
        or "account.live.com/proofs/add" in url
        or "account.live.com/proofs/manage" in url
    )
    return security_setup_url and (
        "add recovery email" in lower
        or "recovery email address" in lower
        or "add a new way to sign in" in lower
        or "add security info" in lower
        or "let us protect your account" in lower
        or "personal email" in lower
        or "添加恢复电子邮件地址" in body
        or "添加新的登录或验证方式" in body
        or "添加安全信息" in body
        or "恢复电子邮件" in body
        or "让我们来保护你的帐户" in body
        or "让我们来保护你的账户" in body
        or "个人电子邮件" in body
    )


async def try_complete_security_email_binding(
    target_page: Page,
    security_page: Page,
    account: SteamAccount,
    bind_email: str,
    args,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
    on_security_mailbox_blocked=lambda email, reason: None,
) -> bool:
    from outlook_batch_login import collect_microsoft_security_mail_ids

    existing_security_mail_ids = await collect_microsoft_security_mail_ids(security_page)
    log(
        f"Captured {len(existing_security_mail_ids)} existing Microsoft security-mail IDs "
        f"before requesting a code for {account.email}."
    )
    code_requested_at = await request_microsoft_security_email_code(
        target_page,
        bind_email,
        account.email,
        log,
        pause_controller,
        allow_add_security_method=True,
        on_security_mailbox_blocked=on_security_mailbox_blocked,
    )
    if not code_requested_at:
        return False
    code = await get_microsoft_security_code_from_mailbox(
        security_page,
        account.email,
        args.microsoft_code_timeout,
        log,
        pause_controller,
        on_manual_wait,
        on_manual_resume,
        not_before_epoch=code_requested_at,
        exclude_message_ids=existing_security_mail_ids,
    )
    if not await submit_microsoft_security_code_with_validation(
        target_page,
        code,
        account.email,
        log,
        pause_controller,
    ):
        raise RuntimeError(f"Microsoft security-email verification code was rejected for {account.email}.")
    await handle_microsoft_post_login_prompts(
        target_page,
        log,
        pause_controller=pause_controller,
        on_manual_wait=on_manual_wait,
        on_manual_resume=on_manual_resume,
    )
    await wait_for_timeout_with_stop(target_page, 2500, pause_controller, "Microsoft security-email binding completion")
    if await microsoft_security_code_entry_visible(target_page):
        raise RuntimeError(
            f"Microsoft security-email binding did not leave the code-entry page for {account.email}."
        )
    log(f"Microsoft security email verified for {account.email}: {bind_email}")
    return True


async def complete_recovery_email_setup(
    target_page: Page,
    security_page: Page,
    account: SteamAccount,
    bind_email: str,
    args,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
    on_security_mailbox_blocked=lambda email, reason: None,
) -> bool:
    if not await is_recovery_email_setup_page(target_page):
        return False
    log(f"Microsoft recovery-email setup detected for {account.email}; binding {bind_email} automatically.")
    completed = await try_complete_security_email_binding(
        target_page,
        security_page,
        account,
        bind_email,
        args,
        log,
        pause_controller,
        on_manual_wait,
        on_manual_resume,
        on_security_mailbox_blocked,
    )
    if not completed:
        await wait_for_manual_continue(
            f"目标邮箱 {account.email} 正在要求添加恢复电子邮件。请在窗口1填写并提交 {bind_email}；"
            "窗口2已经校验为该安全邮箱。提交后点击“确认完成，继续流程”。",
            log,
            pause_controller,
            on_manual_wait,
            on_manual_resume,
            page=target_page,
            screenshot_path=Path(args.error_dir) / f"row_{account.row_number}_{account.steam_username}_recovery_email.png",
            screenshot_label="Microsoft recovery-email setup",
        )
        return False
    return True


async def ensure_microsoft_password_reauthenticated(
    page: Page,
    account: SteamAccount,
    log,
    pause_controller=None,
) -> None:
    """Handle Microsoft's password recheck before opening recovery-email setup."""
    password_input = page.locator(
        "input[type='password']:visible, input[name='passwd']:visible, "
        "input[autocomplete='current-password']:visible"
    ).first
    if not await password_input.count():
        return
    log(f"Microsoft recovery-email page requested target mailbox password again for {account.email}.")
    await password_input.fill(account.email_password, timeout=5000)
    submit = page.locator("button[type='submit']:visible, input[type='submit']:visible").first
    if await submit.count():
        await submit.click(timeout=4000)
    elif not await click_first(page, [r"^Next$", r"^下一步$", r"^Sign in$", r"^登录$"], timeout_ms=4000):
        await password_input.press("Enter")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current_url = (page.url or "").casefold()
        try:
            body = await page.locator("body").inner_text(timeout=1500)
        except Exception:
            body = ""
        lower = body.casefold()
        if any(marker in lower for marker in ["incorrect password", "password is incorrect", "wrong password"]):
            raise RuntimeError(f"Microsoft recovery-email password reauthentication failed for {account.email}.")
        if any(marker in body for marker in ["密码不正确", "密码错误"]):
            raise RuntimeError(f"Microsoft recovery-email password reauthentication failed for {account.email}.")
        remaining_password_inputs = page.locator(
            "input[type='password']:visible, input[name='passwd']:visible, "
            "input[autocomplete='current-password']:visible"
        )
        if not await remaining_password_inputs.count():
            if "ppsecure/post.srf" in current_url:
                await wait_for_timeout_with_stop(
                    page,
                    500,
                    pause_controller,
                    "Microsoft recovery-email password relay",
                )
                continue
            log(f"Microsoft recovery-email password reauthentication completed for {account.email}.")
            return
        await wait_for_timeout_with_stop(page, 500, pause_controller, "Microsoft recovery-email password reauthentication")
    raise TimeoutError(
        f"Microsoft recovery-email password reauthentication did not leave the password page for {account.email}."
    )


async def detect_outlook_blocking_page(page: Page) -> tuple[str, str] | None:
    try:
        url = page.url
    except Exception:
        url = ""
    try:
        body = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        body = ""
    return classify_outlook_page(url, body)


async def handle_outlook_blocking_page(
    page: Page,
    account_label: str,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
) -> bool:
    state = await detect_outlook_blocking_page(page)
    if not state:
        return False
    label, preview = state
    message = (
        f"{account_label} 遇到 Outlook 页面需要人工处理：{label}。\n"
        f"页面摘要：{preview or '无可读取文本'}\n"
        "请在对应浏览器窗口处理到 Outlook 收件箱、Rules/规则页或 Forwarding/转发页可见，然后回到 Web UI 点击“确认完成，继续流程”。"
    )
    screenshot_path = Path("errors") / f"outlook_blocking_{safe_artifact_name(account_label)}.png"
    await wait_for_manual_continue(
        message,
        log,
        pause_controller,
        on_manual_wait,
        on_manual_resume,
        page=page,
        screenshot_path=screenshot_path,
        screenshot_label=f"Outlook blocking page for {account_label}",
    )
    return True


async def submit_security_email_or_record_failure(
    page: Page,
    security_email: str,
    account_email: str,
    log,
    on_security_mailbox_blocked=lambda email, reason: None,
    pause_controller=None,
) -> float:
    submitted = await submit_security_email(page, security_email, pause_controller)
    if not submitted:
        log(f"Microsoft security verification did not expose a security-email input for {account_email}.")
        return False
    failure = ""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        failure = await detect_security_email_delivery_failure(page)
        if failure:
            break
        await wait_for_timeout_with_stop(page, 1000, pause_controller, "security mailbox delivery failure check")
    if not failure:
        return time.time()
    reason = (
        f"Microsoft rejected security-email code delivery after submitting full address "
        f"for target {account_email}: {failure}"
    )
    log(f"Security mailbox marked limited/risk-controlled: {security_email}. {reason}")
    on_security_mailbox_blocked(security_email, reason)
    raise RuntimeError(
        f"安全邮箱 {security_email} 收码受限，Microsoft 提交完整邮箱地址后返回失败。已持久化记录并从安全邮箱池移除，请补充可用安全邮箱或手动完成验证。"
    )


async def resend_microsoft_security_code(page: Page, pause_controller=None) -> bool:
    """Request a fresh Microsoft code without leaving the current account flow."""
    resent = await click_first(
        page,
        [
            r"Send.*code", r"Get a new code", r"Didn't get a code",
            r"发送.*代码", r"获取新代码", r"没有收到代码", r"重新发送",
        ],
        timeout_ms=3000,
    )
    if resent:
        await wait_for_timeout_with_stop(
            page,
            1500,
            pause_controller,
            "Microsoft security-code resend",
        )
    return resent


async def try_change_microsoft_password(
    page: Page,
    current_password: str,
    new_password: str,
    log,
    pause_controller=None,
) -> bool:
    await page.goto("https://account.live.com/password/Change", wait_until="domcontentloaded")
    await wait_for_timeout_with_stop(page, 5000, pause_controller, "Microsoft password change page load")
    await cancel_passkey_prompt(page, log, timeout_seconds=8, pause_controller=pause_controller)

    password_inputs = page.locator("input[type='password']")
    try:
        count = await password_inputs.count()
        if count < 3:
            state = await detect_microsoft_blocking_page(page)
            if state:
                log(f"Microsoft password change page requires handling before form fill: {state[0]}; {state[1]}")
            else:
                log(f"Microsoft password change form not ready; password_input_count={count}; {await page_state_summary(page)}")
            return False
        await password_inputs.nth(0).fill(current_password, timeout=5000)
        await password_inputs.nth(1).fill(new_password, timeout=5000)
        await password_inputs.nth(2).fill(new_password, timeout=5000)
    except Exception as exc:
        log(f"Microsoft password change form fill failed: {exc}; {await page_state_summary(page)}")
        return False

    for pattern in [r"^保存$", r"^Save$", r"^下一步$", r"^Next$", r"^提交$", r"^Submit$"]:
        if await click_first(page, [pattern], timeout_ms=1500):
            await wait_for_timeout_with_stop(page, 5000, pause_controller, "Microsoft password change submit")
            log("Password change form submitted; waiting for manual confirmation before writing Feishu.")
            return True
    log(f"Microsoft password change submit button not found; {await page_state_summary(page)}")
    return False


async def run_mail_security_maintenance(
    page: Page,
    security_page: Page | None,
    args,
    account: SteamAccount,
    feishu_config: dict[str, Any],
    spreadsheet_token: str,
    sheet_id: str,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
    on_security_mailbox_blocked=lambda email, reason: None,
    change_password: bool = True,
    security_binding_completed: bool = False,
) -> None:
    bind_email = (account.backup_security_email or (getattr(args, "bind_security_email", "") or "")).strip()

    if bind_email and not security_binding_completed:
        await page.goto("https://account.live.com/proofs/manage/additional", wait_until="domcontentloaded")
        await wait_for_timeout_with_stop(page, 5000, pause_controller, "Microsoft security binding page load")
        await cancel_passkey_prompt(page, log, timeout_seconds=8, pause_controller=pause_controller)
        await ensure_microsoft_password_reauthenticated(page, account, log, pause_controller)
        security_binding_completed = bool(
            security_page
            and await try_complete_security_email_binding(
                page,
                security_page,
                account,
                bind_email,
                args,
                log,
                pause_controller,
                on_manual_wait,
                on_manual_resume,
                on_security_mailbox_blocked,
            )
        )
        if not security_binding_completed:
            await wait_for_manual_continue(
                f"请在目标邮箱账号 {account.email} 的 Microsoft 页面完成绑定安全邮箱：{bind_email}。完成后点击 Web UI 的“已完成人工操作 / 继续”。",
                log,
                pause_controller,
                on_manual_wait,
                on_manual_resume,
                page=page,
                screenshot_path=Path(args.error_dir) / f"row_{account.row_number}_{account.steam_username}_bind_security.png",
                screenshot_label="Microsoft security binding",
            )
        else:
            # Code submission can finish on login.srf/privacy-notice and
            # only then settle on the security-management page.  Give the
            # post-login prompt handler one bounded pass before writeback.
            for security_page_attempt in range(2):
                await page.goto("https://account.live.com/proofs/manage/additional", wait_until="domcontentloaded")
                await wait_for_timeout_with_stop(
                    page,
                    3000,
                    pause_controller,
                    "Microsoft security binding verification page",
                )
                await handle_microsoft_post_login_prompts(
                    page,
                    log,
                    pause_controller=pause_controller,
                    on_manual_wait=on_manual_wait,
                    on_manual_resume=on_manual_resume,
                )
                current_url = (page.url or "").casefold()
                if "account.live.com/proofs/manage" in current_url:
                    break
                if security_page_attempt == 0:
                    log(
                        "Microsoft security-management page has not settled after code submission; "
                        "reloading it once after post-login prompt handling."
                    )
        await ensure_security_binding_ready_for_writeback(page, bind_email, log)
    if bind_email:
        ensure_backup_security_email_header(args, feishu_config, spreadsheet_token, sheet_id)
        write_sheet_verified(
            feishu_config,
            spreadsheet_token,
            sheet_id,
            f"{BACKUP_SECURITY_EMAIL_COL}{account.row_number}:{BACKUP_SECURITY_EMAIL_COL}{account.row_number}",
            [[bind_email]],
            use_uat=args.feishu_auth == "user",
            package_root=Path(args.lark_mcp_root),
        )
        log(f"Wrote backup security email to row {account.row_number} column {BACKUP_SECURITY_EMAIL_COL}: {bind_email}")

    if not change_password:
        log(f"Skipped mailbox password change for row {account.row_number}; current task only binds security email.")
        return

    new_password = (getattr(args, "new_email_password", "") or "").strip() or generate_email_password()
    submitted = await try_change_microsoft_password(page, account.email_password, new_password, log, pause_controller)
    if submitted:
        message = (
            f"请确认 {account.email} 的邮箱密码已成功改为：{new_password}。"
            "确认后点击“已完成人工操作 / 继续”，程序才会写回飞书 D 列。"
        )
    else:
        log(f"Password change requires manual completion for row {account.row_number}: {await page_state_summary(page)}")
        message = (
            f"请手动打开/停留在 Microsoft 改密页面，把 {account.email} 的邮箱密码改为：{new_password}。"
            "改密成功后点击“已完成人工操作 / 继续”，程序才会写回飞书 D 列。"
        )
    await wait_for_manual_continue(
        message,
        log,
        pause_controller,
        on_manual_wait,
        on_manual_resume,
        page=page,
        screenshot_path=Path(args.error_dir) / f"row_{account.row_number}_{account.steam_username}_password_change.png",
        screenshot_label="Microsoft password change",
    )
    await ensure_password_change_ready_for_writeback(page, log)

    account.email_password = new_password
    write_sheet_verified(
        feishu_config,
        spreadsheet_token,
        sheet_id,
        f"{EMAIL_PASSWORD_COL}{account.row_number}:{EMAIL_PASSWORD_COL}{account.row_number}",
        [[new_password]],
        use_uat=args.feishu_auth == "user",
        package_root=Path(args.lark_mcp_root),
    )
    log(f"Wrote new email password to row {account.row_number} column {EMAIL_PASSWORD_COL}.")


def write_forward_rule_status(
    args,
    feishu_config: dict[str, Any],
    spreadsheet_token: str,
    sheet_id: str,
    row_number: int,
    status: str,
) -> None:
    ensure_forward_rule_header(args, feishu_config, spreadsheet_token, sheet_id)
    write_sheet_verified(
        feishu_config,
        spreadsheet_token,
        sheet_id,
        f"{FORWARD_RULE_COL}{row_number}:{FORWARD_RULE_COL}{row_number}",
        [[status]],
        use_uat=args.feishu_auth == "user",
        package_root=Path(args.lark_mcp_root),
    )


async def open_outlook_rules_settings(
    page: Page,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
) -> None:
    candidate_urls = [
        "https://outlook.live.com/mail/0/options/mail/rules",
        "https://outlook.live.com/mail/options/mail/rules",
        "https://outlook.live.com/mail/0/options/mail/forwarding",
        "https://outlook.live.com/mail/options/mail/forwarding",
    ]
    for url in candidate_urls:
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await wait_for_timeout_with_stop(page, 5000, pause_controller, "Outlook rules settings URL load")
            if await handle_microsoft_blocking_page(
                page,
                "Outlook 转发规则设置页",
                log,
                pause_controller,
                on_manual_wait,
                on_manual_resume,
            ):
                continue
            if await handle_outlook_blocking_page(
                page,
                "Outlook 转发规则设置页",
                log,
                pause_controller,
                on_manual_wait,
                on_manual_resume,
            ):
                continue
            body = await page.locator("body").inner_text(timeout=5000)
            if outlook_rules_surface_detected(url, body):
                log(f"Opened Outlook forwarding/rules settings: {url}")
                return
            log(f"Outlook forwarding/rules URL did not show expected markers: {await page_state_summary(page)}")
        except Exception as exc:
            log(f"Could not open rules URL {url}: {exc}")

    await page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
    await wait_for_timeout_with_stop(page, 5000, pause_controller, "Outlook mailbox load before settings fallback")
    await handle_microsoft_blocking_page(
        page,
        "Outlook 转发规则设置入口",
        log,
        pause_controller,
        on_manual_wait,
        on_manual_resume,
    )
    await handle_outlook_blocking_page(
        page,
        "Outlook 转发规则设置入口",
        log,
        pause_controller,
        on_manual_wait,
        on_manual_resume,
    )
    log(f"Falling back to Outlook settings menu for rules page: {await page_state_summary(page)}")
    for selector in [
        "button[aria-label*='Settings' i]",
        "button[aria-label*='设置' i]",
        "[role=button][aria-label*='Settings' i]",
        "[role=button][aria-label*='设置' i]",
    ]:
        try:
            await page.locator(selector).first.click(timeout=2500)
            await wait_for_timeout_with_stop(page, 2500, pause_controller, "Outlook settings menu open")
            break
        except Exception:
            pass
    await click_first(page, [r"View all Outlook settings", r"查看所有 Outlook 设置", r"查看全部 Outlook 设置"], timeout_ms=2500)
    await click_first(page, [r"^Mail$", r"^邮件$"], timeout_ms=1500)
    if not await click_first(page, [r"^Rules$", r"^规则$"], timeout_ms=1500):
        await click_first(page, [r"^Forwarding$", r"^转发$"], timeout_ms=1500)
    await wait_for_timeout_with_stop(page, 3000, pause_controller, "Outlook rules settings panel open")
    final_url = page.url
    final_body = await page.locator("body").inner_text(timeout=5000)
    if not outlook_rules_surface_detected(final_url, final_body):
        await wait_for_manual_continue(
            "Outlook 规则/转发页未被自动识别，已暂停当前账号供人工/ChatGPT 处理。"
            "请把页面处理到 Rules/规则或 Forwarding/转发设置页，再点击“确认完成，继续”。"
            "程序只复核一次，仍不匹配就停止当前批次，不会反复重试。",
            log,
            pause_controller,
            on_manual_wait,
            on_manual_resume,
            page=page,
            screenshot_path=Path("errors") / "outlook_rules_unhandled.png",
            screenshot_label="Outlook rules unhandled page",
        )
        final_url = page.url
        final_body = await page.locator("body").inner_text(timeout=5000)
        if not outlook_rules_surface_detected(final_url, final_body):
            raise RuntimeError(
                "Outlook Rules/Forwarding page remained unhandled after one manual intervention; "
                f"{await page_state_summary(page)}"
            )
    log(f"Opened Outlook settings panel and confirmed Rules/Forwarding page: {await page_state_summary(page)}")


async def ensure_outlook_forward_rule_ready_for_writeback(
    page: Page,
    forward_to: str,
    log,
    from_email: str = "",
    rule_name: str = "",
    enabled_confirmed: bool = False,
) -> None:
    try:
        url = page.url
    except Exception:
        url = ""
    try:
        body = await outlook_rule_state_text(page)
    except Exception:
        body = ""
    lower = body.casefold()
    settings_url_markers = [
        "/options/mail/rules",
        "/options/mail/forwarding",
        "/mail/options/mail/rules",
        "/mail/options/mail/forwarding",
    ]
    strong_page_markers = [
        "enable forwarding",
        "add new rule",
        "redirect to",
        "forward to",
        "mail rules",
        "inbox rules",
        "rules settings",
        "forwarding settings",
        "启用转发",
        "添加新规则",
        "新建规则",
        "重定向到",
        "转发到",
        "邮件规则",
        "收件箱规则",
        "规则设置",
        "转发设置",
    ]
    page_markers = [
        "rules",
        "forwarding",
        "enable forwarding",
        "add new rule",
        "redirect to",
        "forward to",
        "规则",
        "转发",
        "启用转发",
        "添加新规则",
        "新建规则",
        "重定向到",
    ]
    url_lower = url.casefold()
    has_settings_url = any(marker in url_lower for marker in settings_url_markers)
    has_page_marker = any(marker.casefold() in lower for marker in page_markers)
    has_strong_marker = any(marker.casefold() in lower for marker in strong_page_markers)
    if "outlook.live.com" not in url_lower or not has_page_marker or not (has_settings_url or has_strong_marker):
        raise RuntimeError(
            "转发规则不能写回：当前页面不是 Outlook Rules/Forwarding 设置页。"
            f" 页面摘要：{await page_state_summary(page)}"
        )
    if forward_to and forward_to.casefold() not in lower:
        raise RuntimeError(
            f"转发规则不能写回：当前页面未看到转发目标 {forward_to}。"
            "请确认规则或全量转发已保存并显示在页面上，再点击继续。"
            f" 页面摘要：{await page_state_summary(page)}"
        )
    if from_email and from_email.casefold() not in lower:
        raise RuntimeError(
            f"转发规则不能写回：当前规则未看到 From 发件人 {from_email}。"
            f" 页面摘要：{await page_state_summary(page)}"
        )
    if rule_name and rule_name.casefold() not in lower:
        raise RuntimeError(
            f"转发规则不能写回：当前规则未看到规则名 {rule_name}。"
            f" 页面摘要：{await page_state_summary(page)}"
        )
    if from_email or rule_name:
        scope = await outlook_rule_scope(page)
        if not await outlook_stop_processing_checked(scope):
            raise RuntimeError("转发规则不能写回：Stop processing more rules 未勾选。")
    saved_markers = [
        "enabled",
        "saved",
        "forwarding enabled",
        "forwarding is on",
        "turn off forwarding",
        "disable forwarding",
        "stop forwarding",
        "rule enabled",
        "rule is on",
        "rule on",
        "rule is turned on",
        "已启用",
        "已开启",
        "已保存",
        "转发已启用",
        "关闭转发",
        "禁用转发",
        "停止转发",
        "规则已启用",
    ]
    if forward_to and not enabled_confirmed and not any(marker.casefold() in lower for marker in saved_markers):
        raise RuntimeError(
            f"转发规则不能写回：页面看到转发目标 {forward_to}，但没有看到规则已启用/已保存状态。"
            "请点击保存，确认规则或全量转发显示为启用后再继续。"
            f" 页面摘要：{await page_state_summary(page)}"
        )
    log(f"Outlook forwarding/rules writeback check passed for target {forward_to}.")


async def outlook_rule_scope(page: Page):
    dialogs = page.locator("[role=dialog]:visible, [aria-modal=true]:visible")
    if await dialogs.count():
        return dialogs.last
    return page


async def click_outlook_rule_control(scope, patterns: list[str], timeout_ms: int = 4000) -> bool:
    combined = re.compile("(?:" + ")|(?:".join(patterns) + ")", re.I)
    # Fluent UI renders Outlook's "Select a condition/action" controls as
    # comboboxes, not buttons.  Keep all observed interactive roles here so the
    # same helper works before and after the dropdown opens.
    for role in ["button", "combobox", "option", "menuitem"]:
        try:
            await scope.get_by_role(role, name=combined).first.click(timeout=timeout_ms)
            return True
        except Exception:
            pass
    try:
        await scope.get_by_text(combined).first.click(timeout=timeout_ms)
        return True
    except Exception:
        return False


async def click_outlook_dom_control(page: Page, labels: list[str], log=print) -> bool:
    """Click a visible Fluent control even when Outlook omits usable ARIA roles.

    Current Outlook builds sometimes render the visible dropdown label inside a
    focusable div without exposing it as a Playwright button/combobox.  Search
    every frame for an exact visible label/value/aria-label and click its nearest
    interactive ancestor.  Exact matching avoids confusing section headings such
    as "Add a condition" with the real "Select a condition" control.
    """
    script = """
    (labels) => {
      const normalized = labels.map(value => String(value).trim().toLocaleLowerCase());
      const visible = element => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' &&
          Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
      };
      const textOf = element => [
        element.getAttribute && element.getAttribute('aria-label'),
        element.getAttribute && element.getAttribute('title'),
        'value' in element ? element.value : '',
        element.innerText,
        element.textContent,
      ].map(value => String(value || '').trim().toLocaleLowerCase());
      const nodes = Array.from(document.querySelectorAll('*'));
      for (const node of nodes) {
        if (!visible(node) || !textOf(node).some(value => normalized.includes(value))) continue;
        const control = node.closest(
          "button,[role='button'],[role='combobox'],[role='option'],[role='menuitem']," +
          "[role='listbox'],[tabindex]:not([tabindex='-1']),[data-is-focusable='true']"
        ) || node;
        if (!visible(control)) continue;
        control.click();
        return {
          tag: control.tagName,
          role: control.getAttribute('role') || '',
          aria: control.getAttribute('aria-label') || '',
          text: String(control.innerText || control.textContent || '').trim().slice(0, 120),
        };
      }
      return null;
    }
    """
    for frame in page.frames:
        try:
            result = await frame.evaluate(script, labels)
            if result:
                log(f"Clicked Outlook Fluent control through DOM fallback: {result}")
                return True
        except Exception:
            pass
    return False


async def click_outlook_rule_dropdown_by_geometry(
    page: Page,
    name_input,
    vertical_offset: float,
    label: str,
    log=print,
) -> bool:
    """Last-resort click relative to the verified rule-name input.

    The Outlook rule editor has a stable vertical form layout, while its Fluent
    dropdowns sometimes expose neither text nor ARIA roles.  Anchor the click to
    the actual rule-name textbox instead of screen/window coordinates.
    """
    try:
        box = await name_input.bounding_box()
        if not box:
            return False
        x = box["x"] + min(120, box["width"] / 2)
        y = box["y"] + box["height"] / 2 + vertical_offset
        await page.mouse.click(x, y)
        log(f"Clicked Outlook {label} dropdown through rule-form geometry fallback.")
        return True
    except Exception:
        return False


async def outlook_rule_input_by_geometry(page: Page, name_anchor, vertical_offset: float):
    """Return the editable rule value control on a row.

    Outlook's recipient picker is not stable across deployments: it has been an
    ``input``, a ``div[role=textbox]``, and a contenteditable Fluent control.
    Search all of those shapes and choose the control nearest the expected row.
    """
    try:
        anchor_box = await name_anchor.bounding_box()
    except Exception:
        anchor_box = None
    if not anchor_box:
        return None
    target_y = anchor_box["y"] + anchor_box["height"] / 2 + vertical_offset
    minimum_x = anchor_box["x"] + 180
    inputs = page.locator(
        "input:visible:not([type=checkbox]):not([type=radio]), "
        "textarea:visible, [role='textbox']:visible, [contenteditable='true']:visible"
    )
    best = None
    best_distance = 10_000.0
    for index in range(min(await inputs.count(), 30)):
        candidate = inputs.nth(index)
        try:
            box = await candidate.bounding_box()
        except Exception:
            box = None
        if not box or box["x"] < minimum_x:
            continue
        distance = abs((box["y"] + box["height"] / 2) - target_y)
        if distance < 45 and distance < best_distance:
            best = candidate
            best_distance = distance
    return best


async def wait_for_outlook_rule_input(
    page: Page,
    name_anchor,
    vertical_offset: float,
    pause_controller=None,
):
    """Wait for Fluent recipient picker to render after selecting a rule type."""
    for _ in range(12):
        control = await outlook_rule_input_by_geometry(page, name_anchor, vertical_offset)
        if control is not None:
            return control
        await wait_for_timeout_with_stop(page, 250, pause_controller, "Outlook rule value editor render")
    return None


async def fill_outlook_rule_input(
    page: Page,
    control,
    value: str,
    pause_controller=None,
    log=print,
) -> None:
    """Fill a Fluent recipient picker and accept its address suggestion."""
    try:
        await control.fill(value, timeout=4000)
    except Exception:
        await control.click(timeout=3000)
        await control.press("Control+A")
        await control.press("Backspace")
        await control.type(value, delay=40)
    # Outlook renders "Use this address: ..." asynchronously. Pressing Enter
    # immediately after fill leaves plain text in the picker and later shows
    # "Please add a sender". Wait for and click the real suggestion first.
    await wait_for_timeout_with_stop(page, 700, pause_controller, "Outlook recipient suggestion render")
    escaped = re.escape(value)
    committed = await click_outlook_rule_control(
        page,
        [
            rf"^Use this address:\s*{escaped}$",
            rf"^使用此地址[:：]?\s*{escaped}$",
        ],
        timeout_ms=2500,
    )
    if committed:
        log(f"Committed Outlook rule recipient suggestion: {value}")
    else:
        await control.press("Enter")
        log(f"Committed Outlook rule recipient with Enter fallback: {value}")
    await wait_for_timeout_with_stop(page, 700, pause_controller, "Outlook recipient suggestion commit")


async def outlook_rule_state_text(page: Page) -> str:
    values: list[str] = []
    try:
        values.append(await page.locator("body").inner_text(timeout=5000))
    except Exception:
        pass
    inputs = page.locator("input:visible")
    try:
        for index in range(min(await inputs.count(), 30)):
            item = inputs.nth(index)
            for attribute in ["value", "aria-label", "title"]:
                try:
                    value = await item.get_attribute(attribute, timeout=500)
                    if value:
                        values.append(value)
                except Exception:
                    pass
    except Exception:
        pass
    return "\n".join(values)


async def open_existing_outlook_rule(page: Page, rule_name: str, pause_controller=None) -> bool:
    try:
        rule = page.get_by_text(re.compile(rf"^{re.escape(rule_name)}$", re.I)).first
        if not await rule.count():
            return False
        await rule.click(timeout=4000)
        await wait_for_timeout_with_stop(page, 1500, pause_controller, "Outlook existing rule open")
        return True
    except Exception:
        return False


async def outlook_rule_enabled_in_list(page: Page, rule_name: str) -> bool:
    try:
        rule = page.get_by_text(re.compile(rf"^{re.escape(rule_name)}$", re.I)).first
        if not await rule.count():
            return False
        row = rule.locator(
            "xpath=ancestor::*[.//*[@role='switch'] or .//*[@role='checkbox'] or .//button[@aria-checked or @aria-pressed]][1]"
        )
        controls = row.locator("[role=switch], [role=checkbox], button[aria-checked], button[aria-pressed]")
        for index in range(min(await controls.count(), 6)):
            control = controls.nth(index)
            for attribute in ["aria-checked", "aria-pressed"]:
                value = (await control.get_attribute(attribute, timeout=500) or "").casefold()
                if value == "true":
                    return True
            try:
                if await control.is_checked():
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


async def ensure_outlook_rule_enabled_in_list(page: Page, rule_name: str, pause_controller=None) -> bool:
    if await outlook_rule_enabled_in_list(page, rule_name):
        return True
    try:
        rule = page.get_by_text(re.compile(rf"^{re.escape(rule_name)}$", re.I)).first
        row = rule.locator(
            "xpath=ancestor::*[.//*[@role='switch'] or .//*[@role='checkbox'] or .//button[@aria-checked or @aria-pressed]][1]"
        )
        control = row.locator("[role=switch], [role=checkbox], button[aria-checked], button[aria-pressed]").first
        if await control.count():
            await control.click(timeout=3000)
            await wait_for_timeout_with_stop(page, 1000, pause_controller, "Outlook rule enable")
    except Exception:
        return False
    return await outlook_rule_enabled_in_list(page, rule_name)


async def outlook_stop_processing_checked(scope) -> bool:
    label_pattern = re.compile(r"Stop processing more rules|停止处理更多规则|不再处理更多规则", re.I)
    try:
        checkbox = scope.get_by_role("checkbox", name=label_pattern).first
        if await checkbox.count():
            return await checkbox.is_checked()
    except Exception:
        pass
    try:
        label = scope.get_by_text(label_pattern).first
        container = label.locator("xpath=ancestor::*[.//input[@type='checkbox']][1]")
        checkbox = container.locator("input[type=checkbox]").first
        return bool(await checkbox.count() and await checkbox.is_checked())
    except Exception:
        return False


async def ensure_stop_processing_checked(scope) -> bool:
    label_pattern = re.compile(r"Stop processing more rules|停止处理更多规则|不再处理更多规则", re.I)
    try:
        checkbox = scope.get_by_role("checkbox", name=label_pattern).first
        if await checkbox.count() and not await checkbox.is_checked():
            await checkbox.check(timeout=3000)
        if await outlook_stop_processing_checked(scope):
            return True
    except Exception:
        pass
    # Some Outlook builds expose the visible text as a label without wiring
    # the accessible checkbox name. Clicking that exact label is safe and
    # handles the page variant used by standalone mailboxes.
    try:
        label = scope.get_by_text(label_pattern).first
        if await label.count():
            await label.click(timeout=3000)
            await asyncio.sleep(0.3)
            if await outlook_stop_processing_checked(scope):
                return True
    except Exception:
        pass
    # Final narrow fallback: only act when the current editor exposes exactly
    # one visible native checkbox, never on an arbitrary input/search field.
    try:
        native = scope.locator("input[type=checkbox]:visible")
        if await native.count() == 1:
            checkbox = native.first
            if not await checkbox.is_checked():
                await checkbox.check(timeout=3000)
            if await checkbox.is_checked():
                return True
    except Exception:
        pass
    return await outlook_stop_processing_checked(scope)


async def outlook_forward_verification_required(page: Page) -> bool:
    try:
        body = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    lower = body.casefold()
    return (
        "sign in and verify your account to create a forwarding rule" in lower
        or "sign in and verify account to create a forwarding rule" in lower
        or "登录并验证你的帐户" in body
        or "登录并验证你的账户" in body
        or "登录并验证帐户" in body
        or "登录并验证账户" in body
    )


async def wait_for_optional_page(milliseconds: int, pause_controller=None, step: str = "optional page wait") -> None:
    """Wait without touching a popup that may close itself during OAuth redirect."""
    deadline = time.monotonic() + max(0, milliseconds) / 1000
    while time.monotonic() < deadline:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError(f"Stopped during {step}.")
        await asyncio.sleep(min(0.25, max(0.01, deadline - time.monotonic())))


async def wait_for_outlook_forward_verification_commit(
    rule_page: Page,
    verification_page: Page,
    log,
    pause_controller=None,
    timeout_ms: int = 30000,
) -> bool:
    """Wait until the rule editor, not the OAuth popup, confirms verification.

    Microsoft's forwarding reauthentication popup can redirect back to Outlook
    and then close itself before the opener refreshes.  Redirecting to Outlook or
    closing the popup is therefore not proof that verification was committed.
    The durable success signal is the verification warning disappearing from the
    original rule editor.
    """
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    redirect_logged = False
    close_logged = False
    refresh_attempted = False
    while time.monotonic() < deadline:
        if not await outlook_forward_verification_required(rule_page):
            log("Outlook forwarding verification committed in the rule editor.")
            return True
        if verification_page is not rule_page:
            if verification_page.is_closed():
                if not close_logged:
                    log("Outlook forwarding verification popup closed; waiting for the rule editor to confirm completion.")
                    close_logged = True
                if not refresh_attempted and time.monotonic() + 2 < deadline:
                    refresh_attempted = True
                    try:
                        await rule_page.reload(wait_until="domcontentloaded")
                        await wait_for_optional_page(1800, pause_controller, "Outlook rule editor refresh after verification")
                        log("Refreshed Outlook rule editor after verification popup closed; rechecking commit state.")
                    except Exception as exc:
                        log(f"Outlook rule editor refresh after verification failed: {text_snippet(str(exc), 180)}")
            else:
                try:
                    verification_url = (verification_page.url or "").casefold()
                except Exception:
                    verification_url = ""
                if "outlook.live.com" in verification_url and not redirect_logged:
                    log("Outlook forwarding verification popup returned to Outlook; waiting for the rule editor to refresh.")
                    redirect_logged = True
        await wait_for_optional_page(250, pause_controller, "Outlook forwarding verification commit")
    return not await outlook_forward_verification_required(rule_page)


async def complete_outlook_forward_reauthentication(
    rule_page: Page,
    verification_page: Page,
    security_page: Page,
    account: SteamAccount,
    bind_email: str,
    args,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
    on_security_mailbox_blocked=lambda email, reason: None,
) -> bool:
    """Complete Outlook's forwarding-specific OAuth reauthentication popup.

    This popup is not the recovery-email binding page.  It normally starts by
    asking for the already logged-in target mailbox password and may then ask
    for a code sent to the bound security mailbox.
    """
    if verification_page.is_closed():
        return await wait_for_outlook_forward_verification_commit(
            rule_page, verification_page, log, pause_controller, timeout_ms=5000
        )
    try:
        body = await verification_page.locator("body").inner_text(timeout=3000)
    except Exception:
        body = ""
    url = (verification_page.url or "").casefold()
    password_input = verification_page.locator(
        "input[type='password']:visible, input[name='passwd']:visible, "
        "input[autocomplete='current-password']:visible"
    ).first
    if "login.live.com" not in url and not await password_input.count():
        if "outlook.live.com" in url:
            return await wait_for_outlook_forward_verification_commit(
                rule_page, verification_page, log, pause_controller
            )
        return False

    if await password_input.count():
        log(f"Outlook forwarding reauthentication detected for {account.email}; submitting target mailbox password.")
        await password_input.fill(account.email_password, timeout=5000)
        submit = verification_page.locator("button[type='submit']:visible, input[type='submit']:visible").first
        if await submit.count():
            await submit.click(timeout=4000)
        elif not await click_first(verification_page, [r"^Next$", r"^下一步$", r"^Sign in$", r"^登录$"], timeout_ms=4000):
            await password_input.press("Enter")
        transition_deadline = time.monotonic() + 30
        while time.monotonic() < transition_deadline:
            if not await outlook_forward_verification_required(rule_page):
                log("Outlook forwarding password reauthentication completed; verification popup closed.")
                return True
            if verification_page.is_closed():
                return await wait_for_outlook_forward_verification_commit(
                    rule_page, verification_page, log, pause_controller, timeout_ms=5000
                )
            current_url = (verification_page.url or "").casefold()
            if "outlook.live.com" in current_url:
                # ppsecure can briefly report an Outlook relay URL before the
                # actual recovery-method page finishes loading. Do not treat
                # that transient redirect as a successful reauthentication;
                # inspect the settled body first so the code/bind flow below
                # can handle pages such as “将代码发送到 …”.
                try:
                    settled_body = await verification_page.locator("body").inner_text(timeout=1500)
                except Exception:
                    settled_body = ""
                if settled_body.strip():
                    try:
                        if await security_challenge_visible(verification_page):
                            break
                    except Exception:
                        pass
                    log("Outlook forwarding password reauthentication returned to Outlook.")
                    return await wait_for_outlook_forward_verification_commit(
                        rule_page, verification_page, log, pause_controller
                    )
                await wait_for_optional_page(500, pause_controller, "Outlook forwarding relay settle")
                continue
            try:
                body = await verification_page.locator("body").inner_text(timeout=1500)
            except Exception:
                body = ""
            lower = body.casefold()
            if any(marker in lower for marker in ["incorrect password", "password is incorrect", "wrong password"]):
                raise RuntimeError(f"Outlook 转发规则重新验证失败：{account.email} 的邮箱密码不正确。")
            if any(marker in body for marker in ["密码不正确", "密码错误"]):
                raise RuntimeError(f"Outlook 转发规则重新验证失败：{account.email} 的邮箱密码不正确。")
            # ppsecure/post.srf is Microsoft's blank OAuth relay.  It often
            # closes itself 10-20 seconds after password submission.  Do not
            # mistake that transient empty document for a failed recovery page.
            if body.strip() and "ppsecure/post.srf" not in current_url:
                try:
                    if await security_challenge_visible(verification_page):
                        break
                except Exception:
                    pass
            await wait_for_optional_page(500, pause_controller, "Outlook forwarding OAuth relay")

    if not await outlook_forward_verification_required(rule_page):
        return True
    if verification_page.is_closed():
        return await wait_for_outlook_forward_verification_commit(
            rule_page, verification_page, log, pause_controller, timeout_ms=5000
        )
    try:
        await maybe_handle_stay_signed_in(verification_page)
    except Exception:
        if not verification_page.is_closed():
            raise
    await wait_for_optional_page(1500, pause_controller, "Outlook forwarding reauthentication redirect")
    if not await outlook_forward_verification_required(rule_page):
        log("Outlook forwarding reauthentication completed after redirect.")
        return True
    if verification_page.is_closed():
        return await wait_for_outlook_forward_verification_commit(
            rule_page, verification_page, log, pause_controller, timeout_ms=5000
        )
    if "outlook.live.com" in (verification_page.url or "").casefold():
        return await wait_for_outlook_forward_verification_commit(
            rule_page, verification_page, log, pause_controller
        )

    # Some accounts require the bound recovery mailbox in addition to the
    # password. Select that method, provide the full address when requested,
    # then read and submit the code from the already verified security window.
    try:
        challenge = await security_challenge_visible(verification_page)
    except Exception:
        challenge = False
    if not challenge:
        challenge = await click_first(
            verification_page,
            [r"Send.*code", r"Email.*code", r"将代码发送到", r"发送.*代码", r"电子邮件.*代码"],
            timeout_ms=2000,
        )
        if challenge:
            await wait_for_timeout_with_stop(verification_page, 2000, pause_controller, "Outlook forwarding code method")
    if challenge:
        from outlook_batch_login import collect_microsoft_security_mail_ids

        existing_security_mail_ids = await collect_microsoft_security_mail_ids(security_page)
        log(
            f"Captured {len(existing_security_mail_ids)} existing Microsoft security-mail IDs "
            f"before Outlook forwarding reauthentication for {account.email}."
        )
        code_requested_at = await request_microsoft_security_email_code(
            verification_page,
            bind_email,
            account.email,
            log,
            pause_controller,
            allow_redirect_without_code=True,
            on_security_mailbox_blocked=on_security_mailbox_blocked,
        )
        if not code_requested_at:
            return False
        accepted = False
        for code_attempt in range(1, 3):
            code = await get_microsoft_security_code_from_mailbox(
                security_page,
                account.email,
                args.microsoft_code_timeout,
                log,
                pause_controller,
                on_manual_wait,
                on_manual_resume,
                not_before_epoch=code_requested_at,
                exclude_message_ids=existing_security_mail_ids,
            )
            if not await outlook_forward_verification_required(rule_page):
                log("Outlook forwarding reauthentication completed while reading the security code.")
                return True
            if verification_page.is_closed():
                return await wait_for_outlook_forward_verification_commit(
                    rule_page, verification_page, log, pause_controller, timeout_ms=5000
                )
            try:
                accepted = await submit_microsoft_security_code_with_validation(
                    verification_page,
                    code,
                    account.email,
                    log,
                    pause_controller,
                )
            except RuntimeError as exc:
                if "security code input" in str(exc).casefold():
                    if not await outlook_forward_verification_required(rule_page):
                        log("Outlook forwarding reauthentication completed before security-code submission.")
                        return True
                    if verification_page.is_closed():
                        return await wait_for_outlook_forward_verification_commit(
                            rule_page, verification_page, log, pause_controller, timeout_ms=5000
                        )
                raise
            if accepted:
                break
            if code_attempt < 2 and not verification_page.is_closed():
                log(f"Microsoft security code attempt {code_attempt} did not complete; requesting one newer code.")
                from outlook_batch_login import collect_microsoft_security_mail_ids

                # Exclude the code just tried before requesting another one;
                # otherwise Outlook can surface the same unread message again.
                existing_security_mail_ids.update(
                    await collect_microsoft_security_mail_ids(security_page)
                )
                resent = await resend_microsoft_security_code(verification_page, pause_controller)
                if resent:
                    code_requested_at = time.time()
                else:
                    log(
                        "Microsoft returned to the verification-method page without a resend control; "
                        "restarting the email-delivery method once for this same account."
                    )
                    code_requested_at = await request_microsoft_security_email_code(
                        verification_page,
                        bind_email,
                        account.email,
                        log,
                        pause_controller,
                        allow_redirect_without_code=True,
                        on_security_mailbox_blocked=on_security_mailbox_blocked,
                    )
        if not accepted:
            raise RuntimeError(f"Outlook 转发规则重新验证安全码被拒绝：{account.email}。")
        await wait_for_optional_page(2000, pause_controller, "Outlook forwarding code redirect")
        return await wait_for_outlook_forward_verification_commit(
            rule_page, verification_page, log, pause_controller
        )

    return False


async def complete_outlook_forward_verification(
    rule_page: Page,
    security_page: Page | None,
    account: SteamAccount,
    args,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
    on_security_mailbox_blocked=lambda email, reason: None,
) -> None:
    if not await outlook_forward_verification_required(rule_page):
        return
    if not security_page or security_page.is_closed():
        raise RuntimeError("Outlook 创建转发规则要求验证账号，但对应安全邮箱窗口未保持登录。")
    bind_email = (account.backup_security_email or "").strip()
    if not bind_email:
        raise RuntimeError("Outlook 创建转发规则要求验证账号，但当前行没有可用安全邮箱。")

    context = rule_page.context
    pages_before = set(context.pages)
    if not await click_outlook_rule_control(rule_page, [r"^Sign in$", r"^登录$"], timeout_ms=5000):
        raise RuntimeError("Outlook 转发规则验证提示存在，但未找到 Sign in/登录按钮。")
    await wait_for_timeout_with_stop(rule_page, 2000, pause_controller, "Outlook forward-rule verification window")
    new_pages = [candidate for candidate in context.pages if candidate not in pages_before]
    verification_page = new_pages[-1] if new_pages else rule_page
    if verification_page is not rule_page:
        try:
            await verification_page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass

    completed = await complete_outlook_forward_reauthentication(
        rule_page,
        verification_page,
        security_page,
        account,
        bind_email,
        args,
        log,
        pause_controller,
        on_manual_wait,
        on_manual_resume,
        on_security_mailbox_blocked,
    )
    if not completed and not verification_page.is_closed():
        completed = await complete_recovery_email_setup(
            verification_page,
            security_page,
            account,
            bind_email,
            args,
            log,
            pause_controller,
            on_manual_wait,
            on_manual_resume,
            on_security_mailbox_blocked,
        )
    if completed:
        completed = await wait_for_outlook_forward_verification_commit(
            rule_page, verification_page, log, pause_controller, timeout_ms=10000
        )
    if not completed:
        raise RuntimeError(
            f"Outlook 转发规则验证窗口未能自动绑定/验证安全邮箱 {bind_email}；"
            f"{await page_state_summary(verification_page)}"
        )
    if verification_page is not rule_page and not verification_page.is_closed():
        try:
            await verification_page.close()
        except Exception:
            pass
    try:
        await rule_page.bring_to_front()
    except Exception:
        pass
    await wait_for_timeout_with_stop(rule_page, 1500, pause_controller, "Return to Outlook rule editor")
    log(f"Outlook forwarding-rule account verification completed with security email {bind_email}.")


async def create_or_update_outlook_forward_rule(
    page: Page,
    from_email: str,
    forward_to: str,
    log,
    pause_controller=None,
    rule_name: str = "forward",
    on_verification_required=None,
) -> bool:
    async def complete_verification_before_continuing() -> None:
        if not on_verification_required:
            raise RuntimeError("Outlook 创建转发规则要求再次登录验证账号。")
        await on_verification_required()
        if await outlook_forward_verification_required(page):
            raise RuntimeError(
                "Outlook 转发规则认证尚未完成：原规则编辑器仍显示 Sign in and verify your account。"
            )

    await dismiss_outlook_nonbusiness_popup(page, log, pause_controller)
    enabled_before_edit = await ensure_outlook_rule_enabled_in_list(page, rule_name, pause_controller)
    if await open_existing_outlook_rule(page, rule_name, pause_controller):
        existing = (await outlook_rule_state_text(page)).casefold()
        if from_email.casefold() in existing and forward_to.casefold() in existing:
            log(f"Outlook rule already exists with the requested From/Forward to values: {rule_name}")
            return enabled_before_edit
        # Existing rule has different values; edit it in place using the same form.
        log(f"Outlook rule {rule_name!r} exists but values differ; updating it.")
    else:
        if not await click_outlook_rule_control(
            page,
            [
                r"^Add rule$",
                r"^Add new rule$",
                r"^New rule$",
                r"^添加规则$",
                r"^添加新规则$",
                r"^新建规则$",
            ],
        ):
            raise RuntimeError("Outlook Rules 页面未找到 Add rule/添加规则按钮。")
        await wait_for_timeout_with_stop(page, 1200, pause_controller, "Outlook new rule form open")

    scope = await outlook_rule_scope(page)
    textboxes = scope.get_by_role("textbox")
    if not await textboxes.count():
        textboxes = scope.locator("input:visible:not([type=checkbox]):not([type=radio])")
    if not await textboxes.count():
        raise RuntimeError("Outlook 规则编辑器未找到规则名称输入框。")
    name_input = textboxes.first
    await name_input.fill(rule_name, timeout=4000)
    name_anchor = await name_input.element_handle()
    if not name_anchor:
        name_anchor = name_input

    state = (await outlook_rule_state_text(page)).casefold()
    if from_email.casefold() not in state:
        # The visible "Add a condition" text is a section heading in current
        # Outlook, while "Select a condition" is the actual dropdown.  Clicking
        # a combined text regexp can hit the heading first and do nothing.
        condition_geometry_used = False
        condition_opened = await click_outlook_rule_control(
            page,
            [r"^Select a condition$", r"^选择条件$"],
            timeout_ms=800,
        )
        if not condition_opened:
            condition_opened = await click_outlook_dom_control(
                page,
                ["Select a condition", "选择条件"],
                log,
            )
        if not condition_opened:
            condition_opened = await click_outlook_rule_dropdown_by_geometry(
                page,
                name_anchor,
                101,
                "condition",
                log,
            )
            condition_geometry_used = condition_opened
        if not condition_opened:
            raise RuntimeError("Outlook 规则编辑器未打开 Select a condition/选择条件下拉框。")
        await wait_for_timeout_with_stop(page, 500, pause_controller, "Outlook condition menu open")
        # Fluent UI renders dropdown options in a page-level portal outside the
        # rule dialog, so search the page rather than the dialog scope.
        from_selected = await click_outlook_rule_control(
            page,
            [r"^From$", r"^发件人$", r"^来自$"],
            timeout_ms=2500,
        )
        if not from_selected:
            from_selected = await click_outlook_dom_control(
                page,
                ["From", "发件人", "来自"],
                log,
            )
        if not from_selected:
            await page.keyboard.type("From", delay=60)
            await page.keyboard.press("Enter")
            await wait_for_timeout_with_stop(page, 700, pause_controller, "Outlook From keyboard selection")
            log("Selected Outlook From condition through keyboard fallback; validating recipient editor.")
        await wait_for_timeout_with_stop(page, 500, pause_controller, "Outlook From condition selected")
        from_input = await wait_for_outlook_rule_input(page, name_anchor, 101, pause_controller)
        if from_input is None:
            raise RuntimeError("Outlook 规则编辑器选择 From 后未显示可编辑的发件人输入框。")
        await fill_outlook_rule_input(page, from_input, from_email, pause_controller, log)
        await wait_for_timeout_with_stop(page, 500, pause_controller, "Outlook From address committed")

    if await dismiss_outlook_nonbusiness_popup(page, log, pause_controller):
        scope = await outlook_rule_scope(page)
    state = (await outlook_rule_state_text(page)).casefold()
    if forward_to.casefold() not in state:
        action_geometry_used = False
        action_opened = await click_outlook_rule_control(
            page,
            [r"^Select an action$", r"^选择操作$"],
            timeout_ms=800,
        )
        if not action_opened:
            await dismiss_outlook_nonbusiness_popup(page, log, pause_controller)
            action_opened = await click_outlook_dom_control(
                page,
                ["Select an action", "选择操作"],
                log,
            )
        if not action_opened:
            action_opened = await click_outlook_rule_dropdown_by_geometry(
                page,
                name_anchor,
                235,
                "action",
                log,
            )
            action_geometry_used = action_opened
        if not action_opened:
            raise RuntimeError("Outlook 规则编辑器未打开 Select an action/选择操作下拉框。")
        await wait_for_timeout_with_stop(page, 500, pause_controller, "Outlook action menu open")
        forward_selected = await click_outlook_rule_control(
            page,
            [r"^Forward to$", r"^转发到$"],
            timeout_ms=2500,
        )
        if not forward_selected:
            forward_selected = await click_outlook_dom_control(
                page,
                ["Forward to", "转发到"],
                log,
            )
        if not forward_selected:
            await page.keyboard.type("Forward to", delay=60)
            await page.keyboard.press("Enter")
            await wait_for_timeout_with_stop(page, 700, pause_controller, "Outlook Forward to keyboard selection")
            log("Selected Outlook Forward to action through keyboard fallback; validating recipient editor.")
        await wait_for_timeout_with_stop(page, 500, pause_controller, "Outlook Forward to action selected")
        if await outlook_forward_verification_required(page):
            await complete_verification_before_continuing()
            scope = await outlook_rule_scope(page)
        forward_input = await wait_for_outlook_rule_input(page, name_anchor, 235, pause_controller)
        if forward_input is None:
            raise RuntimeError("Outlook 规则编辑器选择 Forward to 后未显示可编辑的目标邮箱输入框。")
        await fill_outlook_rule_input(page, forward_input, forward_to, pause_controller, log)
        await wait_for_timeout_with_stop(page, 500, pause_controller, "Outlook Forward to address committed")

    if await dismiss_outlook_nonbusiness_popup(page, log, pause_controller):
        scope = await outlook_rule_scope(page)
    if not await ensure_stop_processing_checked(page):
        raise RuntimeError("Outlook 规则编辑器未能勾选 Stop processing more rules。")
    if await outlook_forward_verification_required(page):
        await complete_verification_before_continuing()
        scope = await outlook_rule_scope(page)
    if await dismiss_outlook_nonbusiness_popup(page, log, pause_controller):
        scope = await outlook_rule_scope(page)
    if not await click_outlook_rule_control(page, [r"^Save$", r"^保存$"], timeout_ms=5000):
        raise RuntimeError("Outlook 规则编辑器未找到 Save/保存按钮。")
    await wait_for_timeout_with_stop(page, 2500, pause_controller, "Outlook rule save")
    log(f"Outlook rule saved: name={rule_name}, from={from_email}, forward_to={forward_to}")
    return await outlook_rule_enabled_in_list(page, rule_name)


async def run_forward_rule_setup(
    page: Page,
    security_page: Page | None,
    args,
    account: SteamAccount,
    feishu_config: dict[str, Any],
    spreadsheet_token: str,
    sheet_id: str,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
    on_security_mailbox_blocked=lambda email, reason: None,
) -> None:
    from_email = (getattr(args, "forward_from_email", "") or "").strip() or "noreply@steampowered.com"
    forward_to = (getattr(args, "forward_to_email", "") or "").strip()
    if not forward_to:
        raise RuntimeError("任务3必须配置 Forward to 转发目标邮箱。")
    rule_name = "forward"
    await open_outlook_rules_settings(page, log, pause_controller, on_manual_wait, on_manual_resume)
    try:
        enabled = await create_or_update_outlook_forward_rule(
            page,
            from_email,
            forward_to,
            log,
            pause_controller,
            rule_name,
            on_verification_required=lambda: complete_outlook_forward_verification(
                page,
                security_page,
                account,
                args,
                log,
                pause_controller,
                on_manual_wait,
                on_manual_resume,
                on_security_mailbox_blocked,
            ),
        )
        state = (await outlook_rule_state_text(page)).casefold()
        if not all(value.casefold() in state for value in [rule_name, from_email, forward_to]):
            await open_existing_outlook_rule(page, rule_name, pause_controller)
        await ensure_outlook_forward_rule_ready_for_writeback(
            page,
            forward_to,
            log,
            from_email=from_email,
            rule_name=rule_name,
            enabled_confirmed=enabled,
        )
    except RuntimeError as exc:
        if (
            is_outlook_context_retryable_error(exc)
            and int(getattr(args, "_current_account_retry_count", 0) or 0) < 1
        ):
            log(
                f"Outlook rule operation failed for {account.email}; "
                "deferring the manual branch and retrying this account once with a rebuilt context."
            )
            raise
        retry_message = (
            f"自动创建或核验 Outlook 规则失败：{exc}\n"
            f"请按固定配置修正规则：名称={rule_name}；From={from_email}；Forward to={forward_to}；"
            "勾选 Stop processing more rules，并确认规则已启用。完成后点击“已完成人工操作 / 继续”。"
        )
        log(retry_message)
        await wait_for_manual_continue(
            retry_message,
            log,
            pause_controller,
            on_manual_wait,
            on_manual_resume,
            page=page,
            screenshot_path=Path(args.error_dir) / f"row_{account.row_number}_{account.steam_username}_forward_rule_retry.png",
            screenshot_label="Outlook forward rule retry",
        )
        enabled = await ensure_outlook_rule_enabled_in_list(page, rule_name, pause_controller)
        if not await open_existing_outlook_rule(page, rule_name, pause_controller):
            raise RuntimeError(f"人工处理后仍未找到规则 {rule_name}。")
        await ensure_outlook_forward_rule_ready_for_writeback(
            page,
            forward_to,
            log,
            from_email=from_email,
            rule_name=rule_name,
            enabled_confirmed=enabled,
        )
    if account.external_target or getattr(args, "external_only", False):
        log(f"External target forwarding verified; no Feishu row writeback for {account.email}.")
    else:
        write_forward_rule_status(args, feishu_config, spreadsheet_token, sheet_id, account.row_number, forward_to)
        log(f"Wrote forwarding target to row {account.row_number} column {FORWARD_RULE_COL}: {forward_to}")


async def verify_security_mailbox(page: Page, security_email: str) -> bool:
    expected = security_email.strip().casefold()
    try:
        url = page.url.casefold()
    except Exception:
        return False
    if "outlook.live.com" not in url:
        return False
    active = await read_active_outlook_mailbox(page)
    return active == expected


async def wait_for_security_mailbox(
    page: Page,
    security_email: str,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
    on_security_mailbox_blocked=lambda email, reason: None,
) -> None:
    log(f"Checking security mailbox login: {security_email}")
    next_log = 0.0
    while True:
        if pause_controller and pause_controller.should_stop():
            raise RuntimeError("Stopped while waiting for security mailbox login.")
        now = time.monotonic()
        if now >= next_log:
            log(f"Waiting for security mailbox {security_email}: {await page_state_summary(page)}")
            next_log = now + 10
        await handle_outlook_browser_prompts(page, log, timeout_seconds=2, pause_controller=pause_controller)
        delivery_failure = await detect_security_email_delivery_failure(page)
        if delivery_failure:
            reason = f"Security mailbox {security_email} cannot receive Microsoft verification code while logging in: {delivery_failure}"
            log(f"Security mailbox marked limited/risk-controlled during mailbox login: {security_email}. {reason}")
            on_security_mailbox_blocked(security_email, reason)
            raise RuntimeError(
                f"安全邮箱 {security_email} 自身登录验证收码受限，已持久化记录并从安全邮箱池移除，请补充新的安全邮箱。"
            )
        if await handle_microsoft_blocking_page(
            page,
            f"安全邮箱 {security_email}",
            log,
            pause_controller,
            on_manual_wait,
            on_manual_resume,
        ):
            continue
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
                "建议换一个已能正常进入收件箱的安全邮箱，或改用手动安全验证。"
                "处理完成并看到安全邮箱收件箱后，点击“已完成人工操作 / 继续”。"
            )
        else:
            message = f"请在窗口2登录/确认安全邮箱 {security_email}，完成后回到 localhost 控制台点击“已完成人工操作 / 继续”。"
        safe_email = re.sub(r"[^A-Za-z0-9_.-]+", "_", security_email)
        await wait_for_manual_continue(
            message,
            log,
            pause_controller,
            on_manual_wait,
            on_manual_resume,
            page=page,
            screenshot_path=Path("errors") / f"security_mailbox_{safe_email}.png",
            screenshot_label=f"Security mailbox {security_email}",
        )


async def ensure_security_mailbox_for_account(
    page: Page,
    account: SteamAccount,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
    on_security_mailbox_blocked=lambda email, reason: None,
) -> None:
    security_email = (account.backup_security_email or "").strip()
    if not security_email:
        return
    if await try_reuse_email_scoped_outlook_session(page, "security", security_email, log, pause_controller):
        return
    if await verify_security_mailbox(page, security_email):
        log(f"Security mailbox already active for this row: {security_email}")
        await persist_email_scoped_outlook_session(page.context, "security", security_email)
        return
    if account.security_email_password:
        log(f"Logging in security mailbox for this row: {security_email}")
        await logout_outlook(page, pause_controller)
        login_account = SteamAccount(
            row_number=0,
            steam_username="",
            steam_password="",
            email=security_email,
            email_password=account.security_email_password,
            steam_id="",
        )
        await fill_login(page, login_account, pause_controller)
        await handle_microsoft_post_login_prompts(
            page,
            log,
            pause_controller=pause_controller,
            on_manual_wait=on_manual_wait,
            on_manual_resume=on_manual_resume,
        )
        await redirect_microsoft_account_home_to_outlook(page, log, pause_controller)
        if await prefer_password_sign_in(page, pause_controller):
            log("Security mailbox login offered password sign-in; switched to password option.")
            await handle_microsoft_post_login_prompts(
                page,
                log,
                pause_controller=pause_controller,
                on_manual_wait=on_manual_wait,
                on_manual_resume=on_manual_resume,
            )
            await redirect_microsoft_account_home_to_outlook(page, log, pause_controller)
        await handle_microsoft_blocking_page(
            page,
            f"安全邮箱 {security_email}",
            log,
            pause_controller,
            on_manual_wait,
            on_manual_resume,
        )
        await wait_for_outlook_inbox(
            page,
            log,
            pause_controller=pause_controller,
            on_manual_wait=on_manual_wait,
            on_manual_resume=on_manual_resume,
            account_label=f"安全邮箱 {security_email}",
        )
        await confirm_fresh_outlook_login_identity(page, security_email, log)
        await persist_email_scoped_outlook_session(page.context, "security", security_email)
        return
    await wait_for_security_mailbox(
        page,
        security_email,
        log,
        pause_controller,
        on_manual_wait,
        on_manual_resume,
        on_security_mailbox_blocked,
    )


class PauseController:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._decision_event = threading.Event()
        self._stop = threading.Event()
        self._decision = None

    def wait(self) -> bool:
        self.reset_wait()
        while not self._stop.is_set():
            if self._event.wait(0.5):
                return True
        return False

    def reset_wait(self) -> None:
        self._event.clear()

    def wait_timeout(self, timeout_seconds: float) -> bool:
        return self._event.wait(timeout_seconds)

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


def wait_between_accounts(
    args,
    account: SteamAccount,
    index: int,
    total: int,
    row_result: str,
    log,
    pause_controller=None,
    on_manual_wait=lambda message: None,
    on_manual_resume=lambda message: None,
) -> bool:
    if not args.pause_between or index >= total:
        return True
    message = (
        f"第 {index}/{total} 个账号{row_result}：第 {account.row_number} 行 {account.email}\n"
        "请确认当前账号结果无误。要继续处理下一个账号，请点击“已完成人工操作 / 继续”；要中止请点击“停止”。"
    )
    log(message)
    if pause_controller:
        on_manual_wait(message)
        if not pause_controller.wait():
            log("Stopped while waiting.")
            return False
        on_manual_resume("Continue to next account.")
        return True
    answer = input("Press Enter for next account, or type q then Enter to stop: ").strip().lower()
    return answer != "q"


async def run_with_callbacks(args, callbacks: dict[str, Any] | None = None) -> None:
    callbacks = callbacks or {}
    log = callbacks.get("log", print)
    on_pending = callbacks.get("on_pending", lambda accounts: None)
    on_account_start = callbacks.get("on_account_start", lambda account, index, total: None)
    on_account_done = callbacks.get("on_account_done", lambda account, steam_id: None)
    on_account_error = callbacks.get("on_account_error", lambda account, exc: None)
    on_manual_wait = callbacks.get("on_manual_wait", lambda message: None)
    on_manual_resume = callbacks.get("on_manual_resume", lambda message: None)
    on_security_mailbox_blocked = callbacks.get("on_security_mailbox_blocked", lambda email, reason: None)
    pause_controller = callbacks.get("pause_controller")
    normalize_task_args(args)
    task_mode = getattr(args, "task_mode", "full")

    pending_bundle = getattr(args, "prefetched_pending_bundle", None)
    if pending_bundle:
        accounts = list(pending_bundle["accounts"])
        feishu_config = pending_bundle["feishu_config"]
        spreadsheet_token = pending_bundle["spreadsheet_token"]
        sheet_id = pending_bundle["sheet_id"]
        log(f"Using cached Feishu queue: {len(accounts)} accounts")
    else:
        accounts, feishu_config, spreadsheet_token, sheet_id = read_pending_accounts(args)
    if args.limit:
        accounts = accounts[: args.limit]
    if getattr(args, "external_only", False):
        if not hasattr(args, "security_pool_full_emails"):
            raise RuntimeError(
                "表外转发目标必须先读取飞书 J 列并计算安全邮箱容量，禁止绕过满额过滤。"
            )
        full_security_mailboxes = {
            str(email).strip().casefold()
            for email in (getattr(args, "security_pool_full_emails", []) or [])
        }
        over_limit = sorted(
            {
                account.backup_security_email.strip().casefold()
                for account in accounts
                if account.backup_security_email.strip().casefold() in full_security_mailboxes
            }
        )
        if over_limit:
            raise RuntimeError(
                "表外转发目标选择了已满的安全邮箱，已阻止运行：" + ", ".join(over_limit)
            )
    on_pending(accounts)
    if not accounts:
        log("No pending accounts for selected task mode.")
        return

    log(f"Pending accounts: {len(accounts)}")
    if args.list_pending:
        for account in accounts:
            log(f"row {account.row_number}: {account.steam_username} / {account.email}")
        return

    if task_mode == "forward_rule" and not getattr(args, "external_only", False):
        ensure_forward_rule_header(args, feishu_config, spreadsheet_token, sheet_id)

    steam_exe = None
    if args.enable_steam_login or args.enable_dota_check:
        steam_exe = find_steam_exe(args.steam_exe)
        log(f"Steam exe: {steam_exe}")
    if args.enable_dota_check and not args.enable_steam_login:
        log("Dota check requires Steam login; Dota check will be skipped because Steam login is disabled.")
    configured_security_pool = parse_security_mailbox_pool(getattr(args, "security_mailbox_pool", "") or "")
    if configured_security_pool:
        log(
            "Security mailbox window follows per-row pool assignment: "
            + ", ".join(item["email"] for item in configured_security_pool)
        )
    else:
        log(f"Security mailbox window should be logged in as default mailbox: {args.security_email}")

    async with async_playwright() as playwright:
        outlook_context = None
        isolated_outlook_profile = None
        outlook_page = None
        target_context_email = ""
        security_context = None
        isolated_security_profile = None
        security_page = None
        security_context_email = ""
        try:
            security_pool = parse_security_mailbox_pool(getattr(args, "security_mailbox_pool", "") or "")
            needs_security_window = args.security_code_source == "browser" or (
                task_mode in {"full", "mail_password", "forward_rule"} and bool(security_pool)
            )
            if needs_security_window:
                if security_pool:
                    log("Security mailbox pool is enabled; an isolated mailbox context will be opened for the first assigned row.")
                else:
                    security_context, security_page = await connect_security_context(playwright, args, log)
                    await wait_for_security_mailbox(
                        security_page,
                        args.security_email,
                        log,
                        pause_controller,
                        on_manual_wait,
                        on_manual_resume,
                        on_security_mailbox_blocked,
                    )
            else:
                log("Security verification source: manual. Security mailbox window will not be opened.")

            idx = 1
            browser_retry_counts: dict[int, int] = {}
            while idx <= len(accounts):
                account = accounts[idx - 1]
                if pause_controller and pause_controller.should_stop():
                    log("Stopped before next account.")
                    break

                on_account_start(account, idx, len(accounts))
                log(f"[{idx}/{len(accounts)}] row {account.row_number}: steam={account.steam_username}, email={account.email}")
                row_result = "已完成"
                retry_key = account.row_number if account.row_number else account.email.strip().casefold()
                args._current_account_retry_count = browser_retry_counts.get(retry_key, 0)
                try:
                    security_binding_completed = not account.security_binding_required
                    if (
                        args.enable_mail_maintenance
                        and account.backup_security_email
                        and (account.security_binding_required or task_mode == "forward_rule")
                    ):
                        expected_security_email = account.backup_security_email.strip().casefold()
                        if security_context_email != expected_security_email:
                            if security_page:
                                log(
                                    f"Security mailbox assignment changed to {account.backup_security_email}; "
                                    "closing the previous window while preserving its persisted login."
                                )
                                try:
                                    await logout_outlook(security_page, pause_controller)
                                except Exception:
                                    log("Previous security mailbox logout failed; continuing with isolated context replacement.")
                                try:
                                    if security_context:
                                        await security_context.close()
                                except Exception:
                                    log("Previous security mailbox window was already closed; persisted login was preserved.")
                            security_context, isolated_security_profile = await launch_isolated_security_context(
                                playwright,
                                account.backup_security_email,
                                args.security_x,
                                args.security_y,
                                log,
                            )
                            security_page = (
                                security_context.pages[0]
                                if security_context.pages
                                else await security_context.new_page()
                            )
                            security_context_email = ""
                            await ensure_security_mailbox_for_account(
                                security_page,
                                account,
                                log,
                                pause_controller,
                                on_manual_wait,
                                on_manual_resume,
                                on_security_mailbox_blocked,
                            )
                            security_context_email = expected_security_email
                        else:
                            if not security_page or security_page.is_closed():
                                raise RuntimeError("Security mailbox browser context has been closed.")
                            log(f"Reusing confirmed security mailbox session: {account.backup_security_email}")
                    need_outlook_login = (
                        task_mode == "forward_rule"
                        or args.enable_mail_maintenance
                        or args.enable_steam_login
                    )
                    if need_outlook_login:
                        expected_target_email = account.email.strip().casefold()
                        session_reused = bool(
                            outlook_context
                            and outlook_page
                            and target_context_email == expected_target_email
                        )
                        if not session_reused:
                            if outlook_context:
                                await outlook_context.close()
                                log("Previous target Outlook context closed before account switch.")
                            log(f"Opening a clean isolated target Outlook context for {account.email}.")
                            outlook_context, isolated_outlook_profile = await launch_isolated_target_context(
                                playwright,
                                account.email,
                                args.outlook_x,
                                args.outlook_y,
                                log,
                            )
                            outlook_page = outlook_context.pages[0] if outlook_context.pages else await outlook_context.new_page()
                            session_reused = await try_reuse_email_scoped_outlook_session(
                                outlook_page,
                                "target",
                                account.email,
                                log,
                                pause_controller,
                            )
                            if session_reused:
                                target_context_email = expected_target_email
                            else:
                                await fill_login(outlook_page, account, pause_controller)
                                await handle_microsoft_post_login_prompts(
                                    outlook_page,
                                    log,
                                    pause_controller=pause_controller,
                                    on_manual_wait=on_manual_wait,
                                    on_manual_resume=on_manual_resume,
                                )
                                await redirect_microsoft_account_home_to_outlook(outlook_page, log, pause_controller)
                                if await prefer_password_sign_in(outlook_page, pause_controller):
                                    log("Target mailbox login offered password sign-in; switched to password option.")
                                    await handle_microsoft_post_login_prompts(
                                        outlook_page,
                                        log,
                                        pause_controller=pause_controller,
                                        on_manual_wait=on_manual_wait,
                                        on_manual_resume=on_manual_resume,
                                    )
                                    await redirect_microsoft_account_home_to_outlook(outlook_page, log, pause_controller)
                            if not session_reused and security_page and account.backup_security_email and await is_recovery_email_setup_page(outlook_page):
                                security_binding_completed = await complete_recovery_email_setup(
                                    outlook_page,
                                    security_page,
                                    account,
                                    account.backup_security_email,
                                    args,
                                    log,
                                    pause_controller,
                                    on_manual_wait,
                                    on_manual_resume,
                                    on_security_mailbox_blocked,
                                )
                                if security_binding_completed:
                                    await handle_microsoft_post_login_prompts(
                                        outlook_page,
                                        log,
                                        pause_controller=pause_controller,
                                        on_manual_wait=on_manual_wait,
                                        on_manual_resume=on_manual_resume,
                                    )
                                    await outlook_page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
                            if not session_reused and await security_challenge_visible(outlook_page):
                                challenge_security_email = account.backup_security_email or args.security_email
                                if args.security_code_source == "manual":
                                    await wait_for_manual_continue(
                                        f"窗口1正在登录 {account.email}，页面需要安全验证。请你在窗口1手动完成验证，进入 Outlook 收件箱后回到 Web UI 点击“已完成人工操作 / 继续”。",
                                        log,
                                        pause_controller,
                                        on_manual_wait,
                                        on_manual_resume,
                                        page=outlook_page,
                                        screenshot_path=Path(args.error_dir) / f"row_{account.row_number}_{account.steam_username}_manual_security.png",
                                        screenshot_label="Microsoft manual security verification",
                                    )
                                else:
                                    if account.backup_security_email and not security_page:
                                        security_context, isolated_security_profile = await launch_isolated_security_context(
                                            playwright,
                                            account.backup_security_email,
                                            args.security_x,
                                            args.security_y,
                                            log,
                                        )
                                        security_page = (
                                            security_context.pages[0]
                                            if security_context.pages
                                            else await security_context.new_page()
                                        )
                                        security_context_email = ""
                                    if security_page and account.backup_security_email:
                                        challenge_expected_security_email = account.backup_security_email.strip().casefold()
                                        if security_context_email != challenge_expected_security_email:
                                            await ensure_security_mailbox_for_account(
                                                security_page,
                                                account,
                                                log,
                                                pause_controller,
                                                on_manual_wait,
                                                on_manual_resume,
                                                on_security_mailbox_blocked,
                                            )
                                            security_context_email = challenge_expected_security_email
                                        elif security_page.is_closed():
                                            raise RuntimeError("Security mailbox browser context has been closed.")
                                        else:
                                            log(
                                                f"Using existing security mailbox session for verification: "
                                                f"{account.backup_security_email}"
                                            )
                                    from outlook_batch_login import collect_microsoft_security_mail_ids

                                    existing_security_mail_ids = (
                                        await collect_microsoft_security_mail_ids(security_page)
                                        if security_page
                                        else set()
                                    )
                                    log(
                                        f"Captured {len(existing_security_mail_ids)} existing Microsoft security-mail IDs "
                                        f"before target-login verification for {account.email}."
                                    )
                                    code_requested_at = await request_microsoft_security_email_code(
                                        outlook_page,
                                        challenge_security_email,
                                        account.email,
                                        log,
                                        pause_controller,
                                        on_security_mailbox_blocked=on_security_mailbox_blocked,
                                    )
                                if args.security_code_source == "browser":
                                    code_accepted = False
                                    for microsoft_code_attempt in range(1, 3):
                                        microsoft_code = await get_microsoft_security_code_from_mailbox(
                                            security_page,
                                            account.email,
                                            args.microsoft_code_timeout,
                                            log,
                                            pause_controller,
                                            on_manual_wait,
                                            on_manual_resume,
                                            not_before_epoch=code_requested_at,
                                            exclude_message_ids=existing_security_mail_ids,
                                        )
                                        if await submit_microsoft_security_code_with_validation(
                                            outlook_page,
                                            microsoft_code,
                                            account.email,
                                            log,
                                            pause_controller,
                                        ):
                                            code_accepted = True
                                            await handle_microsoft_post_login_prompts(
                                                outlook_page,
                                                log,
                                                pause_controller=pause_controller,
                                                on_manual_wait=on_manual_wait,
                                                on_manual_resume=on_manual_resume,
                                            )
                                            break
                                        if microsoft_code_attempt < 2:
                                            log(
                                                f"Microsoft security code attempt {microsoft_code_attempt} rejected for "
                                                f"{account.email}; requesting a fresh code before retrying this account."
                                            )
                                            if not await resend_microsoft_security_code(outlook_page, pause_controller):
                                                # After a rejected code Microsoft may return to the
                                                # proof-method chooser. That page has no resend button;
                                                # re-select the assigned email method and submit a new
                                                # request instead of treating the missing button as fatal.
                                                from outlook_batch_login import collect_microsoft_security_mail_ids

                                                existing_security_mail_ids = await collect_microsoft_security_mail_ids(
                                                    security_page
                                                )
                                                code_requested_at = await request_microsoft_security_email_code(
                                                    outlook_page,
                                                    account.backup_security_email or args.security_email,
                                                    account.email,
                                                    log,
                                                    pause_controller,
                                                    on_security_mailbox_blocked=on_security_mailbox_blocked,
                                                )
                                            else:
                                                code_requested_at = time.time()
                                            await wait_for_timeout_with_stop(
                                                outlook_page,
                                                500,
                                                pause_controller,
                                                f"Microsoft security code retry for {account.email}",
                                            )
                                        else:
                                            log(
                                                f"Microsoft security code attempt {microsoft_code_attempt} rejected for "
                                                f"{account.email}; retry limit reached for this account."
                                            )
                                    if not code_accepted:
                                        raise RuntimeError(f"Microsoft security code was rejected twice for {account.email}.")
                            else:
                                await handle_microsoft_blocking_page(
                                    outlook_page,
                                    f"目标邮箱 {account.email}",
                                    log,
                                    pause_controller,
                                    on_manual_wait,
                                    on_manual_resume,
                                )
                        await cancel_passkey_prompt(outlook_page, log, pause_controller=pause_controller)
                        await handle_microsoft_post_login_prompts(
                            outlook_page,
                            log,
                            pause_controller=pause_controller,
                            on_manual_wait=on_manual_wait,
                            on_manual_resume=on_manual_resume,
                        )
                        if not session_reused:
                            await wait_for_outlook_inbox(
                                outlook_page,
                                log,
                                pause_controller=pause_controller,
                                on_manual_wait=on_manual_wait,
                                on_manual_resume=on_manual_resume,
                                account_label=f"目标邮箱 {account.email}",
                            )
                        else:
                            log("Skipped duplicate Outlook inbox wait because current session was already verified.")
                        if session_reused:
                            log(f"Reusing established isolated target Outlook context: {account.email}")
                        else:
                            await confirm_fresh_outlook_login_identity(outlook_page, account.email, log)
                            await persist_email_scoped_outlook_session(outlook_page.context, "target", account.email)
                            target_context_email = expected_target_email
                    if args.enable_mail_maintenance and (task_mode == "mail_password" or account.security_binding_required):
                        await run_mail_security_maintenance(
                            outlook_page,
                            security_page,
                            args,
                            account,
                            feishu_config,
                            spreadsheet_token,
                            sheet_id,
                            log,
                            pause_controller,
                            on_manual_wait,
                            on_manual_resume,
                            on_security_mailbox_blocked,
                            change_password=task_mode == "mail_password",
                            security_binding_completed=security_binding_completed,
                        )
                        if task_mode in {"full", "forward_rule"}:
                            account.security_binding_required = False
                            security_binding_completed = True
                            log(f"Security-email binding stage committed for row {account.row_number}.")
                    elif args.enable_mail_maintenance:
                        log(
                            f"Skipped completed security-email binding stage for row {account.row_number}; "
                            f"Feishu column {BACKUP_SECURITY_EMAIL_COL} already contains {account.backup_security_email}."
                        )
                    if task_mode == "forward_rule":
                        await run_forward_rule_setup(
                            outlook_page,
                            security_page,
                            args,
                            account,
                            feishu_config,
                            spreadsheet_token,
                            sheet_id,
                            log,
                            pause_controller,
                            on_manual_wait,
                            on_manual_resume,
                            on_security_mailbox_blocked,
                        )

                    steam_id = ""
                    if args.enable_steam_login:
                        await outlook_page.goto(OUTLOOK_MAIL_URL, wait_until="domcontentloaded")
                        await wait_for_outlook_inbox(
                            outlook_page,
                            log,
                            pause_controller=pause_controller,
                            on_manual_wait=on_manual_wait,
                            on_manual_resume=on_manual_resume,
                            account_label=f"Steam Guard 目标邮箱 {account.email}",
                        )
                        if target_context_email != account.email.strip().casefold():
                            raise RuntimeError(
                                f"Target Outlook context ownership was lost before Steam Guard for {account.email}."
                            )
                        log(f"Steam Guard mailbox identity gate passed: {account.email}")
                        last_steam_error = None
                        for steam_attempt in range(1, int(getattr(args, "steam_login_attempts", 2) or 2) + 1):
                            try:
                                log(f"Steam login attempt {steam_attempt}: {account.steam_username}")
                                log("Shutting down Steam before login attempt.")
                                shutdown_steam(steam_exe, log=log, pause_controller=pause_controller)
                                log("Starting Steam with login arguments.")
                                start_steam_login(steam_exe, account, log)
                                steam_login_screenshot = (
                                    Path(args.error_dir)
                                    / f"row_{account.row_number}_{safe_artifact_name(account.steam_username)}_steam_login_attempt_{steam_attempt}.png"
                                )
                                steam_guard_screenshot = (
                                    Path(args.error_dir)
                                    / f"row_{account.row_number}_{safe_artifact_name(account.steam_username)}_steam_guard_attempt_{steam_attempt}.png"
                                )
                                log("Submitting Steam username/password through helper.")
                                await asyncio.to_thread(
                                    ensure_steam_login_submitted,
                                    account,
                                    log,
                                    60,
                                    pause_controller,
                                    steam_login_screenshot,
                                )
                                log("Steam username/password submitted; confirming Steam Guard prompt before reading mailbox.")
                                await asyncio.to_thread(
                                    ensure_steam_guard_prompt_visible,
                                    account.steam_username,
                                    log,
                                    45,
                                    pause_controller,
                                    steam_guard_screenshot,
                                )
                                log("Steam Guard prompt confirmed; waiting for Steam Guard email.")
                                manual_steam_guard_completed = False
                                try:
                                    steam_code = await search_steam_guard_mail(
                                        outlook_page,
                                        account.steam_username,
                                        args.steam_code_timeout,
                                        log,
                                        pause_controller,
                                        on_manual_wait,
                                        on_manual_resume,
                                    )
                                    log("Steam Guard code found.")
                                    if not send_guard_code_to_steam(
                                        steam_code,
                                        args.steam_ui_timeout,
                                        log,
                                        pause_controller,
                                        account.steam_username,
                                        steam_guard_screenshot,
                                    ):
                                        raise TimeoutError("Could not find Steam Guard code input window.")
                                except TimeoutError as exc:
                                    log(f"Steam Guard email auto-search timed out: {exc}")
                                    await wait_for_manual_continue(
                                        "Steam Guard 邮件没有被自动找到。请在窗口1 Outlook 检查收件箱/垃圾邮件，确认正在查看 Steam 绑定邮箱，"
                                        "Steam 官方说明验证码发往账号当前绑定邮箱，邮件可能延迟；"
                                        "必要时把 noreply@steampowered.com 和 support@steampowered.com 加入可信发件人。"
                                        "如果你已经在 Steam 窗口手动输入 Guard 并完成登录，请点击“已完成人工操作 / 继续”；"
                                        "点击继续后程序会用 loginusers.vdf 验证是否真的登录成功。",
                                        log,
                                        pause_controller,
                                        on_manual_wait,
                                        on_manual_resume,
                                        page=outlook_page,
                                        screenshot_path=Path(args.error_dir) / f"row_{account.row_number}_{account.steam_username}_steam_guard_manual.png",
                                        screenshot_label="Steam Guard manual recovery",
                                    )
                                    manual_steam_guard_completed = True
                                if manual_steam_guard_completed:
                                    log("Continuing after manual Steam Guard recovery; verifying Steam login via loginusers.vdf.")

                                steam_id = wait_for_steam_id(
                                    steam_exe,
                                    account.steam_username,
                                    args.steam_login_timeout,
                                    log,
                                    pause_controller,
                                )
                                log(f"Steam login confirmed: {account.steam_username} -> {steam_id}")
                                break
                            except Exception as exc:
                                last_steam_error = exc
                                log(f"Steam login attempt {steam_attempt} failed: {exc}")
                                if steam_attempt >= int(getattr(args, "steam_login_attempts", 2) or 2):
                                    raise
                                log("Restarting Steam and retrying this account.")

                        write_sheet_verified(
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
                        dota_screenshot = (
                            Path(args.error_dir)
                            / f"row_{account.row_number}_{safe_artifact_name(account.steam_username)}_dota_room.png"
                        )
                        checker = dota2_arcade.Dota2Automation(
                            log=log,
                            pause_controller=pause_controller,
                            manual_wait=on_manual_wait,
                            manual_resume=on_manual_resume,
                            screenshot_path=dota_screenshot,
                        )
                        if checker.run_check(steam_exe, description, max_attempts=args.dota_max_attempts):
                            write_sheet_verified(
                                feishu_config,
                                spreadsheet_token,
                                sheet_id,
                                f"{BIG_ACCOUNT_COL}{account.row_number}:{BIG_ACCOUNT_COL}{account.row_number}",
                                [["是"]],
                                use_uat=args.feishu_auth == "user",
                                package_root=Path(args.lark_mcp_root),
                            )
                            log(f"Wrote big-account flag to row {account.row_number}: 是")
                        else:
                            log(f"Dota room not confirmed for row {account.row_number}; big-account flag was not written.")
                    elif args.enable_steam_login:
                        log("Dota check skipped because Dota verification is disabled.")
                    on_account_done(account, steam_id)
                except Exception as exc:
                    retry_key = account.row_number if account.row_number else account.email.strip().casefold()
                    retry_count = browser_retry_counts.get(retry_key, 0)
                    if (
                        is_outlook_context_retryable_error(exc)
                        and retry_count < 1
                        and not (pause_controller and pause_controller.should_stop())
                    ):
                        browser_retry_counts[retry_key] = retry_count + 1
                        log(
                            f"Outlook context became closed or unverifiable on row {account.row_number}; "
                            "rebuilding only the affected Outlook context and retrying this row once."
                        )
                        target_closed = not outlook_page or outlook_page.is_closed()
                        security_closed = not security_page or security_page.is_closed()
                        message = str(exc).casefold()
                        security_failed = security_closed or (
                            "security mailbox" in message or "安全邮箱" in message
                        )
                        # Unverifiable/login failures originate from the target
                        # unless the exception explicitly identifies the security mailbox.
                        target_failed = target_closed or not security_failed
                        if target_failed:
                            if outlook_context:
                                try:
                                    await outlook_context.close()
                                except Exception:
                                    log("Target Outlook context was already unavailable during retry cleanup.")
                            isolated_outlook_profile = None
                            outlook_context, outlook_page = None, None
                            target_context_email = ""
                        if security_failed:
                            if security_context:
                                try:
                                    await security_context.close()
                                except Exception:
                                    log("Security mailbox context was already unavailable during retry cleanup.")
                            isolated_security_profile = None
                            security_context, security_page = None, None
                            security_context_email = ""
                        continue
                    row_result = f"失败：{exc}"
                    await log_error_page_state("Target Outlook", outlook_page, log)
                    await log_error_page_state("Security mailbox", security_page, log)
                    screenshot = Path(args.error_dir) / f"row_{account.row_number}_{account.steam_username}_target.png"
                    await save_page_screenshot(outlook_page, screenshot, "Target Outlook", log)
                    security_screenshot = Path(args.error_dir) / f"row_{account.row_number}_{account.steam_username}_security.png"
                    await save_page_screenshot(security_page, security_screenshot, "Security mailbox", log)
                    log(f"ERROR on row {account.row_number}: {exc}")
                    on_account_error(account, exc)
                    # Task 3 has a durable Feishu checkpoint in K.  Any error
                    # must stop the whole run so an account is never silently
                    # skipped after an unrecognized page or login failure.
                    if (
                        not args.continue_on_error
                        or getattr(args, "task_mode", "") == "forward_rule"
                        or is_non_skippable_verification_error(exc)
                    ):
                        break

                if not wait_between_accounts(args, account, idx, len(accounts), row_result, log, pause_controller, on_manual_wait, on_manual_resume):
                    break
                idx += 1

        finally:
            if outlook_context:
                try:
                    await outlook_context.close()
                    log("Target Outlook context closed.")
                except Exception as exc:
                    log(f"Target Outlook context close failed: {exc}")
            if security_context:
                try:
                    await security_context.close()
                    log("Security mailbox context closed.")
                except Exception as exc:
                    log(f"Security mailbox context close failed: {exc}")


async def run(args) -> None:
    await run_with_callbacks(args)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Login Outlook/Steam accounts, fetch verification codes, and write results to Feishu sheet.")
    parser.add_argument("--sheet-url", default=DEFAULT_FEISHU_SHEET_URL)
    parser.add_argument("--sheet-id")
    parser.add_argument("--sheet-range", default="A1:K1000")
    parser.add_argument(
        "--task-mode",
        choices=["full", "mail_password", "forward_rule"],
        default="full",
        help="full=login mailbox/Steam/Dota; mail_password=bind security email/change mailbox password; forward_rule=login mailbox and set forwarding rules.",
    )
    parser.add_argument("--feishu-auth", choices=["user", "app"], default="user")
    parser.add_argument("--feishu-config", default=str(feishu_tool.DEFAULT_CONFIG))
    parser.add_argument("--feishu-start-script", default=str(feishu_tool.DEFAULT_START_SCRIPT))
    parser.add_argument("--lark-mcp-root", default=str(feishu_tool.DEFAULT_LARK_MCP_ROOT))
    parser.add_argument("--steam-exe", help="Path to steam.exe. Auto-detected by default.")
    parser.add_argument("--security-email", default=DEFAULT_SECURITY_EMAIL)
    parser.add_argument("--security-code-source", choices=["browser", "manual"], default="manual")
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
    parser.add_argument("--steam-login-attempts", type=int, default=2)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--start-row", type=int, help="Start from this sheet row and continue downward.")
    parser.add_argument("--target-email", default="", help="Only process this exact Outlook/Hotmail email address.")
    parser.add_argument("--list-pending", action="store_true")
    parser.add_argument("--pause-between", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--error-dir", default="errors")
    parser.add_argument("--enable-steam-login", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--enable-dota-check", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dota-max-attempts", type=int, default=3)
    parser.add_argument("--enable-mail-maintenance", action="store_true", help="Bind security email and change target email password after Outlook login.")
    parser.add_argument("--bind-security-email", default="", help="Security email to bind on the target Microsoft account. Empty skips binding prompt.")
    parser.add_argument("--new-email-password", default="", help="New target email password. Empty generates a strong password per account.")
    parser.add_argument("--forward-from-email", default="noreply@steampowered.com", help="Exact sender address for task mode forward_rule.")
    parser.add_argument("--forward-to-email", default="", help="Forwarding destination for task mode forward_rule. Defaults to row backup security email or --security-email.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
