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
from .session import SessionRegistry, build_log_search_command
from .viewer import start_viewer_server, viewer_defaults_from_env


LOGGER = logging.getLogger(__name__)
mcp = FastMCP("ssh-mcp")
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
def send_text(
    session_id: str,
    text: str,
    enter: bool = True,
    wait_for: str = "",
    timeout: float = 30.0,
    sensitive: bool = False,
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
        )
        return {"ok": True, **result.as_dict(), "transcript_path": str(session.transcript.path)}
    except Exception as exc:
        LOGGER.exception("send_text failed")
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def execute_command(
    session_id: str,
    command: str,
    wait_for: str = "",
    wait_for_prompt: bool = True,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Run a shell command inside the current session state."""
    try:
        session = registry.get(session_id)
        result = session.execute_command(
            command,
            wait_for=wait_for,
            wait_for_prompt=wait_for_prompt,
            timeout=timeout,
        )
        return {"ok": True, **result.as_dict(), "transcript_path": str(session.transcript.path)}
    except Exception as exc:
        LOGGER.exception("execute_command failed")
        return {"ok": False, "error": str(exc)}


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
def interrupt(session_id: str) -> dict[str, Any]:
    """Send Ctrl+C to the session."""
    try:
        session = registry.get(session_id)
        result = session.interrupt()
        return {"ok": True, **result.as_dict(), "transcript_path": str(session.transcript.path)}
    except Exception as exc:
        LOGGER.exception("interrupt failed")
        return {"ok": False, "error": str(exc)}


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
        result = session.execute_command(command, timeout=timeout)
        return {
            "ok": True,
            "command": command,
            **result.as_dict(),
            "transcript_path": str(session.transcript.path),
        }
    except Exception as exc:
        LOGGER.exception("search_logs failed")
        return {"ok": False, "error": str(exc), "command": command}


def main(argv: list[str] | None = None) -> None:
    global registry

    args = _parse_args(argv)
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
