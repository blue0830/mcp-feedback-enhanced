# Agent 间 CLI 控制机制 — 设计讨论记录

> 状态：**设计中（未实现，方案已确认）** · 记录日期：2026-08-01 · 方案确认：2026-08-02 · 语言：中文
> 本文档记录一次功能设计讨论的完整上下文、已确认决策与遗留澄清点，供后续实现参考。

---

## 1. 背景与目标

### 1.1 触发背景

分析了 MCP Feedback Enhanced 的消息存储/获取机制后，提出一个新想法：**当 MCP 弹窗弹出并挂起等待反馈时，增加第二个输入通道——CLI 工具**。

- 输入 1（已有）：用户在弹窗中的输入；
- 输入 2（新增）：Agent 通过 CLI 工具发送的消息。

当 Agent 消息通过 CLI 到达时，视为"已收到反馈"，结束弹窗等待，MCP 调用返回结果，继续执行后续步骤。

### 1.2 真实使用场景

**A Agent 控制 B Agent**：

1. B Agent 完成任务后调用 `interactive_feedback`，弹窗弹出并阻塞等待；
2. A Agent（监督者）通过 CLI 工具进行回复：任务可行 / 不可行 / 存在问题等；
3. B Agent 收到回复（作为 `interactive_feedback` 返回），据此调整行为继续执行。

本质：给弹窗增加一个**监督者通道**，CLI 是 A 向 B 传递决策的桥梁。

---

## 2. 现有架构要点（讨论中确认的事实）

### 2.1 消息存储

| 类别 | 存储位置 | 说明 |
|---|---|---|
| 实时会话消息 | 后端进程内存 | `WebUIManager.sessions` 字典 + `current_session`（单活跃会话模式）；`WebFeedbackSession` 持有 `command_logs`、`user_messages`、`feedback_result`、`images`、`status` |
| 历史会话消息 | 磁盘文件 | `~/.config/mcp-feedback-enhanced/session_history.json`，全量覆盖写入、**无文件锁** |
| 前端缓存 | 浏览器内存 | `SessionDataManager.sessionHistory` / `currentSession` |

### 2.2 消息获取

- **实时**：WebSocket 推送（`status_update` / `notification` / `command_output` / `command_complete` / `command_error` / `session_updated`），前端 `app.js` 按 `data.type` 分发；
- **历史**：`GET /api/all-sessions`（当前进程内存）→ 失败回退 `GET /api/load-session-history`（磁盘文件）。

### 2.3 多实例行为

- MCP 服务器是 **stdio 模式**，每个客户端（Agent 会话）启动独立 MCP 进程；
- 每个进程内 `get_web_ui_manager()` 为进程级单例，端口由 `PortManager.find_free_port_enhanced()` 动态分配；
- **内存完全隔离**：实例 A 看不到实例 B 内存中的会话；
- **磁盘文件共享但互相覆盖**：所有实例共用 `session_history.json` / `ui_settings.json`，最后写入者胜。

### 2.4 弹窗弹出后的流程（挂起期间）

- 后端：`wait_for_feedback()` 阻塞等待 `feedback_completed`（`threading.Event`），超时自动提前 1~5 秒；监听 WebSocket；运行自动清理定时器与内存监控；
- 前端：加载 i18n → 拉取设置（`/api/load-settings`）→ 初始化 11 个管理器 → 建立 WebSocket → 收 `connection_established` / `session_updated` / `status_update` → 拉取会话历史 → 检查自动提交 → 心跳；
- 提交反馈：前端 `POST /api/add-user-message` 记录消息 + WebSocket 发 `submit_feedback` → 后端 `submit_feedback()` 保存反馈、状态流转、`set()` 事件、推送 notification；
- 结束后：会话保持活跃不销毁，等待下次 MCP 调用。

### 2.5 结束等待的全部触发路径

1. 用户提交反馈（`submit_feedback` → `feedback_completed.set()`）；
2. 用户设置超时（`user_timeout_timer`）；
3. 自动清理 / 内存压力清理；
4. **（新增）Agent 通过 CLI 提交** —— 本设计的核心接入点。

**可行性结论：完全可行。** `wait_for_feedback` 只等待一个 `threading.Event`，任何进程能触发 `submit_feedback` 逻辑即可解除等待；Web 服务器监听端口即现成的跨进程 HTTP 通道；`submit_feedback` / `add_user_message` / 事件机制全部复用。

---

## 3. 新功能设计（CLI 工具）

### 3.1 总体消息流

