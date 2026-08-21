# 交接文档：mcp-feedback-enhanced MCP → CLI 改造评估

> 日期：2026-08-21 · 类型：explore 评估（未实施）· 目的：供下一个 Agent 独立评审本方案，找漏洞
> 仓库：`D:\Sources\Github\mcp-feedback-enhanced`（v2.6.0）

---

## 1. 用户需求（原始动机）

把 mcp-feedback-enhanced 从 MCP 工具改造为 CLI 工具，两个目的：

1. **加快响应**：不需要像 MCP 一样每次先启动/连接才能使用
2. **多实例**：多个 Agent（如 A/B/C/D 并行执行不同任务）同时汇报时，能同时弹出 4 个 CLI 实例，且反馈严格"谁发的请求回复给谁"

用户典型场景：多 Agent 并行（多 worktree + Cursor/Codex/Claude Code 多套工具协作），每个 Agent 完成阶段性任务后向用户汇报并等待决策。

## 2. 已确认的关键代码事实（已读源码核实）

| 事实 | 位置 | 意义 |
|---|---|---|
| `launch_web_feedback_ui(project_directory, summary, timeout) -> dict` 是**协议无关**的纯入口 | `src/mcp_feedback_enhanced/web/main.py:1099` | MCP 只是它的一个调用方，CLI 可直接复用，web 核心零改动 |
| `web/main.py` 底部 `__main__` 测试函数直调该入口并可跑通 | `web/main.py:1174-1236` | 直调路径已被验证 |
| 阻塞点 `session.wait_for_feedback(timeout)` 内部用 `feedback_completed`（threading.Event）+ `run_in_executor` | `web/models/feedback_session.py:488` | 阻塞的是进程本身，与外层是 MCP 还是 CLI 无关 |
| `WebUIManager` 为进程级单例，端口由 `PortManager.find_free_port_enhanced()` 动态分配 | `web/main.py:1091`、`web/utils/port_manager.py` | 每进程独立端口/内存 → 天然多实例 |
| `start_server()` 末尾硬编码 `time.sleep(2)` 等待服务器启动 | `web/main.py:611` | 每个新进程固定 +2s 冷启动开销 |
| `_check_active_tabs()` 只查进程内存中的 `current_session.websocket` | `web/main.py:797` | 跨进程无法检测活跃标签页 → CLI 每次弹新浏览器窗口，无法复用 |
| stdout 结果格式化 `create_feedback_text()`、反馈落盘 `save_feedback_to_file()` 均为独立函数 | `server.py:267`、`server.py:219` | CLI 可直接复用 |
| `pyproject.toml` 已有 `[project.scripts]` 两个 MCP 入口 | `pyproject.toml:50-52` | 加一行 console script 即可 |
| Windows 编码初始化 `init_encoding()` 是独立函数 | `server.py:50` | CLI 需复用 |

**用户历史痛点（MCP 客户端侧）**：`interactive_feedback` 阻塞整个 MCP request 且不发 `notifications/progress`，客户端 per-request 超时（~60-120s）掐断调用，服务端 `timeout` 参数设 1200s 也无效。详见 `D:\Sources\Github\mcp-feedback-enhanced\.workbuddy\memory\MEMORY.md`。

## 3. 方案（已与用户对齐）

**薄壳 CLI，与 MCP 并存，核心零改动。**

```
现状（MCP）                          目标（CLI，并存）
─────────────────────                ─────────────────────
Agent → MCP客户端 → stdio            Agent → Bash 直接执行
  → server.py (FastMCP 常驻)           → cli.py (一次性进程)
    → interactive_feedback()            → launch_web_feedback_ui()  ← 核心零改动
      → launch_web_feedback_ui()          → WebUIManager/FastAPI/浏览器
        → WebUIManager + 浏览器          → wait_for_feedback 阻塞
        ← wait_for_feedback              ← stdout 文本 + 图片落盘路径
      ← TextContent/MCPImage
```

### 3.1 改动清单

| 模块 | 操作 | 量 |
|---|---|---|
| `src/mcp_feedback_enhanced/cli.py` | 新增：argparse（`--project/--summary/--timeout`）+ 复用 `init_encoding`、`launch_web_feedback_ui`、`create_feedback_text` + 图片落盘打印路径 | ~100 行 |
| `pyproject.toml` | `[project.scripts]` 加 `mcp-feedback-cli = "mcp_feedback_enhanced.cli:main"` | 1 行 |
| `server.py` / `web/**` | **零改动**，MCP 形态保留 | 0 |

CLI 退出码约定（建议）：0=收到反馈，1=超时/取消。

### 3.2 与既有设计的关系（勿混淆）

仓库已有 `docs/design/agent-cli-control.md`（2026-08-02 确认，**未实施**，配套 openspec change `harden-agent-cli-feedback-control`）：那是"CLI 作为第二输入通道控制已弹出的窗口"（Agent 间监督），与本次"CLI 替代 MCP 作为主入口"是**两个不同功能**。但其中的 `instance_registry`（`~/.config/mcp-feedback-enhanced/instances/<session_id>.json` 注册文件）机制可复用于可选的 daemon 模式。

## 4. 用户三轮追问的结论（评审重点）

### Q1：多实例回复会不会混淆？
**不会，严格 1:1，进程模型结构性保证：**

