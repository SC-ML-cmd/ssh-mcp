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

from .session import SessionRegistry
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

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            LOGGER.debug("viewer %s - %s", self.address_string(), format % args)

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
      grid-template-rows: auto 1fr;
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
    @media (max-width: 760px) {{
      header {{ grid-template-columns: 1fr auto; }}
      header > a {{ display: none; }}
      .meta {{ display: none; }}
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
  <script>
    const SESSION_ID = {encoded};
    const terminal = document.getElementById("terminal");
    const title = document.getElementById("title");
    const meta = document.getElementById("meta");
    const statusNode = document.getElementById("status");
    let afterLine = 0;
    let polling = false;

    function shouldStick() {{
      return terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 48;
    }}

    function updateSession(session) {{
      if (!session) return;
      title.textContent = session.owner_label || session.session_id;
      meta.textContent = [session.session_id, session.profile, session.last_activity_at || session.updated_at].filter(Boolean).join("  ");
      statusNode.textContent = session.status || "history";
      statusNode.className = "status " + (session.status || "history");
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
