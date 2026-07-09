# 验证码转发独立服务

这个服务独立于当前 Steam/Outlook 自动化 Web UI。它复用 `graph_accounts.json` 中已授权的 Microsoft Graph 邮箱，轮询 100+ 邮箱里的新邮件，提取验证码并转发到 Webhook 或 SMTP 邮箱。

## 启动

1. 复制配置：

```bat
copy code_forward_config.sample.json code_forward_config.json
```

2. 按需修改 `code_forward_config.json`：

- `webhooks`: 转发到 HTTP 接口。
- `smtp.enabled`: 设为 `true` 后通过邮件转发。

3. 启动服务：

```bat
run_code_forward_service.bat
```

默认页面：

```text
http://127.0.0.1:8770/
```

默认只转发最近 60 分钟内收到的验证码，避免首次启动时把历史邮件重复转发。需要转发现有最近列表里的旧邮件时，用：

```bat
python code_forward_service.py --forward-existing
```

## API

- `GET /api/state`: 服务状态、账号数量、最近验证码。
- `GET /api/codes?mailbox=xxx@hotmail.com`: 查询指定邮箱最近验证码。
- `POST /api/start`: 开始轮询。
- `POST /api/stop`: 停止轮询。
- `POST /api/reload`: 重新加载账号和转发配置。

## Webhook 数据

```json
{
  "mailbox": "target@hotmail.com",
  "code": "123456",
  "kind": "numeric",
  "subject": "Microsoft account security code",
  "sender": "account-security-noreply@accountprotection.microsoft.com",
  "receivedDateTime": "2026-07-09T08:00:00Z",
  "detectedAt": 1783584000.0,
  "preview": "..."
}
```

## 注意

- `graph_accounts.json` 里的每个账号必须先通过 `python graph_mail.py login --email 邮箱` 完成授权。
- 如果 token 过期，服务会在页面日志里显示失败账号，需要重新登录该邮箱。
- 转发服务只读取邮件并转发验证码，不会操作现有 Steam/Outlook 自动化流程。
