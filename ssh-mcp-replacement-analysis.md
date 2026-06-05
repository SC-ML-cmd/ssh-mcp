# SSH MCP 替代方案分析

本文档用于给新窗口接手：基于现有 `xshell-mcp` 的痛点，评估是否新建一个不依赖 Xshell 的 SSH MCP，并给出推荐方向。

## 背景

当前项目 `xshell-mcp` 的核心能力是让 MCP Server 通过 Xshell 页签执行命令。它的通信链路是：

```text
Agent / Codex / Claude Code
  -> MCP Server
  -> 文件 IPC
  -> Xshell 内运行的 Bridge 脚本
  -> 已经登录好的远程终端
```

这意味着现有项目不是一个真正的 SSH MCP。它不负责建立 SSH 连接，也不负责从本地直接登录远程机器。它只控制一个已经存在、已经登录、已经运行 Bridge 的 Xshell 页签。

用户当前主要痛点有两个：

1. Xshell MCP 经常崩溃，原因长期没有完全定位。
2. 每打开一个 Agent / 编码工具窗口，都需要先手动打开 Xshell 页签并运行 Bridge，才能让 MCP 绑定会话。

第二个痛点是当前架构天然导致的，不是简单配置问题。只要底层仍依赖 Xshell 页签内的 Bridge，MCP 就无法在没有 Xshell 页签的情况下自动拥有远程 SSH 会话。

## 现有实现观察

当前仓库已经有多会话机制：

- `server.py` 提供 `list_sessions`、`connect_session`、`disconnect_session`、`get_bridge_info` 等工具。
- `session_manager.py` 通过 `ipc/registry/*.json` 发现 Bridge 会话，并用 `bound_by` 做 CAS 绑定。
- `bridge_client.py` 通过 `.request.json` 和 `.response.json` 与某个 Xshell Bridge 通信。
- `xshell_bridge_v7.py` / `xshell_bridge_v8.py` 在 Xshell 页签内轮询 IPC 文件并操作 Xshell COM 对象。

仓库里已经出现 `xshell_bridge_v8.py`，其说明里明确提到：

- Session lost 后进入 dormant 模式。
- 不退出 Python 解释器。
- 避免触发 XshellCore 在 `Py_EndInterpreter` 阶段崩溃。

这说明当前崩溃的高风险区域很可能在 Xshell 内嵌 Python / COM 生命周期，而不是 MCP 协议本身。即使 v8 能缓解崩溃，它也只能修复第一类问题，不能消除“必须手动打开 Xshell 页签”的架构前提。

## 用户真实工作流

目标不是简单执行一条远程命令，而是支持多阶段、交互式、长会话的日志排查：

1. Agent 启动后，希望 MCP 可以直接连接远程入口。
2. 初始连接可能进入 CSM / 堡垒机 / 菜单系统，而不是直接进入 Kubernetes master。
3. 需要交互式输入：输入目标 IP、选择账号、选择菜单项。
4. 到达 master 后，需要选择 namespace、定位 pod、进入 pod。
5. 进入 pod 后，后续多次查日志必须复用当前 shell 状态，不能每次重新走 CSM -> master -> pod。
6. MCP 必须记录完整交互过程：本地发了什么、远端回了什么，最好像一个 Shell transcript。
7. transcript 日志应与 MCP 自身运行日志分开，便于审计和调试。

因此，新方案的核心不是“一次性 SSH exec”，而是“持久交互式 PTY 会话”。

## 开源方案参考

调研到几类相关项目，适合用作参考：

