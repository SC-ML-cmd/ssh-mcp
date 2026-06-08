from __future__ import annotations

import argparse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .session import DEFAULT_INPUT_LOCK_TTL, SessionError, SessionRegistry
from .transcript import DEFAULT_TRANSCRIPTS_DIR, get_transcripts_dir, list_transcript_summaries, read_events, render_terminal_delta


LOGGER = logging.getLogger(__name__)
DEFAULT_VIEWER_HOST = "127.0.0.1"
DEFAULT_VIEWER_PORT = 8765
CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)


@dataclass(frozen=True)
class ViewerServer:
    host: str
    port: int
    base_url: str
    transcripts_dir: Path
    httpd: ThreadingHTTPServer
    thread: threading.Thread

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class ViewerState:
    """合并当前实例、活动 session 和旧 transcript，供只读 viewer 展示。"""

    def __init__(self, registry: SessionRegistry, transcripts_dir: str | Path | None = None) -> None:
        self.registry = registry
        self.transcripts_dir = get_transcripts_dir(transcripts_dir or registry.runtime.transcripts_dir)
        self.legacy_transcripts_dir = DEFAULT_TRANSCRIPTS_DIR

    def sessions(self) -> list[dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}

        # 当前实例目录是主数据源；旧 transcripts/ 只作为历史兼容入口。
        for session in list_transcript_summaries(self.transcripts_dir):
            session.setdefault("storage_scope", "instance")
            by_id[session["session_id"]] = session

        if self.legacy_transcripts_dir.resolve() != self.transcripts_dir.resolve():
            for session in list_transcript_summaries(self.legacy_transcripts_dir):
                session.setdefault("storage_scope", "legacy")
                session.setdefault("server_instance_id", "legacy")
                by_id.setdefault(session["session_id"], session)

        active_sessions = self.registry.list()
        active_ids = {active["session_id"] for active in active_sessions}

        for active in active_sessions:
            # 活动 session 的内存状态比历史 JSONL 摘要更新，优先覆盖。
            session_id = active["session_id"]
            current = by_id.get(session_id, {})
            current.update(active)
            current["closed"] = active.get("closed", False)
            current["storage_scope"] = "active"
            by_id[session_id] = current

        sessions = list(by_id.values())
        for session in sessions:
            session["viewer_url"] = session.get("viewer_url") or self.registry.session_url(session["session_id"])
            if session.get("health_status") == "unhealthy":
                session["status"] = "unhealthy"
            elif session.get("closed"):
                session["status"] = "closed"
            elif session["session_id"] in active_ids:
                session["status"] = "open"
            else:
                session["status"] = "history"

        sessions.sort(key=lambda item: item.get("last_activity_at") or item.get("updated_at") or "", reverse=True)
        return sessions

    def session(self, session_id: str) -> dict[str, Any] | None:
        for session in self.sessions():
            if session.get("session_id") == session_id:
                return session
        return None

    def transcript_path(self, session_id: str) -> Path:
        if not _is_safe_session_id(session_id):
            raise ValueError("Invalid session id.")
        for active in self.registry.list():
            if active["session_id"] == session_id:
                return Path(active["transcript_path"])
        instance_path = self.transcripts_dir / f"{session_id}.jsonl"
        if instance_path.exists():
            return instance_path
        legacy_path = self.legacy_transcripts_dir / f"{session_id}.jsonl"
        if legacy_path.exists():
            return legacy_path
        return instance_path


def start_viewer_server(
    registry: SessionRegistry,
    *,
    host: str = DEFAULT_VIEWER_HOST,
    port: str | int = "auto",
    transcripts_dir: str | Path | None = None,
) -> ViewerServer:
    state = ViewerState(registry, transcripts_dir)
    selected_port = _bindable_port(host, port)
    handler = _make_handler(state)
    httpd = ThreadingHTTPServer((host, selected_port), handler)
    thread = threading.Thread(target=httpd.serve_forever, name="ssh-mcp-viewer", daemon=True)
    thread.start()

    base_url = f"http://{host}:{selected_port}"
    registry.set_viewer_base_url(base_url)
    server = ViewerServer(
        host=host,
        port=selected_port,
        base_url=base_url,
        transcripts_dir=state.transcripts_dir,
        httpd=httpd,
        thread=thread,
    )
    LOGGER.info("Started SSH MCP viewer at %s", base_url)
    return server


