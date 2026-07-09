import argparse
import html
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_CACHE = Path(".graph-token-cache.bin")
DEFAULT_CONFIG = Path("graph_config.json")
DEFAULT_ACCOUNTS_CONFIG = Path("graph_accounts.json")
SCOPES = ["Mail.Read", "offline_access", "User.Read"]
AUTHORITY = "https://login.microsoftonline.com/consumers"
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


class GraphMailError(RuntimeError):
    pass


def load_client_id(config_path: Path = DEFAULT_CONFIG, client_id: str | None = None) -> str:
    if client_id:
        return client_id
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if data.get("client_id"):
            return data["client_id"]
    raise GraphMailError(f"Missing Microsoft Graph client_id. Create {config_path} or pass --client-id.")


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


def login(client_id: str, cache_path: Path) -> dict[str, Any]:
    cache = load_cache(cache_path)
    app = build_app(client_id, cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise GraphMailError(f"Failed to create device flow: {flow}")
    print(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    save_cache(cache, cache_path)
    if "access_token" not in result:
        raise GraphMailError(json.dumps(result, ensure_ascii=False))
    return result


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
        normalized.append({"email": email, "client_id": client_id, "cache": cache})
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
