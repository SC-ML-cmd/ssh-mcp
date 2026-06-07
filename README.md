# SSH MCP

一个不依赖 Xshell 的 Python SSH MCP Server。它由 MCP Server 自己建立交互式 SSH PTY 会话，适合 CMSM/堡垒机菜单、进入 master、进入 pod 后复用当前 shell 状态的排障流程。

## 能力概览

- `open_session(profile, owner_label=None)`：按配置打开持久 SSH 会话，并返回对应的浏览器 `viewer_url`。
- `list_sessions()`：查看当前 MCP Server 实例内的活动会话和运行时信息。
- `close_session(session_id)`：关闭指定 SSH 会话。
- `reopen_session(session_id)`：基于旧 session 的 profile 打开一个新的 SSH 登录，不重放菜单或命令。
- `send_text(session_id, text, enter=True, wait_for="", timeout=30)`：向菜单或 shell 发送文本。
- `input_lock_status(session_id)`：查看当前输入锁归属、过期时间和剩余 TTL。
- `acquire_input_lock(session_id, actor="agent", ttl=60, force=False)`：为 Agent、人类或工具占用输入权。
- `release_input_lock(session_id, actor="agent", force=False)`：释放输入锁；必要时可强制释放。
- `execute_command(session_id, command, wait_for_prompt=True, timeout=30)`：在当前 shell 状态下执行命令。
- `get_command(session_id, command_id)`：查询已跟踪命令的状态、退出码和已收集输出。
- `list_commands(session_id)`：查看当前 session 的命令历史。
- `cancel_command(session_id, command_id)`：对正在运行的跟踪命令发送 `Ctrl+C`。
- `get_screen(session_id, lines=100)`：查看内存中最近的终端输出。
- `get_transcript(session_id, tail=200)`：读取 JSONL 审计记录。
- `interrupt(session_id)`：发送 `Ctrl+C`。
- `search_logs(...)`：在当前远端 shell 中执行 `find + grep` 日志搜索。

## 架构

```text
Claude / Codex / MCP Client
  -> ssh-mcp FastMCP Server
  -> Paramiko SSH interactive shell channel
  -> remote CMSM / master / pod shell

ssh-mcp Server
  -> runtime/instances/<server_instance_id>/logs/ssh_mcp.log
  -> runtime/instances/<server_instance_id>/transcripts/<session_id>.jsonl
  -> local browser viewer
```

每个 MCP Server 进程都会生成独立的 `server_instance_id`。多个 LLM 窗口同时启动 MCP Server 时，默认写入不同的实例目录，避免日志和 transcript 混在一起。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

开发测试可直接使用：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## SSH 配置

复制配置模板：

```powershell
Copy-Item .\config\profiles.example.json .\config\profiles.json
```

示例：

```json
{
  "profiles": {
    "dev": {
      "host": "10.0.0.10",
      "port": 22,
      "username": "your-user",
      "key_filename": "C:/Users/you/.ssh/id_rsa",
      "passphrase_env": "SSH_MCP_KEY_PASSPHRASE",
      "allow_agent": true,
      "look_for_keys": true,
      "auto_add_host_key": true,
      "term": "xterm-256color",
      "width": 120,
      "height": 40,
      "keepalive_interval": 30
    }
  }
}
```

`keepalive_interval` 使用 Paramiko transport keepalive，只做 SSH 探活，不会向远端 shell 注入命令。

## 运行

直接启动：

```powershell
.\.venv\Scripts\python.exe -m ssh_mcp.server --viewer-host 127.0.0.1 --viewer-port auto
```

常用环境变量：

- `SSH_MCP_CONFIG`：profile 配置路径，默认 `config/profiles.json`。
- `SSH_MCP_RUNTIME_DIR`：运行产物根目录，默认 `runtime`。
- `SSH_MCP_CLIENT_LABEL`：当前 MCP Server 来源标签，例如 `claude-order-debug`、`codex-local`。
- `SSH_MCP_KEY_PASSPHRASE`：私钥口令。
- `SSH_MCP_KEEPALIVE_INTERVAL`：默认 SSH keepalive 秒数，可被 profile 覆盖。

兼容旧配置：

- 显式设置 `SSH_MCP_LOG_PATH` 时，运行日志写到该路径。
- 显式设置 `SSH_MCP_TRANSCRIPTS_DIR` 时，transcript 写到该目录。

## Claude / Codex 接入

仓库提供 `.mcp.json` 和 `.codex/config.toml` 示例。默认建议只设置：

- `SSH_MCP_CONFIG`
- `SSH_MCP_RUNTIME_DIR`
- `SSH_MCP_CLIENT_LABEL`
- `SSH_MCP_KEY_PASSPHRASE`

