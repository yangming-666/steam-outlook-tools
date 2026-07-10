import argparse
import html
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_CACHE = Path(".graph-token-cache.bin")
DEFAULT_CONFIG = Path("graph_config.json")
DEFAULT_ACCOUNTS_CONFIG = Path("graph_accounts.json")
SCOPES = ["Mail.Read", "User.Read"]
AUTHORITY = "https://login.microsoftonline.com/consumers"
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


class GraphMailError(RuntimeError):
    pass


def validate_client_id(client_id: str, source: str = "Microsoft Graph client_id") -> str:
    value = (client_id or "").strip()
    if not value:
        raise GraphMailError(f"Missing {source}.")
    if "PASTE_YOUR" in value or "YOUR_CLIENT_ID" in value or value in {"你的 client id", "client_id"}:
        raise GraphMailError(f"{source} still contains the sample placeholder. Paste the real Microsoft Entra application client_id.")
    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
        raise GraphMailError(f"Invalid {source}: expected a Microsoft Entra application client_id GUID.")
    return value


def load_client_id(config_path: Path = DEFAULT_CONFIG, client_id: str | None = None) -> str:
    if client_id:
        return validate_client_id(client_id, "--client-id")
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if data.get("client_id"):
            return validate_client_id(data["client_id"], f"{config_path} client_id")
    raise GraphMailError(
        f"Missing Microsoft Graph client_id for the legacy standalone Graph tool. "
        f"Create {config_path} from graph_config.sample.json if you intentionally run graph_mail.py. "
        "The Steam Outlook Web UI task flows no longer use Graph."
    )


def import_msal():
    try:
        import msal
    except ImportError as exc:
        raise GraphMailError("Missing dependency msal. Run: python -m pip install -r requirements.txt") from exc
    return msal


def load_cache(cache_path: Path):
    msal = import_msal()
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text(encoding="utf-8"))
    return cache


def save_cache(cache, cache_path: Path) -> None:
    if cache.has_state_changed:
        cache_path.write_text(cache.serialize(), encoding="utf-8")


def build_app(client_id: str, cache):
    msal = import_msal()
    return msal.PublicClientApplication(client_id=client_id, authority=AUTHORITY, token_cache=cache)


