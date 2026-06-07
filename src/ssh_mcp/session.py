from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import re
import secrets
import shlex
import string
import threading
import time
from typing import Any
from urllib.parse import quote

from .config import SshProfile
from .runtime import ServerRuntime, build_runtime
from .security import SecurityDecision
from .transcript import TranscriptWriter


LOGGER = logging.getLogger(__name__)
MAX_BUFFER_CHARS = 200_000
MAX_COMMAND_OUTPUT_CHARS = 5_000_000
DEFAULT_INPUT_LOCK_TTL = 60.0


class SessionError(RuntimeError):
    """Raised when a session cannot complete the requested operation."""


class TerminalBuffer:
    """保存最近一段 PTY 输出，并用绝对偏移支持增量读取。"""

    def __init__(self, max_chars: int = MAX_BUFFER_CHARS) -> None:
        self.max_chars = max_chars
        self._chunks: deque[str] = deque()
        self._total_chars = 0
        self._dropped_chars = 0
        self._last_append_at = time.monotonic()
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._chunks.append(text)
            self._total_chars += len(text)
            self._last_append_at = time.monotonic()
            self._trim_locked()

    def snapshot(self) -> tuple[int, str]:
        with self._lock:
            return self._total_chars, "".join(self._chunks)

    def text_since(self, offset: int) -> str:
        with self._lock:
            text = "".join(self._chunks)
            start = max(0, offset - self._dropped_chars)
            return text[start:]

    def contains_since(self, offset: int, needle: str) -> bool:
        return needle in self.text_since(offset)

    def last_lines(self, count: int = 100) -> str:
        _, text = self.snapshot()
        if count <= 0:
            return ""
        return "\n".join(text.splitlines()[-count:])

    def quiet_for(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_append_at

    def _trim_locked(self) -> None:
        # 输出量可能很大，只保留最近窗口；_dropped_chars 用来把旧绝对偏移映射回当前缓冲区。
        while self._chunks and sum(len(chunk) for chunk in self._chunks) > self.max_chars:
            dropped = self._chunks.popleft()
            self._dropped_chars += len(dropped)


@dataclass(frozen=True)
class CommandResult:
    output: str
    exit_code: int | None
    timed_out: bool
    matched: str | None
    command_id: str | None = None
    status: str | None = None
    output_truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "matched": self.matched,
            "command_id": self.command_id,
            "status": self.status,
            "output_truncated": self.output_truncated,
        }


class TrackedCommand:
    """Tracks one marker-wrapped command across tool timeouts while reader keeps draining PTY output."""

    def __init__(
        self,
        command_id: str,
        command: str,
        marker: str,
        marker_pattern: re.Pattern[str],
        start_offset: int,
        *,
        actor: str = "agent",
        max_output_chars: int = MAX_COMMAND_OUTPUT_CHARS,
    ) -> None:
        self.command_id = command_id
        self.command = command
        self.actor = actor
        self.marker = marker
        self.marker_pattern = marker_pattern
        self.start_offset = start_offset
        self.max_output_chars = max_output_chars
        self.started_at = datetime.now().astimezone()
        self.updated_at = self.started_at
        self.completed_at: datetime | None = None
        self.status = "running"
        self.exit_code: int | None = None
        self.matched: str | None = None
        self.error: str | None = None
        self.last_timeout_at: datetime | None = None
        self.cancel_requested_at: datetime | None = None
        self._chunks: deque[str] = deque()
        self._total_output_chars = 0
        self._dropped_output_chars = 0

    def append_output(self, text: str) -> None:
        if not text:
            return
        self._chunks.append(text)
        self._total_output_chars += len(text)
        self.updated_at = datetime.now().astimezone()
        self._trim_output()
        self.refresh_status()

    def refresh_status(self) -> None:
        if self.status not in {"running", "cancel_requested"}:
            return
        match = self.marker_pattern.search(self.output())
        if not match:
            return
        self.exit_code = int(match.group(1))
        self.status = "cancelled" if self.cancel_requested_at and self.exit_code != 0 else "completed"
        self.matched = self.marker
        self.completed_at = datetime.now().astimezone()
        self.updated_at = self.completed_at

    def mark_timeout(self) -> None:
        self.last_timeout_at = datetime.now().astimezone()
        self.updated_at = self.last_timeout_at

    def mark_cancel_requested(self) -> None:
        self.cancel_requested_at = datetime.now().astimezone()
        self.updated_at = self.cancel_requested_at
        if self.status == "running":
            self.status = "cancel_requested"

    def mark_failed(self, error: str, *, status: str = "failed") -> None:
        if self.status in {"completed", "failed", "session_closed"}:
            return
        self.status = status
        self.error = error
        self.completed_at = datetime.now().astimezone()
        self.updated_at = self.completed_at

    def output(self) -> str:
        return "".join(self._chunks)

    def output_for_response(self, limit: int | None = None) -> tuple[str, bool]:
        text = self.output()
        truncated = self.output_truncated
        if limit == 0:
            return "", bool(text) or truncated
        if limit is not None and limit >= 0 and len(text) > limit:
            text = text[-limit:]
            truncated = True
        return text, truncated

    @property
    def output_truncated(self) -> bool:
        return self._dropped_output_chars > 0

    def info(self, *, output_limit: int | None = None) -> dict[str, Any]:
        output, truncated = self.output_for_response(output_limit)
        return {
            "command_id": self.command_id,
            "command": self.command,
            "actor": self.actor,
            "status": self.status,
            "started_at": self.started_at.isoformat(timespec="milliseconds"),
            "updated_at": self.updated_at.isoformat(timespec="milliseconds"),
            "completed_at": self.completed_at.isoformat(timespec="milliseconds") if self.completed_at else None,
            "exit_code": self.exit_code,
            "timed_out": self.status == "running" and self.last_timeout_at is not None,
            "last_timeout_at": self.last_timeout_at.isoformat(timespec="milliseconds") if self.last_timeout_at else None,
            "cancel_requested_at": self.cancel_requested_at.isoformat(timespec="milliseconds")
            if self.cancel_requested_at
            else None,
            "matched": self.matched,
            "marker": self.marker,
            "error": self.error,
            "output": output,
            "output_chars": self._total_output_chars,
            "output_dropped_chars": self._dropped_output_chars,
            "output_truncated": truncated,
        }

    def _trim_output(self) -> None:
        while self._chunks and sum(len(chunk) for chunk in self._chunks) > self.max_output_chars:
            dropped = self._chunks.popleft()
            self._dropped_output_chars += len(dropped)


