# 任务1自动化核验记录

核验日期：2026-07-10

原则：外部动作不能因为“点击、跳转或提交函数没有抛错”就视为成功。任务1只在可观察结果满足后置条件后继续；无法从稳定接口或可访问性树确认的步骤，进入明确的人工确认，不使用盲点坐标猜测。

## 1. Microsoft / Outlook 登录与账号切换

- 官方依据：Microsoft OIDC 文档说明，登出只结束对应应用会话，浏览器或设备仍可能保留其他登录态；有效 PRT 还可能让后续登录自动继续。`prompt=login` 用于强制重新输入凭据，`prompt=select_account` 用于强制账号选择。
  - https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc
  - https://learn.microsoft.com/en-us/entra/msal/javascript/browser/msal-js-prompt-behavior
- Playwright 官方依据：不同账号应使用不同 BrowserContext 隔离 Cookie 和认证状态。
  - https://playwright.dev/docs/auth
  - https://playwright.dev/docs/api/class-browsercontext
- 原问题：代码访问 `login.live.com/logout.srf` 后固定等待 2.5 秒，未检查退出结果；随后在复用 Profile 中登录，可能继续使用旧账号。
- 修复：目标邮箱与安全邮箱均使用“邮箱地址专属”的持久化 Profile，目录位于 `.playwright-outlook-sessions/target|security/`。不同邮箱仍然隔离 Cookie，但任务重启不再删除登录态。只有该 Profile 曾通过明确凭据登录并确认收件箱、且当前仍能打开收件箱时才复用；否则重新登录。
- 同一安全邮箱在批量任务中保持登录。只有飞书队列分配的安全邮箱发生变化（当前邮箱已达到绑定上限）才退出旧账号并打开新邮箱 Profile。目标邮箱按行切换时只关闭当前窗口，不注销或删除其持久登录态。
- 通行密钥：BrowserContext 在页面脚本运行前拦截 WebAuthn `publicKey` 请求并返回标准 `NotAllowedError`，等价于用户取消，从源头避免 Windows 安全中心窗口。仍兼容 Microsoft `/bridge/fido` 的 `cancelUrl` 和新版 `/consumers/fido/` 页面；若浏览器上下文仍被锁死，关闭该隔离上下文并重试当前行一次。

## 2. Microsoft 安全邮箱绑定

- 官方依据：Microsoft 个人账号添加安全验证邮箱的流程是进入安全设置、添加验证方式、向新邮箱发送验证码并完成确认；最多可添加 10 种验证方式。
  - https://support.microsoft.com/en-US/accounts-billing/manage/microsoft-account-security-info-verification-codes
- 当前策略：自动进入 Microsoft 安全信息页、选择添加电子邮件、向本行分配的安全邮箱发码、从安全邮箱最新未读 Microsoft 邮件中读取验证码并提交。邮件读取覆盖 Outlook 阅读窗格、`role=document` 和全部子 frame，不再只读取主页面正文。
- 写回门禁：绑定完成且 Microsoft 页面离开验证码等待/错误状态后，立即将安全邮箱写入飞书 J 列，并读取 J 单元格逐值核对。写回成功后立刻把本行绑定阶段标为完成，即使后续 Steam 失败也不会在当前任务重试时再次绑定。
- 阶段跳过：刷新队列时，J 列已有安全邮箱的行不再执行绑定、不预先登录安全邮箱、不重复写 J；该行只继续未完成的 Steam/Dota 阶段。J 为空才分配安全邮箱。每个安全邮箱按飞书 J 列现有数量累计，达到 10 个后才切换下一个账号。

## 3. Outlook 收件箱与 Steam Guard 邮件

- Steam 官方依据：未识别设备登录会向 Steam 账号当前绑定邮箱发送 Guard 码；必须检查正确邮箱，并使用最新验证码。未收到时应检查垃圾邮件及可信发件人。
  - https://help.steampowered.com/en/faqs/view/06B0-26E6-2CF8-254C
  - https://help.steampowered.com/en/wizard/HelpWithSteamGuardCode
