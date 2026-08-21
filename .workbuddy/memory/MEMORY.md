# 项目长期记忆 (mcp-feedback-enhanced)

## 架构关键事实

- `interactive_feedback` 工具 (src/mcp_feedback_enhanced/server.py:429) 通过 `await session.wait_for_feedback(timeout)` (web/main.py:1148 → feedback_session.py:486) **阻塞整个 MCP 请求**等待人类反馈，期间不发任何 `notifications/progress`。
- `MCP error -32001: Request timed out` 由**客户端**判定（客户端 per-request timeout 默认常 ~60–120s），与服务端 `timeout` 参数(600/1200s) 互不相干。服务端设再大也救不了，因为请求被阻塞且无 keepalive。
- mcp.json 里的 `"timeout"` 在多数客户端控制启动/连接/握手超时，非单次工具调用请求超时；且因服务端无 progress，客户端硬上限也不会被续期。
- 根治思路：在 `wait_for_feedback` 等待期间周期性发 `notifications/progress`（MCP 规范允许客户端据此重置超时计时）。临时缓解：调大客户端 per-request timeout（注意单位，部分客户端为毫秒）。