```
B Agent 完成任务
  └─ interactive_feedback → 弹窗弹出，B 阻塞在 wait_for_feedback
                                   ↑
A Agent:  mcp-feedback-cli reply --session <id> --message "方案不可行，原因..."
  └─ 读注册文件 → POST /api/cli-feedback → submit_feedback() → set 事件
                                   ↓
B 的 MCP 调用返回反馈文本 → B 调整策略继续执行
```

### 3.2 已确认的设计决策

| # | 决策 | 说明 |
|---|---|---|
| D1 | **CLI 集成在本库中** | 包内新增 `src/mcp_feedback_enhanced/cli.py`，仅用标准库 `argparse`；`pyproject.toml` 注册 console script `mcp-feedback-cli`，同时支持 `python -m mcp_feedback_enhanced.cli` |
| D2 | **CLI 支持多实例** | 不同端口/不同进程的 MCP Feedback Enhanced 实例均可被控制，通过注册文件发现 |
| D3 | **实例识别方式：任务语义匹配** | CLI `list` 展示每个实例的 `project_directory` + `summary`（任务的语义身份），A Agent 凭内容认领目标；`session_id` 显示短 ID（至少前 8 位）并支持前缀匹配 |
| D4 | **`list` 支持 `--json` 与 `--filter`** | 结构化输出 + 关键词过滤，方便 A Agent 编程式选择目标 |
| D5 | **安全性：暂不加 token 鉴权** | 明确不考虑其他进程竞争伪造反馈的情况 |
| D6 | **Agent 回复后弹窗处理** | 弹窗直接关闭，或先提示"已由其他回复"再关闭（两种均可接受） |

### 3.3 会话发现与路由（注册文件机制）

- **发现层**：每个会话创建时向 `~/.config/mcp-feedback-enhanced/instances/<session_id>.json` 写注册文件，内容：`{session_id, port, project_directory, summary, created_at}`；会话清理/超时/进程退出时删除；
- **状态层**：注册文件只存静态发现信息；实时状态（waiting/已提交/超时）由 CLI 通过该实例的 HTTP API 查询，避免状态同步不一致；
- **路由层**：`reply --session <id>` → 读注册文件拿 port → `POST http://127.0.0.1:<port>/api/cli-feedback`；
- **兜底**：CLI 对"注册文件存在但 HTTP 不通"的实例执行三重判定后再删：连续失败 N 次（建议默认 N=3）+ `pid` 不存活 + `created_at` 超过最小存活时长（如 60 秒）；`reply` 前检查会话状态，已结束会话返回明确错误。

### 3.4 CLI 命令集（建议）

| 命令 | 作用 |
|---|---|
| `mcp-feedback-cli list [--json] [--filter <关键词>]` | 列出所有等待中的会话（项目、摘要、状态、等待时长） |
| `mcp-feedback-cli reply --session <id> --message <文本>` | 发送决策，结束该会话的等待 |
| `mcp-feedback-cli status --session <id>` | 查询会话当前状态 |

---

## 4. 待确认 / 开放问题

| # | 问题 | 当前倾向 |
|---|---|---|
| O1 | 双输入竞态：用户弹窗输入与 Agent CLI 输入并存 | 先到先得；后到输入无效（不合并、不覆盖本次返回） |
| O2 | 弹窗关闭的具体实现方式 | 复用/扩展现有 `desktop_close_request` WebSocket 机制，向后端新增推送关闭指令 |
| O3 | `/api/cli-feedback` 端点参数定义 | 建议 `{session_id, feedback}`，内部调用 `add_user_message()` + `submit_feedback()`，约 30~40 行 |
| O4 | 注册文件写入/清理时机 | 建议 `WebUIManager.create_session()` 时写入、会话清理时删除；stale 删除采用连续失败 + `pid` + `created_at` 保护策略 |
| O5 | 多实例下 `session_history.json` 互相覆盖问题 | 已有问题，与本次功能正交，未决定是否一并解决 |

---

## 5. 参考代码位置

| 模块 | 路径 |
|---|---|
| MCP 工具入口（interactive_feedback） | `src/mcp_feedback_enhanced/server.py` |
| Web UI 管理器（create_session / launch_web_feedback_ui / smart_open_browser） | `src/mcp_feedback_enhanced/web/main.py` |
| 会话模型（wait_for_feedback / submit_feedback / add_user_message） | `src/mcp_feedback_enhanced/web/models/feedback_session.py` |
| HTTP 路由与 WebSocket 处理（/api/all-sessions、/api/add-user-message、submit_feedback 分发） | `src/mcp_feedback_enhanced/web/routes/main_routes.py` |
| 前端初始化与消息分发 | `src/mcp_feedback_enhanced/web/static/js/app.js` |
| 前端会话数据管理（loadFromServer / saveSessionSnapshot） | `src/mcp_feedback_enhanced/web/static/js/modules/session/session-data-manager.js` |
| 端口分配 | `src/mcp_feedback_enhanced/web/utils/port_manager.py` |

