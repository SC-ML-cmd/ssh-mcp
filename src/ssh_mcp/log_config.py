from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any


DEFAULT_LOG_PATH = Path("logs/ssh_mcp.log")
DEFAULT_LOG_FIELDS = {
    "server_instance_id": "-",
    "pid": "-",
    "client_label": "-",
    "session_id": "-",
    "owner_label": "-",
}


class ContextDefaultsFilter(logging.Filter):
    def __init__(self, defaults: dict[str, Any]) -> None:
        super().__init__()
        self.defaults = defaults

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in self.defaults.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


def configure_logging(path: str | Path | None = None, *, context: dict[str, Any] | None = None) -> Path:
    log_path = Path(path or os.getenv("SSH_MCP_LOG_PATH") or DEFAULT_LOG_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.FileHandler(log_path, encoding="utf-8")
    defaults = {**DEFAULT_LOG_FIELDS, **(context or {})}
    handler.addFilter(ContextDefaultsFilter(defaults))
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] "
            "server=%(server_instance_id)s pid=%(pid)s client=%(client_label)s "
            "session=%(session_id)s owner=%(owner_label)s %(message)s"
        )
    )
    root.addHandler(handler)
    return log_path
