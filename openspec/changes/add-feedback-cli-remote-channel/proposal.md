## Why

`feedback-cli` 目前只能在本机窗口里回复：Agent 阻塞等待时，人一旦离开电脑，流程就卡住。需要把每次反馈请求同步到手机可用的即时通讯渠道（首选 Discord），并允许直接在渠道里回复，回复后以与本地提交相同的方式结束等待，Agent 继续执行。

## What Changes

- 新增远程通信能力，**默认关闭**：设置页提供“开启远程通信”开关；配置通过验证并开启后，此后每一次 `feedback-cli` 请求都会同步到 Discord。
- **一次 CLI 调用 = 一个 Discord 论坛帖子**，与该调用的 session 一一绑定；不做跨调用聚合（Agent 级聚合作为后续可选增强，预留 `--thread-key`，不在本次范围）。
- 监听方式：CLI 进程自己定时查询该帖子的新消息；**不引入常驻后台，不使用 Discord Gateway 长连接**。
- 回复规则：帖内白名单用户的**第一条有效文字消息即最终答复**，机器人立即回“已收到”；无合并等待。“文字”包括消息正文和**文字附件**（Discord 会把超长文字自动转成 `message.txt` 附件，需要读取）；第一版不支持图片、其他附件与按钮。
- 本地窗口**照常弹出**；本地提交、远程回复、超时由同一套“先到先得”裁决（依赖 `harden-agent-cli-feedback-control` 的任务 1.1–1.4）。
- 收尾：按结果（远程已答 / 本地已答 / 超时 / 中断）标记并归档帖子；帖内写明截止时间，并每分钟更新“最后存活时间”，便于识别进程被强杀后遗留的帖子。
- 远程任何失败（网络、token 失效、限流）都不影响本地流程；本地窗口显示“远程不可用：原因”。
- 配置入口有两处，共用同一个卡片组件：① `feedback-cli` 窗口设置页里的“远程通信”卡片；② **独立入口 `feedback-cli --remote-settings`**——不创建会话、不占用 Agent 请求，直接打开只含该卡片的设置页。MCP 入口的窗口**不显示**该卡片。
- 配置独立存储，**不写入 `ui_settings.json`**；token 只写不读；设置页提供“测试”按钮，全链路验证通过才允许启用。
- 远程配置接口只接受本机同源访问（Host 必须是回环地址、写请求必须是 JSON、校验 Origin），避免其他网页跨站改写 token 与白名单。
- 顺带修复既有隐患：会话提交后关闭桌面窗口时，不再经全局入口新建管理器（其默认端口 8765 + 自动清理可能杀掉占用该端口的其他 mcp-feedback 进程）。

**不做（Non-goals）**：图片与非文字附件回复、按钮交互、Gateway 常驻连接、常驻中转服务、Agent 级会话聚合、Discord 以外的渠道实现（仅保证接口可扩展）、独立设置页中的非远程类设置（语言、布局等仍只在反馈窗口里改）。

## Capabilities

### New Capabilities
- `feedback-remote-channel`: 与渠道无关的远程通信契约——会话一一绑定、后台运行与失败隔离、与本地提交共用先到先得裁决、收尾与存活标记。
- `feedback-remote-discord`: Discord 适配——论坛帖子创建、白名单回复识别（含文字附件）、回执、限流与错误处理、归档。
- `feedback-remote-settings`: 远程通信配置——开关、独立存储与 token 保护、测试验证流程、启用前置条件、仅 CLI 窗口可见、独立设置入口、本机同源访问限制。

### Modified Capabilities
- None.（`feedback-cli-agent-mode` 与 `feedback-submission-arbitration` 尚未归档为主规范；本变更以前置依赖方式引用，不修改其需求。）

## Impact

- Affected code:
  - `src/mcp_feedback_enhanced/cli.py`（`CliSessionRuntime` 接入远程任务与收尾；新增 `--remote-settings` 与对应运行时；桌面优先启动逻辑抽成共用函数）。
  - `src/mcp_feedback_enhanced/web/models/feedback_session.py`（提交入口接入裁决；关闭桌面窗口不再经全局管理器）。
  - `src/mcp_feedback_enhanced/web/routes/main_routes.py`、`web/main.py`（配置/测试接口与同源校验；`/remote-settings` 路由；“远程设置可用”标志；远程状态推送）。
  - `src/mcp_feedback_enhanced/web/static/**`、`web/templates/feedback.html`、新增 `web/templates/remote_settings.html` 与共用卡片组件、`web/locales/**`（设置卡片、状态标记、三语文案）。
  - 新增 `src/mcp_feedback_enhanced/remote/`（渠道接口、协调器、配置存储、Discord 适配）。
- Dependencies: 无新增第三方依赖（复用已有 `aiohttp`）。
- Prerequisite: `harden-agent-cli-feedback-control` 任务 1.1–1.4（先到先得裁决）必须先落地；其余任务（注册表、HTTP 接口）不属于本变更依赖。
- External requirements（用户侧一次性准备）: Discord 应用与 bot、在开发者后台打开 Message Content 开关、私有服务器中的论坛频道、bot 的发帖/发言/读历史权限、本人的 Discord 用户 ID。
- Key risks to control:
  - 回复会直接成为 Agent 的指令：必须只认白名单用户。
  - 配置接口能改写 token 与白名单：必须限制为本机同源访问。
  - 多窗口并存时旧窗口覆盖设置文件：远程配置必须独立存储。
  - 进程被强杀来不及收尾：帖子遗留“看似仍在等待”。
  - 外层限制不变：CLI 仍受 Agent 所在工具超时、电脑休眠/关机约束。
- Validation baseline:
  - 本地先答 / 远程先答 / 同时答 / 超时 四条路径结果一致且无覆盖。
  - 远程不可用（断网、token 无效、限流）时本地流程不受影响。
  - 3 个并发 CLI 各自对应独立帖子，回复互不串线。
  - 长回复（`message.txt` 文字附件）可作为答复；超限、非 UTF-8、下载失败时提示并继续等待。
  - MCP 窗口不显示卡片且接口返回 404；`--remote-settings` 的打开、关闭、超时、Ctrl+C 行为正确且不产生会话、不向 Discord 转发。
  - 真机验证：手机收到推送、回复后 CLI 结束、帖子被归档；强杀进程后帖子可识别为过期。