---

## 6. 讨论结论摘要

1. 方案**完全可行**，核心改动小：新增 HTTP 端点 + 注册文件机制 + CLI 脚本；
2. 关键设计决策已确认（D1~D6）；
3. 剩余开放问题（O1~O5）在实现前需拍板；
4. 下一步：确认开放问题后，产出具体实现方案（文件清单 + 接口定义 + 命令定义）。

---

## 7. 方案确认与最终解答（2026-08-02）

> 本章节为开放问题 O1~O5 拍板 + 源码复用点验证 + 最小改动实现方案。确认后可直接进入实现。

### 7.1 成熟方案对照（外部调研）

**opencode-mcp**（lobehub 收录，`klutometis/opencode-mcp`）几乎是同构设计，验证了 D1~D4 方向正确：

| 维度 | opencode-mcp | 本项目 |
|---|---|---|
| 实例发现 | 注册文件目录 `RELAY_REGISTRY_DIR`，每实例一个 JSON | `~/.config/mcp-feedback-enhanced/instances/<session_id>.json` |
| 注册文件字段 | `{name, hostname, port, localPort, cwd, pid, connectedAt}` | `{session_id, host, port, project_directory, summary, pid, created_at}` |
| 健康检查 | MCP server 周期性 HTTP 探活，prune stale | CLI `list` 时惰性探活；仅在连续失败 N 次 + `pid` 不存活 + 会话年龄超过阈值后删 stale |
| 实例匹配 | 名称模糊匹配 + session ID 前缀匹配 | 同（D3） |
| 工具集 | `list_instances` / `send_message` / `abort_session` | `list` / `reply` / `status` |
| 鉴权 | HTTP Basic Auth（`OPENCODE_SERVER_PASSWORD`） | 暂不加（D5） |
| 传输 | SSH 反向隧道跨机 | localhost 单机多实例（更简单） |

**FastAPI 跨进程唤醒 `threading.Event` 的可行性**：已读源码确认链路天然成立——

- `wait_for_feedback`（`feedback_session.py:462`）用 `loop.run_in_executor` 把 `feedback_completed.wait()` 丢线程池，主事件循环不阻塞；
- HTTP 端点在事件循环内 `await session.submit_feedback()` → `feedback_completed.set()`（`threading.Event` 线程安全、幂等）即可唤醒等待线程；
- 无需额外线程、无需额外 IPC 机制。

### 7.2 源码复用点验证（已读源码确认）

| 复用点 | 位置 | 用途 |
|---|---|---|
| `WebUIManager.create_session` | `web/main.py:329` | 注入"写注册文件"，每次 MCP 调用必经 |
| `WebFeedbackSession._cleanup_resources_enhanced` | `web/models/feedback_session.py:766` | 注入"删注册文件"，所有清理路径汇聚于此 |
| `submit_feedback` | `feedback_session.py:519` | CLI 端点直接调用，零逻辑重复 |
| `add_user_message` | `feedback_session.py:576` | 已有 `submission_method` 字段，CLI 复用 |
| `feedback_completed` Event + `run_in_executor` | `feedback_session.py:488` | 跨进程唤醒链路核心 |
| `SessionStatus` 单向状态机 + `next_step()` | `feedback_session.py:201` | 幂等保护，防双输入竞态 |
| `PortManager` + `manager.port` | `web/utils/port_manager.py` / `main.py` | 注册文件直接读端口，无需新逻辑 |
| `get_web_ui_manager` 单例 | `web/main.py:1091` | 端点内直接获取，多实例下每进程一个 |
| 前端 `notification`(FEEDBACK_SUBMITTED) | `submit_feedback` 内自动发送 | CLI 触发的 submit 自动复用，无需新关闭指令 |
| `close_desktop_app` | `main.py:724` | 桌面模式自动关闭已内置 |

**关键事实（已调整）**：现有 `/api/add-user-message`（`main_routes.py:220`）只操作 `current_session`、不接受 `session_id` 参数。为降低会话切换时序风险，`/api/cli-feedback` 与 `/api/cli-status` 应优先按 `session_id` 从 `manager.sessions` 定位，会话不存在时再兜底比对 `current_session`，确保路由与目标会话一一对应。

### 7.3 开放问题拍板（O1~O5）