| 项目 | 技术方向 | 价值 | 风险 |
| --- | --- | --- | --- |
| `mcp-ssh-session` | Python + Paramiko | 支持持久 SSH session、交互式输入、异步命令、多主机、自动重连，最接近本项目第一版需求 | 需要检查其 session / transcript 细节是否足够贴合 CSM 菜单场景 |
| `mcp-ssh-tmux` | Python + 系统 ssh + tmux | 更像真实终端，适合长会话、可 attach、MCP 重启后保留状态 | Windows 原生环境可能需要 WSL / tmux，部署复杂度更高 |
| `ssh-session-mcp` | Node + PTY / viewer 思路 | 方向上接近“可视化 Shell 会话”和共享 PTY，可作为未来 UI 参考 | 技术栈不是 Python，直接复用成本较高 |
| `mcp-ssh-manager` | Node DevOps SSH 管理 | 多连接、ProxyJump、审计日志等能力完整 | 偏连接管理，不一定适合复杂菜单交互和 pod 内长期停留 |
| `mcp-ssh-toolkit-py` | Python + Paramiko | 可参考基础 SSH 命令执行 | 更偏一次性命令，不够贴合长会话需求 |

参考链接：

- https://pypi.org/project/mcp-ssh-session/
- https://github.com/devnullvoid/mcp-ssh-session
- https://github.com/devnullvoid/mcp-ssh-tmux
- https://zw-awa.github.io/ssh-session-mcp/
- https://github.com/bvisible/mcp-ssh-manager
- https://github.com/VitalyMalakanov/mcp-ssh-toolkit-py

## 推荐方向

建议在仓库中新建独立目录，例如：

```text
ssh-mcp/
```

这个新项目作为真正的 SSH MCP，不继续依赖 Xshell、Xshell COM、Xshell Bridge 或文件 IPC。现有 `xshell-mcp` 可以保留作为旧方案，不在第一阶段强行迁移。

推荐第一版采用：

- Python
- FastMCP / MCP Python SDK
- Paramiko interactive shell channel
- 内存 session registry
- 本地 transcript JSONL 文件
- 独立运行日志

暂不优先选择 tmux 作为第一版底座，除非明确接受 WSL / Linux 运行环境。tmux 方向长期更强，但第一版会增加部署前提。

## MVP 工具设计

第一版建议提供以下 MCP tools：

| Tool | 用途 |
| --- | --- |
| `open_session(profile: str)` | 根据配置打开 SSH 交互式会话 |
| `list_sessions()` | 查看当前 MCP 进程内维护的会话 |
| `close_session(session_id: str)` | 关闭指定 SSH 会话 |
| `send_text(session_id, text, enter=true, wait_for="", timeout=30)` | 面向 CSM 菜单、账号选择、yes/no、kubectl exec 等交互式输入 |
| `execute_command(session_id, command, wait_for_prompt=true, timeout=30)` | 在当前 shell 状态下执行命令，不重新登录 |
| `get_screen(session_id, lines=100)` | 查看最近终端输出 |
| `interrupt(session_id)` | 发送 Ctrl+C |
| `get_transcript(session_id, tail=200)` | 查看 transcript 片段 |
| `search_logs(...)` | 复用现有日志搜索命令生成逻辑，在当前 pod shell 中执行 |

可选增强：

- `run_route(profile, route_name)`：按配置自动走 CSM -> master -> pod 的固定路径。
- `save_checkpoint(session_id, name)`：标记当前已到达的阶段，比如 `csm`、`master`、`pod`。
- `detect_prompt(session_id)`：辅助识别当前处于入口机、master、pod 还是普通 shell。

## 会话模型

每个 MCP Server 进程维护自己的 SSH session。这样多个 Codex / Claude Code 窗口各自打开 MCP 时，可以各自建立独立 SSH 连接，互不抢占 Xshell 页签。

基本结构：

```text
ssh-mcp/
  src/ssh_mcp/
    server.py
    config.py
    session.py
    transcript.py
    log_config.py
    routes.py
  logs/
    ssh_mcp.log
  transcripts/
    <session_id>.jsonl
```

会话生命周期：

1. `open_session` 读取 profile，建立 SSH。
2. Paramiko 打开 `invoke_shell()` 交互式 channel。
3. 后台 reader 线程持续读取远端输出，写入 ring buffer 和 transcript。
4. `send_text` / `execute_command` 往 channel 写入输入。
5. `wait_for` 或 prompt 检测基于 ring buffer 判断完成。
6. `close_session` 关闭 channel 和 SSH client。

## 日志与审计

必须区分两类日志：

### MCP 运行日志