不要让多个 MCP Server 固定写同一个 `logs/ssh_mcp.log` 或 `transcripts/`，否则排查时很难区分不同 LLM 窗口。

启动 Claude Code 前可设置：

```powershell
$env:SSH_MCP_KEY_PASSPHRASE = "<your-private-key-passphrase>"
$env:SSH_MCP_CLIENT_LABEL = "claude-payment-debug"
claude
```

`open_session` 时建议传入 `owner_label`：

```json
{"profile": "lab", "owner_label": "payment-log-check"}
```

`SSH_MCP_CLIENT_LABEL` 标记 MCP Server 来源，`owner_label` 标记具体 SSH session 的用途。

## 浏览器 Viewer

MCP Server 启动时会同时启动本地 viewer。默认从 `8765` 附近自动选择可用端口。

- `/`：按 `server_instance_id` 分组展示 sessions。
- `/sessions/<session_id>`：单个 session 的终端式页面。
- `/api/sessions`：返回 session 列表和 server 元数据。
- `/api/sessions/<session_id>/events`：长轮询读取 transcript 增量。

`open_session` 会返回准确的 `viewer_url`，不要手动猜 session URL。

也可以只启动历史 viewer：

```powershell
.\.venv\Scripts\python.exe -m ssh_mcp.viewer --port 8765 --transcripts-dir transcripts
```

## 共享终端与人工接管

单个 session 页面现在不只是只读 transcript，也可以在浏览器底部输入内容并发送到同一个远端 PTY。浏览器输入默认使用 `actor=human`，MCP 工具默认使用 `actor=agent`，`search_logs` 这类内部能力默认使用 `actor=tool`。所有 `send`、tracked command 和锁事件都会把 actor 写入 transcript，便于回看是谁在什么时候接管了终端。

输入锁用于避免 Agent 和人工同时向同一个 PTY 写入，规则如下：

- 无锁或锁已过期时，新的 actor 可以获取输入锁。
- 同一个 actor 再次输入会刷新 TTL。
- 其他 actor 输入会被拒绝，并写入 `input_lock_denied`。
- `force=true` 可以强制接管，写入 `input_lock_takeover`。
- viewer 的 `Observer` 模式只旁观，不发送输入，也不会获取锁。

浏览器接口：

- `POST /api/sessions/<session_id>/input`：发送 `{ "text": "...", "enter": true, "actor": "human" }`。
- `POST /api/sessions/<session_id>/lock`：获取或强制接管输入锁。
- `POST /api/sessions/<session_id>/unlock`：释放输入锁。

历史 transcript 页面仍然只能查看；只有当前 MCP Server 进程里的活跃 session 可以输入。

## 长命令与后台轮询

`execute_command` 默认会为命令生成 `command_id` 和完成 marker。命令在 `timeout` 内结束时，返回 `status=completed`、`exit_code` 和输出；命令超过 `timeout` 时，返回 `status=running`、`timed_out=true` 和 `command_id`，reader 线程会继续读取远端输出并写入 transcript。

后续可用 `get_command` 轮询：

```json
{"session_id": "lab-...", "command_id": "cmd-..."}
```

命令状态含义：

- `running`：命令仍在执行，输出还会继续增长。
- `completed`：检测到 marker，已有退出码。
- `cancel_requested`：已通过 `cancel_command` 发送 `Ctrl+C`，等待远端 shell 返回。
- `cancelled`：取消后检测到 marker，并且退出码非 0。
- `failed` / `session_closed`：reader 或 SSH session 断开，命令无法继续跟踪。

同一个交互式 session 同一时间只允许一个被 marker 跟踪的 `execute_command`。这是为了避免多个长命令共享同一个 PTY 输出流时，结果互相混淆。已经被 reader 收到的输出会持续写入 transcript；内存中的命令输出有上限，超大输出应以 transcript 为准。

## Runtime 目录

默认结构：

```text
runtime/
  instances/
    <server_instance_id>/
      server_meta.json
      logs/
        ssh_mcp.log
      transcripts/
        <session_id>.jsonl
```

`server_meta.json` 记录：

- `server_instance_id`
- pid
- cwd
- started_at
- client_label
- viewer_base_url
- log_path
- transcripts_dir
- config_path

旧的 `transcripts/*.jsonl` 仍可被 viewer 读取，并标记为 `legacy`。

## Transcript 与安全

每个 SSH session 都有独立 JSONL 文件。首行是 `session_meta`，包含 profile、host、username、owner_label、server_instance_id、client_label、viewer_url 等信息。

示例事件：