def _make_handler(state: ViewerState) -> type[BaseHTTPRequestHandler]:
    class ViewerRequestHandler(BaseHTTPRequestHandler):
        server_version = "SshMcpViewer/0.1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path == "/":
                    self._send_html(_index_html())
                elif path.startswith("/sessions/"):
                    session_id = unquote(path.removeprefix("/sessions/"))
                    if not _is_safe_session_id(session_id):
                        self._send_error(HTTPStatus.BAD_REQUEST, "Invalid session id.")
                        return
                    self._send_html(_session_html(session_id))
                elif path == "/api/sessions":
                    self._send_json({"ok": True, "server": state.registry.server_info(), "sessions": state.sessions()})
                elif path.startswith("/api/sessions/") and path.endswith("/events"):
                    session_id = unquote(path.removeprefix("/api/sessions/").removesuffix("/events"))
                    self._handle_events(session_id, parsed.query)
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "Not found.")
            except CLIENT_DISCONNECT_ERRORS:
                LOGGER.debug("Viewer client disconnected before response completed")
            except Exception as exc:  # pragma: no cover - protects the viewer loop
                LOGGER.exception("Viewer request failed")
                try:
                    self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                except CLIENT_DISCONNECT_ERRORS:
                    LOGGER.debug("Viewer client disconnected before error response completed")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            parts = parsed.path.strip("/").split("/")
            try:
                if len(parts) != 4 or parts[0] != "api" or parts[1] != "sessions":
                    self._send_error(HTTPStatus.NOT_FOUND, "Not found.")
                    return

                session_id = unquote(parts[2])
                action = parts[3]
                if not _is_safe_session_id(session_id):
                    self._send_error(HTTPStatus.BAD_REQUEST, "Invalid session id.")
                    return
                try:
                    body = self._read_json_body()
                except ValueError as exc:
                    self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
                    return

                if action == "input":
                    self._handle_input(session_id, body)
                elif action == "lock":
                    self._handle_lock(session_id, body, acquire=True)
                elif action == "unlock":
                    self._handle_lock(session_id, body, acquire=False)
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "Not found.")
            except CLIENT_DISCONNECT_ERRORS:
                LOGGER.debug("Viewer client disconnected before response completed")
            except Exception as exc:  # pragma: no cover - protects the viewer loop
                LOGGER.exception("Viewer POST failed")
                try:
                    self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                except CLIENT_DISCONNECT_ERRORS:
                    LOGGER.debug("Viewer client disconnected before error response completed")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            LOGGER.debug("viewer %s - %s", self.address_string(), format % args)

        def _read_json_body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError as exc:
                raise ValueError("Invalid Content-Length.") from exc
            if length <= 0:
                return {}
            if length > 65_536:
                raise ValueError("Request body is too large.")
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("Request body must be JSON.") from exc
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object.")
            return body

        def _active_session(self, session_id: str):
            try:
                return state.registry.get(session_id)
            except SessionError as exc:
                self._send_error(HTTPStatus.NOT_FOUND, f"Session is not active: {exc}")
                return None

        def _handle_input(self, session_id: str, body: dict[str, Any]) -> None:
            session = self._active_session(session_id)
            if not session:
                return
            actor = str(body.get("actor") or "human")
            try:
                result = session.send_text(
                    str(body.get("text") or ""),
                    enter=_bool_value(body.get("enter"), True),
                    enter_sequence=str(body.get("enter_sequence") or "") or None,
                    timeout=_float_value(body.get("timeout"), 0.2),
                    sensitive=_bool_value(body.get("sensitive"), False),
                    actor=actor,
                    lock_ttl=_float_value(body.get("lock_ttl"), DEFAULT_INPUT_LOCK_TTL),
                    force=_bool_value(body.get("force"), False),
                )
            except SessionError as exc:
                self._send_json({"ok": False, **session.error_info(str(exc))}, status=HTTPStatus.CONFLICT)
                return
            self._send_json(
                {
                    "ok": True,
                    **result.as_dict(),
                    "session": session.info(),
                    "input_lock": session.input_lock_info(),
                    "transcript_path": str(session.transcript.path),
                }
            )

        def _handle_lock(self, session_id: str, body: dict[str, Any], *, acquire: bool) -> None:
            session = self._active_session(session_id)
            if not session:
                return
            actor = str(body.get("actor") or "human")
            try:
                if acquire:
                    result = session.acquire_input_lock(
                        actor=actor,
                        ttl=_float_value(body.get("ttl"), DEFAULT_INPUT_LOCK_TTL),
                        force=_bool_value(body.get("force"), False),
                    )
                else:
                    result = session.release_input_lock(
                        actor=actor,
                        force=_bool_value(body.get("force"), False),
                    )
            except SessionError as exc:
                self._send_json({"ok": False, **session.error_info(str(exc))}, status=HTTPStatus.CONFLICT)
                return
            self._send_json(
                {
                    "ok": True,
                    **result,
                    "session": session.info(),
                    "transcript_path": str(session.transcript.path),
                }
            )

        def _handle_events(self, session_id: str, query: str) -> None:
            if not _is_safe_session_id(session_id):
                self._send_error(HTTPStatus.BAD_REQUEST, "Invalid session id.")
                return

            params = parse_qs(query)
            after_line = _int_param(params, "after_line", 0)
            limit = min(_int_param(params, "limit", 1000), 5000)
            wait_ms = min(_int_param(params, "wait_ms", 2500), 10_000)
            path = state.transcript_path(session_id)
            deadline = time.monotonic() + (wait_ms / 1000)
            events: list[dict[str, Any]] = []
            last_line = after_line

            # 用长轮询降低刷新噪音；后续如果需要浏览器输入，可在这里升级 WebSocket。
            while True:
                events, last_line = read_events(path, after_line=after_line, limit=limit)
                if events or time.monotonic() >= deadline:
                    break
                time.sleep(0.2)

            self._send_json(
                {
                    "ok": True,
                    "session": state.session(session_id),
                    "events": events,
                    "after_line": after_line,
                    "last_line": last_line,
                    "terminal_delta": render_terminal_delta(events),
                }
            )

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            self._send_json({"ok": False, "error": message}, status=status)

    return ViewerRequestHandler


