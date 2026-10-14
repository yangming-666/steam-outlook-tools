# Feishu 通用读写工具

本目录的 `feishu_tool.py` 复用 `E:\PVE` 里记录的本机 Lark MCP：

- MCP endpoint: `http://127.0.0.1:33339/mcp`
- 配置: `C:\Users\ym199\.codex\scripts\lark-mcp.config.json`
- 启动脚本: `C:\Users\ym199\.codex\scripts\start-lark-mcp.ps1`

## 常用命令

启动 MCP：

```powershell
python feishu_tool.py start
```

列出当前 MCP 工具：

```powershell
python feishu_tool.py list-tools
```

解析 wiki 链接到真实表格 token：

```powershell
python feishu_tool.py resolve "https://your-team.feishu.cn/wiki/YOUR_DOCUMENT_TOKEN?sheet=YOUR_SHEET_ID"
```

读取表格范围：

```powershell
python feishu_tool.py read-sheet "https://your-team.feishu.cn/wiki/YOUR_DOCUMENT_TOKEN?sheet=YOUR_SHEET_ID" --range A1:Z20
```

写入单元格：

```powershell
python feishu_tool.py write-sheet "https://your-team.feishu.cn/wiki/YOUR_DOCUMENT_TOKEN?sheet=YOUR_SHEET_ID" --range A1:A1 --values-json '[[\"test\"]]'
```

如果写入返回 `91403 Forbidden`，说明当前飞书应用对该文档没有编辑权限，或开放平台没有开通表格写权限。需要把应用/机器人加入该文档协作者并授予可编辑权限，再重试。

## 用当前登录账号写入

如果浏览器里的飞书账号本身有编辑权限，不要用默认应用身份，先登录生成 `user_access_token`：

```powershell
python feishu_tool.py login-user
```

登录完成后用 `--use-uat` 写入：

```powershell
python feishu_tool.py write-sheet "https://your-team.feishu.cn/wiki/YOUR_DOCUMENT_TOKEN?sheet=YOUR_SHEET_ID" --range Z20:Z20 --values-json '[[\"test\"]]' --use-uat
```

读取也可以走当前账号：

```powershell
python feishu_tool.py read-sheet "https://your-team.feishu.cn/wiki/YOUR_DOCUMENT_TOKEN?sheet=YOUR_SHEET_ID" --range A1:Z20 --use-uat
```

直接调用任意 MCP 工具：

```powershell
python feishu_tool.py call-tool wiki_v2_space_getNode "{\"params\":{\"token\":\"IM3jw5X9Sialj4k5OupckZK5nBh\"}}"
```

## 说明

当前本机 `lark-mcp.config.json` 的 tools 是 `preset.doc.default`，MCP 本身只暴露 wiki/doc/权限/导入文档相关工具，没有暴露表格 values 读写接口。

所以脚本采用通用组合：

1. 用 MCP 的 `wiki_v2_space_getNode` 把 wiki 节点解析为真实 spreadsheet token。
2. 用同一份 appId/appSecret 获取 tenant access token。
3. 调用飞书开放 API 的 sheet values 接口读写单元格。
