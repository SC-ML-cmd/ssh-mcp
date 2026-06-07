# CLAUDE.md — ssh-mcp

## 项目概述

ssh-mcp 是一个基于 Python 的 MCP (Model Context Protocol) Server，为 LLM Agent 提供持久化交互式 SSH 会话能力。基于 Paramiko 实现 SSH 连接，通过 FastMCP 暴露工具接口，内置本地终端查看器（viewer）用于人类实时观察和介入。

## 技术栈

- **Python 3.10+**，入口 `pyproject.toml`
- **FastMCP** — MCP 协议服务框架
- **Paramiko** — SSH 客户端
- **标准库 http.server** — 本地 viewer HTTP 服务
- **pytest** — 测试框架

## Code Map — 文件功能索引

### 核心源码 `src/ssh_mcp/`

| 文件 | 职责 | 修改时机 |
|------|------|----------|
| [server.py](src/ssh_mcp/server.py) | **MCP 工具入口**。定义全部 `@mcp.tool()`（open/close/reopen session、execute_command、send_text、get_screen、interrupt、search_logs、input_lock 等），请求路由和错误响应封装。`main()` 启动 viewer + MCP server。 | 新增/修改 API 工具、调整工具返回格式、工具 docstring |
| [session.py](src/ssh_mcp/session.py) | **SSH 会话核心**。`SshSession` 管理单个 SSH channel：reader loop（持续搬运 PTY 输出）、health monitor（连接探活）、tracked command（marker 包裹的长时间命令追踪）、input lock（终端输入互斥锁）、TerminalBuffer（PTY 输出环形缓冲）。`SessionRegistry` 管理进程内所有 session 生命周期。 | SSH 读写逻辑、会话断开检测、输入锁机制、tracked command 超时/取消、终端 buffer 策略、session 元数据字段 |
| [config.py](src/ssh_mcp/config.py) | **配置加载**。`SshProfile` dataclass（含主机、端口、密钥、安全策略等全部连接参数），从 `config/profiles.json` 解析 profile，支持环境变量注入密码/phrase。 | 新增 profile 字段、调整配置解析逻辑、密钥加载策略 |
| [security.py](src/ssh_mcp/security.py) | **安全策略**。三种模式：`unrestricted`（全放行）、`readonly`（只读命令白名单+kubectl 只读动词）、`restricted`（allow/deny 正则）。内含 DANGEROUS_PATTERNS（rm -rf/shutdown/docker rm 等危险命令拒绝列表）。支持文本脱敏（redact）。 | 调整安全策略模式、新增/修改危险命令黑名单、修改只读命令白名单、脱敏规则 |
| [transcript.py](src/ssh_mcp/transcript.py) | **审计记录**。`TranscriptWriter` 以 JSONL 格式记录 send/recv/event/error 全量事件，支持脱敏、保留策略（retention_days/max_files）、终端回放渲染（`render_terminal_delta`）。 | 调整 JSONL 事件格式、脱敏逻辑、文件保留策略、终端回放渲染 |
| [log_config.py](src/ssh_mcp/log_config.py) | **日志配置**。`ContextDefaultsFilter` 为每条日志补齐 server/pid/client/session/owner 字段，统一输出到 `logs/ssh_mcp.log`。 | 日志格式调整、新增日志上下文字段 |
| [runtime.py](src/ssh_mcp/runtime.py) | **运行时隔离**。`ServerRuntime` 为每个 MCP Server 进程生成独立的 instance 目录（日志+transcript+meta），避免多 LLM 同时启动时混写。 | 调整 runtime 目录结构、instance 元数据字段 |
| [viewer.py](src/ssh_mcp/viewer.py) | **本地终端查看器**。基于 `ThreadingHTTPServer` 的只读+输入 viewer：首页 session 列表、单 session 终端实时回放（长轮询 events API）、人类通过 HTTP POST 发送文本/管理 input lock。纯 HTML+JS 前端，无额外依赖。 | viewer UI 调整、events API 行为、输入控制面板 |
| [__init__.py](src/ssh_mcp/__init__.py) | 包声明，版本号 `0.1.0`。 | 版本号变更 |

### 配置与入口

| 文件 | 职责 | 修改时机 |
|------|------|----------|
| [config/profiles.json](config/profiles.json) | SSH 连接 profile 配置（host/port/user/key/security mode）。**含敏感信息，不提交到 git**。 | 新增/修改 SSH 目标主机 |
| [config/profiles.example.json](config/profiles.example.json) | profiles.json 模板，可安全提交 git。 | profile schema 变更时同步更新 |
| [.mcp.json](.mcp.json) | Claude Code 的 MCP server 注册配置，定义启动命令和环境变量。 | 调整启动参数、环境变量 |
| [pyproject.toml](pyproject.toml) | 项目元数据、依赖声明（mcp、paramiko）、CLI 入口（`ssh-mcp`、`ssh-mcp-viewer`）。 | 依赖变更、版本号 |

### 测试与辅助

| 文件 | 职责 | 修改时机 |
|------|------|----------|
| [tests/test_core.py](tests/test_core.py) | 全部单元测试（config、session buffer、security policy、input lock、transcript、health check、command tracking、viewer、runtime、reopen）。 | 功能变更后更新/新增测试 |
| [lab/cmsm_simulator/cmsm.py](lab/cmsm_simulator/cmsm.py) | CMSM 模拟器交互脚本。模拟 CMSM → master → pod 的多层 SSH 跳转流程，用于本地开发测试。 | 调整模拟器菜单行为 |
| [lab/cmsm_simulator/bin/](lab/cmsm_simulator/bin/) | 模拟的 `kubectl` 等工具脚本。 | 新增模拟命令 |
| [scripts/start-claude-lab.ps1](scripts/start-claude-lab.ps1) | Windows PowerShell 启动脚本。 | 调整启动流程 |

## 架构关系

```
MCP Client (Claude Code)
    │
    ▼
server.py  ─── FastMCP 工具路由
    │
    ▼
SessionRegistry  ─── 管理多个 SshSession
    │
    ▼
SshSession  ─── 单个 SSH 交互会话
    │  ├── TerminalBuffer   (PTY 输出环形缓冲)
    │  ├── TrackedCommand   (长时间命令追踪)
    │  ├── InputLock        (终端输入互斥锁)
    │  ├── reader thread    (搬运 PTY 输出)
    │  ├── health thread    (连接探活)
    │  └── TranscriptWriter (JSONL 审计记录)
    │
    ▼
ViewerServer (HTTP)  ─── 人类浏览器实时观察
```

## 关键 log 位置

- **服务日志**: `runtime/instances/<instance-id>/logs/ssh_mcp.log`
- **会话 transcript**: `runtime/instances/<instance-id>/transcripts/<session-id>.jsonl`
- **查看器**: 默认 `http://127.0.0.1:8765`

## 常用命令

```bash
# 安装依赖
pip install -e ".[dev]"

# 运行测试
python -m pytest tests/ -x -v

# 启动 MCP server（生产由 Claude Code 通过 .mcp.json 启动）
python -m ssh_mcp.server

# 独立启动 viewer
python -m ssh_mcp.viewer
```
