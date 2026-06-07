from __future__ import annotations

from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
import re
from threading import Lock
import time
from typing import Any

from .security import redact_extra, redact_text


DEFAULT_TRANSCRIPTS_DIR = Path("transcripts")
_DONE_MARKER_RE = re.compile(r"^.*__SSH_MCP_DONE_[A-Za-z0-9_-]+__.*(?:\r?\n)?", re.MULTILINE)


class TranscriptWriter:
    """线程安全地写入 JSONL 审计记录，并对敏感输入做本地脱敏。"""

    def __init__(
        self,
        session_id: str,
        base_dir: str | Path | None = None,
        *,
        redact: bool = True,
        retention_days: int | None = None,
        max_files: int | None = None,
    ) -> None:
        self.session_id = session_id
        self.base_dir = Path(base_dir or os.getenv("SSH_MCP_TRANSCRIPTS_DIR") or DEFAULT_TRANSCRIPTS_DIR)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        _chmod_private(self.base_dir, 0o700)
        self.path = self.base_dir / f"{session_id}.jsonl"
        self.redact = redact
        self._lock = Lock()
        prune_transcripts(self.base_dir, retention_days=retention_days, max_files=max_files)

    def record(
        self,
        direction: str,
        text: str,
        *,
        tool: str | None = None,
        sensitive: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        visible_text = redact_text(text, force=sensitive) if self.redact else text
        event: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "session_id": self.session_id,
            "dir": direction,
            "text": visible_text,
        }
        if tool:
            event["tool"] = tool
        if sensitive:
            event["sensitive"] = True
            if self.redact:
                event["redacted"] = True
        if extra:
            event.update(redact_extra(extra) if self.redact else extra)

        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            with _open_private_append(self.path) as handle:
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


def get_transcripts_dir(base_dir: str | Path | None = None) -> Path:
    return Path(base_dir or os.getenv("SSH_MCP_TRANSCRIPTS_DIR") or DEFAULT_TRANSCRIPTS_DIR)


def prune_transcripts(
    base_dir: str | Path | None = None,
    *,
    retention_days: int | None = None,
    max_files: int | None = None,
) -> list[Path]:
    transcripts_dir = get_transcripts_dir(base_dir)
    if not transcripts_dir.exists():
        return []

    deleted: list[Path] = []
    files = sorted(
        [path for path in transcripts_dir.glob("*.jsonl") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    if retention_days is not None and retention_days >= 0:
        cutoff = time.time() - (retention_days * 86400)
        for path in list(files):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    deleted.append(path)
                    files.remove(path)
            except OSError:
                continue

    if max_files is not None and max_files >= 0:
        for path in files[max_files:]:
            try:
                path.unlink()
                deleted.append(path)
            except OSError:
                continue

    return deleted


def read_events(path: str | Path, *, after_line: int = 0, limit: int = 1000) -> tuple[list[dict[str, Any]], int]:
    transcript_path = Path(path)
    if limit <= 0 or not transcript_path.exists():
        return [], after_line

    events: list[dict[str, Any]] = []
    last_line = after_line
    with transcript_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line_number <= after_line:
                continue
            if not line.strip():
                last_line = line_number
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = {"dir": "error", "text": line.rstrip("\n")}
            event.setdefault("session_id", transcript_path.stem)
            event["line"] = line_number
            events.append(event)
            last_line = line_number
            if len(events) >= limit:
                break

    return events, last_line


def list_transcript_summaries(base_dir: str | Path | None = None) -> list[dict[str, Any]]:
    transcripts_dir = get_transcripts_dir(base_dir)
    if not transcripts_dir.exists():
        return []

    summaries = [_summarize_transcript(path) for path in transcripts_dir.glob("*.jsonl")]
    summaries = [summary for summary in summaries if summary]
    summaries.sort(key=lambda item: item.get("last_activity_at") or item.get("updated_at") or "", reverse=True)
    return summaries


def render_terminal_delta(events: list[dict[str, Any]]) -> str:
    """把 JSONL 事件还原成终端视图，过滤 execute_command 的内部 marker 噪音。"""

    chunks: list[str] = []
    for index, event in enumerate(events):
        direction = event.get("dir")
        text = str(event.get("text", ""))
        if direction == "recv":
            chunks.append(clean_terminal_text(text))
        elif direction == "send":
            display_text = clean_terminal_text(text)
            if not display_text:
                continue
            next_recv = _next_recv_text(events, index)
            if next_recv and _starts_with_terminal_echo(next_recv, display_text):
                continue
            chunks.append(display_text)
        elif direction == "event":
            chunks.append(f"\r\n[{text}]\r\n")
        elif direction == "error":
            chunks.append(f"\r\n[error] {text}\r\n")
    return "".join(chunks)


def clean_terminal_text(text: str) -> str:
    return _DONE_MARKER_RE.sub("", text)


def _summarize_transcript(path: Path) -> dict[str, Any] | None:
    """从历史 JSONL 中提取 viewer 首页需要的轻量摘要。"""

    try:
        stat = path.stat()
        events, last_line = read_events(path, limit=10_000)
    except OSError:
        return None

    session_id = path.stem
    summary: dict[str, Any] = {
        "session_id": session_id,
        "transcript_path": str(path),
        "bytes": stat.st_size,
        "updated_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="milliseconds"),
        "line_count": last_line,
        "closed": False,
    }

    for event in events:
        direction = event.get("dir")
        text = event.get("text")
        timestamp = event.get("ts")
        if timestamp:
            summary.setdefault("created_at", timestamp)
            summary["last_activity_at"] = timestamp
        if direction == "session_meta":
            for key, value in event.items():
                if key not in {"dir", "text", "ts", "line"}:
                    summary[key] = value
        elif direction == "session_health":
            summary["health_status"] = event.get("health_status") or "unhealthy"
            summary["health_error"] = event.get("health_error") or text
            if summary["health_status"] in {"closed", "unhealthy"}:
                summary["closed"] = True
        elif direction == "event" and text == "session closed":
            summary["closed"] = True

    return summary


def _next_recv_text(events: list[dict[str, Any]], index: int) -> str:
    for event in events[index + 1 :]:
        if event.get("dir") == "recv":
            return str(event.get("text", ""))
        if event.get("dir") in {"send", "event", "error", "session_meta"}:
            continue
    return ""


def _starts_with_terminal_echo(recv_text: str, send_text: str) -> bool:
    normalized_recv = recv_text.replace("\r\n", "\n").replace("\r", "\n")
    normalized_send = send_text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized_recv.startswith(normalized_send)


def _open_private_append(path: Path):
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        _chmod_private(path, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def _chmod_private(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass
