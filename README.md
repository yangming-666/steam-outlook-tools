# BatchSignUp

Windows 本地账号与邮件工作流工具集：通过浏览器、桌面客户端和飞书表格，辅助管理本人或已获授权的 Outlook、Steam 账号流程。

**状态：持续开发。** 提供本地 Web 操作面板和独立命令行工具；第三方登录、验证码、风控和客户端页面仍可能需要人工处理。自动化测试不能替代真实账号环境验收。

## 功能与入口

| 入口 | 用途 | 指南 |
|---|---|---|
| `steam_web_ui.py` | 本地操作面板，默认 `127.0.0.1:8765` | 下方快速开始 |
| `steam_outlook_batch.py` | Outlook / Steam 批量工作流 | [现有命令](命令.md) |
| `feishu_tool.py` | 飞书文档和表格读写 | [飞书配置](FEISHU_TOOL.md) |
| `graph_mail.py` | Microsoft Graph 邮件工具 | [Graph 配置](GRAPH_MAIL.md) |
| `code_forward_service.py` | 验证码转发服务 | [转发配置](CODE_FORWARD_SERVICE.md) |
| `dota2_arcade.py` | 本地 Dota 2 客户端辅助操作 | [操作指南](DOTA2_ARCADE_AUTOMATION.md) |

```text
本地配置 / 账号输入 → 本地操作面板 → 浏览器与客户端操作
                                 → 结果核对 / 飞书表格写回
                                 → 需要时人工确认
```

## 快速开始

在 Windows 上安装 Python 3.11+，再执行：

```powershell
git clone https://github.com/yangming-666/batchsignup.git
cd batchsignup
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe steam_web_ui.py
```

浏览器打开本地面板后，先配置自己的资源，再选择任务。启动面板不代表任务已完成；以具体步骤的返回结果和人工确认状态为准。保持默认回环地址，不把此面板直接暴露到公网。

## 配置与数据

- 从 `accounts_sample.template.csv` 了解账号输入结构；实际数据留在本地。
- Graph 使用 `graph_config.sample.json`、`graph_accounts.sample.json`。
- 转发服务使用 `code_forward_config.sample.json`。
- 飞书依赖独立配置的 MCP 服务和目标文档权限，详见对应指南。
- 不提交真实密码、Cookie、Token、浏览器 Profile、账号报表或失败截图。

## 验证与问题定位

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
```

提交问题时提供 Windows/Python 版本、使用入口、脱敏日志、预期与实际结果。不要为了测试运行真实账号批处理。已知限制与下一步见 [项目状态](PROJECT_STATUS.md)。

## 参与与许可

[贡献指南](CONTRIBUTING.md) · [安全说明](SECURITY.md)

当前没有仓库级开源许可证；公开源码不代表已授予通用再分发许可。
