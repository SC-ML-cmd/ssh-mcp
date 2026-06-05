from __future__ import annotations

import logging
import os
from pathlib import Path


DEFAULT_LOG_PATH = Path("logs/ssh_mcp.log")


def configure_logging(path: str | None = None) -> Path:
    log_path = Path(path or os.getenv("SSH_MCP_LOG_PATH") or DEFAULT_LOG_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    root.addHandler(handler)
    return log_path

