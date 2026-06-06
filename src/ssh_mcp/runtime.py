from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
from typing import Any


DEFAULT_RUNTIME_DIR = Path("runtime")


@dataclass(frozen=True)
class ServerRuntime:
    """一次 MCP Server 进程对应的运行时目录和归属信息。"""

    server_instance_id: str
    client_label: str | None
    started_at: datetime
    cwd: Path
    runtime_dir: Path
    instance_dir: Path
    log_path: Path
    transcripts_dir: Path
    explicit_log_path: bool
    explicit_transcripts_dir: bool
    config_path: Path | None = None
    meta_path: Path = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "meta_path", self.instance_dir / "server_meta.json")

    def ensure_dirs(self) -> None:
        self.instance_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)

    def as_dict(self, *, viewer_base_url: str | None = None) -> dict[str, Any]:
        return {
            "server_instance_id": self.server_instance_id,
            "pid": os.getpid(),
            "cwd": str(self.cwd),
            "started_at": self.started_at.isoformat(timespec="milliseconds"),
            "client_label": self.client_label,
            "runtime_dir": str(self.runtime_dir),
            "instance_dir": str(self.instance_dir),
            "log_path": str(self.log_path),
            "transcripts_dir": str(self.transcripts_dir),
            "explicit_log_path": self.explicit_log_path,
            "explicit_transcripts_dir": self.explicit_transcripts_dir,
            "config_path": str(self.config_path) if self.config_path else None,
            "viewer_base_url": viewer_base_url,
        }

    def write_meta(self, *, viewer_base_url: str | None = None) -> None:
        self.ensure_dirs()
        payload = self.as_dict(viewer_base_url=viewer_base_url)
        self.meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_server_instance_id() -> str:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return f"ssh-mcp-{timestamp}-{secrets.token_hex(4)}"


def build_runtime(
    *,
    server_instance_id: str | None = None,
    client_label: str | None = None,
    config_path: str | Path | None = None,
) -> ServerRuntime:
    """根据环境变量构造实例隔离的 runtime，并保留显式路径的兼容逻辑。"""

    instance_id = server_instance_id or make_server_instance_id()
    resolved_client_label = _optional_env_value(client_label) or _optional_env_value(os.getenv("SSH_MCP_CLIENT_LABEL"))
    runtime_dir = Path(os.getenv("SSH_MCP_RUNTIME_DIR") or DEFAULT_RUNTIME_DIR)
    instance_dir = runtime_dir / "instances" / instance_id

    explicit_log_path = _optional_env_value(os.getenv("SSH_MCP_LOG_PATH"))
    explicit_transcripts_dir = _optional_env_value(os.getenv("SSH_MCP_TRANSCRIPTS_DIR"))

    runtime = ServerRuntime(
        server_instance_id=instance_id,
        client_label=resolved_client_label,
        started_at=datetime.now().astimezone(),
        cwd=Path.cwd(),
        runtime_dir=runtime_dir,
        instance_dir=instance_dir,
        log_path=Path(explicit_log_path) if explicit_log_path else instance_dir / "logs" / "ssh_mcp.log",
        transcripts_dir=Path(explicit_transcripts_dir) if explicit_transcripts_dir else instance_dir / "transcripts",
        explicit_log_path=bool(explicit_log_path),
        explicit_transcripts_dir=bool(explicit_transcripts_dir),
        config_path=Path(config_path) if config_path else None,
    )
    runtime.ensure_dirs()
    return runtime


def _optional_env_value(value: str | None) -> str | None:
    """过滤空值和未展开的 ${...} 模板，避免把配置占位符当作真实值。"""

    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith("${") and text.endswith("}"):
        return None
    return text