def _index_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SSH MCP Sessions</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0d1117;
      --panel: #151b23;
      --line: #2d333b;
      --text: #e6edf3;
      --muted: #8b949e;
      --accent: #2f81f7;
      --ok: #3fb950;
      --closed: #f85149;
      --warn: #d29922;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: #10161d;
    }
    h1 { margin: 0; font-size: 18px; font-weight: 650; letter-spacing: 0; }
    main { max-width: 1180px; margin: 0 auto; padding: 24px; }
    .meta { color: var(--muted); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
    .sessions { display: grid; gap: 10px; }
    .group { display: grid; gap: 10px; margin-bottom: 22px; }
    .group-title { color: var(--muted); font: 12px/1.4 ui-monospace, SFMono-Regular, Consolas, monospace; }
    .session {
      display: grid;
      grid-template-columns: minmax(220px, 1.4fr) minmax(160px, 1fr) 120px 170px;
      gap: 12px;
      align-items: center;
      padding: 14px 16px;
      color: inherit;
      text-decoration: none;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .session:hover { border-color: var(--accent); }
    .title { min-width: 0; }
    .owner { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 650; }
    .id { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; }
    .status { width: max-content; padding: 2px 8px; border-radius: 999px; color: #fff; background: var(--muted); font-size: 12px; }
    .status.open { background: var(--ok); color: #0d1117; }
    .status.unhealthy { background: var(--warn); color: #0d1117; }
    .status.closed { background: var(--closed); }
    .empty { padding: 36px; border: 1px dashed var(--line); border-radius: 8px; color: var(--muted); text-align: center; }
    @media (max-width: 760px) {
      header { align-items: flex-start; flex-direction: column; }
      main { padding: 16px; }
      .session { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>SSH MCP Sessions</h1>
    <span id="server" class="meta"></span>
  </header>
  <main>
    <div id="sessions" class="sessions"></div>
  </main>
  <script>
    const sessionsNode = document.getElementById("sessions");
    const serverNode = document.getElementById("server");

    function text(value, fallback = "") {
      return value === undefined || value === null || value === "" ? fallback : String(value);
    }

    function render(payload) {
      serverNode.textContent = text(payload.server && payload.server.server_instance_id);
      sessionsNode.replaceChildren();
      if (!payload.sessions || payload.sessions.length === 0) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No sessions";
        sessionsNode.appendChild(empty);
        return;
      }
      const groups = new Map();
      for (const session of payload.sessions) {
        const groupKey = session.server_instance_id || session.storage_scope || "unknown";
        if (!groups.has(groupKey)) groups.set(groupKey, []);
        groups.get(groupKey).push(session);
      }
      for (const [groupKey, sessions] of groups) {
        const group = document.createElement("section");
        group.className = "group";
        const groupTitle = document.createElement("div");
        groupTitle.className = "group-title";
        groupTitle.textContent = groupKey;
        group.appendChild(groupTitle);
        for (const session of sessions) {
        const link = document.createElement("a");
        link.className = "session";
        link.href = "/sessions/" + encodeURIComponent(session.session_id);

        const title = document.createElement("span");
        title.className = "title";
        const owner = document.createElement("span");
        owner.className = "owner";
        owner.textContent = text(session.owner_label, session.profile || session.session_id);
        const id = document.createElement("span");
        id.className = "id";
        id.textContent = session.session_id;
        title.append(owner, id);

        const target = document.createElement("span");
        target.textContent = [session.username, session.host].filter(Boolean).join("@") || text(session.profile);

        const status = document.createElement("span");
        status.className = "status " + text(session.status);
        status.textContent = text(session.status, "history");

        const activity = document.createElement("span");
        activity.className = "meta";
        activity.textContent = text(session.last_activity_at || session.updated_at);

          link.append(title, target, status, activity);
          group.appendChild(link);
        }
        sessionsNode.appendChild(group);
      }
    }

    async function refresh() {
      const response = await fetch("/api/sessions", { cache: "no-store" });
      render(await response.json());
    }

    refresh();
    setInterval(refresh, 1500);
  </script>
</body>
</html>"""


def _session_html(session_id: str) -> str:
    encoded = json.dumps(session_id)
    title = json.dumps(f"SSH MCP Session {session_id}")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{json.loads(title)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #05070a;
      --panel: #0d1117;
      --line: #2d333b;
      --text: #e6edf3;
      --muted: #8b949e;
      --accent: #2f81f7;
      --ok: #3fb950;
      --closed: #f85149;
      --warn: #d29922;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      height: 100vh;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      overflow: hidden;
    }}
    header {{
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 14px;
      align-items: center;
      padding: 12px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    .title {{ min-width: 0; }}
    h1 {{ margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 15px; font-weight: 650; letter-spacing: 0; }}
    .meta {{ color: var(--muted); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; }}
    .status {{ width: max-content; padding: 2px 8px; border-radius: 999px; color: #fff; background: var(--muted); font-size: 12px; }}
    .status.open {{ background: var(--ok); color: #05070a; }}
    .status.unhealthy {{ background: var(--warn); color: #05070a; }}
    .status.closed {{ background: var(--closed); }}
    #terminal {{
      margin: 0;
      width: 100%;
      height: 100%;
      padding: 16px;
      overflow: auto;
      background: #05070a;
      color: #d1f1d7;
      font: 13px/1.45 ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      white-space: pre-wrap;
      word-break: break-word;
      tab-size: 4;
    }}
    footer {{
      display: grid;
      grid-template-columns: minmax(110px, 160px) auto minmax(150px, 220px) auto minmax(220px, 1fr) auto minmax(140px, 1fr);
      gap: 8px;
      align-items: center;
      padding: 10px 12px;
      border-top: 1px solid var(--line);
      background: var(--panel);
    }}
    input, textarea, button, label {{
      font: inherit;
    }}
    input, textarea {{
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #05070a;
      color: var(--text);
      outline: none;
    }}
    input:focus, textarea:focus {{
      border-color: var(--accent);
    }}
    #actor {{
      height: 34px;
      padding: 0 9px;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    }}
    #input {{
      width: 100%;
      height: 36px;
      max-height: 120px;
      resize: vertical;
      padding: 7px 9px;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    }}
    button {{
      height: 34px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #151b23;
      color: var(--text);
      cursor: pointer;
    }}
    button:hover:not(:disabled) {{
      border-color: var(--accent);
    }}
    button:disabled, textarea:disabled, input:disabled {{
      cursor: not-allowed;
      opacity: 0.55;
    }}
    .send-controls {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      white-space: nowrap;
    }}
    .enter-sequence {{
      display: inline-grid;
      grid-template-columns: repeat(3, minmax(38px, auto));
      height: 34px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #05070a;
    }}
    .enter-sequence label {{
      min-width: 38px;
      height: 32px;
      color: var(--muted);
      cursor: pointer;
      font: 12px/1 ui-monospace, SFMono-Regular, Consolas, monospace;
    }}
    .enter-sequence label:not(:last-child) {{
      border-right: 1px solid var(--line);
    }}
    .enter-sequence input {{
      position: absolute;
      opacity: 0;
      pointer-events: none;
    }}
    .enter-sequence span {{
      display: grid;
      place-items: center;
      width: 100%;
      height: 100%;
      padding: 0 8px;
    }}
    .enter-sequence input:checked + span {{
      background: var(--accent);
      color: #fff;
    }}
    .toggle {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      white-space: nowrap;
      font-size: 12px;
    }}
    #lockStatus {{
      min-width: 170px;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    #message {{
      min-height: 18px;
      color: var(--muted);
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    @media (max-width: 760px) {{
      header {{ grid-template-columns: 1fr auto; }}
      header > a {{ display: none; }}
      .meta {{ display: none; }}
      footer {{ grid-template-columns: 1fr auto auto; }}
      #lockStatus, #message {{ grid-column: 1 / -1; }}
      #input {{ grid-column: 1 / -1; }}
    }}
  </style>
</head>
<body>
  <header>
    <a href="/">Sessions</a>
    <span class="title">
      <h1 id="title">{session_id}</h1>
      <span id="meta" class="meta"></span>
    </span>
    <span id="status" class="status">history</span>
  </header>
  <pre id="terminal"></pre>
  <footer>
    <input id="actor" value="human" aria-label="Actor">
    <label class="toggle"><input id="observer" type="checkbox">Observer</label>
    <span id="lockStatus">unlocked</span>
    <span>
      <button id="takeLock" title="Acquire input lock">Take</button>
      <button id="forceLock" title="Force takeover">Force</button>
      <button id="releaseLock" title="Release input lock">Release</button>
    </span>
    <textarea id="input" spellcheck="false" aria-label="Terminal input"></textarea>
    <span class="send-controls">
      <label class="toggle"><input id="enter" type="checkbox" checked>Enter</label>
      <span id="enterSequence" class="enter-sequence" role="radiogroup" aria-label="Enter sequence">
        <label title="Send line feed"><input type="radio" name="enterSequence" value="lf" checked><span>LF</span></label>
        <label title="Send carriage return"><input type="radio" name="enterSequence" value="cr"><span>CR</span></label>
        <label title="Send carriage return and line feed"><input type="radio" name="enterSequence" value="crlf"><span>CRLF</span></label>
      </span>
      <button id="send" title="Send input">Send</button>
    </span>
    <span id="message"></span>
  </footer>
  <script>
    const SESSION_ID = {encoded};
    const terminal = document.getElementById("terminal");
    const title = document.getElementById("title");
    const meta = document.getElementById("meta");
    const statusNode = document.getElementById("status");
    const actorInput = document.getElementById("actor");
    const observerInput = document.getElementById("observer");
    const lockStatus = document.getElementById("lockStatus");
    const takeLockButton = document.getElementById("takeLock");
    const forceLockButton = document.getElementById("forceLock");
    const releaseLockButton = document.getElementById("releaseLock");
    const inputNode = document.getElementById("input");
    const enterInput = document.getElementById("enter");
    const enterSequenceInputs = Array.from(document.querySelectorAll("input[name='enterSequence']"));
    const sendButton = document.getElementById("send");
    const messageNode = document.getElementById("message");
    let afterLine = 0;
    let polling = false;
    let currentSession = null;

    function shouldStick() {{
      return terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 48;
    }}

    function actor() {{
      return actorInput.value.trim() || "human";
    }}

    function setMessage(text, error = false) {{
      messageNode.textContent = text || "";
      messageNode.style.color = error ? "var(--closed)" : "var(--muted)";
    }}

    function selectedEnterSequence() {{
      const selected = enterSequenceInputs.find(input => input.checked);
      return selected ? selected.value : "lf";
    }}

    function setEnterSequence(value) {{
      const normalized = ["lf", "cr", "crlf"].includes(value) ? value : "lf";
      enterSequenceInputs.forEach(input => {{
        input.checked = input.value === normalized;
      }});
    }}

    function updateControls(session) {{
      const isOpen = session && session.status === "open" && !session.closed;
      const observing = observerInput.checked;
      const lock = session && session.input_lock ? session.input_lock : null;
      if (lock && lock.locked) {{
        const ttl = Math.max(Math.ceil(lock.ttl_remaining_seconds || 0), 0);
        lockStatus.textContent = `locked:${{lock.actor || "unknown"}} ${{ttl}}s`;
      }} else {{
        lockStatus.textContent = "unlocked";
      }}
      inputNode.disabled = observing || !isOpen;
      sendButton.disabled = observing || !isOpen;
      enterInput.disabled = observing || !isOpen;
      enterSequenceInputs.forEach(input => {{
        input.disabled = observing || !isOpen;
      }});
      takeLockButton.disabled = observing || !isOpen;
      forceLockButton.disabled = observing || !isOpen;
      releaseLockButton.disabled = observing || !isOpen;
    }}

    function updateSession(session) {{
      if (!session) return;
      currentSession = session;
      title.textContent = session.owner_label || session.session_id;
      meta.textContent = [session.session_id, session.profile, session.last_activity_at || session.updated_at].filter(Boolean).join("  ");
      statusNode.textContent = session.status || "history";
      statusNode.className = "status " + (session.status || "history");
      if (session.enter_sequence && !window.__enterSequenceTouched) {{
        setEnterSequence(session.enter_sequence);
      }}
      updateControls(session);
    }}

    async function postJSON(path, body) {{
      const response = await fetch(path, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(body),
        cache: "no-store"
      }});
      const payload = await response.json();
      if (!payload.ok) {{
        throw new Error(payload.error || "request failed");
      }}
      updateSession(payload.session);
      return payload;
    }}

    async function lockAction(action, force = false) {{
      setMessage("");
      try {{
        const path = `/api/sessions/${{encodeURIComponent(SESSION_ID)}}/${{action}}`;
        await postJSON(path, {{ actor: actor(), ttl: 60, force }});
      }} catch (error) {{
        setMessage(String(error.message || error), true);
      }}
    }}

    async function sendInput() {{
      if (observerInput.checked || !currentSession || currentSession.status !== "open") return;
      const text = inputNode.value;
      if (!text && !enterInput.checked) return;
      setMessage("");
      try {{
        await postJSON(`/api/sessions/${{encodeURIComponent(SESSION_ID)}}/input`, {{
          text,
          enter: enterInput.checked,
          enter_sequence: selectedEnterSequence(),
          actor: actor(),
          lock_ttl: 60,
          force: false
        }});
        inputNode.value = "";
        inputNode.focus();
      }} catch (error) {{
        setMessage(String(error.message || error), true);
      }}
    }}

    async function poll() {{
      if (polling) return;
      polling = true;
      try {{
        const response = await fetch(`/api/sessions/${{encodeURIComponent(SESSION_ID)}}/events?after_line=${{afterLine}}&wait_ms=5000`, {{ cache: "no-store" }});
        const payload = await response.json();
        if (payload.ok) {{
          const stick = shouldStick();
          afterLine = payload.last_line || afterLine;
          if (payload.terminal_delta) {{
            terminal.textContent += payload.terminal_delta;
          }}
          updateSession(payload.session);
          if (stick) terminal.scrollTop = terminal.scrollHeight;
        }} else {{
          terminal.textContent += "\\n[viewer] " + payload.error + "\\n";
        }}
      }} catch (error) {{
        terminal.textContent += "\\n[viewer] " + error + "\\n";
        await new Promise(resolve => setTimeout(resolve, 1000));
      }} finally {{
        polling = false;
        setTimeout(poll, 100);
      }}
    }}

    observerInput.addEventListener("change", () => updateControls(currentSession));
    enterSequenceInputs.forEach(input => {{
      input.addEventListener("change", () => {{
        window.__enterSequenceTouched = true;
      }});
    }});
    takeLockButton.addEventListener("click", () => lockAction("lock", false));
    forceLockButton.addEventListener("click", () => lockAction("lock", true));
    releaseLockButton.addEventListener("click", () => lockAction("unlock", false));
    sendButton.addEventListener("click", sendInput);
    inputNode.addEventListener("keydown", event => {{
      if (event.key === "Enter" && !event.shiftKey) {{
        event.preventDefault();
        sendInput();
      }}
    }});
    updateControls(null);
    poll();
  </script>
</body>
</html>"""


def _bindable_port(host: str, port: str | int) -> int:
    if str(port).lower() == "auto":
        start = DEFAULT_VIEWER_PORT
    else:
        start = int(port)

    for candidate in [start, *range(start + 1, start + 100)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, candidate))
            except OSError:
                continue
            return candidate
    raise OSError(f"No free viewer port found near {start}.")


def _bool_value(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _float_value(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_param(params: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int(params.get(name, [str(default)])[0])
    except (TypeError, ValueError):
        return default


def _is_safe_session_id(session_id: str) -> bool:
    """限制 session_id 为文件名安全字符，避免通过 URL 读取任意路径。"""

    if not session_id or "/" in session_id or "\\" in session_id:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    return Path(session_id).name == session_id and not session_id.endswith(".jsonl") and all(ch in allowed for ch in session_id)


def viewer_defaults_from_env() -> tuple[str, str]:
    return (
        os.getenv("SSH_MCP_VIEWER_HOST") or DEFAULT_VIEWER_HOST,
        os.getenv("SSH_MCP_VIEWER_PORT") or "auto",
    )


def main(argv: list[str] | None = None) -> None:
    default_host, default_port = viewer_defaults_from_env()
    parser = argparse.ArgumentParser(description="Run a read-only SSH MCP transcript viewer.")
    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", default=default_port)
    parser.add_argument("--transcripts-dir", default=None)
    args = parser.parse_args(argv)

    registry = SessionRegistry()
    viewer = start_viewer_server(
        registry,
        host=args.host,
        port=args.port,
        transcripts_dir=args.transcripts_dir,
    )
    registry.runtime.write_meta(viewer_base_url=viewer.base_url)
    print(viewer.base_url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        viewer.shutdown()


if __name__ == "__main__":
    main()
