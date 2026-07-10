# Microsoft Graph 邮件读取

历史独立工具说明。当前 Steam Outlook Web UI 的三个任务流程不再使用 Microsoft Graph，也没有 Graph Client ID 配置项。

功能3已经改为 Outlook 转发规则方案：登录邮箱后在 Outlook Rules/Forwarding 中把 Steam Guard / Microsoft 安全码邮件转发到指定收码邮箱，然后写回 K 列“转发规则”状态。

只有在你明确要单独运行 `graph_mail.py` / `graph_login.bat` 时，才需要继续阅读本文。

## 准备

需要一个 Microsoft Entra 应用的 `client_id`。`client_id` 是应用标识，不是邮箱账号密码，也不是 K 列最终写回的 API 凭据。

## 获取 Microsoft Graph Client ID

官方文档：

- App registration: https://learn.microsoft.com/en-us/graph/auth-register-app-v2
- Device code flow: https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code
- Graph permissions: https://learn.microsoft.com/en-us/graph/permissions-reference

操作步骤：

1. 打开 `https://entra.microsoft.com`。
2. 进入 `Identity` -> `Applications` -> `App registrations` -> `New registration`。
3. Name 随便填，例如 `BatchSignUp Mail Reader`。
4. Supported account types 选择：
   - 只处理 Hotmail/Outlook 个人邮箱：`Personal Microsoft accounts only`。
   - 同时可能处理企业/学校邮箱：`Accounts in any organizational directory and personal Microsoft accounts`。
5. Redirect URI 可以先不填。当前工具使用 device code flow，不依赖回调地址。
6. 点 `Register` 后，在 Overview 页面复制 `Application (client) ID`。
7. 把 `Application (client) ID` 写入本目录的 `graph_config.json`。
8. 进入 `API permissions`，添加 Microsoft Graph delegated permissions：

- `Mail.Read`
- `User.Read`

当前工具使用的是 delegated/user 授权：每个邮箱都必须由用户在 Microsoft 授权页面确认一次。不要使用 client secret；这个工具不是 application permission / daemon 模式。MSAL 会在 device code flow 中处理刷新 token 所需的保留 scope，不要在代码里手动传 `offline_access`。

把 `graph_config.sample.json` 复制为 `graph_config.json`，填入：

```json
{
  "client_id": "你的 client id"
}
```

## 登录单个安全邮箱

```bat
cd /d D:\BatchSignUp
graph_login.bat security@example.com
```

按终端里的 device code 提示在浏览器授权对应安全邮箱。

检查当前授权账号：

```bat
python graph_mail.py me
```

测试读取验证码：

```bat
python graph_mail.py security-code target@example.com --timeout 180
```

## 多个安全邮箱

每个安全邮箱登录一次：

```bat
graph_login.bat security1@example.com
graph_login.bat security2@example.com
```

登录成功后会自动写入 `graph_accounts.json`。查看已保存安全邮箱：

```bat
python graph_mail.py list-accounts
```

从所有安全邮箱里智能查找目标账号验证码：

```bat
python graph_mail.py security-code target@example.com --all-accounts --timeout 180
```

## 历史 K 列 API 凭据说明

旧 Graph 方案曾把 K 列写成类似：

```text
graph:user@example.com;cache:.graph-token-user_example.com.bin;saved:2026-07-09 19:00:00
```

含义：

- `graph`：授权成功的邮箱。
- `cache`：本项目目录下的 MSAL token cache 文件。
- `saved`：写回时间。

能不能给其他开发直接用，取决于是否同时具备：

- 同一个 `graph_config.json` / Graph Client ID。
- K 列里的 `cache` 文件，例如 `.graph-token-user_example.com.bin`。
- 运行环境能访问 Microsoft Graph。

只复制 K 列文本到另一台机器不够；还必须把对应 `.graph-token-*.bin` cache 文件一起交付，或者在另一台机器上重新执行功能3/授权生成新的 cache。

开发验证命令：

```bat
python graph_mail.py credential-me "graph:user@example.com;cache:.graph-token-user_example.com.bin;saved:2026-07-09 19:00:00"
python graph_mail.py credential-recent "graph:user@example.com;cache:.graph-token-user_example.com.bin;saved:2026-07-09 19:00:00" --top 3
```

推荐交付给其他开发时先导出完整包：

```bat
python graph_mail.py credential-export "graph:user@example.com;cache:.graph-token-user_example.com.bin;saved:2026-07-09 19:00:00" --out-dir graph_credential_exports
```

导出目录会包含：

- `graph_config.json`
- `.graph-token-*.bin`
- `graph_mail.py`
- `requirements.txt`
- `credential.json`
- `README.txt`

开发把该目录拿走后，在目录中按 `README.txt` 执行验证命令即可，不需要再从原项目目录找脚本。这个包必须按敏感凭据处理；拥有 cache 文件的人可以在 token 有效/可刷新期间读取对应邮箱授权范围内的邮件。

开发代码里可复用的核心调用：

```python
from pathlib import Path
import graph_mail

credential = "graph:user@example.com;cache:.graph-token-user_example.com.bin;saved:2026-07-09 19:00:00"
parsed = graph_mail.parse_graph_credential(credential)
client_id = graph_mail.load_client_id()
messages = graph_mail.recent_messages(client_id, Path(parsed["cache"]), top=10)
```
