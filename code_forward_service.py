import argparse
import json
import re
import smtplib
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import graph_mail


DEFAULT_CONFIG = Path("code_forward_config.json")
DEFAULT_EVENTS_LOG = Path("code_forward_events.jsonl")


@dataclass
class CodeEvent:
    mailbox: str
    code: str
    kind: str
    subject: str
    sender: str
    receivedDateTime: str
    detectedAt: float
    preview: str
    messageId: str = ""


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>验证码转发服务</title>
  <style>
    :root { font-family: "Segoe UI", Arial, sans-serif; color-scheme: light; }
    body { margin: 0; background: #f6f7fb; color: #172033; }
    header { height: 56px; padding: 0 20px; display: flex; align-items: center; justify-content: space-between; background: #263241; color: white; }
    main { padding: 16px; display: grid; grid-template-columns: 320px 1fr; gap: 16px; }
    section { background: white; border: 1px solid #d7deea; border-radius: 8px; padding: 14px; }
    h2 { font-size: 16px; margin: 0 0 12px; }
    button { height: 34px; padding: 0 12px; border: 1px solid #98a6ba; border-radius: 6px; background: white; cursor: pointer; }
    button.primary { background: #2563eb; color: white; border-color: #2563eb; }
    button.danger { background: #dc2626; color: white; border-color: #dc2626; }
    input { width: 100%; box-sizing: border-box; height: 34px; padding: 6px 8px; border: 1px solid #b8c2d1; border-radius: 6px; }
    label { display: block; margin-top: 10px; font-size: 12px; color: #536174; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; }
    .metric { border: 1px solid #e1e7f0; border-radius: 6px; padding: 8px; margin-bottom: 8px; }
    .metric b { display: block; font-size: 22px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 7px 6px; border-bottom: 1px solid #e4e9f1; text-align: left; vertical-align: top; }
    th { background: #f8fafc; position: sticky; top: 0; }
    .table-wrap { max-height: 430px; overflow: auto; border: 1px solid #e1e7f0; border-radius: 6px; }
    pre { height: 220px; overflow: auto; background: #111827; color: #d1d5db; border-radius: 6px; padding: 10px; font-size: 12px; line-height: 1.45; }
    @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header><strong>验证码转发服务</strong><span id="status">loading</span></header>
  <main>
    <section>
      <h2>控制</h2>
      <div class="metric">运行状态<b id="running">-</b></div>
      <div class="metric">邮箱数量<b id="accounts">0</b></div>
      <div class="metric">验证码数量<b id="events">0</b></div>
      <label>查询邮箱</label>
      <input id="mailbox" placeholder="可空，过滤某个邮箱">
      <div class="actions">
        <button class="primary" onclick="post('/api/start')">启动</button>
        <button onclick="post('/api/reload')">重载配置</button>
        <button class="danger" onclick="post('/api/stop')">停止</button>
      </div>
    </section>
    <section>
      <h2>最近验证码</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>时间</th><th>邮箱</th><th>验证码</th><th>来源</th><th>标题</th></tr></thead>
          <tbody id="codes"></tbody>
        </table>
      </div>
    </section>
    <section style="grid-column:1 / -1;">
      <h2>日志</h2>
      <pre id="logs"></pre>
    </section>
  </main>
<script>
async function api(path, body) {
  const opt = body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const data = await r.json();
  if (!r.ok || data.ok === false) throw new Error(data.error || `请求失败 ${r.status}`);
  return data;
}
async function post(path) {
  try { await api(path, {}); } catch (err) { alert(err.message || String(err)); }
  refresh();
}
async function refresh() {
  let s;
  try { s = await api("/api/state"); } catch (err) { document.getElementById("status").textContent = "服务连接失败"; return; }
  document.getElementById("status").textContent = s.running ? "running" : "stopped";
  document.getElementById("running").textContent = s.running ? "是" : "否";
  document.getElementById("accounts").textContent = s.account_count;
  document.getElementById("events").textContent = s.events.length;
  const filter = document.getElementById("mailbox").value.trim().toLowerCase();
  const rows = s.events.filter(e => !filter || e.mailbox.toLowerCase().includes(filter)).map(e =>
    `<tr><td>${new Date(e.detectedAt * 1000).toLocaleString()}</td><td>${e.mailbox}</td><td><b>${e.code}</b></td><td>${e.sender}</td><td>${e.subject}</td></tr>`
  ).join("");
  document.getElementById("codes").innerHTML = rows;
  const logs = document.getElementById("logs");
  logs.textContent = s.logs.join("\n");
  logs.scrollTop = logs.scrollHeight;
}
document.getElementById("mailbox").addEventListener("input", refresh);
setInterval(refresh, 1500);
refresh();
</script>
</body>
</html>
"""


class ForwardService:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.accounts: list[dict[str, Any]] = []
        self.config: dict[str, Any] = {}
        self.events: list[CodeEvent] = []
        self.seen: set[str] = set()
        self.logs: list[str] = []
        self.reload()

    def log(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line)
        with self.lock:
            self.logs.append(line)
            self.logs = self.logs[-500:]

    def reload(self) -> None:
        accounts = graph_mail.load_accounts_config(Path(self.args.accounts_config))
        config_path = Path(self.args.config)
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        with self.lock:
            self.accounts = accounts
            self.config = config
        self.log(f"loaded {len(accounts)} mailbox accounts; config={config_path}")

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        self.log("polling started")

    def stop(self) -> None:
        self.stop_event.set()
        self.log("polling stop requested")

    def loop(self) -> None:
        while not self.stop_event.is_set():
            self.poll_once()
            self.stop_event.wait(self.args.interval)

    def poll_once(self) -> None:
        with self.lock:
            accounts = list(self.accounts)
        for account in accounts:
            if self.stop_event.is_set():
                return
            try:
                self.poll_account(account)
            except Exception as exc:
                self.log(f"{account.get('email')}: poll failed: {exc}")

    def poll_account(self, account: dict[str, Any]) -> None:
        mailbox = account["email"]
        messages = graph_mail.recent_messages(account["client_id"], Path(account["cache"]), top=self.args.top)
        for message in reversed(messages):
            event = build_event(mailbox, message)
            if not event:
                continue
            if not self.args.forward_existing and is_older_than(event.receivedDateTime, self.args.max_age_minutes):
                continue
            key = event_key(event)
            with self.lock:
                if key in self.seen:
                    continue
                self.seen.add(key)
                self.events.append(event)
                self.events = self.events[-self.args.keep:]
            self.persist_event(event)
            self.forward_event(event)
            self.log(f"{mailbox}: forwarded code {event.code} from {event.sender or 'unknown sender'}")

    def persist_event(self, event: CodeEvent) -> None:
        if not self.args.events_log:
            return
        path = Path(self.args.events_log)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")

    def forward_event(self, event: CodeEvent) -> None:
        payload = asdict(event)
        with self.lock:
            config = dict(self.config)
        for webhook in config.get("webhooks") or []:
            try:
                send_webhook(webhook, payload)
            except Exception as exc:
                self.log(f"webhook {webhook.get('name') or webhook.get('url')}: failed: {exc}")
        smtp_config = config.get("smtp") or {}
        if smtp_config.get("enabled"):
            try:
                send_smtp(smtp_config, event)
            except Exception as exc:
                self.log(f"smtp forward failed: {exc}")

    def state(self) -> dict[str, Any]:
        with self.lock:
            return {
                "ok": True,
                "running": bool(self.thread and self.thread.is_alive() and not self.stop_event.is_set()),
                "account_count": len(self.accounts),
                "events": [asdict(event) for event in reversed(self.events[-100:])],
                "logs": list(self.logs),
            }


def build_event(mailbox: str, message: dict[str, Any]) -> CodeEvent | None:
    text = graph_mail.message_text(message)
    code, kind = extract_code(text)
    if not code:
        return None
    sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address", "")
    subject = str(message.get("subject") or "")
    if not likely_code_mail(subject, sender, text):
        return None
    preview = re.sub(r"\s+", " ", str(message.get("bodyPreview") or ""))[:300]
    return CodeEvent(
        mailbox=mailbox,
        code=code,
        kind=kind,
        subject=subject,
        sender=sender,
        receivedDateTime=str(message.get("receivedDateTime") or ""),
        detectedAt=time.time(),
        preview=preview,
        messageId=str(message.get("id") or ""),
    )


def extract_code(text: str) -> tuple[str | None, str]:
    normalized = re.sub(r"\s+", " ", text)
    numeric_patterns = [
        r"security code[^0-9]{0,120}([0-9]{4,8})",
        r"single-use code[^0-9]{0,120}([0-9]{4,8})",
        r"verification code[^0-9]{0,120}([0-9]{4,8})",
        r"login code[^0-9]{0,120}([0-9]{4,8})",
        r"安全代码[^0-9]{0,120}([0-9]{4,8})",
        r"验证码[^0-9]{0,120}([0-9]{4,8})",
    ]
    for pattern in numeric_patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            return match.group(1), "numeric"
    steam_patterns = [
        r"STEAM\s+GUARD\s+CODE[^A-Z0-9]{0,120}([A-Z0-9]{5})",
        r"CODE\s+YOU\s+NEED[^A-Z0-9]{0,120}([A-Z0-9]{5})",
        r"YOUR\s+STEAM\s+CODE[^A-Z0-9]{0,120}([A-Z0-9]{5})",
    ]
    upper = normalized.upper()
    for pattern in steam_patterns:
        match = re.search(pattern, upper, re.I)
        if match:
            return match.group(1), "steam_guard"
    if "STEAM" in upper:
        ignored = {"STEAM", "GUARD", "LOGIN", "EMAIL", "HTTPS", "CODES"}
        for match in re.finditer(r"\b([A-Z0-9]{5})\b", upper):
            candidate = match.group(1)
            if candidate not in ignored and re.search(r"\d", candidate):
                return candidate, "steam_guard"
    fallback = re.search(r"\b([0-9]{6,8})\b", normalized)
    if fallback:
        return fallback.group(1), "numeric"
    return None, ""


def likely_code_mail(subject: str, sender: str, text: str) -> bool:
    haystack = " ".join([subject, sender, text[:1000]]).casefold()
    markers = [
        "code",
        "verification",
        "security",
        "login",
        "microsoft",
        "steam",
        "验证码",
        "安全",
        "登录",
    ]
    return any(marker in haystack for marker in markers)


def event_key(event: CodeEvent) -> str:
    if event.messageId:
        return f"{event.mailbox}|{event.messageId}|{event.code}"
    return f"{event.mailbox}|{event.receivedDateTime}|{event.subject}|{event.code}"


def is_older_than(received_at: str, max_age_minutes: int) -> bool:
    if max_age_minutes <= 0 or not received_at:
        return False
    try:
        normalized = received_at.replace("Z", "+00:00")
        received = datetime.fromisoformat(normalized)
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - received.astimezone(timezone.utc)).total_seconds()
        return age_seconds > max_age_minutes * 60
    except ValueError:
        return False


def send_webhook(webhook: dict[str, Any], payload: dict[str, Any]) -> None:
    url = webhook.get("url")
    if not url:
        return
    method = webhook.get("method") or "POST"
    headers = dict(webhook.get("headers") or {})
    headers.setdefault("Content-Type", "application/json")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=20) as response:
        response.read()


def send_smtp(config: dict[str, Any], event: CodeEvent) -> None:
    recipients = config.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    if not recipients:
        return
    msg = EmailMessage()
    msg["Subject"] = f"验证码 {event.code} - {event.mailbox}"
    msg["From"] = config.get("from") or config.get("username")
    msg["To"] = ", ".join(recipients)
    msg.set_content(
        "\n".join(
            [
                f"邮箱: {event.mailbox}",
                f"验证码: {event.code}",
                f"类型: {event.kind}",
                f"来源: {event.sender}",
                f"标题: {event.subject}",
                f"收信时间: {event.receivedDateTime}",
                "",
                event.preview,
            ]
        )
    )
    with smtplib.SMTP(config["host"], int(config.get("port") or 587), timeout=30) as smtp:
        if config.get("use_tls", True):
            smtp.starttls()
        if config.get("username"):
            smtp.login(config["username"], config.get("password") or "")
        smtp.send_message(msg)


SERVICE: ForwardService | None = None


class Handler(BaseHTTPRequestHandler):
    def _json(self, data: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0") or 0)
        if size <= 0:
            return {}
        return json.loads(self.rfile.read(size).decode("utf-8"))

    def do_GET(self) -> None:
        global SERVICE
        path = urlparse(self.path).path
        if path == "/" or path.startswith("/index"):
            raw = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/state":
            self._json(SERVICE.state())
            return
        if path == "/api/codes":
            query = parse_qs(urlparse(self.path).query)
            mailbox = (query.get("mailbox") or [""])[0].casefold()
            state = SERVICE.state()
            events = state["events"]
            if mailbox:
                events = [event for event in events if event["mailbox"].casefold() == mailbox]
            self._json({"ok": True, "events": events})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        global SERVICE
        path = urlparse(self.path).path
        try:
            if path == "/api/start":
                SERVICE.start()
                self._json({"ok": True})
                return
            if path == "/api/stop":
                SERVICE.stop()
                self._json({"ok": True})
                return
            if path == "/api/reload":
                SERVICE.reload()
                self._json({"ok": True})
                return
            if path == "/api/test-forward":
                body = self._body()
                event = CodeEvent(
                    mailbox=body.get("mailbox") or "test@example.com",
                    code=body.get("code") or "123456",
                    kind="test",
                    subject="Test verification code",
                    sender="code-forward-service",
                    receivedDateTime="",
                    detectedAt=time.time(),
                    preview="This is a test forward event.",
                )
                SERVICE.forward_event(event)
                self._json({"ok": True})
                return
        except Exception as exc:
            SERVICE.log(f"api {path} failed: {exc}")
            self._json({"ok": False, "error": str(exc)}, 500)
            return
        self.send_error(404)

    def log_message(self, format, *args):
        return


def main() -> None:
    global SERVICE
    parser = argparse.ArgumentParser(description="Standalone multi-mailbox verification-code forwarding service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--accounts-config", default=str(graph_mail.DEFAULT_ACCOUNTS_CONFIG))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--events-log", default=str(DEFAULT_EVENTS_LOG))
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--keep", type=int, default=500)
    parser.add_argument("--max-age-minutes", type=int, default=60, help="Skip older messages unless --forward-existing is set. 0 disables age filtering.")
    parser.add_argument("--forward-existing", action="store_true", help="Forward matching messages already present in the latest mailbox page.")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--no-auto-start", action="store_true")
    args = parser.parse_args()

    SERVICE = ForwardService(args)
    if not args.no_auto_start:
        SERVICE.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Code forward service: {url}")
    if not args.no_open:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
