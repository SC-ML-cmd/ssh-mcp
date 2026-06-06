# SSH MCP

一个不依赖 Xshell 的最小 SSH MCP。它由 MCP Server 自己建立 SSH 交互式 PTY 会话，适合 CSM / 堡垒机菜单、进入 master、进入 pod 后复用当前 shell 状态的排障流程。

## 当前 MVP

- `open_session(profile)`：按配置建立 SSH 交互式 session。
- `list_sessions()`：查看当前 MCP 进程内的活动 session。
- `close_session(session_id)`：关闭 session。
- `send_text(session_id, text, enter, wait_for)`：发送菜单输入或任意文本。
- `execute_command(session_id, command)`：在当前 shell 状态下执行命令，默认用 marker 等待命令结束。
- `get_screen(session_id, lines)`：查看最近终端输出。
- `interrupt(session_id)`：发送 `Ctrl+C`。
- `get_transcript(session_id, tail)`：查看审计 transcript。
- `search_logs(...)`：在当前 shell 中用 `find + grep` 搜索日志。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

## 配置

复制配置样例：

```powershell
Copy-Item .\config\profiles.example.json .\config\profiles.json
```

`config/profiles.json` 示例：

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
      "look_for_keys": true
    }
  }
}
```

也可以通过环境变量指定配置和输出位置：

- `SSH_MCP_CONFIG`
- `SSH_MCP_LOG_PATH`
- `SSH_MCP_TRANSCRIPTS_DIR`

## 运行

```powershell
python -m ssh_mcp.server
```

MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "ssh-mcp": {
      "command": "python",
      "args": ["-m", "ssh_mcp.server"],
      "cwd": "D:/dev/workspace/AI/ssh-mcp"
    }
  }
}
```

## Claude Code 配置

仓库根目录已经提供 `.mcp.json`。启动 Claude Code 前先设置私钥口令环境变量：

```powershell
$env:SSH_MCP_KEY_PASSPHRASE = "<your-private-key-passphrase>"
claude
```

如果项目路径不是 `D:/dev/workspace/AI/ssh-mcp`，额外设置：

```powershell
$env:SSH_MCP_PROJECT = "D:/path/to/ssh-mcp"
```

进入 Claude Code 后运行 `/mcp` 查看 `ssh-mcp` 是否已连接。

如果看到私钥相关报错，先确认环境变量是在启动 `claude` 之前设置的：

```powershell
$env:SSH_MCP_KEY_PASSPHRASE = "<your-private-key-passphrase>"
claude
```

也可以用仓库里的启动脚本，脚本会提示输入私钥口令并启动 Claude Code：

```powershell
.\scripts\start-claude-lab.ps1
```

`-----BEGIN RSA PRIVATE KEY-----` 这种旧 PEM RSA 私钥是支持的，不需要为了 ssh-mcp 强行转换成 `OPENSSH PRIVATE KEY`。如果没有设置私钥口令，错误通常会表现为“需要 passphrase”或“无法解密私钥”。

## Transcript

每个 session 会生成独立 JSONL 文件：

```text
transcripts/<session_id>.jsonl
```

每行记录一次交互事件，`send_text(..., sensitive=true)` 会把发送内容脱敏写入 transcript。

## 本地替身测试环境

仓库内置了一个可部署到 Linux ECS / 虚拟机的 CMSM 测试替身：

```text
lab/cmsm_simulator/
```

它模拟 `CMSM 菜单 -> master shell -> kubectl exec -> pod shell` 的完整链路，并在 fake pod 内提供可搜索日志。部署和测试步骤见 [lab/cmsm_simulator/README.md](lab/cmsm_simulator/README.md)。

## Product viewer

`ssh-mcp` now starts a local browser viewer with the MCP server. The default bind address is `127.0.0.1`, and the default port mode is `auto`, starting near `8765`.

```powershell
python -m ssh_mcp.server --viewer-host 127.0.0.1 --viewer-port auto
```

Useful URLs:

- `http://127.0.0.1:<port>/` lists known sessions.
- `http://127.0.0.1:<port>/sessions/<session_id>` shows one session as a terminal-style page.
- `open_session(...)` returns the exact `viewer_url` for the session.

`open_session` accepts an optional `owner_label` value. Use it to bind the SSH session to the LLM tab or troubleshooting task that created it:

```json
{"profile": "lab", "owner_label": "claude-payment-debug"}
```

Each transcript starts with a `session_meta` event containing `session_id`, `profile`, `owner_label`, `server_instance_id`, process metadata, and `viewer_url`.

Important: `send_text(..., sensitive=true)` no longer redacts transcript text. The `sensitive` flag is kept as metadata, but the raw input is written to JSONL and may appear in the viewer if it is part of the terminal stream. Protect the `transcripts/` directory accordingly.