- 修复：开始 Steam 登录前再次导航到目标邮箱收件箱并检查该邮箱专属 Profile 所有权。安全邮箱不参与 Steam Guard 收码。只有 Steam Guard 窗口已确认后才读取邮件。
- 收码：先选择最新未读 Steam Guard 邮件行，再读取阅读窗格与全部子 frame；仅在直接读取失败时执行两条限定发件人搜索。验证码必须满足 Steam Guard 上下文和固定五位格式。
- Steam 登录页总会显示二维码作为备选方式；仅看到“二维码/手机应用”不再判定必须手机批准。只有账号密码表单已消失且页面明确要求批准登录/扫码时才进入对应阻塞处理。
- 局限：Outlook.com 没有公开邮件 DOM 契约。自动搜索最终超时才进入人工检查，不从整个页面任意提取五位字符串。

## 4. Steam 客户端启动、凭据与 SteamID

- 官方依据：Steam Guard 是凭据之外的第二层认证；Valve 官方的安全身份验证方式包括 Steamworks 会话票据和 OpenID。当前工具不是 Steamworks 合作方后端，无法使用发行商 API 验证任意账号。
  - https://help.steampowered.com/en/faqs/view/6639-eb3c-ec79-ff60
  - https://partner.steamgames.com/doc/features/auth
- 原问题：Steam 用户名和密码曾通过 `steam.exe -login <user> <password>` 放入进程命令行，可能被本机进程查看工具读取。
- 修复：现在只启动 `steam.exe`，凭据通过受控 Steam UI 输入，不再进入进程参数。Guard 输入完成后，只有本机 `loginusers.vdf` 同时出现精确 `AccountName` 且 `MostRecent=1` 才得到本地 SteamID；否则超时失败，不写 I 列。
- 局限：`loginusers.vdf` 是本地客户端状态，不等价于 Valve 服务器签名认证。它适合本机批处理确认，不应被当作安全后端身份凭据。

## 5. Dota2 自定义房间与“大号”标记

- Valve 文档能确认的公开流程是进入 Custom Games、选择插件、创建/加入 Lobby；没有公开且稳定的 Dota2 客户端 UI 自动化接口。
  - https://developer.valvesoftware.com/wiki/Dota_2_Workshop_Tools/Addon_Overview/Playing_Addons
- 原问题：OCR 找不到控件时使用固定屏幕坐标继续点击；房间存在判断还会匹配通用游戏名 `IM觉醒`，可能把游戏页面误认为目标房间。
- 修复：移除创建/搜索主流程中的坐标兜底。找不到控件时保存截图并进入人工恢复。房间自动判定现在只匹配本次唯一房间描述，不再匹配通用游戏名；自动识别失败时由用户明确选择“存在/不存在”。只有“存在”才写 G 列“是”。

## 6. 飞书写回

- 原问题：HTTP 写请求返回成功后直接记录“已写入”，没有读取同一单元格核对最终值。
- 修复：任务相关写入统一使用“写入后立即读取同一范围并逐格比较”。写请求失败、读回失败或值不一致均抛错；不会把该步骤标记完成。

## 7. 仍需保留人工确认的步骤

- Microsoft CAPTCHA、账号锁定、无法通过浏览器取消的系统认证窗口，以及其他明确风控页面。
- Outlook.com DOM 变化导致无法稳定确认邮件或设置页。
- Dota2 客户端没有稳定可访问性文本，或 OCR 无法确认唯一房间描述。

这些步骤不会再使用固定坐标或宽松文本匹配假装成功；人工确认前后均保存状态/截图，后置条件不满足时停止写回。

## 8. 本次核验结果

- `python -m pytest -q`：155 passed（最终交付前会再次执行一次完整检查）。
- `python -m compileall`：通过。
- `git diff --check`：通过（仅有 Windows 换行提示，无空白错误）。
- 登录身份由邮箱专属 Profile、明确凭据登录、收件箱后置条件和持久确认标记共同约束；账号控件能读取时继续做精确邮箱比对，读取不到时不再错误关闭已确认会话。
