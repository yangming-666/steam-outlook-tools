import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


DEFAULT_CODEX_HOME = Path.home() / ".codex"
DEFAULT_CONFIG = DEFAULT_CODEX_HOME / "scripts" / "lark-mcp.config.json"
DEFAULT_START_SCRIPT = DEFAULT_CODEX_HOME / "scripts" / "start-lark-mcp.ps1"
DEFAULT_LARK_MCP_ROOT = DEFAULT_CODEX_HOME / "vendor" / "lark-mcp"


class FeishuError(RuntimeError):
    pass


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def http_json(method: str, url: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FeishuError(f"HTTP {method} failed: {url}: {exc.code} {exc.reason}: {detail}") from exc
    except Exception as exc:
        raise FeishuError(f"HTTP {method} failed: {url}: {exc}") from exc
    if not raw:
        return {}
    return json.loads(raw)


def start_mcp(start_script: Path, force: bool = False) -> None:
    if not start_script.exists():
        raise FeishuError(f"Missing start script: {start_script}")
    command = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(start_script),
    ]
    if force:
        command.append("-ForceRestart")
    subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_lark_login(config: dict[str, Any], package_root: Path, scopes: str | None = None) -> None:
    cli = package_root / "node_modules" / "@larksuiteoapi" / "lark-mcp" / "dist" / "cli.js"
    if not cli.exists():
        raise FeishuError(f"Missing lark-mcp CLI: {cli}")
    command = [
        "node",
        str(cli),
        "login",
        "-a",
        str(config["appId"]),
        "-s",
        str(config["appSecret"]),
    ]
    if scopes:
        command.extend(["--scope", scopes])
    subprocess.run(command, cwd=str(package_root), check=True)


def get_stored_user_access_token(config: dict[str, Any], package_root: Path) -> str:
    script = r"""
const { authStore } = require(process.argv[2]);
authStore.getLocalAccessToken(process.argv[1])
  .then(token => {
    if (token) process.stdout.write(token);
    process.exit(0);
  })
  .catch(err => {
    process.stderr.write(String(err || 'failed_to_read_local_access_token'));
    process.exit(1);
  });
"""
    store_js = package_root / "node_modules" / "@larksuiteoapi" / "lark-mcp" / "dist" / "auth" / "store.js"
    completed = subprocess.run(
        ["node", "-e", script, str(config["appId"]), str(store_js).replace("\\", "/")],
        capture_output=True,
        text=True,
        check=False,
    )
    token = completed.stdout.strip()
    if token:
        return token
    raise FeishuError("No stored user access token. Run: python feishu_tool.py login-user")


def parse_sse_json(text: str) -> dict[str, Any]:
    data_lines = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if not data_lines:
        return json.loads(text)
    return json.loads("\n".join(data_lines))


def mcp_request(config: dict[str, Any], method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"http://{config['host']}:{config['port']}/mcp"
    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000) % 1_000_000_000,
        "method": method,
    }
    if params is not None:
        payload["params"] = params

    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8")
    result = parse_sse_json(text)
    if "error" in result:
        raise FeishuError(json.dumps(result["error"], ensure_ascii=False))
    return result


def mcp_initialize(config: dict[str, Any]) -> None:
    mcp_request(
        config,
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "feishu-tool", "version": "1.0"},
        },
    )


