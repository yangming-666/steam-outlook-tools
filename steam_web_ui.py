import argparse
import asyncio
import importlib.util
import json
import re
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import steam_outlook_batch


APP_VERSION = "2026-07-10-persistent-staged-flow"
DEFAULT_STEAM_EXE = "D:\\Steam\\steam.exe"
DEFAULT_FORWARD_FROM_EMAIL = "noreply@steampowered.com"
FORWARD_ADDRESS_PLACEHOLDERS = {"sender@example.com", "a@example.com"}
WEB_UI_SETTINGS = Path("steam_web_ui_settings.json")
WEB_UI_RUN_LOG = Path("steam_web_ui.run.log")
WEB_UI_RUN_LOG_MAX_BYTES = 5 * 1024 * 1024


def normalize_ui_settings(data: dict) -> dict:
    normalized = dict(data) if isinstance(data, dict) else {}
    pool = steam_outlook_batch.parse_security_mailbox_pool(normalized.get("security_mailbox_pool") or "")
    security_email = (normalized.get("security_email") or "").strip()
    placeholder = steam_outlook_batch.DEFAULT_SECURITY_EMAIL.strip().casefold()
    if pool and (not security_email or security_email.casefold() == placeholder):
        normalized["security_email"] = pool[0]["email"]
    if pool:
        normalized["bind_security_email"] = ""
    return normalized