```
Agent A ──Bash──▶ cli 进程 A（端口 9123）──▶ 浏览器窗口 A
Agent B ──Bash──▶ cli 进程 B（端口 9124）──▶ 浏览器窗口 B
Agent C ──Bash──▶ cli 进程 C（端口 9125）──▶ 浏览器窗口 C
Agent D ──Bash──▶ cli 进程 D（端口 9126）──▶ 浏览器窗口 D
```

三层隔离：① 提交路由——每窗口 URL 含独立端口，WebSocket 1:1；② 等待解除——每进程独立 session + Event，内存互不可见；③ 结果返回——OS 保证子进程 stdout 只回到发起它的 Bash 调用。
**唯一真实风险是人为的**：4 个窗口外观一样，用户看错窗口把 A 的决策打给 B。缓解：UI 顶部已显示 project_directory + summary，多 worktree 目录名可区分。

### Q2：CLI 后 Agent 还会阻塞等回复吗？
**会，阻塞语义完全保持。** MCP：request 挂起不返回；CLI：进程阻塞在 `wait_for_feedback` 不退出 → Bash 调用不返回。Agent 视角等价（"调用没返回，我在等"）。

**配置注意点（关键）**：等待时长 = 弹窗 `--timeout`（内层）与 Bash 工具超时（外层）的**较小者**。外层必须 > 内层，否则 Bash 先杀 CLI 进程，弹窗还在但反馈无处可去：
- WorkBuddy Bash：默认 120s，最大 600s → `--timeout` 应 ≤ 590
- Claude Code：`BASH_DEFAULT_TIMEOUT_MS` 可调
- Claude Code 支持后台跑 + 轮询（Agent 不阻塞，干完别的再收反馈）——可选灵活性

### Q3：功能会不会改变？
**核心功能不变**（同一入口）：弹窗、Markdown 摘要、文字反馈、图片上传、命令执行展示、超时。

差异表：

| 项 | MCP | CLI | 影响 |
|---|---|---|---|
| 图片返回 | MCPImage 直接给 Agent | 落盘打印路径，Agent 用 Read 读 | 多一步，功能等价 |
| 跨调用会话连续性 | 常驻进程复用标签页/状态（持久 Web UI 架构） | 每调用独立进程、新窗口 | 单 Agent 连续汇报体验略降 |
| 超时来源 | MCP 客户端 per-request 超时（被掐断的根源） | Bash 工具超时（可配、可控） | CLI 的真实收益所在 |
| 冷启动 | 仅首次慢，后续复用 server_thread | **每次付 ~3-5s**（import + `sleep(2)` + uvicorn） | 见 5.1 |

## 5. 已识别风险/权衡（评审时请重点核查）

### 5.1 "加快响应"的诚实评估
- CLI 免掉的：MCP 客户端连接管理、握手、**per-request 超时掐断**（用户 1200s 配置 1-2 分钟断的根源）→ 目的 1 成立
- CLI 付出的：每次调用冷启动 ≈3-5s（Python import FastAPI + `start_server` 硬编码 `sleep(2)` + uvicorn 起服务）
- 结论：CLI 的真实收益是**摆脱 MCP 客户端的超时/连接管理**，不是单次调用延迟本身。要极致快需 daemon 模式（常驻 server + 薄 client），可复用 agent-cli-control.md 的 instance_registry 设计——**可选增强，非必须**

### 5.2 其他已知点
- 浏览器每次弹新窗（`_check_active_tabs` 只查进程内存）；多 Agent 场景=每 Agent 一窗，是特性；单 Agent 连续调用会窗满为患
- `session_history.json`/`ui_settings.json` 多实例互相覆盖（无文件锁，最后写者胜）——agent-cli-control.md O5 已定"不正交不解决"；CLI 场景 stdout 返回主流程，不受影响
- Bash 外层超时杀进程后弹窗成为孤儿（服务已死，用户提交无响应）——需在评审中考虑体验
- Windows 编码：cli.py 必须复用 `init_encoding()`

## 6. 请下一个 Agent 评审的问题清单

1. 薄壳方案是否有遗漏的坑（信号处理、进程退出清理、atexit、uvicorn daemon 线程退出是否干净）？
2. Bash 超时 vs `--timeout` 的嵌套超时语义，是否有更优处理（如 CLI 内自动对齐外层超时）？
3. 多实例端口分配在 4+ 并发下是否可靠（PortManager 竞态）？
4. 孤儿弹窗问题是否需要处理（Bash 超时后自动关窗/提示）？
5. daemon 模式是否值得一步到位（instance_registry 已有设计）？
6. 是否有更简单的替代方案满足用户两目的（如修 MCP progress notification 保活）？

## 7. 参考文件

| 文件 | 说明 |
|---|---|
| `D:\Sources\Github\mcp-feedback-enhanced\docs\design\agent-cli-control.md` | 既有 Agent 间 CLI 控制设计（含源码复用点验证、instance_registry 机制） |
| `D:\Sources\Github\mcp-feedback-enhanced\.workbuddy\memory\MEMORY.md` | MCP 客户端超时根因分析 |
| `D:\Sources\Github\mcp-feedback-enhanced\docs\architecture\system-overview.md` | 四层架构总览 |

## 8. 用户工作偏好（对接手 Agent 的要求）

- 中文回复，caveman 极简风格：只回答用户关心的问题，不复述现状、不写背景铺垫
- 用户会主动质疑结论，要求验证事实后才接受——所有陈述须有源码/文档依据
- 面对过度设计会 push back，偏好最小改动、复用优先
