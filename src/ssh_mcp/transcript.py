from __future__ import annotations

from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any


DEFAULT_TRANSCRIPTS_DIR = Path("transcripts")


class TranscriptWriter:
    def __init__(self, session_id: str, base_dir: str | Path | None = None) -> None:
        self.session_id = session_id
        self.base_dir = Path(base_dir or os.getenv("SSH_MCP_TRANSCRIPTS_DIR") or DEFAULT_TRANSCRIPTS_DIR)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / f"{session_id}.jsonl"
        self._lock = Lock()

    def record(
        self,
        direction: str,
        text: str,
        *,
        tool: str | None = None,
        sensitive: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "session_id": self.session_id,
            "dir": direction,
            "text": "[REDACTED]" if sensitive else text,
        }
        if tool:
            event["tool"] = tool
        if sensitive:
            event["sensitive"] = True
        if extra:
            event.update(extra)

        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def tail(self, count: int = 200) -> list[dict[str, Any]]:
        if count <= 0 or not self.path.exists():
            return []

        lines: deque[str] = deque(maxlen=count)
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    lines.append(line)

        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"dir": "error", "text": line.rstrip("\n")})
        return events