def mcp_call_tool(config: dict[str, Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    mcp_initialize(config)
    return mcp_request(config, "tools/call", {"name": name, "arguments": arguments})


def get_tenant_access_token(config: dict[str, Any]) -> str:
    response = http_json(
        "POST",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": config["appId"], "app_secret": config["appSecret"]},
    )
    if response.get("code") != 0:
        raise FeishuError(f"tenant_access_token failed: {json.dumps(response, ensure_ascii=False)}")
    return response["tenant_access_token"]


def feishu_api(
    config: dict[str, Any],
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    use_uat: bool = False,
    package_root: Path = DEFAULT_LARK_MCP_ROOT,
) -> dict[str, Any]:
    token = get_stored_user_access_token(config, package_root) if use_uat else get_tenant_access_token(config)
    return http_json(method, f"https://open.feishu.cn{path}", body, {"Authorization": f"Bearer {token}"})


def parse_feishu_url(url: str) -> dict[str, str | None]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    parts = [part for part in parsed.path.split("/") if part]
    info: dict[str, str | None] = {
        "kind": None,
        "token": None,
        "sheet_id": query.get("sheet", [None])[0],
    }
    for idx, part in enumerate(parts[:-1]):
        if part in {"wiki", "sheets", "sheet"}:
            info["kind"] = "sheet" if part in {"sheets", "sheet"} else "wiki"
            info["token"] = parts[idx + 1]
            return info
    raise FeishuError(f"Unsupported Feishu URL: {url}")


def extract_mcp_text(result: dict[str, Any]) -> str:
    content = result.get("result", {}).get("content", [])
    chunks = []
    for item in content:
        if item.get("type") == "text":
            chunks.append(item.get("text", ""))
    return "\n".join(chunks)


def resolve_spreadsheet(config: dict[str, Any], url_or_token: str, sheet_id: str | None = None) -> dict[str, str | None]:
    if url_or_token.startswith("http"):
        parsed = parse_feishu_url(url_or_token)
        kind = parsed["kind"]
        token = parsed["token"]
        sheet_id = sheet_id or parsed["sheet_id"]
    else:
        kind = "sheet"
        token = url_or_token

    if not token:
        raise FeishuError("Missing Feishu token.")

    if kind == "sheet":
        return {"spreadsheet_token": token, "sheet_id": sheet_id}

    result = mcp_call_tool(config, "wiki_v2_space_getNode", {"params": {"token": token}})
    text = extract_mcp_text(result)
    try:
        node = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise FeishuError(f"Could not parse wiki node response: {text}")
        node = json.loads(match.group(0))

    data = node.get("data", node)
    node_data = data.get("node", data)
    obj_token = node_data.get("obj_token") or node_data.get("objToken") or node_data.get("token")
    obj_type = node_data.get("obj_type") or node_data.get("objType")
    if obj_type and obj_type not in {"sheet", "sheets"}:
        raise FeishuError(f"Wiki node is not a spreadsheet: obj_type={obj_type}, obj_token={obj_token}")
    if not obj_token:
        raise FeishuError(f"Could not resolve spreadsheet token from wiki node: {text}")
    return {"spreadsheet_token": obj_token, "sheet_id": sheet_id}


def read_sheet(
    config: dict[str, Any],
    spreadsheet_token: str,
    sheet_id: str,
    cell_range: str,
    use_uat: bool = False,
    package_root: Path = DEFAULT_LARK_MCP_ROOT,
) -> dict[str, Any]:
    api_range = cell_range if "!" in cell_range else f"{sheet_id}!{cell_range}"
    return feishu_api(
        config,
        "GET",
        f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{api_range}",
        use_uat=use_uat,
        package_root=package_root,
    )


def write_sheet(
    config: dict[str, Any],
    spreadsheet_token: str,
    sheet_id: str,
    cell_range: str,
    values: list[list[Any]],
    use_uat: bool = False,
    package_root: Path = DEFAULT_LARK_MCP_ROOT,
) -> dict[str, Any]:
    api_range = cell_range if "!" in cell_range else f"{sheet_id}!{cell_range}"
    return feishu_api(
        config,
        "PUT",
        f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values",
        {"valueRange": {"range": api_range, "values": values}},
        use_uat=use_uat,
        package_root=package_root,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generic local Feishu/Lark MCP and Sheet helper.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--start-script", type=Path, default=DEFAULT_START_SCRIPT)
    parser.add_argument("--lark-mcp-root", type=Path, default=DEFAULT_LARK_MCP_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start").add_argument("--force", action="store_true")
    login = sub.add_parser("login-user")
    login.add_argument("--scope", help="Optional OAuth scopes, separated by spaces or commas.")
    sub.add_parser("list-tools")

    call = sub.add_parser("call-tool")
    call.add_argument("name")
    call.add_argument("arguments_json")

    resolve = sub.add_parser("resolve")
    resolve.add_argument("url_or_token")
    resolve.add_argument("--sheet-id")

    read = sub.add_parser("read-sheet")
    read.add_argument("url_or_token")
    read.add_argument("--sheet-id")
    read.add_argument("--range", default="A1:Z20")
    read.add_argument("--use-uat", action="store_true", help="Use locally stored user access token.")

    write = sub.add_parser("write-sheet")
    write.add_argument("url_or_token")
    write.add_argument("--sheet-id")
    write.add_argument("--range", required=True)
    write.add_argument("--values-json", required=True, help='2D JSON array, for example [["ok"]].')
    write.add_argument("--use-uat", action="store_true", help="Use locally stored user access token.")

    args = parser.parse_args()
    config = load_config(args.config)

    if args.command == "start":
        start_mcp(args.start_script, args.force)
        return

    if args.command == "login-user":
        run_lark_login(config, args.lark_mcp_root, args.scope)
        return

    if args.command in {"list-tools", "call-tool", "resolve"}:
        start_mcp(args.start_script)

    if args.command == "list-tools":
        mcp_initialize(config)
        print(json.dumps(mcp_request(config, "tools/list", {}), ensure_ascii=False, indent=2))
    elif args.command == "call-tool":
        print(json.dumps(mcp_call_tool(config, args.name, json.loads(args.arguments_json)), ensure_ascii=False, indent=2))
    elif args.command == "resolve":
        print(json.dumps(resolve_spreadsheet(config, args.url_or_token, args.sheet_id), ensure_ascii=False, indent=2))
    elif args.command == "read-sheet":
        resolved = resolve_spreadsheet(config, args.url_or_token, args.sheet_id)
        if not resolved.get("sheet_id"):
            raise FeishuError("Missing sheet id. Pass --sheet-id or use a URL with ?sheet=...")
        print(
            json.dumps(
                read_sheet(
                    config,
                    resolved["spreadsheet_token"],
                    resolved["sheet_id"],
                    args.range,
                    use_uat=args.use_uat,
                    package_root=args.lark_mcp_root,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "write-sheet":
        resolved = resolve_spreadsheet(config, args.url_or_token, args.sheet_id)
        if not resolved.get("sheet_id"):
            raise FeishuError("Missing sheet id. Pass --sheet-id or use a URL with ?sheet=...")
        values = json.loads(args.values_json)
        if not isinstance(values, list) or any(not isinstance(row, list) for row in values):
            raise FeishuError("--values-json must be a 2D JSON array.")
        print(
            json.dumps(
                write_sheet(
                    config,
                    resolved["spreadsheet_token"],
                    resolved["sheet_id"],
                    args.range,
                    values,
                    use_uat=args.use_uat,
                    package_root=args.lark_mcp_root,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