def begin_login_flow(client_id: str, cache_path: Path) -> tuple[Any, Any, dict[str, Any]]:
    cache = load_cache(cache_path)
    app = build_app(client_id, cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        description = flow.get("error_description") or flow.get("error") or str(flow)
        error_uri = flow.get("error_uri")
        if error_uri:
            description = f"{description}\n{error_uri}"
        raise GraphMailError(f"Failed to create device flow: {description}")
    return app, cache, flow


def device_flow_message(email: str, flow: dict[str, Any]) -> str:
    verification_uri = flow.get("verification_uri") or flow.get("verification_url") or "https://microsoft.com/devicelogin"
    user_code = flow.get("user_code") or ""
    expires_in = flow.get("expires_in")
    lines = [
        f"正在为 {email} 开启 Microsoft Graph API 登录。",
        f"打开授权页面：{verification_uri}",
    ]
    if user_code:
        lines.append(f"输入代码：{user_code}")
    if expires_in:
        lines.append(f"代码有效期：约 {int(expires_in) // 60} 分钟")
    lines.append("授权完成后程序会自动继续；如果长时间无响应，可点停止。")
    return "\n".join(lines)


def complete_login_flow(app, cache, flow: dict[str, Any], cache_path: Path) -> dict[str, Any]:
    result = app.acquire_token_by_device_flow(flow)
    save_cache(cache, cache_path)
    if "access_token" not in result:
        raise GraphMailError(json.dumps(result, ensure_ascii=False))
    return result


def login(client_id: str, cache_path: Path) -> dict[str, Any]:
    app, cache, flow = begin_login_flow(client_id, cache_path)
    print(flow["message"], flush=True)
    return complete_login_flow(app, cache, flow, cache_path)


def get_token(client_id: str, cache_path: Path) -> str:
    cache = load_cache(cache_path)
    app = build_app(client_id, cache)
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
    save_cache(cache, cache_path)
    if not result or "access_token" not in result:
        raise GraphMailError("No valid Graph token. Run: python graph_mail.py login --client-id YOUR_CLIENT_ID")
    return result["access_token"]


def safe_cache_name(email: str) -> str:
    return ".graph-token-" + re.sub(r"[^A-Za-z0-9_.-]+", "_", email).lower() + ".bin"


def load_accounts_config(path: Path = DEFAULT_ACCOUNTS_CONFIG) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    accounts = data.get("accounts", data if isinstance(data, list) else [])
    if not isinstance(accounts, list):
        raise GraphMailError(f"Invalid accounts config: {path}")
    normalized = []
    for account in accounts:
        email = account.get("email") or account.get("name")
        client_id = account.get("client_id") or data.get("client_id")
        cache = account.get("cache") or safe_cache_name(email or "default")
        if not email or not client_id:
            raise GraphMailError(f"Each Graph account needs email and client_id: {account}")
        normalized.append({"email": email, "client_id": validate_client_id(client_id, f"{path} account client_id"), "cache": cache})
    return normalized


def save_account_config(path: Path, email: str, client_id: str, cache: Path) -> None:
    data: dict[str, Any] = {"accounts": []}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, list):
            data = {"accounts": raw}
    accounts = data.setdefault("accounts", [])
    existing = next((item for item in accounts if (item.get("email") or "").casefold() == email.casefold()), None)
    item = {"email": email, "client_id": client_id, "cache": str(cache)}
    if existing:
        existing.update(item)
    else:
        accounts.append(item)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def graph_get(access_token: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = GRAPH_ROOT + path
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def get_me(client_id: str, cache_path: Path) -> dict[str, Any]:
    return graph_get(get_token(client_id, cache_path), "/me")


def recent_messages(client_id: str, cache_path: Path, top: int = 20) -> list[dict[str, Any]]:
    token = get_token(client_id, cache_path)
    response = graph_get(
        token,
        "/me/messages",
        {
            "$top": str(top),
            "$orderby": "receivedDateTime desc",
            "$select": "id,conversationId,subject,bodyPreview,body,from,toRecipients,receivedDateTime",
        },
    )
    return response.get("value", [])


def message_text(message: dict[str, Any]) -> str:
    body = message.get("body") or {}
    content = body.get("content") or ""
    content = re.sub(r"<[^>]+>", " ", content)
    return " ".join(
        [
            str(message.get("subject") or ""),
            str(message.get("bodyPreview") or ""),
            html.unescape(content),
        ]
    )


def extract_security_code(text: str) -> str | None:
    normalized = re.sub(r"\s+", " ", text)
    patterns = [
        r"security code[^0-9]{0,100}([0-9]{6,8})",
        r"single-use code[^0-9]{0,100}([0-9]{6,8})",
        r"code[^0-9]{0,100}([0-9]{6,8})",
        r"安全代码[^0-9]{0,100}([0-9]{6,8})",
        r"验证码[^0-9]{0,100}([0-9]{6,8})",
        r"\b([0-9]{6,8})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            return match.group(1)
    return None


def find_security_code(client_id: str, cache_path: Path, target_email: str, timeout_seconds: int = 180) -> str:
    deadline = time.monotonic() + timeout_seconds
    target = target_email.casefold()
    while time.monotonic() < deadline:
        for message in recent_messages(client_id, cache_path, top=25):
            text = message_text(message)
            lower = text.casefold()
            subject = str(message.get("subject") or "").casefold()
            sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address", "").casefold()
            if "microsoft" not in lower and "microsoft" not in sender and "security" not in subject and "安全" not in subject:
                continue
            if target not in lower and target_email:
                # Microsoft sometimes omits the account address in the preview/body; keep looking but allow generic codes below.
                pass
            code = extract_security_code(text)
            if code:
                return code
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for Microsoft security code for {target_email}.")


def find_security_code_in_account(account: dict[str, Any], target_email: str) -> tuple[str | None, bool]:
    """Return (code, exact_match). exact_match means target email appeared in the message text."""
    target = target_email.casefold()
    fallback_code = None
    for message in recent_messages(account["client_id"], Path(account["cache"]), top=25):
        text = message_text(message)
        lower = text.casefold()
        subject = str(message.get("subject") or "").casefold()
        sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address", "").casefold()
        if "microsoft" not in lower and "microsoft" not in sender and "security" not in subject and "安全" not in subject:
            continue
        code = extract_security_code(text)
        if not code:
            continue
        if target and target in lower:
            return code, True
        if not fallback_code:
            fallback_code = code
    return fallback_code, False


def find_security_code_across_accounts(accounts: list[dict[str, Any]], target_email: str, timeout_seconds: int = 180) -> tuple[str, str]:
    if not accounts:
        raise GraphMailError(f"No Graph accounts configured. Create {DEFAULT_ACCOUNTS_CONFIG}.")
    deadline = time.monotonic() + timeout_seconds
    last_fallback: tuple[str, str] | None = None
    while time.monotonic() < deadline:
        for account in accounts:
            try:
                code, exact = find_security_code_in_account(account, target_email)
            except Exception:
                continue
            if code and exact:
                return code, account["email"]
            if code and not last_fallback:
                last_fallback = (code, account["email"])
        if last_fallback:
            return last_fallback
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for Microsoft security code for {target_email} in {len(accounts)} mailboxes.")


def parse_graph_credential(credential: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for item in (credential or "").split(";"):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        parts[key.strip()] = value.strip()
    email = parts.get("graph")
    cache = parts.get("cache")
    if not email or not cache:
        raise GraphMailError("Invalid API凭据. Expected format: graph:<email>;cache:<cache_path>;saved:<time>")
    return {"email": email, "cache": cache, "saved": parts.get("saved", "")}


def export_graph_credential_package(credential: str, out_dir: Path, config_path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    parsed = parse_graph_credential(credential)
    cache_path = Path(parsed["cache"])
    if not cache_path.exists():
        raise GraphMailError(f"Token cache file not found: {cache_path}")
    if not config_path.exists():
        raise GraphMailError(f"Graph config not found: {config_path}")

    safe_email = re.sub(r"[^A-Za-z0-9_.-]+", "_", parsed["email"]).strip("_") or "mailbox"
    package_dir = out_dir / safe_email
    package_dir.mkdir(parents=True, exist_ok=True)

    cache_target = package_dir / cache_path.name
    config_target = package_dir / config_path.name
    script_target = package_dir / Path(__file__).name
    requirements_target = package_dir / "requirements.txt"
    shutil.copy2(cache_path, cache_target)
    shutil.copy2(config_path, config_target)
    shutil.copy2(Path(__file__), script_target)
    requirements_target.write_text("msal>=1.30.0\n", encoding="utf-8")

    portable_credential = f"graph:{parsed['email']};cache:{cache_target.name};saved:{parsed.get('saved', '')}"
    manifest = {
        "email": parsed["email"],
        "credential": portable_credential,
        "cache": cache_target.name,
        "config": config_target.name,
        "script": script_target.name,
        "requirements": requirements_target.name,
        "saved": parsed.get("saved", ""),
        "usage": [
            f"cd {package_dir}",
            f"python -m pip install -r {requirements_target.name}",
            f'python {script_target.name} --config {config_target.name} credential-me "{portable_credential}"',
            f'python {script_target.name} --config {config_target.name} credential-recent "{portable_credential}" --top 3',
        ],
    }
    (package_dir / "credential.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (package_dir / "README.txt").write_text(
        "\n".join(
            [
                "Microsoft Graph mailbox credential package",
                "",
                f"Email: {parsed['email']}",
                f"Credential: {portable_credential}",
                "",
                "Keep the cache file private. Anyone with this package can use the delegated mailbox token until it expires or is revoked.",
                "Validate from this directory:",
                f"python -m pip install -r {requirements_target.name}",
                f'python {script_target.name} --config {config_target.name} credential-me "{portable_credential}"',
                f'python {script_target.name} --config {config_target.name} credential-recent "{portable_credential}" --top 3',
            ]
        ),
        encoding="utf-8",
    )
    return {"package_dir": str(package_dir), **manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read Outlook/Hotmail mail with Microsoft Graph.")
    parser.add_argument("--client-id")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--accounts-config", type=Path, default=DEFAULT_ACCOUNTS_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    login_parser = sub.add_parser("login")
    login_parser.add_argument("--email", help="Save this login under a security mailbox email in graph_accounts.json.")
    sub.add_parser("me")
    recent = sub.add_parser("recent")
    recent.add_argument("--top", type=int, default=5)
    credential_me = sub.add_parser("credential-me", help="Validate a sheet K-column API credential.")
    credential_me.add_argument("credential")
    credential_recent = sub.add_parser("credential-recent", help="Read recent messages using a sheet K-column API credential.")
    credential_recent.add_argument("credential")
    credential_recent.add_argument("--top", type=int, default=5)
    credential_export = sub.add_parser("credential-export", help="Export a K-column API credential with its cache/config files.")
    credential_export.add_argument("credential")
    credential_export.add_argument("--out-dir", type=Path, default=Path("graph_credential_exports"))
    code = sub.add_parser("security-code")
    code.add_argument("target_email")
    code.add_argument("--timeout", type=int, default=180)
    code.add_argument("--all-accounts", action="store_true")
    sub.add_parser("list-accounts")
    args = parser.parse_args()

    if args.command == "login":
        client_id = load_client_id(args.config, args.client_id)
        result = login(client_id, args.cache)
        email = args.email or result.get("id_token_claims", {}).get("preferred_username")
        if email:
            save_account_config(args.accounts_config, email, client_id, args.cache)
        print(json.dumps({"ok": True, "account": result.get("id_token_claims", {}).get("preferred_username")}, ensure_ascii=False, indent=2))
    elif args.command == "me":
        client_id = load_client_id(args.config, args.client_id)
        print(json.dumps(get_me(client_id, args.cache), ensure_ascii=False, indent=2))
    elif args.command == "recent":
        client_id = load_client_id(args.config, args.client_id)
        messages = recent_messages(client_id, args.cache, args.top)
        print(json.dumps(messages, ensure_ascii=False, indent=2))
    elif args.command == "credential-me":
        parsed = parse_graph_credential(args.credential)
        client_id = load_client_id(args.config, args.client_id)
        me = get_me(client_id, Path(parsed["cache"]))
        print(json.dumps({"credential": parsed, "me": me}, ensure_ascii=False, indent=2))
    elif args.command == "credential-recent":
        parsed = parse_graph_credential(args.credential)
        client_id = load_client_id(args.config, args.client_id)
        messages = recent_messages(client_id, Path(parsed["cache"]), args.top)
        print(json.dumps({"credential": parsed, "messages": messages}, ensure_ascii=False, indent=2))
    elif args.command == "credential-export":
        print(json.dumps(export_graph_credential_package(args.credential, args.out_dir, args.config), ensure_ascii=False, indent=2))
    elif args.command == "security-code":
        if args.all_accounts:
            code_value, mailbox = find_security_code_across_accounts(load_accounts_config(args.accounts_config), args.target_email, args.timeout)
            print(json.dumps({"code": code_value, "mailbox": mailbox}, ensure_ascii=False, indent=2))
        else:
            client_id = load_client_id(args.config, args.client_id)
            print(find_security_code(client_id, args.cache, args.target_email, args.timeout))
    elif args.command == "list-accounts":
        print(json.dumps(load_accounts_config(args.accounts_config), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