| # | 问题 | 最终决策 |
|---|---|---|
| O1 | 双输入竞态 | **先到先得 + 状态机保护**。`WAITING` → 首个到达者（用户或 CLI）提交成功并结束等待；非 `WAITING` 状态的后到输入一律拒绝（409），不合并、不覆盖本次返回。 |
| O2 | 弹窗关闭实现 | **复用现有 notification，不新增关闭指令**。`submit_feedback` 内已发 `notification`(FEEDBACK_SUBMITTED) + 桌面模式自动 `close_desktop_app()`。CLI 触发的 submit 走同一路径，前端自动收到通知。无需 `desktop_close_request`。 |
| O3 | `/api/cli-feedback` 端点 | 参数 `{session_id, feedback}`。优先按 `session_id` 从 `manager.sessions` 定位；不存在时再兜底校验 `current_session`。约 25 行（见 7.4）。 |
| O4 | 注册文件时机 | 路径 `~/.config/mcp-feedback-enhanced/instances/<session_id>.json`；`create_session()` 末尾写（原子写：tempfile + `os.replace`）；`_cleanup_resources_enhanced` 末尾删；`atexit.register` 兜底；CLI `list` 惰性清理 stale（连续失败 N 次 + `pid` 不存活 + 年龄超阈值后才删）。 |
| O5 | session_history.json 覆盖 | **不解决**，与本次正交。CLI 功能只依赖注册文件 + HTTP，不受该问题影响。 |

### 7.4 端点定义

```python
# main_routes.py — setup_routes 内新增
@manager.app.post("/api/cli-feedback")
async def cli_feedback(request: Request):
    data = await request.json()
    session_id = data["session_id"]
    session = manager.sessions.get(session_id) or manager.get_current_session()
    if not session or session.session_id != session_id:
        return JSONResponse(status_code=404, content={"error": "session mismatch"})
    if session.status != SessionStatus.WAITING:
        return JSONResponse(status_code=409, content={"error": "feedback already resolved"})
    session.add_user_message({"content": data["feedback"], "submission_method": "cli"})
    await session.submit_feedback(data["feedback"], [], {})
    return {"status": "submitted", "source": "cli"}

@manager.app.get("/api/cli-status")
async def cli_status(session_id: str):
    session = manager.sessions.get(session_id) or manager.get_current_session()
    if not session or session.session_id != session_id:
        return JSONResponse(status_code=404, content={"error": "session not found"})
    return session.get_status_info()
```

### 7.5 CLI 命令集

```
mcp-feedback-cli list [--json] [--filter <kw>]
mcp-feedback-cli reply --session <id-prefix> --message <text>
mcp-feedback-cli status --session <id-prefix>
```

- `reply`/`status` 支持 ID 前缀匹配（注册文件名前缀，至少 8 位）；命中多个时报错要求补全。
- CLI 用标准库 `argparse` + `urllib`，**零新依赖**。

### 7.6 最小改动文件清单

| 操作 | 文件 | 改动量 |
|---|---|---|
| 新增 | `src/mcp_feedback_enhanced/web/instance_registry.py` | ~80 行 |
| 新增 | `src/mcp_feedback_enhanced/cli.py`（argparse + urllib） | ~150 行 |
| 改 | `src/mcp_feedback_enhanced/web/main.py`：`create_session` 末尾写注册文件；`stop` 批量清理 | +6 行 |
| 改 | `src/mcp_feedback_enhanced/web/models/feedback_session.py`：`_cleanup_resources_enhanced` 末尾删注册文件 | +3 行 |
| 改 | `src/mcp_feedback_enhanced/web/routes/main_routes.py`：加 `/api/cli-feedback` + `/api/cli-status` | +30 行 |
| 改 `pyproject.toml`：`[project.scripts]` 加 `mcp-feedback-cli = "mcp_feedback_enhanced.cli:main"` | +1 行 |

**总计**：~270 行新增 + 4 处插入，零新依赖。

### 7.7 明确不做（避免过度设计）

- ❌ 新增 WebSocket 关闭指令 —— notification 已够
- ❌ token 鉴权 —— D5 已定
- ❌ 解决 `session_history.json` 覆盖 —— O5 正交
- ❌ 引入新依赖 —— urllib 标准库够用
- ❌ 改 `submit_feedback` 签名 —— `submission_method` 走 `add_user_message` 现有字段

### 7.8 下一步

按 7.6 文件清单实现，建议顺序：
1. `instance_registry.py`（无依赖，可独立测试）
2. `main.py` + `feedback_session.py` 注入点（接入注册文件）
3. `main_routes.py` 端点（可 curl 验证）
4. `cli.py`（依赖前面三步）
5. `pyproject.toml` 注册 entry point
