# Microsoft Graph 邮件读取

用于替代安全邮箱网页窗口。配置完成后，程序可以用 Graph API 读取安全邮箱的最新邮件并提取 Microsoft 安全验证码。

## 准备

需要一个 Microsoft Entra 应用的 `client_id`，应用类型用公共客户端/native app，支持个人 Microsoft 账号，权限包含：

- `Mail.Read`
- `User.Read`
- `offline_access`

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

自动化主流程在 Web UI 选择 `Microsoft Graph 多邮箱` 后，会读取 `graph_accounts.json` 中所有安全邮箱并查找匹配验证码。优先使用邮件正文中包含目标 Hotmail 地址的验证码；如果微软邮件没有写目标地址，则使用最新可识别的 Microsoft 安全验证码作为兜底。