文件示例：

```text
logs/ssh_mcp.log
```

记录内容：

- MCP 启动、关闭。
- session 创建、关闭。
- tool 调用入口和耗时。
- 异常、超时、重连。
- 不记录完整远端输出，避免运行日志过大。

### SSH transcript 日志

文件示例：

```text
transcripts/<session_id>.jsonl
```

每行一个事件：

```json
{"ts":"2026-06-05T20:30:00.123+08:00","dir":"send","text":"kubectl get pod -n xxx\n","tool":"execute_command"}
{"ts":"2026-06-05T20:30:00.456+08:00","dir":"recv","text":"pod-a Running\npod-b Running\n"}
```

设计要求：

- transcript 与 MCP 运行日志分离。
- 默认记录完整交互内容。
- 对密码、token、密钥类输入支持显式脱敏。
- 每个 session 单独一个 transcript 文件。
- tool 返回值里带 `transcript_path`，方便排查。

## 与现有 `xshell-mcp` 的关系

不建议直接在 `xshell-mcp` 内硬改底层，因为两者架构不同：

| 维度 | 现有 Xshell MCP | 新 SSH MCP |
| --- | --- | --- |
| 连接来源 | 外部 Xshell 页签 | MCP 自己建立 SSH |
| 会话依赖 | Bridge 脚本 + 文件 IPC | Paramiko channel / PTY |
| 交互方式 | 操作 Xshell 屏幕 | 直接读写 SSH channel |
| 崩溃风险 | 受 Xshell COM / Python 生命周期影响 | 受 Paramiko / 网络影响 |
| 多窗口 | 多 Xshell 页签 + CAS 绑定 | 每个 MCP 进程独立连接 |
| 审计 | Bridge log + MCP log，不是完整 transcript | 独立 transcript，天然完整 |

可以复用的部分：

- MCP tool 命名和交互习惯。
- `search_logs` 的命令构造逻辑。
- 日志配置思路。
- 超时、截断、错误返回的格式。
- 多会话概念。

不建议复用的部分：

- Xshell Bridge。
- 文件 IPC。
- registry / bound_by 页签抢占机制。
- Xshell 屏幕行读取逻辑。

## 第一阶段成功标准

MVP 完成后，至少应满足：

1. 不打开 Xshell，也能从 MCP 建立 SSH 连接。
2. 能通过 `send_text` 完成菜单式交互输入。
3. 能保持一个 session 长时间停留在 master 或 pod 内。
4. 下一次查日志复用当前 pod shell，不重新登录。
5. 多个 Agent 窗口可以各自建立独立 SSH session。
6. 每个 session 有独立 transcript，能看到完整 send / recv。
7. MCP 运行日志和 SSH transcript 分离。
8. `search_logs` 能在当前 pod shell 中执行并返回结果。
9. 常见超时、断网、远端关闭能返回可读错误，不导致 MCP Server 崩溃。

## 建议实施顺序

1. 新建 `ssh-mcp/` Python MCP 项目骨架。
2. 实现单 session 的 `open_session`、`send_text`、`get_screen`、`close_session`。
3. 加 transcript JSONL，确保 send / recv 全量落盘。
4. 加 `execute_command` 的 marker / prompt 等待机制。
5. 加多 session registry。
6. 迁移或复用 `search_logs` 命令构造逻辑。
7. 加 route 配置，自动化 CSM -> master -> pod。
8. 再考虑 tmux / UI viewer / 会话恢复等增强能力。

## 新窗口可直接使用的任务描述

请在当前仓库中新建 `ssh-mcp/`，实现一个不依赖 Xshell 的 Python SSH MCP。目标是替代 `xshell-mcp` 的核心远程排障能力：MCP 自己建立 SSH 交互式会话，支持 CSM 菜单式输入、进入 master、进入 pod 后保持当前 shell 状态，并提供完整 transcript 审计日志。第一版优先使用 Paramiko interactive shell，不做 UI，不依赖 tmux。保留现有 `xshell-mcp` 不动，必要时复用其 `search_logs` 命令构造和日志格式思路。