@dataclass
class InputLock:
    actor: str
    acquired_at: datetime
    expires_at: datetime

    def expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now().astimezone()) >= self.expires_at

    def refresh(self, ttl: float) -> None:
        now = datetime.now().astimezone()
        self.acquired_at = now
        self.expires_at = now + timedelta(seconds=max(ttl, 1.0))

    def info(self) -> dict[str, Any]:
        now = datetime.now().astimezone()
        return {
            "actor": self.actor,
            "acquired_at": self.acquired_at.isoformat(timespec="milliseconds"),
            "expires_at": self.expires_at.isoformat(timespec="milliseconds"),
            "expired": self.expired(now),
            "ttl_remaining_seconds": max((self.expires_at - now).total_seconds(), 0.0),
        }


class SshSession:
    """一个长期存活的交互式 SSH shell，会同时维护 reader、health 和 transcript。"""

    def __init__(
        self,
        session_id: str,
        profile: SshProfile,
        client: Any,
        channel: Any,
        transcript: TranscriptWriter,
        *,
        owner_label: str | None = None,
        server_instance_id: str,
        viewer_url: str | None = None,
        previous_session_id: str | None = None,
        previous_transcript_path: str | None = None,
    ) -> None:
        self.id = session_id
        self.profile = profile
        self.client = client
        self.channel = channel
        self.transcript = transcript
        self.owner_label = owner_label
        self.server_instance_id = server_instance_id
        self.viewer_url = viewer_url
        self.previous_session_id = previous_session_id
        self.previous_transcript_path = previous_transcript_path
        self.buffer = TerminalBuffer()
        self.created_at = datetime.now().astimezone()
        self.last_activity_at = self.created_at
        self.health_status = "healthy"
        self.last_heartbeat_at = self.created_at
        self.health_error: str | None = None
        self.closed = False
        self.read_error: str | None = None
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._health_lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._input_lock_guard = threading.Lock()
        self._input_lock: InputLock | None = None
        self._commands: dict[str, TrackedCommand] = {}
        self._active_command_id: str | None = None
        self._health_event_recorded = False
        self._reader = threading.Thread(target=self._reader_loop, name=f"ssh-mcp-reader-{session_id}", daemon=True)
        self._health_monitor = threading.Thread(
            target=self._health_loop,
            name=f"ssh-mcp-health-{session_id}",
            daemon=True,
        )
        self._reader.start()
        self._health_monitor.start()

    def send_text(
        self,
        text: str,
        *,
        enter: bool = True,
        wait_for: str = "",
        timeout: float = 30.0,
        sensitive: bool = False,
        tool: str = "send_text",
        actor: str = "agent",
        lock_ttl: float = DEFAULT_INPUT_LOCK_TTL,
        force: bool = False,
    ) -> CommandResult:
        actor = self._normalize_actor(actor)
        if tool not in {"cancel_command", "execute_command", "interrupt"}:
            self._enforce_text_policy(text, tool=tool, actor=actor)
        self._ensure_open()
        self._require_input_lock(actor=actor, ttl=lock_ttl, force=force)
        payload = text
        if enter and not payload.endswith("\n"):
            payload += "\n"
        offset = self._send_payload(payload, tool=tool, sensitive=sensitive, actor=actor)

        if wait_for:
            timed_out = not self._wait_for(wait_for, offset, timeout)
            output = self.buffer.text_since(offset)
            return CommandResult(output=output, exit_code=None, timed_out=timed_out, matched=None if timed_out else wait_for)

        self._wait_until_quiet(min(timeout, 0.5), quiet_for=0.15)
        return CommandResult(output=self.buffer.text_since(offset), exit_code=None, timed_out=False, matched=None)

    def execute_command(
        self,
        command: str,
        *,
        wait_for: str = "",
        wait_for_prompt: bool = True,
        timeout: float = 30.0,
        policy_tool: str = "execute_command",
        actor: str = "agent",
        lock_ttl: float = DEFAULT_INPUT_LOCK_TTL,
        force: bool = False,
    ) -> CommandResult:
        actor = self._normalize_actor(actor)
        self._enforce_command_policy(command, tool=policy_tool, actor=actor)
        if wait_for:
            return self.send_text(
                command,
                enter=True,
                wait_for=wait_for,
                timeout=timeout,
                tool="execute_command",
                actor=actor,
                lock_ttl=lock_ttl,
                force=force,
            )
        if not wait_for_prompt:
            return self.send_text(
                command,
                enter=True,
                wait_for="",
                timeout=timeout,
                tool="execute_command",
                actor=actor,
                lock_ttl=lock_ttl,
                force=force,
            )

        self._ensure_open()
        self._require_input_lock(actor=actor, ttl=lock_ttl, force=force)
        tracked = self._start_tracked_command(command, actor=actor)
        completed = self._wait_for_command(tracked.command_id, timeout)
        info = tracked.info()
        timed_out = not completed and info["status"] in {"running", "cancel_requested"}
        if timed_out:
            tracked.mark_timeout()
            self.transcript.record(
                "command_timeout",
                "command still running after tool timeout",
                extra=tracked.info(output_limit=0),
            )
        info = tracked.info()
        return CommandResult(
            output=info["output"],
            exit_code=info["exit_code"],
            timed_out=timed_out,
            matched=info["matched"],
            command_id=tracked.command_id,
            status=info["status"],
            output_truncated=info["output_truncated"],
        )

    def interrupt(
        self,
        *,
        actor: str = "agent",
        lock_ttl: float = DEFAULT_INPUT_LOCK_TTL,
        force: bool = False,
    ) -> CommandResult:
        return self.send_text(
            "\x03",
            enter=False,
            wait_for="",
            timeout=1.0,
            tool="interrupt",
            actor=actor,
            lock_ttl=lock_ttl,
            force=force,
        )

    def get_command(self, command_id: str, *, output_limit: int | None = None) -> dict[str, Any]:
        with self._command_lock:
            command = self._commands.get(command_id)
            if not command:
                raise SessionError(f"Unknown command_id: {command_id}")
            command.refresh_status()
            return command.info(output_limit=output_limit)

    def list_commands(self, *, output_limit: int = 0) -> list[dict[str, Any]]:
        with self._command_lock:
            commands = list(self._commands.values())
            for command in commands:
                command.refresh_status()
            commands.sort(key=lambda item: item.started_at, reverse=True)
            return [command.info(output_limit=output_limit) for command in commands]

    def cancel_command(
        self,
        command_id: str,
        *,
        actor: str = "agent",
        lock_ttl: float = DEFAULT_INPUT_LOCK_TTL,
        force: bool = False,
    ) -> CommandResult:
        actor = self._normalize_actor(actor)
        self._ensure_open()
        self._require_input_lock(actor=actor, ttl=lock_ttl, force=force)
        with self._command_lock:
            command = self._commands.get(command_id)
            if not command:
                raise SessionError(f"Unknown command_id: {command_id}")
            if command.status not in {"running", "cancel_requested"}:
                info = command.info()
                return CommandResult(
                    output=info["output"],
                    exit_code=info["exit_code"],
                    timed_out=False,
                    matched=info["matched"],
                    command_id=command_id,
                    status=info["status"],
                    output_truncated=info["output_truncated"],
                )
            command.mark_cancel_requested()
            self.transcript.record(
                "command_cancel",
                "Ctrl+C requested for tracked command",
                extra={**command.info(output_limit=0), "actor": actor},
            )
        self._send_payload(
            "\x03",
            tool="cancel_command",
            sensitive=False,
            actor=actor,
            extra={"command_id": command_id},
        )
        info = self.get_command(command_id)
        return CommandResult(
            output=info["output"],
            exit_code=info["exit_code"],
            timed_out=info["status"] in {"running", "cancel_requested"},
            matched=info["matched"],
            command_id=command_id,
            status=info["status"],
            output_truncated=info["output_truncated"],
        )

    def input_lock_info(self) -> dict[str, Any]:
        with self._input_lock_guard:
            if not self._input_lock:
                return {"locked": False, "actor": None, "expired": False, "ttl_remaining_seconds": 0.0}
            info = self._input_lock.info()
            return {"locked": not info["expired"], **info}

    def acquire_input_lock(
        self,
        *,
        actor: str = "agent",
        ttl: float = DEFAULT_INPUT_LOCK_TTL,
        force: bool = False,
    ) -> dict[str, Any]:
        self._ensure_open()
        return self._acquire_input_lock(
            actor=self._normalize_actor(actor),
            ttl=ttl,
            force=force,
            record_refresh=True,
        )

    def release_input_lock(
        self,
        *,
        actor: str = "agent",
        force: bool = False,
    ) -> dict[str, Any]:
        actor = self._normalize_actor(actor)
        previous: dict[str, Any] | None = None
        denied = False
        with self._input_lock_guard:
            if not self._input_lock:
                input_lock = {"locked": False, "actor": None, "expired": False, "ttl_remaining_seconds": 0.0}
                return {"released": False, "input_lock": input_lock}
            now = datetime.now().astimezone()
            if self._input_lock.expired(now):
                previous = self._input_lock.info()
                self._input_lock = None
                released = False
            elif self._input_lock.actor != actor and not force:
                previous = self._input_lock.info()
                denied = True
                released = False
            else:
                previous = self._input_lock.info()
                self._input_lock = None
                released = True
            input_lock = (
                {"locked": False, "actor": None, "expired": False, "ttl_remaining_seconds": 0.0}
                if self._input_lock is None
                else {"locked": True, **self._input_lock.info()}
            )

        if denied:
            self.transcript.record(
                "input_lock_denied",
                "input lock release denied",
                extra={"actor": actor, "requested_action": "release", "current_lock": previous},
            )
            raise SessionError(
                f"Input lock is held by '{previous.get('actor') if previous else 'unknown'}'; "
                "use force=True to release it."
            )

        self.transcript.record(
            "input_lock_released",
            "input lock released",
            extra={"actor": actor, "force": force, "released": released, "previous_lock": previous},
        )
        return {"released": released, "input_lock": input_lock, "previous_lock": previous}

    def screen(self, lines: int = 100) -> str:
        return self.buffer.last_lines(lines)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._stop_event.set()
        self._mark_active_command_failed("session closed", status="session_closed")
        try:
            self.channel.close()
        except Exception:
            LOGGER.exception("Failed to close SSH channel for %s", self.id)
        try:
            self.client.close()
        except Exception:
            LOGGER.exception("Failed to close SSH client for %s", self.id)
        self._clear_input_lock()
        self.health_status = "closed"
        self.transcript.record("event", "session closed")
        LOGGER.info(
            "SSH session closed: %s",
            self.id,
            extra={"session_id": self.id, "owner_label": self.owner_label},
        )

    def info(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "profile": self.profile.name,
            "host": self.profile.host,
            "port": self.profile.port,
            "username": self.profile.username,
            "owner_label": self.owner_label,
            "server_instance_id": self.server_instance_id,
            "viewer_url": self.viewer_url,
            "previous_session_id": self.previous_session_id,
            "previous_transcript_path": self.previous_transcript_path,
            "created_at": self.created_at.isoformat(timespec="milliseconds"),
            "last_activity_at": self.last_activity_at.isoformat(timespec="milliseconds"),
            "health_status": self.health_status,
            "last_heartbeat_at": self.last_heartbeat_at.isoformat(timespec="milliseconds")
            if self.last_heartbeat_at
            else None,
            "health_error": self.health_error,
            "closed": self.closed,
            "read_error": self.read_error,
            "transcript_path": str(self.transcript.path),
            "active_command_id": self._active_command_id,
            "commands": self.list_commands(output_limit=0),
            "input_lock": self.input_lock_info(),
            "security": {
                "mode": self.profile.security.mode,
                "redact_transcripts": self.profile.security.redact_transcripts,
                "transcript_retention_days": self.profile.security.transcript_retention_days,
                "transcript_max_files": self.profile.security.transcript_max_files,
            },
        }

    def error_info(self, message: str) -> dict[str, Any]:
        info = self.info()
        return {
            "error": message,
            "session_id": self.id,
            "health_status": info["health_status"],
            "health_error": info["health_error"],
            "last_activity_at": info["last_activity_at"],
            "last_heartbeat_at": info["last_heartbeat_at"],
            "closed": info["closed"],
            "read_error": info["read_error"],
            "transcript_path": info["transcript_path"],
            "session": info,
        }

    def _normalize_actor(self, actor: str | None) -> str:
        normalized = (actor or "agent").strip()
        if not normalized:
            normalized = "agent"
        if len(normalized) > 64 or any(ch in normalized for ch in "\r\n\t"):
            raise SessionError("Invalid actor label.")
        return normalized

    def _normalize_lock_ttl(self, ttl: float) -> float:
        try:
            value = float(ttl)
        except (TypeError, ValueError) as exc:
            raise SessionError("Invalid input lock ttl.") from exc
        return min(max(value, 1.0), 3600.0)

    def _require_input_lock(self, *, actor: str, ttl: float, force: bool) -> dict[str, Any]:
        return self._acquire_input_lock(actor=actor, ttl=ttl, force=force, record_refresh=False)

    def _acquire_input_lock(
        self,
        *,
        actor: str,
        ttl: float,
        force: bool,
        record_refresh: bool,
    ) -> dict[str, Any]:
        ttl = self._normalize_lock_ttl(ttl)
        now = datetime.now().astimezone()
        expires_at = now + timedelta(seconds=ttl)
        previous: dict[str, Any] | None = None
        event_dir = "input_lock_acquired"
        should_record = True

        with self._input_lock_guard:
            current = self._input_lock
            if current and not current.expired(now):
                previous = current.info()
                if current.actor == actor:
                    current.refresh(ttl)
                    info = {"locked": True, **current.info()}
                    event_dir = "input_lock_refreshed"
                    should_record = record_refresh
                elif force:
                    self._input_lock = InputLock(actor=actor, acquired_at=now, expires_at=expires_at)
                    info = {"locked": True, **self._input_lock.info()}
                    event_dir = "input_lock_takeover"
                else:
                    info = {"locked": True, **current.info()}
                    denied_extra = {
                        "actor": actor,
                        "requested_action": "acquire",
                        "force": force,
                        "current_lock": previous,
                    }
                    self.transcript.record("input_lock_denied", "input lock acquire denied", extra=denied_extra)
                    raise SessionError(
                        f"Input lock is held by '{current.actor}' until "
                        f"{current.expires_at.isoformat(timespec='milliseconds')}; use force=True to take over."
                    )
            else:
                previous = current.info() if current else None
                self._input_lock = InputLock(actor=actor, acquired_at=now, expires_at=expires_at)
                info = {"locked": True, **self._input_lock.info()}

        if should_record:
            self.transcript.record(
                event_dir,
                "input lock acquired",
                extra={"actor": actor, "force": force, "ttl_seconds": ttl, "input_lock": info, "previous_lock": previous},
            )
        return {"input_lock": info, "previous_lock": previous}

    def _clear_input_lock(self) -> None:
        with self._input_lock_guard:
            self._input_lock = None

    def _reader_loop(self) -> None:
        # reader 线程只负责持续搬运 PTY 输出，所有发送动作由调用线程串行完成。
        while not self._stop_event.is_set():
            try:
                if self.channel.recv_ready():
                    data = self.channel.recv(4096)
                    if not data:
                        break
                    text = data.decode("utf-8", errors="replace")
                    self._record_recv_text(text)
                    continue
                if self.channel.exit_status_ready():
                    break
                time.sleep(0.05)
            except Exception as exc:
                self.read_error = str(exc)
                LOGGER.exception("Reader loop failed for session %s", self.id)
                self.transcript.record("error", str(exc))
                break
        if not self._stop_event.is_set() and self.health_status == "healthy":
            self._mark_unhealthy("SSH reader loop ended", status="closed")
        else:
            self._clear_input_lock()
            self.closed = True
            if self.health_status == "healthy":
                self.health_status = "closed"

    def check_health_once(self) -> bool:
        if self.closed:
            self._mark_unhealthy("session is closed", status="closed")
            return False

        try:
            transport = self.client.get_transport()
            if transport is None:
                self._mark_unhealthy("SSH transport is missing")
                return False
            if not transport.is_active():
                self._mark_unhealthy("SSH transport is inactive")
                return False
            if getattr(self.channel, "closed", False):
                self._mark_unhealthy("SSH channel is closed")
                return False
        except Exception as exc:
            self._mark_unhealthy(f"SSH health check failed: {exc}")
            return False

        with self._health_lock:
            self.health_status = "healthy"
            self.health_error = None
            self.last_heartbeat_at = datetime.now().astimezone()
        return True

    def _health_loop(self) -> None:
        # health monitor 只探测连接状态，不尝试重连或重放 CMSM/master/pod 路径。
        interval = max(float(self.profile.keepalive_interval or 30.0), 1.0)
        while not self._stop_event.wait(interval):
            if not self.check_health_once():
                return

    def _mark_unhealthy(self, message: str, *, status: str = "unhealthy") -> None:
        with self._health_lock:
            if self.health_status == status and self.health_error == message:
                return
            self.health_status = status
            self.health_error = message
            self.last_heartbeat_at = datetime.now().astimezone()
            first_record = not self._health_event_recorded
            self._health_event_recorded = True

        self.closed = True
        self._stop_event.set()
        self._clear_input_lock()
        self._mark_active_command_failed(message, status="session_closed")
        if first_record:
            self.transcript.record("session_health", message, extra={"health_status": status, "health_error": message})
        prefix = "SSH session closed" if status == "closed" else "SSH session unhealthy"
        LOGGER.warning(
            "%s: %s",
            prefix,
            message,
            extra={"session_id": self.id, "owner_label": self.owner_label},
        )

    def _send_payload(
        self,
        payload: str,
        *,
        tool: str,
        sensitive: bool,
        actor: str,
        extra: dict[str, Any] | None = None,
    ) -> int:
        self._ensure_open()
        offset, _ = self.buffer.snapshot()
        with self._write_lock:
            self.channel.send(payload)
            self.last_activity_at = datetime.now().astimezone()
            event_extra = {"actor": actor, **(extra or {})}
            self.transcript.record("send", payload, tool=tool, sensitive=sensitive, extra=event_extra)
        return offset

    def _enforce_command_policy(self, command: str, *, tool: str, actor: str) -> None:
        decision = self.profile.security.evaluate_command(command, tool=tool)
        if not decision.allowed:
            self._record_security_block("command", command, decision, tool=tool, actor=actor)
            raise SessionError(f"Security policy blocked {tool}: {decision.reason}")

    def _enforce_text_policy(self, text: str, *, tool: str, actor: str) -> None:
        decision = self.profile.security.evaluate_text(text, tool=tool)
        if not decision.allowed:
            self._record_security_block("text", text, decision, tool=tool, actor=actor)
            raise SessionError(f"Security policy blocked {tool}: {decision.reason}")

    def _record_security_block(self, kind: str, text: str, decision: SecurityDecision, *, tool: str, actor: str) -> None:
        self.transcript.record(
            "security_block",
            f"blocked {kind}",
            tool=tool,
            extra={
                "actor": actor,
                "blocked_kind": kind,
                "blocked_text": text,
                "security": decision.as_dict(),
            },
        )

    def _start_tracked_command(self, command: str, *, actor: str) -> TrackedCommand:
        self._ensure_open()
        with self._command_lock:
            if self._active_command_id:
                active = self._commands.get(self._active_command_id)
                if active and active.status in {"running", "cancel_requested"}:
                    raise SessionError(
                        f"Session '{self.id}' already has running command {active.command_id}. "
                        "Poll it with get_command or cancel it before starting another tracked command."
                    )
                self._active_command_id = None

            command_id = _make_command_id()
            marker = f"__SSH_MCP_DONE_{command_id}__"
            marker_pattern = re.compile(rf"{re.escape(marker)}:(-?\d+)")
            offset, _ = self.buffer.snapshot()
            tracked = TrackedCommand(command_id, command, marker, marker_pattern, offset, actor=actor)
            self._commands[command_id] = tracked
            self._active_command_id = command_id

        self.transcript.record(
            "command_start",
            "tracked command started",
            extra={**tracked.info(output_limit=0), "actor": actor},
        )
        wrapped = f"{command}\nprintf '\\n{marker}:%s\\n' \"$?\""
        try:
            self._send_payload(
                wrapped + "\n",
                tool="execute_command",
                sensitive=False,
                actor=actor,
                extra={"command_id": command_id, "command_marker": marker},
            )
        except Exception:
            with self._command_lock:
                if self._active_command_id == command_id:
                    self._active_command_id = None
                tracked.mark_failed("failed to send command")
                failed_info = tracked.info(output_limit=0)
            self.transcript.record("command_failed", "tracked command failed", extra=failed_info)
            raise
        return tracked

    def _record_recv_text(self, text: str) -> None:
        self.buffer.append(text)
        extra: dict[str, Any] | None = None
        completed: TrackedCommand | None = None
        with self._command_lock:
            active = self._commands.get(self._active_command_id or "")
            if active:
                active.append_output(text)
                extra = {"command_id": active.command_id}
                if active.status in {"completed", "cancelled"}:
                    completed = active
                    self._active_command_id = None
        self.transcript.record("recv", text, extra=extra)
        if completed:
            self.transcript.record(
                "command_complete",
                "tracked command completed",
                extra=completed.info(output_limit=0),
            )

    def _wait_for_command(self, command_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._raise_if_reader_failed()
            with self._command_lock:
                command = self._commands.get(command_id)
                if not command:
                    raise SessionError(f"Unknown command_id: {command_id}")
                command.refresh_status()
                if command.status in {"completed", "cancelled", "failed", "session_closed"}:
                    return command.status in {"completed", "cancelled"}
            if self.closed:
                return False
            time.sleep(0.05)
        return False

    def _mark_active_command_failed(self, error: str, *, status: str) -> None:
        with self._command_lock:
            active = self._commands.get(self._active_command_id or "")
            if not active:
                return
            active.mark_failed(error, status=status)
            self._active_command_id = None
            info = active.info(output_limit=0)
        self.transcript.record("command_failed", "tracked command failed", extra=info)

    def _wait_for(self, needle: str, offset: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._raise_if_reader_failed()
            if self.buffer.contains_since(offset, needle):
                return True
            if self.closed:
                return False
            time.sleep(0.05)
        return False

    def _wait_for_regex(self, pattern: re.Pattern[str], offset: int, timeout: float) -> re.Match[str] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._raise_if_reader_failed()
            match = pattern.search(self.buffer.text_since(offset))
            if match:
                return match
            if self.closed:
                return None
            time.sleep(0.05)
        return None

    def _wait_until_quiet(self, timeout: float, *, quiet_for: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.buffer.quiet_for() >= quiet_for:
                return
            if self.closed:
                return
            time.sleep(0.05)

    def _ensure_open(self) -> None:
        if self.closed:
            detail = f" {self.health_error}" if self.health_error else ""
            raise SessionError(f"Session '{self.id}' is closed.{detail}")
        if not self.check_health_once():
            detail = f" {self.health_error}" if self.health_error else ""
            raise SessionError(f"Session '{self.id}' is closed.{detail}")
        self._raise_if_reader_failed()

    def _raise_if_reader_failed(self) -> None:
        if self.read_error:
            raise SessionError(f"Session '{self.id}' reader failed: {self.read_error}")


class SessionRegistry:
    """当前 MCP Server 进程内的 session 索引和 viewer URL 绑定。"""

    def __init__(self, runtime: ServerRuntime | None = None) -> None:
        self._sessions: dict[str, SshSession] = {}
        self._lock = threading.Lock()
        self._runtime_lock = threading.Lock()
        self._runtime = runtime
        self.viewer_base_url: str | None = None

    @property
    def runtime(self) -> ServerRuntime:
        if self._runtime is not None:
            return self._runtime
        with self._runtime_lock:
            if self._runtime is None:
                self._runtime = build_runtime()
            return self._runtime

    @property
    def server_instance_id(self) -> str:
        return self.runtime.server_instance_id

    @property
    def client_label(self) -> str | None:
        return self.runtime.client_label

    @property
    def started_at(self) -> datetime:
        return self.runtime.started_at

    def set_viewer_base_url(self, base_url: str | None) -> None:
        self.viewer_base_url = base_url.rstrip("/") if base_url else None

    def session_url(self, session_id: str) -> str | None:
        if not self.viewer_base_url:
            return None
        return f"{self.viewer_base_url}/sessions/{quote(session_id, safe='')}"

    def server_info(self) -> dict[str, Any]:
        return {
            "server_instance_id": self.server_instance_id,
            "pid": os.getpid(),
            "cwd": str(Path.cwd()),
            "started_at": self.started_at.isoformat(timespec="milliseconds"),
            "client_label": self.client_label,
            "runtime_dir": str(self.runtime.runtime_dir),
            "instance_dir": str(self.runtime.instance_dir),
            "log_path": str(self.runtime.log_path),
            "transcripts_dir": str(self.runtime.transcripts_dir),
            "viewer_base_url": self.viewer_base_url,
        }

    def open(
        self,
        profile: SshProfile,
        *,
        password: str | None = None,
        passphrase: str | None = None,
        owner_label: str | None = None,
        previous_session_id: str | None = None,
        previous_transcript_path: str | None = None,
    ) -> SshSession:
        import paramiko

        session_id = _make_session_id(profile.name)
        transcript = TranscriptWriter(
            session_id,
            self.runtime.transcripts_dir,
            redact=profile.security.redact_transcripts,
            retention_days=profile.security.transcript_retention_days,
            max_files=profile.security.transcript_max_files,
        )
        viewer_url = self.session_url(session_id)
        # 首行元数据用于把 session 和 MCP 实例、LLM 标签、人类用途标签稳定关联起来。
        transcript.record(
            "session_meta",
            "session metadata",
            extra={
                "profile": profile.name,
                "host": profile.host,
                "port": profile.port,
                "username": profile.username,
                "owner_label": owner_label,
                "server_instance_id": self.server_instance_id,
                "client_label": self.client_label,
                "pid": os.getpid(),
                "cwd": str(Path.cwd()),
                "instance_dir": str(self.runtime.instance_dir),
                "viewer_url": viewer_url,
                "previous_session_id": previous_session_id,
                "previous_transcript_path": previous_transcript_path,
            },
        )
        client = paramiko.SSHClient()
        if profile.auto_add_host_key:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        resolved_passphrase = profile.resolved_passphrase(passphrase)
        connect_kwargs: dict[str, Any] = {
            "hostname": profile.host,
            "port": profile.port,
            "username": profile.username,
            "password": profile.resolved_password(password),
            "pkey": _load_private_key(profile, resolved_passphrase) if profile.key_filename else None,
            "timeout": profile.timeout,
            "banner_timeout": profile.banner_timeout,
            "auth_timeout": profile.auth_timeout,
            "allow_agent": profile.allow_agent,
            "look_for_keys": profile.look_for_keys,
        }
        connect_kwargs = {key: value for key, value in connect_kwargs.items() if value is not None}

        LOGGER.info("Opening SSH session %s to %s@%s:%s", session_id, profile.username, profile.host, profile.port)
        try:
            client.connect(**connect_kwargs)
            transport = client.get_transport()
            if transport and profile.keepalive_interval > 0:
                transport.set_keepalive(int(profile.keepalive_interval))
            channel = client.invoke_shell(term=profile.term, width=profile.width, height=profile.height)
        except Exception:
            client.close()
            raise

        session = SshSession(
            session_id,
            profile,
            client,
            channel,
            transcript,
            owner_label=owner_label,
            server_instance_id=self.server_instance_id,
            viewer_url=viewer_url,
            previous_session_id=previous_session_id,
            previous_transcript_path=previous_transcript_path,
        )
        transcript.record("event", "session opened")
        if previous_session_id:
            transcript.record(
                "event",
                "session reopened from previous session",
                extra={
                    "previous_session_id": previous_session_id,
                    "previous_transcript_path": previous_transcript_path,
                    "reopen_scope": "ssh-login-only",
                },
            )
        with self._lock:
            self._sessions[session_id] = session
        LOGGER.info("Opened SSH session", extra={"session_id": session_id, "owner_label": owner_label})
        return session

    def reopen(
        self,
        session_id: str,
        *,
        password: str | None = None,
        passphrase: str | None = None,
    ) -> SshSession:
        previous = self.get(session_id)
        previous_info = previous.info()
        previous.transcript.record(
            "event",
            "reopen requested",
            extra={
                "reopen_scope": "ssh-login-only",
                "health_status": previous_info["health_status"],
                "health_error": previous_info["health_error"],
            },
        )
        try:
            reopened = self.open(
                previous.profile,
                password=password,
                passphrase=passphrase,
                owner_label=previous.owner_label,
                previous_session_id=previous.id,
                previous_transcript_path=str(previous.transcript.path),
            )
            previous.transcript.record(
                "event",
                "reopen succeeded",
                extra={
                    "reopen_scope": "ssh-login-only",
                    "new_session_id": reopened.id,
                    "new_transcript_path": str(reopened.transcript.path),
                },
            )
            return reopened
        except Exception as exc:
            previous.transcript.record(
                "event",
                "reopen failed",
                extra={"reopen_scope": "ssh-login-only", "error": str(exc)},
            )
            raise

    def get(self, session_id: str) -> SshSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise SessionError(f"Unknown session_id: {session_id}")
        return session

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [session.info() for session in sessions]

    def close(self, session_id: str) -> dict[str, Any]:
        session = self.get(session_id)
        session.close()
        with self._lock:
            self._sessions.pop(session_id, None)
        return session.info()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()


def build_log_search_command(
    pattern: str,
    *,
    path: str = ".",
    include: str = "*.log",
    context: int = 0,
    ignore_case: bool = True,
    max_count: int = 200,
) -> str:
    """生成可在远端当前 shell 中执行的 find/grep 日志搜索命令。"""

    flags = ["-n", "-I"]
    if ignore_case:
        flags.append("-i")
    if context > 0:
        flags.extend(["-C", str(context)])
    if max_count > 0:
        flags.extend(["-m", str(max_count)])

    grep_args = " ".join(shlex.quote(arg) for arg in flags)
    quoted_pattern = shlex.quote(pattern)
    quoted_path = shlex.quote(path)
    quoted_include = shlex.quote(include)
    return (
        f"if [ -f {quoted_path} ]; then "
        f"grep {grep_args} -- {quoted_pattern} {quoted_path}; "
        f"else find {quoted_path} -type f -name {quoted_include} -print0 "
        f"| xargs -0 -r grep {grep_args} -- {quoted_pattern}; fi"
    )


def _make_session_id(profile_name: str) -> str:
    safe_name = "".join(ch if ch in string.ascii_letters + string.digits + "-_" else "-" for ch in profile_name)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return f"{safe_name}-{timestamp}-{secrets.token_hex(3)}"


def _make_command_id() -> str:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return f"cmd-{timestamp}-{secrets.token_hex(3)}"


def _load_private_key(profile: SshProfile, passphrase: str | None) -> Any:
    import paramiko

    key_path = Path(profile.key_filename or "")
    if not key_path.exists():
        raise SessionError(f"Private key file not found: {key_path}")

    key_classes = _key_classes_for_file(paramiko, key_path)
    errors: list[str] = []
    for key_class in key_classes:
        try:
            return key_class.from_private_key_file(str(key_path), password=passphrase)
        except paramiko.PasswordRequiredException as exc:
            hint = (
                f" Set environment variable {profile.passphrase_env} before starting Claude Code."
                if profile.passphrase_env
                else " Provide a passphrase or passphrase_env in the profile."
            )
            raise SessionError(f"Private key is encrypted and needs a passphrase.{hint}") from exc
        except Exception as exc:
            message = str(exc)
            if passphrase and ("Bad password" in message or "incorrect passphrase" in message):
                hint = (
                    f" Environment variable {profile.passphrase_env} is set but does not decrypt this key."
                    if profile.passphrase_env
                    else " The provided passphrase does not decrypt this key."
                )
                raise SessionError(f"Private key passphrase is incorrect.{hint}") from exc
            errors.append(f"{key_class.__name__}: {exc}")

    joined_errors = "; ".join(errors)
    raise SessionError(f"Unable to load private key {key_path}. {joined_errors}")


def _key_classes_for_file(paramiko: Any, key_path: Path) -> list[Any]:
    header = _read_key_header(key_path)
    if "BEGIN RSA PRIVATE KEY" in header:
        return [paramiko.RSAKey]
    if "BEGIN DSA PRIVATE KEY" in header:
        return [paramiko.DSSKey]
    if "BEGIN EC PRIVATE KEY" in header:
        return [paramiko.ECDSAKey]
    return [paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.DSSKey]


def _read_key_header(key_path: Path) -> str:
    with key_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if line.startswith("-----BEGIN "):
                return line.strip()
    return ""
