from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
import re
import secrets
import shlex
import string
import threading
import time
from typing import Any

from .config import SshProfile
from .transcript import TranscriptWriter


LOGGER = logging.getLogger(__name__)
MAX_BUFFER_CHARS = 200_000


class SessionError(RuntimeError):
    """Raised when a session cannot complete the requested operation."""


class TerminalBuffer:
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
        while self._chunks and sum(len(chunk) for chunk in self._chunks) > self.max_chars:
            dropped = self._chunks.popleft()
            self._dropped_chars += len(dropped)


@dataclass(frozen=True)
class CommandResult:
    output: str
    exit_code: int | None
    timed_out: bool
    matched: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "matched": self.matched,
        }


class SshSession:
    def __init__(self, session_id: str, profile: SshProfile, client: Any, channel: Any, transcript: TranscriptWriter) -> None:
        self.id = session_id
        self.profile = profile
        self.client = client
        self.channel = channel
        self.transcript = transcript
        self.buffer = TerminalBuffer()
        self.created_at = datetime.now().astimezone()
        self.last_activity_at = self.created_at
        self.closed = False
        self.read_error: str | None = None
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._reader = threading.Thread(target=self._reader_loop, name=f"ssh-mcp-reader-{session_id}", daemon=True)
        self._reader.start()

    def send_text(
        self,
        text: str,
        *,
        enter: bool = True,
        wait_for: str = "",
        timeout: float = 30.0,
        sensitive: bool = False,
        tool: str = "send_text",
    ) -> CommandResult:
        self._ensure_open()
        payload = text
        if enter and not payload.endswith("\n"):
            payload += "\n"
        offset = self._send_payload(payload, tool=tool, sensitive=sensitive)

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
    ) -> CommandResult:
        if wait_for:
            return self.send_text(command, enter=True, wait_for=wait_for, timeout=timeout, tool="execute_command")
        if not wait_for_prompt:
            return self.send_text(command, enter=True, wait_for="", timeout=timeout, tool="execute_command")

        marker = f"__SSH_MCP_DONE_{secrets.token_hex(8)}__"
        marker_pattern = re.compile(rf"{re.escape(marker)}:(-?\d+)")
        wrapped = f"{command}\nprintf '\\n{marker}:%s\\n' \"$?\""
        offset = self._send_payload(wrapped + "\n", tool="execute_command", sensitive=False)
        match = self._wait_for_regex(marker_pattern, offset, timeout)
        output = self.buffer.text_since(offset)
        exit_code = int(match.group(1)) if match else None
        return CommandResult(
            output=output,
            exit_code=exit_code,
            timed_out=match is None,
            matched=marker if match else None,
        )

    def interrupt(self) -> CommandResult:
        return self.send_text("\x03", enter=False, wait_for="", timeout=1.0, tool="interrupt")

    def screen(self, lines: int = 100) -> str:
        return self.buffer.last_lines(lines)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._stop_event.set()
        try:
            self.channel.close()
        except Exception:
            LOGGER.exception("Failed to close SSH channel for %s", self.id)
        try:
            self.client.close()
        except Exception:
            LOGGER.exception("Failed to close SSH client for %s", self.id)
        self.transcript.record("event", "session closed")

    def info(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "profile": self.profile.name,
            "host": self.profile.host,
            "port": self.profile.port,
            "username": self.profile.username,
            "created_at": self.created_at.isoformat(timespec="milliseconds"),
            "last_activity_at": self.last_activity_at.isoformat(timespec="milliseconds"),
            "closed": self.closed,
            "read_error": self.read_error,
            "transcript_path": str(self.transcript.path),
        }

    def _reader_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.channel.recv_ready():
                    data = self.channel.recv(4096)
                    if not data:
                        break
                    text = data.decode("utf-8", errors="replace")
                    self.buffer.append(text)
                    self.transcript.record("recv", text)
                    continue
                if self.channel.exit_status_ready():
                    break
                time.sleep(0.05)
            except Exception as exc:
                self.read_error = str(exc)
                LOGGER.exception("Reader loop failed for session %s", self.id)
                self.transcript.record("error", str(exc))
                break
        self.closed = True

    def _send_payload(self, payload: str, *, tool: str, sensitive: bool) -> int:
        self._ensure_open()
        offset, _ = self.buffer.snapshot()
        with self._write_lock:
            self.channel.send(payload)
            self.last_activity_at = datetime.now().astimezone()
            self.transcript.record("send", payload, tool=tool, sensitive=sensitive)
        return offset

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
            raise SessionError(f"Session '{self.id}' is closed.")
        self._raise_if_reader_failed()

    def _raise_if_reader_failed(self) -> None:
        if self.read_error:
            raise SessionError(f"Session '{self.id}' reader failed: {self.read_error}")


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, SshSession] = {}
        self._lock = threading.Lock()

    def open(self, profile: SshProfile, *, password: str | None = None, passphrase: str | None = None) -> SshSession:
        import paramiko

        session_id = _make_session_id(profile.name)
        transcript = TranscriptWriter(session_id)
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
            channel = client.invoke_shell(term=profile.term, width=profile.width, height=profile.height)
        except Exception:
            client.close()
            raise

        session = SshSession(session_id, profile, client, channel, transcript)
        transcript.record("event", "session opened")
        with self._lock:
            self._sessions[session_id] = session
        return session

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
