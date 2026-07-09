import argparse
import asyncio
import json
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


STATE_LOCK = threading.Lock()
STATE = {
    "running": False,
    "waiting": False,
    "status": "idle",
    "current": None,
    "pending": [],
    "logs": [],
    "message": "",
    "started_at": None,
    "finished_at": None,
}
RUN_THREAD: threading.Thread | None = None
PAUSE_CONTROLLER: steam_outlook_batch.PauseController | None = None
FEISHU_LOGIN_THREAD: threading.Thread | None = None


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Steam Outlook 自动化</title>
  <style>
    :root { color-scheme: light; font-family: "Segoe UI", Arial, sans-serif; }
    body { margin: 0; background: #f5f7fb; color: #172033; }
    header { height: 56px; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; background: #1f2937; color: white; }
    main { display: grid; grid-template-columns: 360px 1fr; gap: 16px; padding: 16px; }
    section { background: white; border: 1px solid #d8dee9; border-radius: 8px; padding: 14px; }
    h2 { font-size: 16px; margin: 0 0 12px; }
    label { display: block; font-size: 12px; color: #516073; margin-top: 10px; }
    input, select { width: 100%; box-sizing: border-box; height: 34px; padding: 6px 8px; border: 1px solid #b8c2d1; border-radius: 6px; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .actions { display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; }
    button { height: 34px; padding: 0 12px; border: 1px solid #9aa7b8; border-radius: 6px; background: #fff; cursor: pointer; }
    button.primary { background: #2563eb; color: white; border-color: #2563eb; }
    button.danger { background: #dc2626; color: white; border-color: #dc2626; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .status { display: flex; gap: 8px; align-items: center; font-size: 14px; }
    .dot { width: 10px; height: 10px; border-radius: 50%; background: #94a3b8; }
    .dot.running { background: #16a34a; }
    .dot.waiting { background: #f59e0b; }
    .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 12px; }
    .metric { border: 1px solid #e2e8f0; border-radius: 6px; padding: 8px; }
    .metric b { display: block; font-size: 20px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid #e2e8f0; padding: 7px 6px; text-align: left; }
    th { background: #f8fafc; position: sticky; top: 0; }
    .table-wrap { max-height: 260px; overflow: auto; border: 1px solid #e2e8f0; border-radius: 6px; }
    pre { height: 360px; margin: 0; overflow: auto; background: #111827; color: #d1d5db; border-radius: 6px; padding: 12px; font-size: 12px; line-height: 1.45; }
    @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <strong>Steam Outlook 自动化</strong>
    <div class="status"><span id="dot" class="dot"></span><span id="status">idle</span></div>
  </header>
  <main>
    <section>
      <h2>控制</h2>
      <label>飞书表格链接</label>
      <input id="sheet_url">
      <div class="row">
        <div><label>读取范围</label><input id="sheet_range" value="A1:I1000"></div>
        <div><label>限制数量</label><input id="limit" type="number" min="1" placeholder="空=全部"></div>
      </div>
      <div class="row">
        <div><label>从此行开始向下</label><input id="start_row" type="number" min="2" placeholder="空=从第2行开始"></div>
        <div><label>安全邮箱</label><input id="security_email" value="__DEFAULT_SECURITY_EMAIL__"></div>
      </div>
      <div class="row">
        <div><label>安全验证码来源</label><select id="security_code_source"><option value="browser">手动网页登录</option><option value="graph">Microsoft Graph 多邮箱</option></select></div>
        <div><label>Graph Client ID</label><input id="graph_client_id" placeholder="可留空使用 graph_config.json"></div>
      </div>
      <label>Graph 多邮箱配置</label>
      <input id="graph_accounts_config" value="graph_accounts.json">
      <div class="row">
        <div><label>Steam 路径</label><input id="steam_exe" placeholder="自动检测"></div>
        <div><label>认证</label><select id="feishu_auth"><option value="user">当前账号</option><option value="app">应用</option></select></div>
      </div>
      <div class="row">
        <div><label>Outlook X</label><input id="outlook_x" type="number" value="0"></div>
        <div><label>安全邮箱 X</label><input id="security_x" type="number" value="1280"></div>
      </div>
      <div class="row">
        <div><label>Steam 登录</label><select id="enable_steam_login"><option value="1">开启</option><option value="0">关闭</option></select></div>
        <div><label>Dota 验证</label><select id="enable_dota_check"><option value="0">关闭</option><option value="1">开启</option></select></div>
      </div>
      <div class="row">
        <div><label>邮箱安全维护</label><select id="enable_mail_maintenance"><option value="0">关闭</option><option value="1">开启</option></select></div>
        <div><label>Dota 最多开房次数</label><input id="dota_max_attempts" type="number" min="1" max="5" value="3"></div>
      </div>
      <label>绑定安全邮箱（可空；开启邮箱安全维护后生效）</label>
      <input id="bind_security_email" placeholder="例如 security@example.com">
      <label>新邮箱密码（可空；留空自动生成并在提示中显示）</label>
      <input id="new_email_password" placeholder="空=每个账号自动生成">
      <div class="actions">
        <button onclick="loadPending()">刷新队列</button>
        <button onclick="feishuLogin()">飞书重新授权</button>
        <button class="primary" onclick="startRun()">启动</button>
        <button onclick="continueNext()">已完成人工操作 / 继续</button>
        <button onclick="dotaFound()">Dota 找到了</button>
        <button onclick="dotaNotFound()">Dota 没找到</button>
        <button class="danger" onclick="stopRun()">停止</button>
      </div>
    </section>
    <section>
      <h2>状态</h2>
      <div class="grid">
        <div class="metric">队列<b id="pending_count">0</b></div>
        <div class="metric">当前行<b id="current_row">-</b></div>
        <div class="metric">等待<b id="waiting">否</b></div>
        <div class="metric">运行<b id="running">否</b></div>
      </div>
      <div id="message" style="margin-bottom:12px;color:#92400e;background:#fffbeb;border:1px solid #fde68a;border-radius:6px;padding:8px;display:none;"></div>
      <div class="table-wrap">
        <table><thead><tr><th>行</th><th>Steam</th><th>邮箱</th></tr></thead><tbody id="pending_body"></tbody></table>
      </div>
    </section>
    <section style="grid-column: 1 / -1;">
      <h2>日志</h2>
      <pre id="logs"></pre>
    </section>
  </main>
<script>
const defaultUrl = "__DEFAULT_URL__";
document.getElementById("sheet_url").value = defaultUrl;
async function api(path, body) {
  const opt = body ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const data = await r.json();
  if (!r.ok || data.ok === false) {
    throw new Error(data.error || `请求失败: ${r.status}`);
  }
  return data;
}
async function callAction(path, body) {
  try {
    await api(path, body);
  } catch (err) {
    alert(err.message || String(err));
  }
  refresh();
}
function settings() {
  const limit = document.getElementById("limit").value.trim();
  return {
    sheet_url: document.getElementById("sheet_url").value.trim(),
    sheet_range: document.getElementById("sheet_range").value.trim(),
    steam_exe: document.getElementById("steam_exe").value.trim(),
    feishu_auth: document.getElementById("feishu_auth").value,
    outlook_x: Number(document.getElementById("outlook_x").value || 0),
    security_x: Number(document.getElementById("security_x").value || 1280),
    limit: limit ? Number(limit) : null
    ,start_row: document.getElementById("start_row").value.trim() ? Number(document.getElementById("start_row").value.trim()) : null
    ,security_email: document.getElementById("security_email").value.trim()
    ,security_code_source: document.getElementById("security_code_source").value
    ,graph_client_id: document.getElementById("graph_client_id").value.trim()
    ,graph_accounts_config: document.getElementById("graph_accounts_config").value.trim()
    ,enable_steam_login: document.getElementById("enable_steam_login").value === "1"
    ,enable_dota_check: document.getElementById("enable_dota_check").value === "1"
    ,dota_max_attempts: Number(document.getElementById("dota_max_attempts").value || 3)
    ,enable_mail_maintenance: document.getElementById("enable_mail_maintenance").value === "1"
    ,bind_security_email: document.getElementById("bind_security_email").value.trim()
    ,new_email_password: document.getElementById("new_email_password").value.trim()
  };
}
async function loadPending(){ await callAction("/api/pending", settings()); }
async function startRun(){ await callAction("/api/start", settings()); }
async function feishuLogin(){ await callAction("/api/feishu-login", {}); }
async function continueNext(){ await callAction("/api/continue", {}); }
async function dotaFound(){ await callAction("/api/dota-found", {}); }
async function dotaNotFound(){ await callAction("/api/dota-not-found", {}); }
async function stopRun(){ await callAction("/api/stop", {}); }
async function refresh(){
  let s;
  try {
    s = await api("/api/state");
  } catch (err) {
    document.getElementById("status").textContent = "服务连接失败";
    document.getElementById("message").textContent = err.message || String(err);
    document.getElementById("message").style.display = "block";
    return;
  }
  document.getElementById("status").textContent = s.status;
  document.getElementById("pending_count").textContent = s.pending.length;
  document.getElementById("current_row").textContent = s.current ? s.current.row_number : "-";
  document.getElementById("waiting").textContent = s.waiting ? "是" : "否";
  document.getElementById("running").textContent = s.running ? "是" : "否";
  const msg = document.getElementById("message");
  msg.textContent = s.message || "";
  msg.style.display = s.message ? "block" : "none";
  const dot = document.getElementById("dot");
  dot.className = "dot" + (s.waiting ? " waiting" : (s.running ? " running" : ""));
  document.getElementById("pending_body").innerHTML = s.pending.map(a => `<tr><td>${a.row_number}</td><td>${a.steam_username}</td><td>${a.email}</td></tr>`).join("");
  const logs = document.getElementById("logs");
  logs.textContent = s.logs.join("\n");
  logs.scrollTop = logs.scrollHeight;
}
setInterval(refresh, 1500);
refresh();
</script>
</body>
</html>
""".replace("__DEFAULT_URL__", steam_outlook_batch.DEFAULT_FEISHU_SHEET_URL).replace(
    "__DEFAULT_SECURITY_EMAIL__", steam_outlook_batch.DEFAULT_SECURITY_EMAIL
)


def append_log(message: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {message}"
    with STATE_LOCK:
        STATE["logs"].append(line)
        STATE["logs"] = STATE["logs"][-500:]


def build_args(data: dict) -> argparse.Namespace:
    return argparse.Namespace(
        sheet_url=data.get("sheet_url") or steam_outlook_batch.DEFAULT_FEISHU_SHEET_URL,
        sheet_id=None,
        sheet_range=data.get("sheet_range") or "A1:I1000",
        feishu_auth=data.get("feishu_auth") or "user",
        feishu_config=str(steam_outlook_batch.feishu_tool.DEFAULT_CONFIG),
        feishu_start_script=str(steam_outlook_batch.feishu_tool.DEFAULT_START_SCRIPT),
        lark_mcp_root=str(steam_outlook_batch.feishu_tool.DEFAULT_LARK_MCP_ROOT),
        steam_exe=data.get("steam_exe") or None,
        security_email=data.get("security_email") or steam_outlook_batch.DEFAULT_SECURITY_EMAIL,
        security_code_source=data.get("security_code_source") or "browser",
        graph_client_id=data.get("graph_client_id") or None,
        graph_config=str(steam_outlook_batch.graph_mail.DEFAULT_CONFIG),
        graph_cache=str(steam_outlook_batch.graph_mail.DEFAULT_CACHE),
        graph_accounts_config=data.get("graph_accounts_config") or str(steam_outlook_batch.graph_mail.DEFAULT_ACCOUNTS_CONFIG),
        graph_all_accounts=True,
        outlook_profile=".playwright-steam-outlook-profile",
        security_profile=".playwright-security-profile",
        outlook_x=int(data.get("outlook_x") or 0),
        outlook_y=0,
        security_x=int(data.get("security_x") or 1280),
        security_y=0,
        microsoft_code_timeout=180,
        steam_code_timeout=180,
        steam_ui_timeout=90,
        steam_login_timeout=180,
        limit=data.get("limit"),
        start_row=data.get("start_row"),
        list_pending=False,
        pause_between=True,
        continue_on_error=False,
        error_dir="errors",
        enable_steam_login=bool(data.get("enable_steam_login", True)),
        enable_dota_check=bool(data.get("enable_dota_check")),
        dota_max_attempts=int(data.get("dota_max_attempts") or 3),
        enable_mail_maintenance=bool(data.get("enable_mail_maintenance")),
        bind_security_email=data.get("bind_security_email") or "",
        new_email_password=data.get("new_email_password") or "",
    )


def serialize_account(account) -> dict:
    return {
        "row_number": account.row_number,
        "steam_username": account.steam_username,
        "email": account.email,
    }


def worker(args: argparse.Namespace, pause_controller: steam_outlook_batch.PauseController) -> None:
    def on_pending(accounts):
        with STATE_LOCK:
            STATE["pending"] = [serialize_account(account) for account in accounts]

    def on_start(account, index, total):
        with STATE_LOCK:
            STATE["current"] = serialize_account(account)
            STATE["status"] = f"running {index}/{total}"
            STATE["waiting"] = False

    def on_done(account, steam_id):
        suffix = f": {steam_id}" if steam_id else ""
        append_log(f"done row {account.row_number}{suffix}")
        with STATE_LOCK:
            STATE["waiting"] = True
            STATE["status"] = "waiting"

    def on_error(account, exc):
        append_log(f"error row {account.row_number}: {exc}")

    def on_manual_wait(message):
        with STATE_LOCK:
            STATE["waiting"] = True
            STATE["status"] = "manual action"
            STATE["message"] = message

    def on_manual_resume(message):
        with STATE_LOCK:
            STATE["waiting"] = False
            STATE["status"] = "running"
            STATE["message"] = ""

    fatal_message = ""
    try:
        with STATE_LOCK:
            STATE["running"] = True
            STATE["status"] = "starting"
            STATE["message"] = ""
            STATE["started_at"] = time.time()
            STATE["finished_at"] = None
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
            STATE["status"] = "error" if fatal_message else "idle"
            STATE["message"] = fatal_message
            STATE["finished_at"] = time.time()


def feishu_login_worker() -> None:
    append_log("Feishu login started")
    with STATE_LOCK:
        STATE["status"] = "feishu login"
        STATE["message"] = "正在进行飞书授权。浏览器如果弹出授权页，请完成登录/授权；完成后本页会自动显示结果。"
    try:
        completed = subprocess.run(
            [sys.executable, "feishu_tool.py", "login-user"],
            cwd="D:\\BatchSignUp",
            capture_output=True,
            text=True,
            check=False,
        )
        output = "\n".join(part.strip() for part in [completed.stdout, completed.stderr] if part.strip())
        if completed.returncode == 0:
            append_log("Feishu login succeeded")
            if output:
                append_log(output.splitlines()[-1])
            with STATE_LOCK:
                STATE["status"] = "feishu login ok"
                STATE["message"] = "飞书授权成功。现在可以点击“刷新队列”，确认能读到表格后再点“启动”。"
        else:
            append_log(f"Feishu login failed: {output or completed.returncode}")
            with STATE_LOCK:
                STATE["status"] = "feishu login failed"
                STATE["message"] = f"飞书授权失败：{output or completed.returncode}"
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
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        size = int(self.headers.get("Content-Length", "0") or 0)
        if size <= 0:
            return {}
        return json.loads(self.rfile.read(size).decode("utf-8"))

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            raw = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_error(404)

    def do_POST(self):
        global RUN_THREAD, PAUSE_CONTROLLER, FEISHU_LOGIN_THREAD
        path = urlparse(self.path).path
        if path == "/api/state":
            with STATE_LOCK:
                self._json(dict(STATE))
            return
        if path == "/api/pending":
            data = self._body()
            try:
                args = build_args(data)
                accounts, _, _, _ = steam_outlook_batch.read_pending_accounts(args)
                if args.limit:
                    accounts = accounts[: args.limit]
                with STATE_LOCK:
                    STATE["pending"] = [serialize_account(account) for account in accounts]
                    STATE["status"] = "pending loaded"
                append_log(f"loaded pending: {len(accounts)}")
                self._json({"ok": True})
            except Exception as exc:
                append_log(f"pending failed: {exc}")
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        if path == "/api/start":
            data = self._body()
            with STATE_LOCK:
                if STATE["running"]:
                    self._json({"ok": False, "error": "already running"}, 409)
                    return
            args = build_args(data)
            PAUSE_CONTROLLER = steam_outlook_batch.PauseController()
            RUN_THREAD = threading.Thread(target=worker, args=(args, PAUSE_CONTROLLER), daemon=True)
            append_log("start requested")
            RUN_THREAD.start()
            self._json({"ok": True})
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
            if PAUSE_CONTROLLER:
                with STATE_LOCK:
                    STATE["waiting"] = False
                    STATE["message"] = ""
                PAUSE_CONTROLLER.continue_next()
            self._json({"ok": True})
            return
        if path == "/api/dota-found":
            if PAUSE_CONTROLLER:
                with STATE_LOCK:
                    STATE["waiting"] = False
                    STATE["message"] = ""
                PAUSE_CONTROLLER.decide("dota_found")
            self._json({"ok": True})
            return
        if path == "/api/dota-not-found":
            if PAUSE_CONTROLLER:
                with STATE_LOCK:
                    STATE["waiting"] = False
                    STATE["message"] = ""
                PAUSE_CONTROLLER.decide("dota_not_found")
            self._json({"ok": True})
            return
        if path == "/api/stop":
            if PAUSE_CONTROLLER:
                PAUSE_CONTROLLER.stop()
            with STATE_LOCK:
                STATE["status"] = "stopping"
            self._json({"ok": True})
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
    url = f"http://{args.host}:{args.port}/"
    print(f"Web UI: {url}")
    if not args.no_open:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
