from __future__ import annotations

import argparse
import atexit
import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import get_config_path, load_profile
from .log_config import configure_logging
from .runtime import build_runtime
from .session import DEFAULT_INPUT_LOCK_TTL, SessionError, SessionRegistry, SshSession, build_log_search_command
from .viewer import start_viewer_server, viewer_defaults_from_env


LOGGER = logging.getLogger(__name__)
mcp = FastMCP("ssh-mcp")
# FastMCP 装饰器在模块导入时绑定工具函数；main() 启动后会替换为带 runtime 的真实 registry。
registry = SessionRegistry()


@mcp.tool()
def open_session(
    profile: str,
    config_path: str | None = None,
    password: str | None = None,
    passphrase: str | None = None,
    owner_label: str | None = None,
) -> dict[str, Any]:
    """Open a persistent interactive SSH session from a named profile."""
    try:
        ssh_profile = load_profile(profile, config_path)
        session = registry.open(ssh_profile, password=password, passphrase=passphrase, owner_label=owner_label)
        return {"ok": True, "session": session.info(), "viewer_url": session.viewer_url}
    except Exception as exc:
        LOGGER.exception("open_session failed")
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def diagnose_profile(profile: str, config_path: str | None = None) -> dict[str, Any]:
    """Check profile configuration without opening an SSH connection or exposing secrets."""
    try:
        ssh_profile = load_profile(profile, config_path)
        passphrase_value = ssh_profile.resolved_passphrase()
        key_header = ""
        key_exists = False
        if ssh_profile.key_filename:
            from pathlib import Path

            key_path = Path(ssh_profile.key_filename)
            key_exists = key_path.exists()
            if key_exists:
                with key_path.open("r", encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        if line.startswith("-----BEGIN "):
                            key_header = line.strip()
                            break
        return {
            "ok": True,
            "profile": ssh_profile.name,
            "host": ssh_profile.host,
            "port": ssh_profile.port,
            "username": ssh_profile.username,
            "key_filename": ssh_profile.key_filename,
            "key_exists": key_exists,
            "key_header": key_header,
            "passphrase_env": ssh_profile.passphrase_env,
            "passphrase_env_present": bool(passphrase_value),
            "passphrase_env_length": len(passphrase_value) if passphrase_value else 0,
            "security": {
                "mode": ssh_profile.security.mode,
                "allow_patterns": list(ssh_profile.security.allow_patterns),
                "deny_patterns": list(ssh_profile.security.deny_patterns),
                "allow_interactive_text": ssh_profile.security.allow_interactive_text,
                "redact_transcripts": ssh_profile.security.redact_transcripts,
                "transcript_retention_days": ssh_profile.security.transcript_retention_days,
                "transcript_max_files": ssh_profile.security.transcript_max_files,
            },
        }
    except Exception as exc:
        LOGGER.exception("diagnose_profile failed")
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def list_sessions() -> dict[str, Any]:
    """List active sessions owned by this MCP process."""
    return {"ok": True, "server": registry.server_info(), "sessions": registry.list()}


@mcp.tool()
def close_session(session_id: str) -> dict[str, Any]:
    """Close a persistent SSH session."""
    try:
        return {"ok": True, "session": registry.close(session_id)}
    except Exception as exc:
        LOGGER.exception("close_session failed")
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def reopen_session(
    session_id: str,
    password: str | None = None,
    passphrase: str | None = None,
) -> dict[str, Any]:
    """Open a fresh SSH login using a previous session's profile; does not replay menus or commands."""
    try:
        previous = registry.get(session_id)
        previous_info = previous.info()
        session = registry.reopen(session_id, password=password, passphrase=passphrase)
        return {
            "ok": True,
            "session": session.info(),
            "viewer_url": session.viewer_url,
            "previous_session": previous_info,
            "previous_session_id": previous.id,
            "previous_transcript_path": str(previous.transcript.path),
            "reopen_scope": "ssh-login-only",
            "note": "Opened a new SSH login only; CMSM/master/pod path and previous shell state were not replayed.",
        }
    except Exception as exc:
        LOGGER.exception("reopen_session failed")
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def send_text(
    session_id: str,
    text: str,
    enter: bool = True,
    wait_for: str = "",
    timeout: float = 30.0,
    sensitive: bool = False,
    actor: str = "agent",
    lock_ttl: float = DEFAULT_INPUT_LOCK_TTL,
    force: bool = False,
) -> dict[str, Any]:
    """Send raw text to the current interactive shell or menu."""
    try:
        session = registry.get(session_id)
        result = session.send_text(
            text,
            enter=enter,
            wait_for=wait_for,
            timeout=timeout,
            sensitive=sensitive,
            actor=actor,
            lock_ttl=lock_ttl,
            force=force,
        )
        return {"ok": True, **result.as_dict(), "transcript_path": str(session.transcript.path)}
    except SessionError as exc:
        LOGGER.warning("send_text failed: %s", exc)
        return _session_error_response(exc, session if "session" in locals() else None)
    except Exception as exc:
        LOGGER.exception("send_text failed")
        return _session_error_response(exc, session if "session" in locals() else None)


@mcp.tool()
def execute_command(
    session_id: str,
    command: str,
    wait_for: str = "",
    wait_for_prompt: bool = True,
    timeout: float = 30.0,
    actor: str = "agent",
    lock_ttl: float = DEFAULT_INPUT_LOCK_TTL,
    force: bool = False,
) -> dict[str, Any]:
    """Run a shell command inside the current session state."""
    try:
        session = registry.get(session_id)
        result = session.execute_command(
            command,
            wait_for=wait_for,
            wait_for_prompt=wait_for_prompt,
            timeout=timeout,
            actor=actor,
            lock_ttl=lock_ttl,
            force=force,
        )
        return {"ok": True, **result.as_dict(), "transcript_path": str(session.transcript.path)}
    except SessionError as exc:
        LOGGER.warning("execute_command failed: %s", exc)
        return _session_error_response(exc, session if "session" in locals() else None)
    except Exception as exc:
        LOGGER.exception("execute_command failed")
        return _session_error_response(exc, session if "session" in locals() else None)


@mcp.tool()
def get_command(session_id: str, command_id: str, output_limit: int | None = None) -> dict[str, Any]:
    """Return status and collected output for a tracked command."""
    try:
        session = registry.get(session_id)
        return {
            "ok": True,
            "command": session.get_command(command_id, output_limit=output_limit),
            "transcript_path": str(session.transcript.path),
        }
    except SessionError as exc:
        LOGGER.warning("get_command failed: %s", exc)
        return _session_error_response(exc, session if "session" in locals() else None)
    except Exception as exc:
        LOGGER.exception("get_command failed")
        return _session_error_response(exc, session if "session" in locals() else None)


@mcp.tool()
def list_commands(session_id: str, output_limit: int = 0) -> dict[str, Any]:
    """List tracked command history for a session."""
    try:
        session = registry.get(session_id)
        return {
            "ok": True,
            "commands": session.list_commands(output_limit=output_limit),
            "transcript_path": str(session.transcript.path),
        }
    except SessionError as exc:
        LOGGER.warning("list_commands failed: %s", exc)
        return _session_error_response(exc, session if "session" in locals() else None)
    except Exception as exc:
        LOGGER.exception("list_commands failed")
        return _session_error_response(exc, session if "session" in locals() else None)


@mcp.tool()
def cancel_command(
    session_id: str,
    command_id: str,
    actor: str = "agent",
    lock_ttl: float = DEFAULT_INPUT_LOCK_TTL,
    force: bool = False,
) -> dict[str, Any]:
    """Send Ctrl+C for a running tracked command."""
    try:
        session = registry.get(session_id)
        result = session.cancel_command(command_id, actor=actor, lock_ttl=lock_ttl, force=force)
        return {"ok": True, **result.as_dict(), "transcript_path": str(session.transcript.path)}
    except SessionError as exc:
        LOGGER.warning("cancel_command failed: %s", exc)
        return _session_error_response(exc, session if "session" in locals() else None)
    except Exception as exc:
        LOGGER.exception("cancel_command failed")
        return _session_error_response(exc, session if "session" in locals() else None)


@mcp.tool()
def get_screen(session_id: str, lines: int = 100) -> dict[str, Any]:
    """Return the latest terminal output kept in memory."""
    try:
        session = registry.get(session_id)
        return {"ok": True, "screen": session.screen(lines), "session": session.info()}
    except Exception as exc:
        LOGGER.exception("get_screen failed")
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def interrupt(
    session_id: str,
    actor: str = "agent",
    lock_ttl: float = DEFAULT_INPUT_LOCK_TTL,
    force: bool = False,
) -> dict[str, Any]:
    """Send Ctrl+C to the session."""
    try:
        session = registry.get(session_id)
        result = session.interrupt(actor=actor, lock_ttl=lock_ttl, force=force)
        return {"ok": True, **result.as_dict(), "transcript_path": str(session.transcript.path)}
    except SessionError as exc:
        LOGGER.warning("interrupt failed: %s", exc)
        return _session_error_response(exc, session if "session" in locals() else None)
    except Exception as exc:
        LOGGER.exception("interrupt failed")
        return _session_error_response(exc, session if "session" in locals() else None)


@mcp.tool()
def input_lock_status(session_id: str) -> dict[str, Any]:
    """Return the current input lock state for a session."""
    try:
        session = registry.get(session_id)
        return {"ok": True, "input_lock": session.input_lock_info(), "session": session.info()}
    except Exception as exc:
        LOGGER.exception("input_lock_status failed")
        return _session_error_response(exc, session if "session" in locals() else None)


@mcp.tool()
def acquire_input_lock(
    session_id: str,
    actor: str = "agent",
    ttl: float = DEFAULT_INPUT_LOCK_TTL,
    force: bool = False,
) -> dict[str, Any]:
    """Acquire or refresh the input lock before sending terminal input."""
    try:
        session = registry.get(session_id)
        result = session.acquire_input_lock(actor=actor, ttl=ttl, force=force)
        return {"ok": True, **result, "session": session.info(), "transcript_path": str(session.transcript.path)}
    except SessionError as exc:
        LOGGER.warning("acquire_input_lock failed: %s", exc)
        return _session_error_response(exc, session if "session" in locals() else None)
    except Exception as exc:
        LOGGER.exception("acquire_input_lock failed")
        return _session_error_response(exc, session if "session" in locals() else None)


@mcp.tool()
def release_input_lock(
    session_id: str,
    actor: str = "agent",
    force: bool = False,
) -> dict[str, Any]:
    """Release the input lock for the owning actor, or force release it."""
    try:
        session = registry.get(session_id)
        result = session.release_input_lock(actor=actor, force=force)
        return {"ok": True, **result, "session": session.info(), "transcript_path": str(session.transcript.path)}
    except SessionError as exc:
        LOGGER.warning("release_input_lock failed: %s", exc)
        return _session_error_response(exc, session if "session" in locals() else None)
    except Exception as exc:
        LOGGER.exception("release_input_lock failed")
        return _session_error_response(exc, session if "session" in locals() else None)


@mcp.tool()
def get_transcript(session_id: str, tail: int = 200) -> dict[str, Any]:
    """Return the tail of the JSONL transcript for a session."""
    try:
        session = registry.get(session_id)
        return {
            "ok": True,
            "transcript_path": str(session.transcript.path),
            "events": session.transcript.tail(tail),
        }
    except Exception as exc:
        LOGGER.exception("get_transcript failed")
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def search_logs(
    session_id: str,
    pattern: str,
    path: str = ".",
    include: str = "*.log",
    context: int = 0,
    ignore_case: bool = True,
    max_count: int = 200,
    timeout: float = 30.0,
    actor: str = "tool",
    lock_ttl: float = DEFAULT_INPUT_LOCK_TTL,
    force: bool = False,
) -> dict[str, Any]:
    """Search log files from the current remote shell state."""
    command = build_log_search_command(
        pattern,
        path=path,
        include=include,
        context=context,
        ignore_case=ignore_case,
        max_count=max_count,
    )
    try:
        session = registry.get(session_id)
        result = session.execute_command(
            command,
            timeout=timeout,
            policy_tool="search_logs",
            actor=actor,
            lock_ttl=lock_ttl,
            force=force,
        )
        return {
            "ok": True,
            "command": command,
            **result.as_dict(),
            "transcript_path": str(session.transcript.path),
        }
    except SessionError as exc:
        LOGGER.warning("search_logs failed: %s", exc)
        return {**_session_error_response(exc, session if "session" in locals() else None), "command": command}
    except Exception as exc:
        LOGGER.exception("search_logs failed")
        return {**_session_error_response(exc, session if "session" in locals() else None), "command": command}


def _session_error_response(exc: Exception, session: SshSession | None) -> dict[str, Any]:
    if not session:
        return {"ok": False, "error": str(exc)}
    return {"ok": False, **session.error_info(str(exc))}


def main(argv: list[str] | None = None) -> None:
    global registry

    args = _parse_args(argv)
    # 每个 MCP Server 进程独立生成 runtime，避免多个 LLM 同时启动时混写日志和 transcript。
    runtime = build_runtime(config_path=get_config_path())
    registry = SessionRegistry(runtime)
    log_path = configure_logging(
        runtime.log_path,
        context={
            "server_instance_id": runtime.server_instance_id,
            "pid": runtime.as_dict().get("pid"),
            "client_label": runtime.client_label or "-",
        },
    )
    LOGGER.info("Starting ssh-mcp, log_path=%s", log_path)
    viewer = start_viewer_server(registry, host=args.viewer_host, port=args.viewer_port)
    LOGGER.info("Viewer URL: %s", viewer.base_url)
    runtime.write_meta(viewer_base_url=viewer.base_url)
    atexit.register(viewer.shutdown)
    atexit.register(registry.close_all)
    mcp.run()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_host, default_port = viewer_defaults_from_env()
    parser = argparse.ArgumentParser(description="Run the SSH MCP server.")
    parser.add_argument("--viewer-host", default=default_host, help="Viewer bind host. Defaults to 127.0.0.1.")
    parser.add_argument(
        "--viewer-port",
        default=default_port,
        help="Viewer port or 'auto'. Defaults to SSH_MCP_VIEWER_PORT or auto.",
    )
    args, unknown = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    if unknown:
        LOGGER.debug("Ignoring unknown ssh-mcp args: %s", unknown)
    return args


if __name__ == "__main__":
    main()