def load_ui_settings() -> dict:
    try:
        if WEB_UI_SETTINGS.exists():
            data = json.loads(WEB_UI_SETTINGS.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            normalized = normalize_ui_settings(data)
            if normalized != data:
                WEB_UI_SETTINGS.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
            return normalized
    except Exception:
        pass
    return {}


def save_ui_settings(data: dict) -> None:
    existing = load_ui_settings()
    for key in ["security_email", "bind_security_email", "forward_from_email", "forward_to_email", "security_mailbox_pool"]:
        if key in data:
            value = (data.get(key) or "").strip()
            if (
                key in {"forward_from_email", "forward_to_email"}
                and value.casefold() in FORWARD_ADDRESS_PLACEHOLDERS
                and (existing.get(key) or "").strip().casefold() not in FORWARD_ADDRESS_PLACEHOLDERS
            ):
                # A stale browser can keep sending the original HTML test
                # placeholders during periodic preflight calls. Never let
                # those values overwrite an already configured real target.
                continue
            existing[key] = value
    existing = normalize_ui_settings(existing)
    WEB_UI_SETTINGS.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


def remove_security_mailboxes_from_pool(emails: list[str]) -> int:
    if not emails:
        return 0
    settings = load_ui_settings()
    pool = steam_outlook_batch.parse_security_mailbox_pool(settings.get("security_mailbox_pool") or "")
    blocked = {email.strip().casefold() for email in emails if email.strip()}
    kept = [item for item in pool if item["email"].casefold() not in blocked]
    if len(kept) == len(pool):
        return 0
    settings["security_mailbox_pool"] = "\n".join(
        f'{item["email"]},{item["password"]}' if item.get("password") else item["email"]
        for item in kept
    )
    current_default = (settings.get("security_email") or "").strip().casefold()
    if current_default in blocked:
        settings["security_email"] = kept[0]["email"] if kept else ""
    settings = normalize_ui_settings(settings)
    WEB_UI_SETTINGS.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(pool) - len(kept)


def prune_full_security_mailboxes(full_emails: list[str]) -> int:
    # A full mailbox must stop receiving new binding assignments, but task3 still
    # needs its credentials to verify forwarding rules for already-bound rows.
    # Capacity is enforced from Feishu J counts in read_pending_accounts().
    return 0


def mark_security_mailbox_blocked(email: str, reason: str) -> int:
    email = (email or "").strip()
    if not email:
        return 0
    settings = load_ui_settings()
    records = settings.get("blocked_security_mailboxes")
    if not isinstance(records, list):
        records = []
    key = email.casefold()
    records = [record for record in records if not isinstance(record, dict) or (record.get("email") or "").casefold() != key]
    records.append(
        {
            "email": email,
            "reason": reason or "Microsoft security-code delivery failed after submitting full security email address.",
            "blocked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    settings["blocked_security_mailboxes"] = records
    WEB_UI_SETTINGS.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return remove_security_mailboxes_from_pool([email])


def blocked_security_mailbox_emails() -> list[str]:
    records = load_ui_settings().get("blocked_security_mailboxes")
    if not isinstance(records, list):
        return []
    emails = []
    for record in records:
        if isinstance(record, dict):
            email = (record.get("email") or "").strip()
            if email:
                emails.append(email)
    return emails


UI_SETTINGS = load_ui_settings()
STATE_LOCK = threading.Lock()
STATE = {
    "running": False,
    "waiting": False,
    "status": "idle",
    "current": None,
    "pending": [],
    "pending_signature": None,
    "skipped_with_remark": 0,
    "skipped_missing_security_email": 0,
    "skipped_missing_required_fields": 0,
    "skipped_security_bind_limit": 0,
    "checklist": [],
    "can_start": False,
    "logs": [],
    "message": "",
    "manual_kind": "",
    "started_at": None,
    "finished_at": None,
}
RUN_THREAD: threading.Thread | None = None
PAUSE_CONTROLLER: steam_outlook_batch.PauseController | None = None
FEISHU_LOGIN_THREAD: threading.Thread | None = None
PENDING_CACHE: dict | None = None
FEISHU_AUTH_CACHE = {"checked_at": 0.0, "status": "unknown", "message": "尚未检查飞书授权。"}


def check_feishu_auth_status(*, force: bool = False) -> dict:
    now = time.time()
    if not force and now - float(FEISHU_AUTH_CACHE.get("checked_at") or 0) < 60:
        return dict(FEISHU_AUTH_CACHE)
    try:
        config = steam_outlook_batch.feishu_tool.load_config(steam_outlook_batch.feishu_tool.DEFAULT_CONFIG)
        result = steam_outlook_batch.feishu_tool.feishu_api(
            config,
            "GET",
            "/open-apis/authen/v1/user_info",
            use_uat=True,
            package_root=steam_outlook_batch.feishu_tool.DEFAULT_LARK_MCP_ROOT,
        )
        if result.get("code") not in {None, 0}:
            raise RuntimeError(result.get("msg") or f"飞书返回错误码 {result.get('code')}")
        status, message = "ok", "飞书授权有效，可以刷新队列。"
    except Exception as exc:
        detail = str(exc)
        if "99991677" in detail or "token expired" in detail.casefold() or "401 unauthorized" in detail.casefold():
            status, message = "expired", "飞书登录已过期，请先点击“飞书重新授权”，完成后再刷新队列。"
        elif "No stored user access token" in detail:
            status, message = "missing", "尚未找到飞书登录信息，请先点击“飞书重新授权”。"
        else:
            status, message = "error", f"飞书授权状态检查失败：{sanitize_log_message(detail)}"
    FEISHU_AUTH_CACHE.update({"checked_at": now, "status": status, "message": message})
    return dict(FEISHU_AUTH_CACHE)


TASK_LABELS = {
    "full": "功能1：登录新邮箱 / 绑定安全邮箱 / Steam / Dota2",
    "mail_password": "功能2：绑定安全邮箱 / 改邮箱密码",
    "forward_rule": "功能3：批量登录 / 设置转发规则",
}


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Steam Outlook 自动化</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Inter, "Segoe UI", "Microsoft YaHei UI", system-ui, sans-serif;
      --ink: #172033; --muted: #64748b; --line: #e4e9f1; --surface: #ffffff;
      --canvas: #f4f6fa; --primary: #4f46e5; --primary-dark: #4338ca;
      --success: #16a34a; --warning: #d97706; --danger: #dc2626;
      --shadow: 0 1px 2px rgba(15,23,42,.04), 0 10px 28px rgba(15,23,42,.06);
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--canvas); color: var(--ink); min-width: 320px; }
    header { height: 68px; display: flex; align-items: center; justify-content: space-between; padding: 0 28px; background: rgba(15,23,42,.96); color: white; position: sticky; top: 0; z-index: 20; box-shadow: 0 8px 24px rgba(15,23,42,.14); }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand-mark { width: 36px; height: 36px; display: grid; place-items: center; border-radius: 11px; background: linear-gradient(145deg,#6366f1,#3b82f6); font-size: 18px; box-shadow: inset 0 1px 0 rgba(255,255,255,.25); }
    .brand strong { display: block; font-size: 15px; letter-spacing: .01em; }
    .brand small { display: block; margin-top: 2px; color: #94a3b8; font-size: 11px; font-weight: 500; }
    main { max-width: 1500px; margin: 0 auto; display: grid; grid-template-columns: minmax(430px, .9fr) minmax(520px, 1.35fr); gap: 18px; padding: 22px; align-items: start; }
    section { background: var(--surface); border: 1px solid rgba(226,232,240,.9); border-radius: 16px; padding: 20px; box-shadow: var(--shadow); min-width: 0; }
    .section-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
    h2 { font-size: 17px; margin: 0; letter-spacing: -.01em; }
    .section-subtitle { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
    label { display: flex; align-items: center; gap: 5px; font-size: 12px; font-weight: 600; color: #475569; margin: 13px 0 6px; }
    input, select, textarea { width: 100%; color: var(--ink); background: #fff; border: 1px solid #cfd7e4; border-radius: 9px; outline: none; transition: border-color .16s, box-shadow .16s, background .16s; }
    input, select { height: 39px; padding: 0 11px; }
    textarea { min-height: 94px; padding: 10px 11px; resize: vertical; font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; line-height: 1.55; }
    input:hover, select:hover, textarea:hover { border-color: #aab7c8; }
    input:focus, select:focus, textarea:focus { border-color: #818cf8; box-shadow: 0 0 0 3px rgba(99,102,241,.12); }
    input:disabled { background: #f6f8fb; color: #64748b; border-color: #e2e8f0; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .subsection { border-top: 1px solid var(--line); margin-top: 18px; padding-top: 17px; }
    .subsection h3 { margin: 0 0 2px; font-size: 13px; color: #334155; text-transform: uppercase; letter-spacing: .055em; }
    .mode-select { height: 46px; font-size: 13px; font-weight: 600; border-color: #bcc6d7; }
    .process-rail { display: grid; grid-template-columns: repeat(3,1fr); gap: 8px; margin: 2px 0 16px; }
    .process-step { display: flex; align-items: center; gap: 7px; min-width: 0; color: #64748b; font-size: 11px; font-weight: 600; }
    .process-step i { width: 23px; height: 23px; flex: 0 0 auto; display: grid; place-items: center; border-radius: 50%; background: #eef2f7; color: #64748b; font-style: normal; }
    .process-step.active { color: var(--primary); }
    .process-step.active i { background: #eef2ff; color: var(--primary); }
    .process-step.done { color: var(--success); }
    .process-step.done i { background: #ecfdf3; color: var(--success); }
    .actions { position: sticky; bottom: 12px; z-index: 10; margin: 18px -8px -8px; padding: 10px; border: 1px solid #dbe2ec; border-radius: 13px; background: rgba(255,255,255,.94); box-shadow: 0 12px 30px rgba(15,23,42,.13); backdrop-filter: blur(12px); }
    .action-group { display: grid; grid-template-columns: 1fr 1.25fr auto; gap: 8px; }
    .action-secondary { display: flex; gap: 7px; margin-top: 8px; }
    button { min-height: 38px; padding: 0 13px; border: 1px solid #cbd5e1; border-radius: 9px; background: #fff; color: #334155; font: inherit; font-size: 12px; font-weight: 650; cursor: pointer; transition: transform .12s, box-shadow .12s, border-color .12s, background .12s; }
    button:hover:not(:disabled) { border-color: #94a3b8; background: #f8fafc; transform: translateY(-1px); box-shadow: 0 4px 10px rgba(15,23,42,.07); }
    button:active:not(:disabled) { transform: translateY(0); }
    button.primary { background: var(--primary); color: white; border-color: var(--primary); box-shadow: 0 5px 12px rgba(79,70,229,.22); }
    button.primary:hover:not(:disabled) { background: var(--primary-dark); border-color: var(--primary-dark); }
    button.danger { color: var(--danger); border-color: #fecaca; background: #fffafa; }
    button.danger:hover:not(:disabled) { background: #fef2f2; border-color: #fca5a5; }
    button.ghost { min-height: 32px; padding: 0 10px; color: #64748b; border-color: transparent; background: transparent; }
    button:disabled { opacity: .42; cursor: not-allowed; box-shadow: none; }
    button.busy::after { content: ""; display: inline-block; width: 11px; height: 11px; margin-left: 7px; vertical-align: -1px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: spin .7s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .status { display: flex; gap: 9px; align-items: center; padding: 7px 11px; border: 1px solid rgba(148,163,184,.22); border-radius: 999px; background: rgba(255,255,255,.07); color: #dbe4f0; font-size: 12px; font-weight: 600; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #94a3b8; box-shadow: 0 0 0 4px rgba(148,163,184,.12); }
    .dot.running { background: #4ade80; box-shadow: 0 0 0 4px rgba(74,222,128,.12); animation: pulse 1.6s infinite; }
    .dot.waiting { background: #fbbf24; box-shadow: 0 0 0 4px rgba(251,191,36,.14); animation: pulse 1.2s infinite; }
    .dot.error { background: #fb7185; box-shadow: 0 0 0 4px rgba(251,113,133,.14); }
    @keyframes pulse { 50% { opacity: .55; } }
    .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 9px; margin-bottom: 15px; }
    .metric { min-height: 72px; border: 1px solid var(--line); border-radius: 11px; padding: 10px 12px; background: linear-gradient(180deg,#fff,#fafbfd); color: var(--muted); font-size: 11px; }
    .metric b { display: block; margin-top: 4px; color: #1e293b; font-size: 21px; line-height: 1.15; letter-spacing: -.02em; }
    .checklist { display: grid; gap: 8px; margin-bottom: 15px; }
    .check { display: grid; grid-template-columns: 20px 1fr; gap: 9px; align-items: start; border: 1px solid var(--line); border-radius: 10px; padding: 10px 11px; background: #fbfcfe; }
    .check .mark { width: 17px; height: 17px; display: grid; place-items: center; border-radius: 50%; margin-top: 1px; background: #e2e8f0; }
    .check .mark::after { content: "·"; color: white; font-size: 15px; line-height: 1; }
    .check.ok { border-color: #d9f1e1; background: #fbfefc; }
    .check.ok .mark { background: var(--success); }
    .check.ok .mark::after { content: "✓"; font-size: 11px; font-weight: 800; }
    .check.action { border-color: #fde6bc; background: #fffdf8; }
    .check.action .mark { background: #f59e0b; }
    .check.action .mark::after { content: "!"; font-size: 11px; font-weight: 800; }
    .check.error { border-color: #fecaca; background: #fffafa; }
    .check.error .mark { background: var(--danger); }
    .check.error .mark::after { content: "×"; font-size: 13px; font-weight: 700; }
    .check b { display: block; margin-bottom: 2px; font-size: 12px; }
    .check span { display: block; color: var(--muted); font-size: 11px; line-height: 1.45; white-space: pre-wrap; }
    .flow { border: 1px solid #dfe3ff; background: linear-gradient(145deg,#f7f7ff,#f3f7ff); border-radius: 11px; padding: 12px 13px; margin-top: 12px; }
    .flow b { display: block; font-size: 12px; margin-bottom: 7px; color: #3730a3; }
    .flow ol { margin: 0; padding-left: 19px; color: #475569; font-size: 11px; line-height: 1.6; }
    .note { color: var(--muted); font-size: 11px; margin-top: 7px; line-height: 1.5; }
    .auth-banner { display: flex; align-items: center; gap: 8px; margin-top: 9px; padding: 9px 10px; border: 1px solid var(--line); border-radius: 9px; background: #f8fafc; color: var(--muted); font-size: 11px; line-height: 1.4; }
    .auth-banner::before { content: ""; width: 8px; height: 8px; flex: 0 0 auto; border-radius: 50%; background: #94a3b8; }
    .auth-banner.ok { color: #166534; border-color: #bbf7d0; background: #f0fdf4; }
    .auth-banner.ok::before { background: var(--success); }
    .auth-banner.expired, .auth-banner.missing, .auth-banner.error { color: #9a3412; border-color: #fed7aa; background: #fff7ed; }
    .auth-banner.expired::before, .auth-banner.missing::before, .auth-banner.error::before { background: var(--warning); }
    .message { margin-bottom: 14px; color: #854d0e; background: #fffbeb; border: 1px solid #fde68a; border-radius: 10px; padding: 11px 12px; display: none; white-space: pre-wrap; font-size: 12px; line-height: 1.5; }
    .message a { color: #4338ca; }
    .decision-panel { display: none; border: 1px solid #fed7aa; background: linear-gradient(135deg,#fffaf0,#fff7ed); border-radius: 11px; padding: 13px; margin-bottom: 14px; box-shadow: 0 5px 15px rgba(217,119,6,.08); }
    .decision-panel b { display: block; font-size: 13px; color: #9a3412; margin-bottom: 9px; }
    .decision-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .table-wrap { max-height: 330px; overflow: auto; border: 1px solid var(--line); border-radius: 11px; background: #fff; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { border-bottom: 1px solid #edf0f5; padding: 9px 10px; text-align: left; white-space: nowrap; }
    th { background: #f8fafc; position: sticky; top: 0; z-index: 1; color: #64748b; font-size: 10px; letter-spacing: .04em; text-transform: uppercase; }
    tbody tr:hover { background: #f8faff; }
    tbody tr.current { background: #eef2ff; }
    .empty-row td { padding: 34px 10px; text-align: center; color: #94a3b8; }
    .log-section { grid-column: 1 / -1; }
    .log-toolbar { display: flex; align-items: center; gap: 5px; }
    .log-shell { border-radius: 12px; background: #0d1320; overflow: hidden; border: 1px solid #1e293b; }
    .log-dots { display: flex; gap: 5px; padding: 9px 12px; border-bottom: 1px solid #1e293b; }
    .log-dots i { width: 8px; height: 8px; border-radius: 50%; background: #334155; }
    pre { height: 320px; margin: 0; overflow: auto; color: #cbd5e1; padding: 13px 15px; font: 11px/1.55 "Cascadia Code", Consolas, monospace; white-space: pre-wrap; word-break: break-word; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0,0,0,0); }
    @media (max-width: 1040px) { main { grid-template-columns: 1fr; max-width: 820px; } .log-section { grid-column: auto; } }
    @media (max-width: 620px) { header { height: 60px; padding: 0 14px; } .brand-mark { display: none; } main { padding: 12px; gap: 12px; } section { padding: 15px; border-radius: 13px; } .row { grid-template-columns: 1fr; gap: 0; } .grid { grid-template-columns: 1fr 1fr; } .process-step span { display: none; } .action-group { grid-template-columns: 1fr 1fr; } .action-group .danger { grid-column: 1 / -1; } .action-secondary { display: grid; grid-template-columns: 1fr 1fr; } }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">S</div>
      <div><strong>Steam Outlook 自动化</strong><small>批处理控制台 · v__APP_VERSION__</small></div>
    </div>
    <div class="status" role="status" aria-live="polite"><span id="dot" class="dot"></span><span id="status">空闲</span></div>
  </header>
  <main>
    <section>
      <div class="section-head"><div><h2>任务配置</h2><p class="section-subtitle">选择任务并确认本次执行参数</p></div></div>
      <div class="process-rail" aria-label="操作流程">
        <div class="process-step active" id="step_config"><i>1</i><span>配置任务</span></div>
        <div class="process-step" id="step_queue"><i>2</i><span>刷新队列</span></div>
        <div class="process-step" id="step_run"><i>3</i><span>启动执行</span></div>
      </div>
      <label>任务模式</label>
      <select id="task_mode" class="mode-select" onchange="applyTaskMode(); refreshChecklist();">
        <option value="full">1. 登录新邮箱 / 绑定安全邮箱 / 登录 Steam / Dota2 房间检测大号 / 更新飞书</option>
        <option value="mail_password">2. 绑定安全邮箱 / 改邮箱密码</option>
        <option value="forward_rule">3. 已测 Steam 的邮箱批量登录 / 设置转发规则</option>
      </select>
      <div id="workflow_summary" class="flow"></div>
      <div class="subsection">
      <h3>通用配置</h3>
      <label>飞书表格链接</label>
      <input id="sheet_url">
      <div class="row">
        <div><label>读取范围</label><input id="sheet_range" value="A1:K1000"></div>
        <div><label>限制数量</label><input id="limit" type="number" min="1" placeholder="空=全部"></div>
      </div>
      <label>只处理指定目标邮箱（可空）</label>
      <input id="target_email" placeholder="例如 furkan_galatasaray16@hotmail.com">
      <div class="row">
        <div><label>从此行开始向下</label><input id="start_row" type="number" min="2" placeholder="空=从第2行开始"></div>
        <div><label>默认安全邮箱（兜底）</label><input id="security_email" value="__DEFAULT_SECURITY_EMAIL__"></div>
      </div>
      <div data-common-section="security_verification">
      <label>目标邮箱登录安全验证方式</label>
      <select id="security_code_source"><option value="manual">手动完成 Microsoft 验证</option><option value="browser">用安全邮箱网页登录收码</option></select>
      <label>安全邮箱账号密码池（任务1、任务2、任务3共用；每行：邮箱,密码）</label>
      <textarea id="security_mailbox_pool" placeholder="security01@example.com,password&#10;security02@example.com,password">__DEFAULT_SECURITY_MAILBOX_POOL__</textarea>
      <div class="note">任务1和任务3用于目标邮箱登录时收 Microsoft 安全码；三个任务均可按 J 列状态分配并绑定安全邮箱。每个安全邮箱最多绑定 10 个账号，已满后才切换下一个。</div>
      </div>
      <div class="row">
        <div><label>飞书认证方式</label><select id="feishu_auth"><option value="user">当前账号</option><option value="app">应用</option></select></div>
        <div><label>目标 Outlook 窗口 X 坐标</label><input id="outlook_x" type="number" value="0"></div>
      </div>
      <div id="feishu_auth_banner" class="auth-banner" role="status">正在检查飞书授权状态...</div>
      </div>
      <div class="subsection" data-section="full">
      <h3>功能1：新邮箱 Steam / Dota2</h3>
      <label>功能1固定执行内容</label>
      <input value="登录新邮箱 / 绑定安全邮箱 / 登录 Steam / 写回 SteamID / Dota2 房间检测大号" disabled>
      <div class="row">
        <div><label>Steam 程序路径</label><input id="steam_exe" value="__DEFAULT_STEAM_EXE__"></div>
        <div><label>Dota2 房间检测大号</label><select id="enable_dota_check"><option value="1">开启</option><option value="0">关闭</option></select></div>
      </div>
      <div class="row">
        <div><label>Steam 登录</label><select id="enable_steam_login"><option value="1">开启</option><option value="0">关闭</option></select></div>
        <div><label>Dota2 房间检测最多尝试次数</label><input id="dota_max_attempts" type="number" min="1" max="5" value="3"></div>
      </div>
      <div class="row">
        <div><label>Steam 登录重试次数</label><input id="steam_login_attempts" type="number" min="1" max="3" value="2"></div>
        <div></div>
      </div>
      <div class="note">任务1默认流程：登录 Outlook -> 绑定安全邮箱并写回 J 列 -> 输入 Steam 账号密码 -> 等 Steam Guard 邮件 -> 回填 SteamID -> 进入 Dota2 创建/搜索房间。房间存在才标记大号。任务1不修改邮箱密码。</div>
      </div>
      <div class="subsection" data-section="mail_password">
      <h3>功能2：邮箱安全维护</h3>
      <input id="enable_mail_maintenance" type="hidden" value="1">
      <label>功能2固定执行内容</label>
      <input value="绑定安全邮箱 / 修改邮箱密码（功能2固定执行）" disabled>
      <label>指定要绑定的安全邮箱（可空；不填则优先使用安全邮箱账号密码池）</label>
      <input id="bind_security_email" value="__DEFAULT_BIND_SECURITY_EMAIL__" placeholder="例如 security@example.com">
      <label>回填规则</label>
      <input value="绑定确认后写入 J 列“备用安全邮箱”；改密后写入 D 列“密码”。" disabled>
      <label>新邮箱密码（可空；留空自动生成并在提示中显示）</label>
      <input id="new_email_password" placeholder="空=每个账号自动生成">
      </div>
      <div class="subsection" data-section="forward_rule">
      <h3>功能3：批量登录 / 设置转发规则</h3>
      <label>规则发件人（From）</label>
      <input id="forward_from_email" value="__DEFAULT_FORWARD_FROM_EMAIL__" placeholder="例如 noreply@steampowered.com">
      <label>转发目标邮箱</label>
      <input id="forward_to_email" value="__DEFAULT_FORWARD_TO_EMAIL__" placeholder="例如 code@steam.mail.gpt404.cn；空=优先用 J 列备用安全邮箱，否则用安全邮箱">
      <label>处理规则</label>
      <input value="读取已有 steam id、邮箱密码存在且 K 列“转发规则”为空的行；K 列有任何内容均视为已完成；逐个登录 Outlook，进入规则页，确认后写入 K 列。" disabled>
      </div>
      <div class="actions">
        <div class="action-group">
          <button id="refresh_button" onclick="loadPending()">① 刷新并校验队列</button>
          <button id="start_button" class="primary" onclick="startRun()" disabled>② 启动当前任务</button>
          <button id="stop_button" class="danger" onclick="stopRun()" disabled>停止运行</button>
        </div>
        <div class="action-secondary"><button onclick="debugService()">服务自检</button><button onclick="feishuLogin()">飞书重新授权</button></div>
      </div>
    </section>
    <section>
      <div class="section-head"><div><h2>运行状态</h2><p class="section-subtitle">队列、前置检查与人工确认</p></div></div>
      <div class="grid">
        <div class="metric">队列<b id="pending_count">0</b></div>
        <div class="metric">当前行<b id="current_row">-</b></div>
        <div class="metric">人工确认<b id="waiting">否</b></div>
        <div class="metric">运行<b id="running">否</b></div>
      </div>
      <div id="message" class="message" role="alert"></div>
      <div id="manual_decision" class="decision-panel">
        <b id="manual_decision_title">等待人工确认</b>
        <div class="decision-actions">
          <button id="continue_button" onclick="continueNext()">确认完成，继续流程</button>
          <button id="dota_found_button" onclick="dotaFound()">房间存在，标记大号</button>
          <button id="dota_not_found_button" onclick="dotaNotFound()">房间不存在，不标记大号</button>
        </div>
      </div>
      <div id="checklist" class="checklist"></div>
      <div class="table-wrap">
        <table><thead><tr><th>行</th><th>Steam</th><th>目标邮箱</th><th>分配安全邮箱</th></tr></thead><tbody id="pending_body"></tbody></table>
      </div>
    </section>
    <section class="log-section">
      <div class="section-head">
        <div><h2>运行日志</h2><p class="section-subtitle">实时显示最近 500 条记录</p></div>
        <div class="log-toolbar"><button class="ghost" onclick="copyLogs()">复制日志</button><button class="ghost" onclick="clearLogsView()">清空显示</button></div>
      </div>
      <div class="log-shell"><div class="log-dots" aria-hidden="true"><i></i><i></i><i></i></div><pre id="logs" aria-live="polite"></pre></div>
    </section>
  </main>
<script>
const defaultUrl = "__DEFAULT_URL__";
let currentRunning = false;
let settingsDirty = true;
let actionBusy = false;
let refreshingState = false;
let refreshingPreflight = false;
let visibleLogOffset = 0;
document.getElementById("sheet_url").value = defaultUrl;
const statusLabels = {
  "idle":"空闲", "starting":"正在启动", "running":"运行中", "working":"处理中",
  "manual action":"等待人工确认", "loading pending":"正在读取队列", "pending loaded":"队列已就绪",
  "error":"发生错误", "start failed":"启动失败", "feishu login":"飞书授权中",
  "feishu login ok":"飞书授权成功", "feishu login failed":"飞书授权失败",
  "security mailbox limited":"安全邮箱受限"
};
function displayStatus(value) {
  const raw = String(value || "idle");
  if (/^running\s+\d+\/\d+$/i.test(raw)) return raw.replace(/^running/i, "运行中");
  return statusLabels[raw] || raw;
}
function setActionBusy(buttonId, busy) {
  actionBusy = busy;
  const button = document.getElementById(buttonId);
  if (button) button.classList.toggle("busy", busy);
  updateControls();
}
function updateControls(canStart) {
  const start = document.getElementById("start_button");
  if (typeof canStart === "boolean") start.dataset.canStart = canStart ? "1" : "0";
  start.disabled = actionBusy || settingsDirty || start.dataset.canStart !== "1" || currentRunning;
  document.getElementById("refresh_button").disabled = actionBusy || currentRunning;
  document.getElementById("stop_button").disabled = actionBusy || !currentRunning;
  document.getElementById("step_config").className = "process-step " + (settingsDirty ? "active" : "done");
  document.getElementById("step_queue").className = "process-step " + (settingsDirty ? "" : (currentRunning ? "done" : "active"));
  document.getElementById("step_run").className = "process-step " + (currentRunning ? "active" : "");
}
async function api(path, body) {
  const opt = body ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const text = await r.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch (err) {
    const preview = text.replace(/\s+/g, " ").slice(0, 300);
    throw new Error(`${path} 接口返回的不是 JSON：${preview || r.status}`);
  }
  if (!r.ok || data.ok === false) {
    throw new Error(data.error || `请求失败: ${r.status}`);
  }
  return data;
}
async function callAction(path, body) {
  try {
    await api(path, body);
    refreshChecklist();
    refresh();
  } catch (err) {
    localError(err);
    refreshChecklist();
  }
}
function localNotice(text) {
  document.getElementById("status").textContent = "处理中";
  const msg = document.getElementById("message");
  renderMessage(msg, text);
  msg.style.display = "block";
  const logs = document.getElementById("logs");
  logs.textContent += (logs.textContent ? "\n" : "") + new Date().toLocaleTimeString() + " " + text;
  logs.scrollTop = logs.scrollHeight;
}
function localError(err) {
  const text = err && err.message ? err.message : String(err);
  document.getElementById("status").textContent = "发生错误";
  const msg = document.getElementById("message");
  renderMessage(msg, text);
  msg.style.display = "block";
  const logs = document.getElementById("logs");
  logs.textContent += (logs.textContent ? "\n" : "") + new Date().toLocaleTimeString() + " 前端错误：" + text;
  logs.scrollTop = logs.scrollHeight;
}
function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function renderMessage(el, text) {
  const escaped = escapeHtml(text || "");
  el.innerHTML = escaped.replace(/https?:\/\/[^\s<]+/g, url => `<a href="${url}" target="_blank" rel="noreferrer">${url}</a>`);
}
function renderChecklist(items, canStart) {
  const box = document.getElementById("checklist");
  const checks = settingsDirty
    ? [{status:"action", label:"飞书队列", detail:"当前表单设置已变化，请先点击“刷新队列”。刷新成功后才允许启动，避免用旧任务配置执行。"}]
    : (items || []);
  box.innerHTML = checks.map(item => `
    <div class="check ${escapeHtml(item.status || 'action')}">
      <div class="mark"></div>
      <div><b>${escapeHtml(item.label || '')}</b><span>${escapeHtml(item.detail || '')}</span></div>
    </div>
  `).join("");
  const startBlocked = settingsDirty || !canStart || currentRunning;
  updateControls(!!canStart);
  if (!actionBusy) document.getElementById("start_button").disabled = startBlocked;
}
function fieldValue(id, fallback = "") {
  const el = document.getElementById(id);
  return el ? el.value : fallback;
}
function trimmedFieldValue(id, fallback = "") {
  return fieldValue(id, fallback).trim();
}
function numberFieldValue(id, fallback = 0) {
  const raw = fieldValue(id, String(fallback));
  const value = Number(raw || fallback);
  return Number.isFinite(value) ? value : fallback;
}
function boolSelectValue(id, fallback = false) {
  const el = document.getElementById(id);
  return el ? el.value === "1" : fallback;
}
function settings() {
  const limit = trimmedFieldValue("limit");
  const startRow = trimmedFieldValue("start_row");
  const taskMode = fieldValue("task_mode", "full");
  return {
    task_mode: taskMode,
    sheet_url: trimmedFieldValue("sheet_url"),
    sheet_range: trimmedFieldValue("sheet_range", "A1:K1000"),
    steam_exe: trimmedFieldValue("steam_exe"),
    feishu_auth: fieldValue("feishu_auth", "user"),
    outlook_x: numberFieldValue("outlook_x", 0),
    security_x: numberFieldValue("security_x", 1280),
    limit: limit ? Number(limit) : null
    ,target_email: trimmedFieldValue("target_email")
    ,start_row: startRow ? Number(startRow) : null
    ,security_email: trimmedFieldValue("security_email")
    ,security_code_source: fieldValue("security_code_source", "manual")
    ,enable_steam_login: boolSelectValue("enable_steam_login", true)
    ,steam_login_attempts: numberFieldValue("steam_login_attempts", 2)
    ,enable_dota_check: boolSelectValue("enable_dota_check", true)
    ,dota_max_attempts: numberFieldValue("dota_max_attempts", 3)
    ,enable_mail_maintenance: boolSelectValue("enable_mail_maintenance", false)
    ,bind_security_email: trimmedFieldValue("bind_security_email")
    ,security_mailbox_pool: trimmedFieldValue("security_mailbox_pool")
    ,new_email_password: trimmedFieldValue("new_email_password")
    ,forward_from_email: trimmedFieldValue("forward_from_email", "noreply@steampowered.com")
    ,forward_to_email: trimmedFieldValue("forward_to_email")
  };
}
function applyTaskMode() {
  const mode = document.getElementById("task_mode").value;
  document.querySelectorAll("[data-section]").forEach(el => {
    el.style.display = el.dataset.section === mode ? "block" : "none";
  });
  document.querySelectorAll("[data-common-section]").forEach(el => {
    const section = el.dataset.commonSection;
    const visible = true;
    el.style.display = visible ? "block" : "none";
  });
  if (mode === "full") {
    document.getElementById("enable_steam_login").value = "1";
    document.getElementById("enable_dota_check").value = "1";
    document.getElementById("enable_mail_maintenance").value = "1";
  } else if (mode === "mail_password") {
    document.getElementById("enable_steam_login").value = "0";
    document.getElementById("enable_dota_check").value = "0";
    document.getElementById("enable_mail_maintenance").value = "1";
    if (!trimmedFieldValue("bind_security_email")) {
      document.getElementById("bind_security_email").value = trimmedFieldValue("security_email");
    }
  } else if (mode === "forward_rule") {
    document.getElementById("enable_steam_login").value = "0";
    document.getElementById("enable_dota_check").value = "0";
    document.getElementById("enable_mail_maintenance").value = "1";
  }
  updateWorkflowSummary();
}
function updateWorkflowSummary() {
  const mode = fieldValue("task_mode", "full");
  const steamOn = boolSelectValue("enable_steam_login", true);
  const dotaOn = boolSelectValue("enable_dota_check", true);
  let title = "当前会执行";
  let steps = [];
  if (mode === "full") {
    steps = [
      "读取飞书待处理账号",
      "登录目标 Outlook 邮箱并确认收件箱",
      "绑定安全邮箱并写回 J 列；任务1不修改邮箱密码",
      steamOn ? "启动 Steam，输入账号密码，等待 Steam Guard 页面" : "跳过 Steam 登录",
      steamOn ? "只从目标邮箱打开的 Steam 邮件里提取验证码并提交" : "不读取 Steam Guard 邮件",
      steamOn ? "确认 loginusers.vdf 中的新 SteamID 并写回 I 列" : "不写回 SteamID",
      dotaOn ? "进入 Dota2 创建/搜索指定房间；房间存在则判定为大号并写回 G 列“是”" : "Dota2 房间检测已关闭，不会判断大号"
    ];
  } else if (mode === "mail_password") {
    steps = ["读取需要维护的邮箱", "绑定安全邮箱或使用安全邮箱账号密码池", "按配置修改邮箱密码", "写回安全邮箱和新密码"];
  } else {
    steps = ["读取已有 SteamID 且 K 列为空的邮箱", "逐个复用或登录 Outlook", "自动创建 From / Forward to 规则", "保存后重新打开并核验规则详情", "核验通过后 K 列只写 Forward to 邮箱"];
  }
  document.getElementById("workflow_summary").innerHTML = `<b>${escapeHtml(title)}</b><ol>${steps.map(step => `<li>${escapeHtml(step)}</li>`).join("")}</ol>`;
}
async function loadPending(){
  if (actionBusy || currentRunning) return;
  setActionBusy("refresh_button", true);
  localNotice("已点击刷新队列，正在请求后端...");
  try {
    await api("/api/pending", settings());
    settingsDirty = false;
    await loadUiSettings();
  } catch (err) {
    localError(err);
  }
  await refreshChecklist();
  await refresh();
  setActionBusy("refresh_button", false);
}
async function startRun(){
  if (actionBusy || currentRunning || settingsDirty) return;
  setActionBusy("start_button", true);
  localNotice("正在启动当前任务...");
  await callAction("/api/start", settings());
  setActionBusy("start_button", false);
}
async function debugService(){ localNotice("正在执行服务自检..."); await callAction("/api/debug", {}); }
async function refreshChecklist(){
  if (refreshingPreflight) return;
  if (settingsDirty) {
    renderChecklist([], false);
    return;
  }
  refreshingPreflight = true;
  try {
    const data = await api("/api/preflight", settings());
    renderChecklist(data.checklist, data.can_start);
  } catch (err) {
    localError(err);
  } finally {
    refreshingPreflight = false;
  }
}
async function feishuLogin(){ localNotice("已点击飞书重新授权，正在请求后端..."); await callAction("/api/feishu-login", {}); }
async function refreshFeishuAuth(force = false) {
  const banner = document.getElementById("feishu_auth_banner");
  if (fieldValue("feishu_auth", "user") !== "user") {
    banner.className = "auth-banner ok";
    banner.textContent = "当前使用应用身份，不依赖个人登录令牌。";
    return;
  }
  try {
    const data = await api("/api/auth-status" + (force ? "?force=1" : ""));
    banner.className = "auth-banner " + (data.status || "error");
    banner.textContent = data.message || "飞书授权状态未知。";
  } catch (err) {
    banner.className = "auth-banner error";
    banner.textContent = err.message || String(err);
  }
}
async function continueNext(){ localNotice("已点击继续，正在通知后端..."); await callAction("/api/continue", {}); }
async function dotaFound(){ localNotice("已确认 Dota2 房间存在，当前账号按大号处理，正在通知后端..."); await callAction("/api/dota-found", {}); }
async function dotaNotFound(){ localNotice("已确认 Dota2 房间不存在，当前账号不标记大号，正在通知后端..."); await callAction("/api/dota-not-found", {}); }
async function stopRun(){
  if (actionBusy || !currentRunning) return;
  if (!window.confirm("确定停止当前任务吗？正在处理的账号可能需要重新检查。")) return;
  setActionBusy("stop_button", true);
  localNotice("正在安全停止当前任务...");
  await callAction("/api/stop", {});
  setActionBusy("stop_button", false);
}
async function copyLogs(){
  const text = document.getElementById("logs").textContent;
  if (!text) { localNotice("当前没有可复制的日志。"); return; }
  try { await navigator.clipboard.writeText(text); localNotice("日志已复制到剪贴板。"); }
  catch (err) { localError(new Error("复制失败，请手动选择日志文本。")); }
}
function clearLogsView(){
  visibleLogOffset += document.getElementById("logs").textContent.split("\n").filter(Boolean).length;
  document.getElementById("logs").textContent = "";
}
async function loadUiSettings(){
  try {
    const data = await api("/api/ui-settings");
    const settings = data.settings || {};
    ["security_email", "bind_security_email", "forward_from_email", "forward_to_email", "security_mailbox_pool"].forEach(id => {
      const el = document.getElementById(id);
      if (el && settings[id]) {
        el.value = settings[id];
      }
    });
    if (trimmedFieldValue("security_mailbox_pool")) {
      document.getElementById("security_code_source").value = "browser";
    }
    updateWorkflowSummary();
  } catch (err) {
    localError(err);
  }
}
window.addEventListener("error", event => localError(event.error || event.message));
window.addEventListener("unhandledrejection", event => localError(event.reason || "Promise rejected"));
async function refresh(){
  if (refreshingState) return;
  refreshingState = true;
  let s;
  try {
    s = await api("/api/state");
  } catch (err) {
    document.getElementById("status").textContent = "服务连接失败";
    renderMessage(document.getElementById("message"), err.message || String(err));
    document.getElementById("message").style.display = "block";
    refreshingState = false;
    return;
  }
  document.getElementById("status").textContent = displayStatus(s.status);
  currentRunning = !!s.running;
  document.getElementById("pending_count").textContent = s.pending.length;
  document.getElementById("current_row").textContent = s.current ? s.current.row_number : "-";
  document.getElementById("waiting").textContent = s.waiting ? "是" : "否";
  document.getElementById("running").textContent = s.running ? "是" : "否";
  const msg = document.getElementById("message");
  renderMessage(msg, s.message || "");
  msg.style.display = s.message ? "block" : "none";
  const decision = document.getElementById("manual_decision");
  const continueButton = document.getElementById("continue_button");
  const dotaFoundButton = document.getElementById("dota_found_button");
  const dotaNotFoundButton = document.getElementById("dota_not_found_button");
  const manualKind = s.manual_kind || "";
  document.getElementById("manual_decision_title").textContent = manualKind === "dota_room" ? "等待确认 Dota2 房间是否存在" : "等待人工确认";
  decision.style.display = s.waiting ? "block" : "none";
  continueButton.style.display = manualKind === "dota_room" ? "none" : "inline-block";
  dotaFoundButton.style.display = manualKind === "dota_room" ? "inline-block" : "none";
  dotaNotFoundButton.style.display = manualKind === "dota_room" ? "inline-block" : "none";
  const dot = document.getElementById("dot");
  const hasError = /error|failed|limited/i.test(s.status || "");
  dot.className = "dot" + (s.waiting ? " waiting" : (s.running ? " running" : (hasError ? " error" : "")));
  const pendingRows = (s.pending || []).map(a => `<tr class="${s.current && a.row_number === s.current.row_number ? "current" : ""}"><td>${escapeHtml(a.row_number)}</td><td>${escapeHtml(a.steam_username || "-")}</td><td>${escapeHtml(a.email || "-")}</td><td>${escapeHtml(a.backup_security_email || "-")}</td></tr>`).join("");
  document.getElementById("pending_body").innerHTML = pendingRows || `<tr class="empty-row"><td colspan="4">刷新队列后，待处理账号会显示在这里</td></tr>`;
  renderChecklist(s.checklist || [], !!s.can_start);
  const logs = document.getElementById("logs");
  const allLogs = s.logs || [];
  if (visibleLogOffset > allLogs.length) visibleLogOffset = 0;
  const atBottom = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 36;
  logs.textContent = allLogs.slice(visibleLogOffset).join("\n");
  if (atBottom) logs.scrollTop = logs.scrollHeight;
  updateControls(!!s.can_start);
  refreshingState = false;
}
setInterval(refresh, 1500);
setInterval(refreshChecklist, 3000);
setInterval(refreshFeishuAuth, 60000);
applyTaskMode();
document.querySelectorAll("input,select,textarea").forEach(el => {
  el.addEventListener("change", () => {
    settingsDirty = true;
    updateWorkflowSummary();
    document.getElementById("start_button").disabled = true;
    refreshChecklist();
  });
  el.addEventListener("input", () => {
    settingsDirty = true;
    updateWorkflowSummary();
    document.getElementById("start_button").disabled = true;
    clearTimeout(window.__checklistTimer);
    window.__checklistTimer = setTimeout(refreshChecklist, 400);
  });
});
loadUiSettings().finally(() => {
  refreshFeishuAuth(true);
  refreshChecklist();
  refresh();
});
</script>
</body>
</html>
""".replace("__APP_VERSION__", APP_VERSION).replace("__DEFAULT_URL__", steam_outlook_batch.DEFAULT_FEISHU_SHEET_URL).replace(
    "__DEFAULT_SECURITY_EMAIL__", UI_SETTINGS.get("security_email") or steam_outlook_batch.DEFAULT_SECURITY_EMAIL
).replace(
    "__DEFAULT_STEAM_EXE__", DEFAULT_STEAM_EXE
).replace(
    "__DEFAULT_BIND_SECURITY_EMAIL__", UI_SETTINGS.get("bind_security_email") or ""
).replace(
    "__DEFAULT_FORWARD_FROM_EMAIL__", UI_SETTINGS.get("forward_from_email") or DEFAULT_FORWARD_FROM_EMAIL
).replace(
    "__DEFAULT_FORWARD_TO_EMAIL__", UI_SETTINGS.get("forward_to_email") or ""
).replace(
    "__DEFAULT_SECURITY_MAILBOX_POOL__", UI_SETTINGS.get("security_mailbox_pool") or ""
)


def rotate_run_log_if_needed() -> None:
    try:
        if WEB_UI_RUN_LOG.exists() and WEB_UI_RUN_LOG.stat().st_size >= WEB_UI_RUN_LOG_MAX_BYTES:
            rotated = WEB_UI_RUN_LOG.with_suffix(WEB_UI_RUN_LOG.suffix + ".1")
            if rotated.exists():
                rotated.unlink()
            WEB_UI_RUN_LOG.replace(rotated)
    except Exception:
        pass


def sanitize_log_message(message: str) -> str:
    text = str(message)
    replacements = [
        (r"(邮箱密码已成功改为[:：])\s*[^\s。；;，,]+", r"\1 [hidden]"),
        (r"(邮箱密码改为[:：])\s*[^\s。；;，,]+", r"\1 [hidden]"),
        (r"(把\s+[^\n。；;，,]+?\s+的邮箱密码改为[:：])\s*[^\s。；;，,]+", r"\1 [hidden]"),
        (r"(Microsoft security code(?: for [^:。；;\n]+)?[:：])\s*[0-9]{4,8}", r"\1 [hidden]"),
        (r"(Steam Guard code(?: found)?[:：])\s*[A-Z0-9]{5}", r"\1 [hidden]"),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


def append_log(message: str) -> None:
    safe_message = sanitize_log_message(message)
    line = f"{time.strftime('%H:%M:%S')} {safe_message}"
    rotate_run_log_if_needed()
    try:
        with WEB_UI_RUN_LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass
    with STATE_LOCK:
        STATE["logs"].append(line)
        STATE["logs"] = STATE["logs"][-500:]
        if STATE.get("running") and not STATE.get("waiting") and STATE.get("status") not in {"error", "start failed"}:
            STATE["message"] = f"最近步骤：{safe_message}"


def validate_start_args(args: argparse.Namespace) -> None:
    if args.task_mode not in {"full", "mail_password", "forward_rule"}:
        raise RuntimeError(f"未知任务模式：{args.task_mode}")
    if args.security_code_source not in {"manual", "browser"}:
        raise RuntimeError(f"未知 Microsoft 验证方式：{args.security_code_source}")
    if args.limit is not None and int(args.limit) <= 0:
        raise RuntimeError("限制数量必须为空或大于 0。")
    if args.start_row is not None and int(args.start_row) < 2:
        raise RuntimeError("起始行必须为空或大于等于 2。")
    if args.task_mode == "full":
        if int(args.steam_login_attempts or 0) < 1 or int(args.steam_login_attempts or 0) > 3:
            raise RuntimeError("Steam 登录重试次数必须在 1 到 3 之间。")
        if int(args.dota_max_attempts or 0) < 1 or int(args.dota_max_attempts or 0) > 5:
            raise RuntimeError("Dota2 房间检测最多尝试次数必须在 1 到 5 之间。")
    if args.task_mode == "forward_rule":
        for value, label in [
            (args.forward_from_email, "规则发件人 From"),
            (args.forward_to_email, "转发目标 Forward to"),
        ]:
            if not value or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
                raise RuntimeError(f"{label}必须填写有效邮箱地址。")


def args_signature(args: argparse.Namespace) -> str:
    keys = [
        "task_mode",
        "sheet_url",
        "sheet_range",
        "feishu_auth",
        "target_email",
        "start_row",
        "limit",
        "security_email",
        "bind_security_email",
        "security_mailbox_pool",
        "security_code_source",
        "forward_from_email",
        "forward_to_email",
        "steam_exe",
        "enable_steam_login",
        "enable_dota_check",
        "steam_login_attempts",
        "dota_max_attempts",
        "new_email_password",
    ]
    data = {key: getattr(args, key, None) for key in keys}
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def check_item(status: str, label: str, detail: str) -> dict:
    return {"status": status, "label": label, "detail": detail}


def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def preflight(args: argparse.Namespace) -> dict:
    checklist = []
    blockers = []

    feishu_config = Path(args.feishu_config)
    if not feishu_config.exists():
        checklist.append(check_item("error", "飞书配置", f"缺少配置文件：{feishu_config}。请先完成飞书工具配置/授权。"))
        blockers.append("飞书配置缺失")
    else:
        checklist.append(check_item("ok", "飞书配置", f"已找到：{feishu_config}"))

    signature = args_signature(args)
    with STATE_LOCK:
        pending_signature = STATE.get("pending_signature")
        pending_count = len(STATE.get("pending") or [])
    if pending_signature != signature:
        checklist.append(check_item("action", "飞书队列", "请先点击“刷新队列”。读取成功后才能启动，避免使用旧设置或过期授权。"))
        blockers.append("飞书队列未刷新")
    elif pending_count <= 0:
        if args.task_mode == "full":
            queue_hint = "任务1读取 steam id 为空且 Steam账号、Steam密码、邮箱账号、邮箱密码完整的行；请检查目标邮箱、起始行、I 列 steam id 或两列密码。"
        elif args.task_mode == "mail_password":
            queue_hint = "功能2读取邮箱账号、邮箱密码完整的行；请检查目标邮箱、起始行、备注列、邮箱密码和安全邮箱容量。"
        else:
            queue_hint = "功能3读取已有 steam id、邮箱账号、邮箱密码完整且 K 列“转发规则”为空的行；K 列有任何内容均跳过。"
        checklist.append(check_item("error", "飞书队列", f"当前任务没有待处理账号。{queue_hint}"))
        blockers.append("没有待处理账号")
    else:
        checklist.append(check_item("ok", "飞书队列", f"已读取 {pending_count} 个待处理账号。"))

    if args.task_mode == "forward_rule":
        forward_from = (getattr(args, "forward_from_email", "") or "").strip()
        forward_to = (getattr(args, "forward_to_email", "") or "").strip()
        invalid = [
            label
            for value, label in [(forward_from, "From"), (forward_to, "Forward to")]
            if not value or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value)
        ]
        if invalid:
            checklist.append(check_item("error", "转发规则", "请填写有效邮箱：" + "、".join(invalid)))
            blockers.append("转发规则邮箱配置无效")
        else:
            checklist.append(check_item("ok", "转发规则", f"From：{forward_from}；Forward to：{forward_to}；规则名：forward；保存后自动核验。"))
        checklist.append(check_item("ok", "登录处理", "启动后逐个复用邮箱专属 Outlook 登录态；未登录时才输入凭据，然后自动创建并核验规则。"))

    pool = steam_outlook_batch.parse_security_mailbox_pool(args.security_mailbox_pool or "")
    missing_pool_passwords = [item["email"] for item in pool if not item.get("password")]
    if args.task_mode in {"full", "mail_password", "forward_rule"} and missing_pool_passwords:
        checklist.append(
            check_item(
                "error",
                "安全邮箱池密码",
                "任务1和任务2使用安全邮箱池时必须填写每个安全邮箱密码；缺少密码：" + ", ".join(missing_pool_passwords[:5]),
            )
        )
        blockers.append("安全邮箱池密码缺失")

    if args.security_code_source == "browser":
        if missing_pool_passwords and args.task_mode not in {"full", "mail_password", "forward_rule"}:
            checklist.append(
                check_item(
                    "error",
                    "安全邮箱池密码",
                    "浏览器收码需要能登录对应安全邮箱；以下安全邮箱缺少密码：" + ", ".join(missing_pool_passwords[:5]),
                )
            )
            blockers.append("安全邮箱池密码缺失")
        if pool:
            security_window_message = (
                "当前使用安全邮箱池，启动后窗口2会按每个账号的备用安全邮箱登录或确认后收码；"
                "不是固定默认安全邮箱。"
            )
        else:
            security_window_message = (
                f"启动后会检查默认安全邮箱 {args.security_email} 的网页登录状态；"
                "如果现有窗口不可操作，会自动打开一个可操作窗口并等待你登录确认。"
            )
        checklist.append(
            check_item(
                "action",
                "安全邮箱窗口",
                security_window_message,
            )
        )

    if args.task_mode in {"full", "mail_password", "forward_rule"} and args.bind_security_email:
        checklist.append(check_item("ok", "指定安全邮箱绑定上限", f"将绑定到 {args.bind_security_email}；刷新队列时按 J 列统计，单个安全邮箱最多处理到 10 个账号。"))
    if args.task_mode in {"full", "mail_password", "forward_rule"} and args.security_mailbox_pool:
        pool_count = len(steam_outlook_batch.parse_security_mailbox_pool(args.security_mailbox_pool))
        checklist.append(check_item("ok", "安全邮箱池", f"已配置 {pool_count} 个安全邮箱，供任务1和任务2共用；刷新队列时会按每个最多 10 个目标邮箱自动分配。"))
        checklist.append(check_item("action", "安全邮箱窗口", "使用安全邮箱池时会打开窗口2；程序会按当前行分配的安全邮箱登录或确认，用于绑定安全邮箱和必要的收码。"))
    elif args.task_mode in {"full", "mail_password", "forward_rule"}:
        checklist.append(check_item("action", "安全邮箱池", f"未填写安全邮箱池，将使用默认安全邮箱 {args.security_email}；单个安全邮箱最多绑定 10 个账号，不足时刷新队列会提示补充。"))
    blocked_count = len(getattr(args, "blocked_security_mailboxes", []) or [])
    if args.task_mode in {"full", "mail_password", "forward_rule"} and blocked_count:
        checklist.append(check_item("action", "受限安全邮箱", f"已持久化记录 {blocked_count} 个 Microsoft 收码受限的安全邮箱；刷新队列时会排除它们，请补充新的安全邮箱。"))

    if args.enable_steam_login or args.enable_dota_check:
        try:
            steam_exe = steam_outlook_batch.find_steam_exe(args.steam_exe)
            checklist.append(check_item("ok", "Steam 程序", f"已找到：{steam_exe}"))
        except SystemExit as exc:
            checklist.append(check_item("error", "Steam 程序", f"{exc}\n请填写 Steam 路径。"))
            blockers.append("Steam 程序不可用")
        except Exception as exc:
            checklist.append(check_item("error", "Steam 程序", f"{exc}\n请填写 Steam 路径。"))
            blockers.append("Steam 程序不可用")

    if args.enable_dota_check:
        missing = [name for name in ["pyautogui", "pyperclip", "pywinauto", "cv2", "numpy", "rapidocr_onnxruntime"] if not module_available(name)]
        if missing:
            checklist.append(check_item("error", "Dota 自动化依赖", "缺少依赖：" + ", ".join(missing) + "\n请运行 python -m pip install -r requirements.txt"))
            blockers.append("Dota 自动化依赖缺失")
        else:
            checklist.append(check_item("ok", "Dota 自动化依赖", "GUI/OCR 依赖已安装。启动后仍需要按状态区完成人工确认。"))

    if not module_available("playwright"):
        checklist.append(check_item("error", "浏览器自动化", "缺少 Playwright。请运行 python -m pip install -r requirements.txt"))
        blockers.append("Playwright 缺失")
    else:
        checklist.append(check_item("ok", "浏览器自动化", "Playwright 可用。"))

    return {"checklist": checklist, "can_start": not blockers, "blockers": blockers}


def set_preflight_state(result: dict) -> None:
    with STATE_LOCK:
        STATE["checklist"] = result["checklist"]
        STATE["can_start"] = result["can_start"]


def parse_optional_int(value, label: str, *, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise RuntimeError(f"{label}必须是整数。")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise RuntimeError(f"{label}必须是整数。")
    text = str(value).strip()
    if not re.fullmatch(r"-?\d+", text):
        raise RuntimeError(f"{label}必须是整数。")
    return int(text)


def build_args(data: dict) -> argparse.Namespace:
    stored_settings = load_ui_settings()
    for key in ["forward_from_email", "forward_to_email"]:
        incoming = (data.get(key) or "").strip()
        stored = (stored_settings.get(key) or "").strip()
        if incoming.casefold() in FORWARD_ADDRESS_PLACEHOLDERS and stored.casefold() not in FORWARD_ADDRESS_PLACEHOLDERS:
            data[key] = stored
    task_mode = data.get("task_mode") or "full"
    security_mailbox_pool = data.get("security_mailbox_pool") or ""
    pool = steam_outlook_batch.parse_security_mailbox_pool(security_mailbox_pool)
    security_email = (data.get("security_email") or "").strip()
    if pool and (
        not security_email
        or security_email.casefold() == steam_outlook_batch.DEFAULT_SECURITY_EMAIL.strip().casefold()
    ):
        security_email = pool[0]["email"]
    security_email = security_email or steam_outlook_batch.DEFAULT_SECURITY_EMAIL
    security_code_source = data.get("security_code_source") or "manual"
    if pool and task_mode in {"full", "mail_password", "forward_rule"}:
        security_code_source = "browser"
    persisted_data = dict(data)
    persisted_data["security_email"] = security_email
    save_ui_settings(persisted_data)
    bind_security_email = data.get("bind_security_email") or ""
    if security_mailbox_pool:
        bind_security_email = ""
    if task_mode in {"full", "mail_password", "forward_rule"} and not security_mailbox_pool and not bind_security_email:
        bind_security_email = security_email
    return argparse.Namespace(
        task_mode=task_mode,
        sheet_url=data.get("sheet_url") or steam_outlook_batch.DEFAULT_FEISHU_SHEET_URL,
        sheet_id=None,
        sheet_range=data.get("sheet_range") or "A1:K1000",
        feishu_auth=data.get("feishu_auth") or "user",
        feishu_config=str(steam_outlook_batch.feishu_tool.DEFAULT_CONFIG),
        feishu_start_script=str(steam_outlook_batch.feishu_tool.DEFAULT_START_SCRIPT),
        lark_mcp_root=str(steam_outlook_batch.feishu_tool.DEFAULT_LARK_MCP_ROOT),
        steam_exe=data.get("steam_exe") or DEFAULT_STEAM_EXE,
        security_email=security_email,
        security_code_source=security_code_source,
        outlook_profile=".playwright-steam-outlook-profile",
        security_profile=".playwright-security-profile",
        outlook_x=parse_optional_int(data.get("outlook_x"), "目标 Outlook 窗口 X 坐标", default=0),
        outlook_y=0,
        security_x=parse_optional_int(data.get("security_x"), "安全邮箱窗口 X 坐标", default=1280),
        security_y=0,
        microsoft_code_timeout=180,
        steam_code_timeout=180,
        steam_ui_timeout=90,
        steam_login_timeout=180,
        steam_login_attempts=parse_optional_int(data.get("steam_login_attempts"), "Steam 登录重试次数", default=2),
        security_email_bind_limit=10,
        limit=parse_optional_int(data.get("limit"), "限制数量", default=None),
        target_email=data.get("target_email") or "",
        start_row=parse_optional_int(data.get("start_row"), "起始行", default=None),
        list_pending=False,
        pause_between=False,
        continue_on_error=True,
        error_dir="errors",
        enable_steam_login=bool(data.get("enable_steam_login", True)) and task_mode == "full",
        enable_dota_check=bool(data.get("enable_dota_check", True)) and task_mode == "full",
        dota_max_attempts=parse_optional_int(data.get("dota_max_attempts"), "Dota2 房间检测最多尝试次数", default=3),
        enable_mail_maintenance=(task_mode in {"full", "mail_password", "forward_rule"}) or bool(data.get("enable_mail_maintenance")),
        bind_security_email=bind_security_email,
        security_mailbox_pool=security_mailbox_pool,
        blocked_security_mailboxes=blocked_security_mailbox_emails(),
        new_email_password=data.get("new_email_password") or "",
        forward_from_email=(data.get("forward_from_email") or DEFAULT_FORWARD_FROM_EMAIL).strip(),
        forward_to_email=data.get("forward_to_email") or "",
    )


def serialize_account(account) -> dict:
    return {
        "row_number": account.row_number,
        "steam_username": account.steam_username,
        "email": account.email,
        "backup_security_email": getattr(account, "backup_security_email", ""),
    }


def worker(args: argparse.Namespace, pause_controller: steam_outlook_batch.PauseController) -> None:
    had_account_error = False
    security_mailbox_limited_message = ""

    def on_pending(accounts):
        with STATE_LOCK:
            STATE["pending"] = [serialize_account(account) for account in accounts]
            STATE["message"] = f"已读取队列：{len(accounts)} 个账号。"

    def on_start(account, index, total):
        with STATE_LOCK:
            STATE["current"] = serialize_account(account)
            STATE["status"] = f"running {index}/{total}"
            STATE["waiting"] = False
            STATE["manual_kind"] = ""
            STATE["message"] = f"正在处理第 {index}/{total} 个：第 {account.row_number} 行 {account.email}"

    def on_done(account, steam_id):
        suffix = f": {steam_id}" if steam_id else ""
        append_log(f"done row {account.row_number}{suffix}")
        with STATE_LOCK:
            STATE["waiting"] = False
            STATE["manual_kind"] = ""
            STATE["status"] = "running"
            STATE["message"] = f"第 {account.row_number} 行已完成{suffix}，正在准备下一步。"

    def on_error(account, exc):
        nonlocal had_account_error
        had_account_error = True
        append_log(f"error row {account.row_number}: {exc}")
        safe_exc = sanitize_log_message(str(exc))
        with STATE_LOCK:
            STATE["status"] = "error"
            STATE["message"] = f"第 {account.row_number} 行失败：{safe_exc}"

    def on_manual_wait(message):
        safe_message = sanitize_log_message(message)
        with STATE_LOCK:
            STATE["waiting"] = True
            STATE["manual_kind"] = "dota_room" if "房间存在" in message and "标记大号" in message else "continue"
            STATE["status"] = "manual action"
            STATE["message"] = safe_message

    def on_manual_resume(message):
        with STATE_LOCK:
            STATE["waiting"] = False
            STATE["manual_kind"] = ""
            STATE["status"] = "running"
            STATE["message"] = ""

    def on_security_mailbox_blocked(email, reason):
        nonlocal security_mailbox_limited_message
        removed = mark_security_mailbox_blocked(email, reason)
        append_log(f"security mailbox blocked: {email}; removed_from_pool={removed}; reason={reason}")
        security_mailbox_limited_message = (
            f"安全邮箱 {email} 被 Microsoft 判定收码受限，已持久化记录"
            f"{'并从安全邮箱池移除' if removed else ''}。请补充新的安全邮箱后点击“刷新队列”，确认容量后再启动。"
        )
        with STATE_LOCK:
            STATE["status"] = "security mailbox limited"
            STATE["message"] = security_mailbox_limited_message
            STATE["can_start"] = False
            STATE["checklist"] = [
                check_item("error", "受限安全邮箱", security_mailbox_limited_message),
                check_item("action", "飞书队列", "补充安全邮箱账号密码后，必须点击“刷新队列”重新计算安全邮箱容量。"),
            ]

    fatal_message = ""
    try:
        with STATE_LOCK:
            STATE["running"] = True
            STATE["status"] = "starting"
            STATE["message"] = f"{TASK_LABELS.get(args.task_mode, args.task_mode)} 已启动，正在使用已刷新队列..."
            STATE["started_at"] = time.time()
            STATE["finished_at"] = None
        append_log(f"worker started: {TASK_LABELS.get(args.task_mode, args.task_mode)}")
        asyncio.run(
            steam_outlook_batch.run_with_callbacks(
                args,
                {
                    "log": append_log,
                    "on_pending": on_pending,
                    "on_account_start": on_start,
                    "on_account_done": on_done,
                    "on_account_error": on_error,
                    "on_manual_wait": on_manual_wait,
                    "on_manual_resume": on_manual_resume,
                    "on_security_mailbox_blocked": on_security_mailbox_blocked,
                    "pause_controller": pause_controller,
                },
            )
        )
    except Exception as exc:
        fatal_message = f"启动失败/运行中止：{exc}"
        append_log(f"fatal: {exc}")
    finally:
        with STATE_LOCK:
            STATE["running"] = False
            STATE["waiting"] = False
            STATE["manual_kind"] = ""
            STATE["status"] = "error" if fatal_message or had_account_error else "idle"
            if security_mailbox_limited_message:
                STATE["status"] = "security mailbox limited"
                STATE["message"] = security_mailbox_limited_message + (
                    f"\n运行已中止：{sanitize_log_message(fatal_message)}" if fatal_message else ""
                )
                STATE["can_start"] = False
            elif fatal_message:
                STATE["message"] = sanitize_log_message(fatal_message)
            elif had_account_error:
                STATE["message"] = STATE.get("message") or f"{TASK_LABELS.get(args.task_mode, args.task_mode)} 有账号处理失败，请查看日志。"
            elif STATE.get("message", "").startswith("正在处理") or STATE.get("status") == "idle":
                STATE["message"] = f"{TASK_LABELS.get(args.task_mode, args.task_mode)} 已结束。"
            STATE["finished_at"] = time.time()


def normalize_ocr_label(text: str) -> str:
    return re.sub(r"[\s:：]+", "", str(text or "")).casefold()


def select_feishu_authorize_ocr_item(items: list[dict]) -> dict | None:
    """Return only the exact authorize button from a verified Feishu consent page."""
    visible = [item for item in items if float(item.get("score") or 0) >= 0.45]
    labels = [normalize_ocr_label(item.get("text") or "") for item in visible]
    combined = "|".join(labels)
    has_reject = any(label in {"拒绝", "reject", "deny"} for label in labels)
    has_context = any(
        marker in combined
        for marker in [
            "请求以下飞书账号进行授权",
            "可授予的权限",
            "获取用户身份标识",
            "requestingauthorization",
            "permissionsrequested",
        ]
    )
    targets = [
        item
        for item in visible
        if normalize_ocr_label(item.get("text") or "") in {"授权", "authorize", "allow"}
    ]
    if has_reject and has_context and len(targets) == 1:
        return targets[0]
    return None


def feishu_consent_controls(window):
    """Return the exact consent controls only while a Feishu consent page is visible."""
    controls = window.descendants()
    labels = [normalize_ocr_label(control.window_text() or "") for control in controls]
    combined = "|".join(labels)
    has_context = any(
        marker in combined
        for marker in [
            "请求以下飞书账号进行授权",
            "可授予的权限",
            "获取用户身份标识",
            "requestingauthorization",
            "permissionsrequested",
        ]
    )
    reject_controls = [
        control
        for control in controls
        if normalize_ocr_label(control.window_text() or "") in {"拒绝", "reject", "deny"}
    ]
    authorize_controls = [
        control
        for control in controls
        if normalize_ocr_label(control.window_text() or "") in {"授权", "authorize", "allow"}
    ]
    if has_context and len(reject_controls) == 1 and len(authorize_controls) == 1:
        return authorize_controls[0]
    return None


def click_feishu_authorize_control(window, control, log=print) -> bool:
    """Click a consent control and require the consent surface to disappear."""
    import pyautogui

    methods = []
    try:
        control.invoke()
        methods.append("UIA invoke")
    except Exception:
        pass
    time.sleep(0.8)
    try:
        if feishu_consent_controls(window) is None:
            log(f"Feishu authorize action confirmed after {'/'.join(methods)}.")
            return True
    except Exception:
        try:
            if not window.exists():
                log(f"Feishu authorize action confirmed after {'/'.join(methods)}; consent window closed.")
                return True
        except Exception:
            pass

    try:
        control.click_input()
        methods.append("UIA click_input")
    except Exception:
        pass
    time.sleep(0.8)
    try:
        if feishu_consent_controls(window) is None:
            log(f"Feishu authorize action confirmed after {'/'.join(methods)}.")
            return True
    except Exception:
        try:
            if not window.exists():
                log(f"Feishu authorize action confirmed after {'/'.join(methods)}; consent window closed.")
                return True
        except Exception:
            pass

    # Chrome's UIA tree can expose the button while ignoring the control
    # pattern. Use the control's own screen rectangle for a real mouse event,
    # avoiding window/DPI coordinate conversions.
    try:
        rect = control.rectangle()
        pyautogui.moveTo((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2, duration=0.05)
        pyautogui.click()
        methods.append("screen click")
    except Exception:
        pass
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        time.sleep(0.4)
        try:
            if feishu_consent_controls(window) is None:
                log(f"Feishu authorize action confirmed after {'/'.join(methods)}.")
                return True
        except Exception:
            try:
                if not window.exists():
                    log(f"Feishu authorize action confirmed after {'/'.join(methods)}; consent window closed.")
                    return True
            except Exception:
                pass
    log(f"Feishu authorize click did not change the consent page after {'/'.join(methods)}.")
    return False


def auto_click_feishu_authorize(
    log=print,
    stop_event: threading.Event | None = None,
    timeout_seconds: int = 50,
) -> bool:
    """Screenshot the Feishu consent window and click its exact OCR button via SendInput."""
    stop_event = stop_event or threading.Event()
    try:
        import cv2
        import numpy as np
        import pyautogui
        from pywinauto import Desktop
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        log(f"Feishu authorize auto-click unavailable: missing GUI/OCR dependency: {exc}")
        return False

    pyautogui.FAILSAFE = False
    engine = RapidOCR()
    deadline = time.monotonic() + timeout_seconds
    next_log = 0.0
    while time.monotonic() < deadline and not stop_event.is_set():
        browser_windows = []
        try:
            for window in Desktop(backend="uia").windows():
                title = (window.window_text() or "").casefold()
                if "chrome" in title or "edge" in title or "chromium" in title:
                    rect = window.rectangle()
                    if rect.width() >= 700 and rect.height() >= 500:
                        browser_windows.append(window)
        except Exception:
            browser_windows = []

        if not browser_windows:
            if time.monotonic() >= next_log:
                log("Feishu authorize auto-click waiting for consent browser window.")
                next_log = time.monotonic() + 5
            time.sleep(0.5)
            continue

        found_text = []
        for target_window in browser_windows:
            try:
                target_window.set_focus()
                time.sleep(0.25)
                rect = target_window.rectangle()
                # Chrome exposes the Feishu consent controls through UIA even
                # when the page title/bitmap is affected by DPI scaling. Use
                # the exact context and a unique button label before falling
                # back to OCR coordinates.
                try:
                    authorize_control = feishu_consent_controls(target_window)
                    if authorize_control is not None:
                        if click_feishu_authorize_control(target_window, authorize_control, log=log):
                            time.sleep(0.8)
                            return True
                        # Do not run OCR against the same unchanged page in
                        # this iteration; retry the full control discovery.
                        continue
                        return True
                except Exception:
                    pass
                try:
                    # Capture from the window handle so Windows DPI scaling
                    # cannot shift the OCR coordinates away from the button.
                    image = target_window.capture_as_image()
                except Exception:
                    image = pyautogui.screenshot(region=(rect.left, rect.top, rect.width(), rect.height()))
                array = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
                result, _ = engine(array)
                items = []
                for box, text, score in result or []:
                    xs = [point[0] for point in box]
                    ys = [point[1] for point in box]
                    items.append(
                        {
                            "text": str(text),
                            "score": float(score),
                            "cx": sum(xs) / len(xs),
                            "cy": sum(ys) / len(ys),
                        }
                    )
                found_text.extend(items)
                target = select_feishu_authorize_ocr_item(items)
                if target is None:
                    continue

                before_path = Path("errors") / "feishu_authorize_before.png"
                before_path.parent.mkdir(parents=True, exist_ok=True)
                image.save(before_path)
                try:
                    target_window.click_input(coords=(int(target["cx"]), int(target["cy"])))
                except Exception:
                    pyautogui.click(int(rect.left + target["cx"]), int(rect.top + target["cy"]))
                # OCR gives us a candidate, but it is not success until the
                # consent surface actually disappears.
                if click_feishu_authorize_control(target_window, target, log=log):
                    try:
                        target_window.capture_as_image().save(Path("errors") / "feishu_authorize_after.png")
                    except Exception:
                        pass
                    return True
            except Exception as exc:
                if time.monotonic() >= next_log:
                    log(f"Feishu authorize auto-click retrying after window/OCR error: {exc}")
                    next_log = time.monotonic() + 5

        if time.monotonic() >= next_log:
            summary = " | ".join(
                f"{item['text']}({item['score']:.2f})"
                for item in sorted(found_text, key=lambda item: item["score"], reverse=True)[:12]
            )
            log(f"Feishu authorize OCR waiting; visible={summary or 'no text'}")
            next_log = time.monotonic() + 5
        time.sleep(0.6)

    if not stop_event.is_set():
        log("Feishu authorize auto-click timed out without a verified consent button.")
    return False


def feishu_login_worker() -> None:
    append_log("Feishu login started")
    with STATE_LOCK:
        STATE["status"] = "feishu login"
        STATE["message"] = "正在进行飞书授权。浏览器如果弹出授权页，请完成登录/授权；完成后本页会自动显示结果。"
    try:
        auto_click_stop = threading.Event()
        auto_click_thread = None
        process = subprocess.Popen(
            [sys.executable, "feishu_tool.py", "login-user"],
            cwd="D:\\BatchSignUp",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        output_lines = []
        if process.stdout is not None:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                output_lines.append(line)
                append_log(f"Feishu login: {line}")
                if line.startswith("http://localhost:"):
                    with STATE_LOCK:
                        STATE["message"] = "飞书授权链接已生成；程序正在截图识别并真实点击“授权”。"
                    webbrowser.open(line)
                    if auto_click_thread is None or not auto_click_thread.is_alive():
                        auto_click_thread = threading.Thread(
                            target=auto_click_feishu_authorize,
                            kwargs={"log": append_log, "stop_event": auto_click_stop},
                            daemon=True,
                        )
                        auto_click_thread.start()
        returncode = process.wait()
        auto_click_stop.set()
        if auto_click_thread is not None:
            auto_click_thread.join(timeout=3)
        output = "\n".join(output_lines)
        if returncode == 0:
            append_log("Feishu login succeeded")
            FEISHU_AUTH_CACHE["checked_at"] = 0.0
            if output:
                append_log(output.splitlines()[-1])
            with STATE_LOCK:
                STATE["status"] = "feishu login ok"
                STATE["message"] = "飞书授权成功。现在可以点击“刷新队列”，确认能读到表格后再点“启动”。"
        else:
            append_log(f"Feishu login failed: {output or returncode}")
            with STATE_LOCK:
                STATE["status"] = "feishu login failed"
                STATE["message"] = f"飞书授权失败：{output or returncode}"
    except Exception as exc:
        append_log(f"Feishu login failed: {exc}")
        with STATE_LOCK:
            STATE["status"] = "feishu login failed"
            STATE["message"] = f"飞书授权失败：{exc}"


class Handler(BaseHTTPRequestHandler):
    def _json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        size = int(self.headers.get("Content-Length", "0") or 0)
        if size <= 0:
            return {}
        return json.loads(self.rfile.read(size).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            with STATE_LOCK:
                self._json(dict(STATE))
            return
        if path == "/api/auth-status":
            query = parse_qs(urlparse(self.path).query)
            self._json({"ok": True, **check_feishu_auth_status(force=query.get("force") == ["1"])})
            return
        if path == "/api/ui-settings":
            self._json({"ok": True, "settings": load_ui_settings()})
            return
        if path == "/" or path.startswith("/index"):
            raw = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path.startswith("/api/"):
            self._json({"ok": False, "error": f"API not found: {path}"}, 404)
            return
        self.send_error(404)

    def do_POST(self):
        global RUN_THREAD, PAUSE_CONTROLLER, FEISHU_LOGIN_THREAD, PENDING_CACHE
        path = urlparse(self.path).path
        if path == "/api/state":
            with STATE_LOCK:
                self._json(dict(STATE))
            return
        if path == "/api/preflight":
            data = self._body()
            try:
                args = build_args(data)
                result = preflight(args)
                set_preflight_state(result)
                self._json({"ok": True, **result})
            except Exception as exc:
                self._json({"ok": False, "error": sanitize_log_message(str(exc))}, 500)
            return
        if path == "/api/pending":
            data = self._body()
            try:
                args = build_args(data)
                with STATE_LOCK:
                    STATE["status"] = "loading pending"
                    STATE["message"] = f"正在读取飞书队列：{TASK_LABELS.get(args.task_mode, args.task_mode)}..."
                    STATE["checklist"] = [check_item("action", "飞书队列", "正在读取并重新计算队列，请稍候...")]
                    STATE["can_start"] = False
                accounts, feishu_config, spreadsheet_token, sheet_id = steam_outlook_batch.read_pending_accounts(args)
                if args.limit:
                    accounts = accounts[: args.limit]
                pruned_security_pool_count = prune_full_security_mailboxes(getattr(args, "security_pool_full_emails", []) or [])
                signature = args_signature(args)
                PENDING_CACHE = {
                    "signature": signature,
                    "accounts": accounts,
                    "feishu_config": feishu_config,
                    "spreadsheet_token": spreadsheet_token,
                    "sheet_id": sheet_id,
                }
                with STATE_LOCK:
                    STATE["pending"] = [serialize_account(account) for account in accounts]
                    STATE["pending_signature"] = signature
                    STATE["skipped_with_remark"] = int(getattr(args, "skipped_with_remark", 0) or 0)
                    STATE["skipped_missing_security_email"] = int(getattr(args, "skipped_missing_security_email", 0) or 0)
                    STATE["skipped_missing_required_fields"] = int(getattr(args, "skipped_missing_required_fields", 0) or 0)
                    STATE["skipped_security_bind_limit"] = int(getattr(args, "skipped_security_bind_limit", 0) or 0)
                    STATE["status"] = "pending loaded"
                    skipped_bind_limit = STATE["skipped_security_bind_limit"]
                    if args.task_mode in {"full", "mail_password", "forward_rule"}:
                        STATE["message"] = "安全邮箱：不够" if skipped_bind_limit else "安全邮箱：够"
                    else:
                        STATE["message"] = f"队列读取完成：{len(accounts)} 个账号。"
                result = preflight(args)
                set_preflight_state(result)
                append_log(
                    f"loaded pending: {len(accounts)}, "
                    f"skipped with remark: {getattr(args, 'skipped_with_remark', 0)}, "
                    f"remark rows: {getattr(args, 'skipped_remark_rows', [])}, "
                    f"skipped missing required fields: {getattr(args, 'skipped_missing_required_fields', 0)}, "
                    f"skipped security bind limit: {getattr(args, 'skipped_security_bind_limit', 0)}, "
                    f"skipped missing security email: {getattr(args, 'skipped_missing_security_email', 0)}, "
                    f"skipped blocked security mailbox pool: {getattr(args, 'skipped_blocked_security_mailbox_pool', 0)}, "
                    f"pruned full security mailboxes: {pruned_security_pool_count}"
                )
                self._json({"ok": True})
            except Exception as exc:
                safe_exc = sanitize_log_message(str(exc))
                auth_expired = any(
                    marker in safe_exc.casefold()
                    for marker in ["99991677", "token expired", "401 unauthorized", "no stored user access token"]
                )
                append_log(f"pending failed: {safe_exc}")
                PENDING_CACHE = None
                with STATE_LOCK:
                    STATE["pending_signature"] = None
                    STATE["skipped_with_remark"] = 0
                    STATE["skipped_missing_security_email"] = 0
                    STATE["skipped_missing_required_fields"] = 0
                    STATE["skipped_security_bind_limit"] = 0
                    STATE["can_start"] = False
                    STATE["checklist"] = [
                        check_item(
                            "error",
                            "飞书授权已过期" if auth_expired else "飞书队列",
                            (
                                "当前飞书登录已失效。请点击“飞书重新授权”，授权成功后再刷新队列。"
                                if auth_expired
                                else f"读取失败：{safe_exc}"
                            ),
                        )
                    ]
                    STATE["status"] = "pending failed"
                    STATE["message"] = (
                        "飞书授权已过期，任务无法启动。请先重新授权。"
                        if auth_expired
                        else f"队列读取失败：{safe_exc}"
                    )
                self._json({"ok": False, "error": safe_exc}, 500)
            return
        if path == "/api/start":
            data = self._body()
            with STATE_LOCK:
                if STATE["running"]:
                    self._json({"ok": False, "error": "already running"}, 409)
                    return
            args = build_args(data)
            try:
                result = preflight(args)
                set_preflight_state(result)
                if not result["can_start"]:
                    raise RuntimeError("启动前检查未通过：" + "；".join(result["blockers"]))
                validate_start_args(args)
                with STATE_LOCK:
                    pending_signature = STATE.get("pending_signature")
                if PENDING_CACHE and PENDING_CACHE.get("signature") == pending_signature == args_signature(args):
                    args.prefetched_pending_bundle = PENDING_CACHE
                else:
                    raise RuntimeError("飞书队列缓存不存在或已过期，请先点击“刷新队列”。")
            except Exception as exc:
                message = f"启动前检查失败：{sanitize_log_message(str(exc))}"
                append_log(message)
                with STATE_LOCK:
                    STATE["running"] = False
                    STATE["waiting"] = False
                    STATE["manual_kind"] = ""
                    STATE["status"] = "start failed"
                    STATE["message"] = message
                self._json({"ok": False, "error": message}, 400)
                return
            PAUSE_CONTROLLER = steam_outlook_batch.PauseController()
            RUN_THREAD = threading.Thread(target=worker, args=(args, PAUSE_CONTROLLER), daemon=True)
            task_label = TASK_LABELS.get(args.task_mode, args.task_mode)
            append_log(f"start requested: {task_label}")
            with STATE_LOCK:
                STATE["running"] = True
                STATE["waiting"] = False
                STATE["status"] = "starting"
                STATE["current"] = None
                STATE["message"] = f"{task_label} 已收到启动请求，正在准备后台任务..."
                STATE["started_at"] = time.time()
                STATE["finished_at"] = None
            RUN_THREAD.start()
            self._json({"ok": True})
            return
        if path == "/api/debug":
            with STATE_LOCK:
                STATE["status"] = "debug ok"
                STATE["message"] = f"服务自检正常：版本 {APP_VERSION}，后端 API 可用。"
                snapshot = dict(STATE)
            append_log(f"debug ok: {APP_VERSION}")
            self._json({"ok": True, "version": APP_VERSION, "state": snapshot})
            return
        if path == "/api/feishu-login":
            try:
                if FEISHU_LOGIN_THREAD and FEISHU_LOGIN_THREAD.is_alive():
                    self._json({"ok": False, "error": "飞书授权正在进行中"}, 409)
                    return
                FEISHU_LOGIN_THREAD = threading.Thread(target=feishu_login_worker, daemon=True)
                FEISHU_LOGIN_THREAD.start()
                with STATE_LOCK:
                    STATE["message"] = "已开始飞书授权。请在弹出的浏览器授权页完成操作，本页会自动更新成功/失败提示。"
                self._json({"ok": True})
            except Exception as exc:
                append_log(f"feishu login failed: {exc}")
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        if path == "/api/continue":
            with STATE_LOCK:
                can_continue = bool(PAUSE_CONTROLLER and STATE.get("waiting") and STATE.get("manual_kind") != "dota_room")
            if not can_continue:
                self._json({"ok": False, "error": "当前没有可继续的普通人工等待。"}, 409)
                return
            with STATE_LOCK:
                STATE["waiting"] = False
                STATE["manual_kind"] = ""
                STATE["message"] = ""
            PAUSE_CONTROLLER.continue_next()
            self._json({"ok": True})
            return
        if path == "/api/dota-found":
            with STATE_LOCK:
                can_decide = bool(PAUSE_CONTROLLER and STATE.get("waiting") and STATE.get("manual_kind") == "dota_room")
            if not can_decide:
                self._json({"ok": False, "error": "当前没有等待 Dota2 房间确认。"}, 409)
                return
            with STATE_LOCK:
                STATE["waiting"] = False
                STATE["manual_kind"] = ""
                STATE["message"] = ""
            PAUSE_CONTROLLER.decide("dota_found")
            self._json({"ok": True})
            return
        if path == "/api/dota-not-found":
            with STATE_LOCK:
                can_decide = bool(PAUSE_CONTROLLER and STATE.get("waiting") and STATE.get("manual_kind") == "dota_room")
            if not can_decide:
                self._json({"ok": False, "error": "当前没有等待 Dota2 房间确认。"}, 409)
                return
            with STATE_LOCK:
                STATE["waiting"] = False
                STATE["manual_kind"] = ""
                STATE["message"] = ""
            PAUSE_CONTROLLER.decide("dota_not_found")
            self._json({"ok": True})
            return
        if path == "/api/stop":
            if PAUSE_CONTROLLER:
                PAUSE_CONTROLLER.stop()
            append_log("stop requested")
            with STATE_LOCK:
                STATE["waiting"] = False
                STATE["manual_kind"] = ""
                if STATE["running"]:
                    STATE["status"] = "stopping"
                    STATE["message"] = "正在停止当前任务..."
                else:
                    STATE["status"] = "idle"
                    STATE["message"] = "当前没有运行中的任务。"
            self._json({"ok": True})
            return
        if path.startswith("/api/"):
            self._json({"ok": False, "error": f"API not found: {path}"}, 404)
            return
        self.send_error(404)

    def log_message(self, format, *args):
        return


def main():
    parser = argparse.ArgumentParser(description="Local web UI for Steam Outlook automation.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/?v={APP_VERSION}"
    print(f"Web UI: {url}")
    print(f"Version: {APP_VERSION}")
    if not args.no_open:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
