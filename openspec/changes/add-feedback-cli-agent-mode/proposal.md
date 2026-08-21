## Why

当前 `mcp-feedback-enhanced` 主要以 MCP 工具方式提供交互反馈，但 Agent 在长流程中需要一个可直接阻塞调用的 CLI 入口。现状下，超时约束、实例隔离、窗口回退和清理语义不完整，容易出现会话混淆、端口竞态、孤儿进程和结果不可预期问题，必须先形成一版可落地且与现有 MCP 语义兼容的方案。

## What Changes

- 新增 `feedback-cli` 入口，支持 Agent 以命令行方式发起一次反馈会话并阻塞等待结果。
- CLI 采用“单调用单进程单会话”模型：每次调用独立创建 Session、Web 服务、端口和窗口，天然支持多实例并发隔离。
- 交互策略固定为“桌面窗口优先，失败自动回退浏览器”；用户手动关闭窗口不视为取消，CLI 继续等待反馈或超时。
- CLI 的 timeout、错误提示、默认参数和返回文本语义与现有 MCP 保持一致，不引入第二套用户可见协议。
- 图片处理采用“真实文件落盘 + 路径返回 + TTL 清理”模型，避免输出膨胀，同时保证 Agent 可读取结果。
- 修正当前多实例关键风险点：端口分配竞态、前端固定端口依赖、日志字段不一致、桌面子进程管道阻塞、异常退出清理不完整。
- 保留现有 `mcp-feedback-enhanced` 与 `interactive-feedback-mcp` 入口，兼容迁移，不移除 MCP 路径。

## Capabilities

### New Capabilities
- `feedback-cli-agent-mode`: 提供专供 Agent 调用的阻塞式 CLI 反馈能力，覆盖多实例隔离、桌面优先回退、结果回传与资源清理契约。

### Modified Capabilities
- None.

## Impact

- Affected systems:
  - `D:/Sources/Github/mcp-feedback-enhanced` 项目中的 CLI 入口、Web 会话生命周期、桌面启动链路、反馈格式化与临时文件清理流程。
  - 上游 Agent 调用链（Shell 调用 `feedback-cli`）的阻塞等待与超时配置规范。
- Affected runtime contracts:
  - 多实例并发下每个 CLI 调用必须绑定独立会话和独立端口，禁止共享会话状态。
  - CLI 输出保持 MCP 风格文本，不新增 JSON 协议作为第一版必需能力。
  - 图片路径在会话结束后短期可读，按 TTL（默认 1 小时）回收，不允许“退出即删”导致路径失效。
- Key risks to control:
  - 启动就绪检测不足导致假成功。
  - 固定端口或探测后绑定导致并发冲突。
  - 外层工具超时先杀 CLI 导致孤儿窗口/残留后端。
  - 字段不一致导致命令日志丢失。
- Validation baseline:
  - 至少通过 3 个并发 CLI 实例的隔离验证（独立窗口、独立端口、独立结果回传）。
  - 覆盖手动关窗、桌面回退浏览器、超时结束、异常中断、图片清理等关键路径验证。