```json
{"dir":"session_meta","session_id":"lab-...","owner_label":"payment-log-check","server_instance_id":"ssh-mcp-..."}
{"dir":"send","tool":"send_text","actor":"human","text":"2\n"}
{"dir":"recv","text":"Logged in to master...\n"}
{"dir":"session_health","health_status":"unhealthy","health_error":"SSH transport is inactive"}
```

`send_text(..., sensitive=true)` 会把输入内容写成 `[REDACTED]`，不会把原文落入 transcript。transcript 也会对常见 `password=...`、`token=...`、`Bearer ...` 等文本做基础脱敏。请仍然保护 `runtime/`、`logs/`、`transcripts/` 的文件权限。

## 安全策略

每个 profile 可以配置本地安全策略。策略在命令发往远端 SSH channel 前执行；被拒绝的命令会写入 `security_block` transcript 事件，但不会发送到远端。

```json
{
  "profiles": {
    "prod-readonly": {
      "host": "10.0.0.10",
      "username": "ops",
      "security": {
        "mode": "readonly",
        "redact_transcripts": true,
        "transcript_retention_days": 14,
        "transcript_max_files": 200
      }
    },
    "prod-restricted": {
      "host": "10.0.0.11",
      "username": "ops",
      "security": {
        "mode": "restricted",
        "allow_patterns": [
          "^tail\\s+-n\\s+\\d+\\s+[/\\.\\w-]+$",
          "^grep\\s+-n\\s+-i\\s+--\\s+.+$"
        ],
        "deny_patterns": [
          "(?i)kubectl\\s+delete"
        ]
      }
    }
  }
}
```

模式说明：

- `unrestricted`：默认模式，保持兼容；只执行显式 `deny_patterns`。
- `readonly`：只允许常见读取命令，例如 `cat`、`grep`、`find`、`tail`、`ps`、`df`、`kubectl get/logs/describe` 等，并阻止重定向写入和危险命令。
- `restricted`：必须匹配 `allow_patterns` 才能执行。

内置危险命令规则会拦截高风险操作，例如 `rm -rf`、`shutdown/reboot`、`mkfs/dd/fdisk`、`chmod -R`、`kubectl delete/apply/patch/scale`、`curl | sh` 等。`send_text` 在严格模式下只允许菜单式短输入或符合策略的文本，避免用 raw input 绕过 `execute_command`。

transcript 文件默认尽量使用私有权限：目录 `0700`、文件 `0600`。可用 `transcript_retention_days` 和 `transcript_max_files` 控制历史 transcript 清理。

## SSH 心跳与断线处理

当前策略是“只探活，不自动重连”：

- 使用 Paramiko transport keepalive 保持 SSH 连接活跃。
- 后台 health monitor 定期检查 transport/channel。
- 断开后 session 会标记为 `unhealthy` 或 `closed`。
- 后续 `send_text` / `execute_command` 返回可读错误，并附带 `health_status`、`health_error`、`last_activity_at`、`last_heartbeat_at`、`transcript_path` 和 session 摘要。
- `get_screen` 仍可查看断开前内存中的最后屏幕内容。
- `reopen_session` 可以基于旧 session 的 profile 打开一个新的 SSH 登录，并在新旧 transcript 中记录关联信息。
- 不自动重放 CMSM -> master -> pod 路径，避免误以为恢复了原始 shell 状态。
- 不自动恢复到 pod，不自动重放最后一条命令，不伪装成原 shell 仍然存活。

## 本地 CMSM 测试环境

仓库内置 CMSM simulator：

```text
lab/cmsm_simulator/
```

它模拟：

```text
CMSM 菜单 -> master shell -> kubectl exec -> pod shell
```

部署和测试步骤见 [lab/cmsm_simulator/README.md](lab/cmsm_simulator/README.md)。

## 常见问题

### 为什么 viewer 端口不是 8765？

如果端口被占用，viewer 会自动选择后续可用端口。以 `open_session` 返回的 `viewer_url` 为准。

### 多个 Claude/Codex 窗口如何区分？

设置不同的 `SSH_MCP_CLIENT_LABEL`，并在 `open_session` 传入 `owner_label`。运行日志、server_meta 和 transcript 都会记录这些字段。

### MCP 重启后 SSH session 还在吗？

不在。当前实现使用 Paramiko interactive shell channel，MCP Server 进程退出后 SSH 连接也会关闭。历史 transcript 会保留在 runtime 目录。

### 什么时候考虑 tmux？

如果需要 MCP 重启后仍能 attach 原 SSH 会话，可以在后续版本评估 tmux 底座。但 Windows 原生部署会更复杂，本项目当前优先保持 Python/Paramiko 的轻量实现。
