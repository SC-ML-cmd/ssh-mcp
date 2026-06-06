from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import unittest
import urllib.request

from ssh_mcp.config import load_profile, load_profiles
from ssh_mcp.log_config import configure_logging
from ssh_mcp.runtime import build_runtime
from ssh_mcp.session import SessionRegistry, SshSession, TerminalBuffer, _key_classes_for_file, build_log_search_command
from ssh_mcp.transcript import TranscriptWriter, list_transcript_summaries, read_events, render_terminal_delta
from ssh_mcp.viewer import start_viewer_server


class ConfigTests(unittest.TestCase):
    def test_load_profiles_from_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "profiles.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "dev": {
                                "host": "127.0.0.1",
                                "username": "alice",
                                "port": 2222,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            profiles = load_profiles(config_path)

        self.assertEqual(profiles["dev"].host, "127.0.0.1")
        self.assertEqual(profiles["dev"].username, "alice")
        self.assertEqual(profiles["dev"].port, 2222)

    def test_resolves_secret_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "profiles.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "dev": {
                                "host": "127.0.0.1",
                                "username": "alice",
                                "password_env": "SSH_MCP_TEST_PASSWORD",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            os.environ["SSH_MCP_TEST_PASSWORD"] = "secret"
            try:
                profile = load_profile("dev", config_path)
                self.assertEqual(profile.resolved_password(), "secret")
            finally:
                os.environ.pop("SSH_MCP_TEST_PASSWORD", None)

    def test_ignores_unexpanded_env_template_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "profiles.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": {
                            "dev": {
                                "host": "127.0.0.1",
                                "username": "alice",
                                "passphrase_env": "SSH_MCP_TEST_TEMPLATE",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            os.environ["SSH_MCP_TEST_TEMPLATE"] = "${SSH_MCP_TEST_TEMPLATE}"
            try:
                profile = load_profile("dev", config_path)
                self.assertIsNone(profile.resolved_passphrase())
            finally:
                os.environ.pop("SSH_MCP_TEST_TEMPLATE", None)


class TranscriptTests(unittest.TestCase):
    def test_records_and_tails_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = TranscriptWriter("session-1", temp_dir)
            writer.record("send", "password\n", tool="send_text", sensitive=True)
            writer.record("recv", "ok\n")
            events = writer.tail(10)

        self.assertEqual(events[0]["text"], "password\n")
        self.assertTrue(events[0]["sensitive"])
        self.assertEqual(events[1]["dir"], "recv")
        self.assertEqual(events[1]["text"], "ok\n")

    def test_reads_incremental_events_and_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = TranscriptWriter("session-1", temp_dir)
            writer.record(
                "session_meta",
                "session metadata",
                extra={"profile": "dev", "owner_label": "codex-test", "viewer_url": "http://127.0.0.1:8765/sessions/session-1"},
            )
            writer.record("recv", "ok\n")
            events, last_line = read_events(writer.path, after_line=1)
            summaries = list_transcript_summaries(temp_dir)

        self.assertEqual(last_line, 2)
        self.assertEqual(events[0]["dir"], "recv")
        self.assertEqual(summaries[0]["session_id"], "session-1")
        self.assertEqual(summaries[0]["owner_label"], "codex-test")

    def test_terminal_renderer_hides_execute_command_marker_noise(self) -> None:
        events = [
            {
                "dir": "send",
                "text": "whoami\nprintf '\\n__SSH_MCP_DONE_abc123__:%s\\n' \"$?\"\n",
                "tool": "execute_command",
            },
            {
                "dir": "recv",
                "text": "whoami\r\nroot\r\n[root@host ~]# printf '\\n__SSH_MCP_DONE_abc123__:%s\\n' \"$?\"\r\n\r\n__SSH_MCP_DONE_abc123__:0\r\n[root@host ~]# ",
            },
        ]

        rendered = render_terminal_delta(events)

        self.assertIn("whoami", rendered)
        self.assertIn("root", rendered)
        self.assertNotIn("__SSH_MCP_DONE", rendered)
        self.assertNotIn("printf", rendered)


class BufferTests(unittest.TestCase):
    def test_text_since_uses_absolute_offsets(self) -> None:
        buffer = TerminalBuffer(max_chars=10)
        offset, _ = buffer.snapshot()
        buffer.append("hello")
        self.assertEqual(buffer.text_since(offset), "hello")
        offset, _ = buffer.snapshot()
        buffer.append(" world")
        self.assertEqual(buffer.text_since(offset), " world")

    def test_trims_old_chunks(self) -> None:
        buffer = TerminalBuffer(max_chars=5)
        buffer.append("abc")
        buffer.append("def")
        self.assertEqual(buffer.last_lines(1), "def")


class SearchCommandTests(unittest.TestCase):
    def test_builds_find_grep_command_with_quoted_pattern(self) -> None:
        command = build_log_search_command("error: nope", path="/tmp/logs", include="*.log", context=2)

        self.assertIn("find /tmp/logs -type f -name '*.log'", command)
        self.assertIn("'error: nope'", command)
        self.assertIn("-C 2", command)


class RuntimeTests(unittest.TestCase):
    def test_runtime_uses_instance_directories_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_runtime = os.environ.get("SSH_MCP_RUNTIME_DIR")
            old_log = os.environ.pop("SSH_MCP_LOG_PATH", None)
            old_transcripts = os.environ.pop("SSH_MCP_TRANSCRIPTS_DIR", None)
            os.environ["SSH_MCP_RUNTIME_DIR"] = temp_dir
            try:
                runtime = build_runtime(server_instance_id="server-1", client_label="codex-test")
            finally:
                _restore_env("SSH_MCP_RUNTIME_DIR", old_runtime)
                _restore_env("SSH_MCP_LOG_PATH", old_log)
                _restore_env("SSH_MCP_TRANSCRIPTS_DIR", old_transcripts)

        self.assertEqual(runtime.instance_dir.name, "server-1")
        self.assertEqual(runtime.log_path, Path(temp_dir) / "instances" / "server-1" / "logs" / "ssh_mcp.log")
        self.assertEqual(runtime.transcripts_dir, Path(temp_dir) / "instances" / "server-1" / "transcripts")
        self.assertEqual(runtime.client_label, "codex-test")

    def test_runtime_respects_explicit_log_and_transcript_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_runtime = os.environ.get("SSH_MCP_RUNTIME_DIR")
            old_log = os.environ.get("SSH_MCP_LOG_PATH")
            old_transcripts = os.environ.get("SSH_MCP_TRANSCRIPTS_DIR")
            os.environ["SSH_MCP_RUNTIME_DIR"] = str(Path(temp_dir) / "runtime")
            os.environ["SSH_MCP_LOG_PATH"] = str(Path(temp_dir) / "custom.log")
            os.environ["SSH_MCP_TRANSCRIPTS_DIR"] = str(Path(temp_dir) / "custom-transcripts")
            try:
                runtime = build_runtime(server_instance_id="server-2")
            finally:
                _restore_env("SSH_MCP_RUNTIME_DIR", old_runtime)
                _restore_env("SSH_MCP_LOG_PATH", old_log)
                _restore_env("SSH_MCP_TRANSCRIPTS_DIR", old_transcripts)

        self.assertTrue(runtime.explicit_log_path)
        self.assertTrue(runtime.explicit_transcripts_dir)
        self.assertEqual(runtime.log_path, Path(temp_dir) / "custom.log")
        self.assertEqual(runtime.transcripts_dir, Path(temp_dir) / "custom-transcripts")

    def test_log_formatter_supplies_default_context_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "ssh_mcp.log"
            configure_logging(log_path, context={"server_instance_id": "server-3", "client_label": "codex-test"})
            logging.getLogger("test").info("hello")

            text = log_path.read_text(encoding="utf-8")
            for handler in list(logging.getLogger().handlers):
                logging.getLogger().removeHandler(handler)
                handler.close()

        self.assertIn("server=server-3", text)
        self.assertIn("client=codex-test", text)
        self.assertIn("session=-", text)


class ViewerTests(unittest.TestCase):
    def test_viewer_serves_sessions_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = TranscriptWriter("session-1", temp_dir)
            writer.record("session_meta", "session metadata", extra={"profile": "dev", "owner_label": "codex-test"})
            writer.record("recv", "hello\n")
            runtime = _build_test_runtime(temp_dir, server_instance_id="viewer-test", client_label="codex-test")
            registry = SessionRegistry(runtime)
            viewer = start_viewer_server(registry, port="auto", transcripts_dir=temp_dir)
            try:
                sessions = _json_get(f"{viewer.base_url}/api/sessions")
                events = _json_get(f"{viewer.base_url}/api/sessions/session-1/events?after_line=0&wait_ms=1")
            finally:
                viewer.shutdown()

        session = next(item for item in sessions["sessions"] if item["session_id"] == "session-1")
        self.assertTrue(sessions["ok"])
        self.assertEqual(session["owner_label"], "codex-test")
        self.assertTrue(events["ok"])
        self.assertIn("hello", events["terminal_delta"])

    def test_viewer_moves_to_next_port_when_requested_port_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = SessionRegistry(_build_test_runtime(temp_dir, server_instance_id="viewer-port-test"))
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
                occupied.bind(("127.0.0.1", 0))
                occupied.listen(1)
                busy_port = occupied.getsockname()[1]
                viewer = start_viewer_server(registry, port=str(busy_port))
                try:
                    self.assertNotEqual(viewer.port, busy_port)
                    self.assertEqual(registry.server_info()["viewer_base_url"], viewer.base_url)
                finally:
                    viewer.shutdown()

    def test_viewer_defaults_to_registry_runtime_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = _build_test_runtime(
                temp_dir,
                server_instance_id="viewer-runtime-test",
                client_label="codex-test",
                config_path=Path(temp_dir) / "profiles.json",
            )
            writer = TranscriptWriter("runtime-1", runtime.transcripts_dir)
            writer.record("session_meta", "session metadata", extra={"profile": "dev", "server_instance_id": runtime.server_instance_id})
            registry = SessionRegistry(runtime)
            viewer = start_viewer_server(registry, port="auto")
            try:
                sessions = _json_get(f"{viewer.base_url}/api/sessions")
            finally:
                viewer.shutdown()

        found = next(item for item in sessions["sessions"] if item["session_id"] == "runtime-1")
        self.assertEqual(found["storage_scope"], "instance")
        self.assertEqual(found["server_instance_id"], "viewer-runtime-test")

    def test_viewer_reads_legacy_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_cwd = Path.cwd()
            runtime_dir = Path(temp_dir) / "runtime"
            legacy_dir = Path(temp_dir) / "transcripts"
            legacy_dir.mkdir()
            writer = TranscriptWriter("legacy-1", legacy_dir)
            writer.record("recv", "legacy\n")
            try:
                os.chdir(temp_dir)
                registry = SessionRegistry(_build_test_runtime(temp_dir, server_instance_id="viewer-legacy-test"))
                viewer = start_viewer_server(registry, port="auto", transcripts_dir=runtime_dir / "instances" / "viewer-legacy-test" / "transcripts")
                try:
                    sessions = _json_get(f"{viewer.base_url}/api/sessions")
                finally:
                    viewer.shutdown()
            finally:
                os.chdir(old_cwd)

        legacy = next(item for item in sessions["sessions"] if item["session_id"] == "legacy-1")
        self.assertEqual(legacy["storage_scope"], "legacy")
        self.assertEqual(legacy["server_instance_id"], "legacy")

    def test_viewer_summarizes_session_health_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = TranscriptWriter("health-history-1", temp_dir)
            writer.record("session_meta", "session metadata", extra={"profile": "dev"})
            writer.record(
                "session_health",
                "SSH transport is inactive",
                extra={"health_status": "unhealthy", "health_error": "SSH transport is inactive"},
            )
            registry = SessionRegistry(_build_test_runtime(temp_dir, server_instance_id="viewer-health-test"))
            viewer = start_viewer_server(registry, port="auto", transcripts_dir=temp_dir)
            try:
                sessions = _json_get(f"{viewer.base_url}/api/sessions")
            finally:
                viewer.shutdown()

        health = next(item for item in sessions["sessions"] if item["session_id"] == "health-history-1")
        self.assertEqual(health["status"], "unhealthy")
        self.assertEqual(health["health_error"], "SSH transport is inactive")


class HealthTests(unittest.TestCase):
    def test_session_info_includes_health_fields(self) -> None:
        session = _fake_session()
        try:
            info = session.info()
        finally:
            _close_fake_session(session)

        self.assertEqual(info["health_status"], "healthy")
        self.assertIn("last_heartbeat_at", info)
        self.assertIsNone(info["health_error"])

    def test_health_check_marks_inactive_transport_unhealthy(self) -> None:
        session = _fake_session(transport_active=False)
        try:
            with self.assertLogs("ssh_mcp.session", level="WARNING") as captured:
                ok = session.check_health_once()
            info = session.info()
            events = session.transcript.tail(10)
        finally:
            _close_fake_session(session)

        self.assertFalse(ok)
        self.assertEqual(info["health_status"], "unhealthy")
        self.assertIn("inactive", info["health_error"])
        self.assertIn("SSH transport is inactive", captured.output[0])
        health_events = [event for event in events if event["dir"] == "session_health"]
        self.assertEqual(health_events[0]["health_status"], "unhealthy")

    def test_closed_session_error_info_includes_diagnostics(self) -> None:
        session = _fake_session(transport_active=False)
        try:
            session.buffer.append("last screen\n")
            session.check_health_once()
            with self.assertRaisesRegex(Exception, "closed"):
                session.send_text("pwd")
            error = session.error_info("Session is closed.")
        finally:
            _close_fake_session(session)

        self.assertEqual(error["session_id"], "fake-session")
        self.assertEqual(error["health_status"], "unhealthy")
        self.assertIn("inactive", error["health_error"])
        self.assertIn("last_activity_at", error)
        self.assertIn("transcript_path", error)

    def test_server_send_text_returns_closed_session_diagnostics(self) -> None:
        import ssh_mcp.server as server_module

        with tempfile.TemporaryDirectory() as temp_dir:
            registry = SessionRegistry(_build_test_runtime(temp_dir, server_instance_id="server-error-test"))
            session = _fake_session(transport_active=False)
            session.buffer.append("last screen\n")
            session.check_health_once()
            with registry._lock:
                registry._sessions[session.id] = session
            old_registry = server_module.registry
            server_module.registry = registry
            try:
                response = server_module.send_text(session.id, "pwd")
                command_response = server_module.execute_command(session.id, "pwd")
                screen_response = server_module.get_screen(session.id)
            finally:
                server_module.registry = old_registry
                registry.close_all()
                session._test_temp_dir.cleanup()

        self.assertFalse(response["ok"])
        self.assertEqual(response["session_id"], "fake-session")
        self.assertEqual(response["health_status"], "unhealthy")
        self.assertIn("transcript_path", response)
        self.assertFalse(command_response["ok"])
        self.assertEqual(command_response["health_status"], "unhealthy")
        self.assertTrue(screen_response["ok"])
        self.assertIn("last screen", screen_response["screen"])


class ReopenTests(unittest.TestCase):
    def test_registry_reopen_links_new_session_to_previous_session(self) -> None:
        class FakeReopenRegistry(SessionRegistry):
            def open(self, profile, **kwargs):  # type: ignore[override]
                self.open_kwargs = kwargs
                session = _fake_session(
                    profile=profile,
                    session_id="fake-reopened",
                    previous_session_id=kwargs.get("previous_session_id"),
                    previous_transcript_path=kwargs.get("previous_transcript_path"),
                )
                self.created_session = session
                with self._lock:
                    self._sessions[session.id] = session
                return session

        with tempfile.TemporaryDirectory() as temp_dir:
            registry = FakeReopenRegistry(_build_test_runtime(temp_dir, server_instance_id="reopen-test"))
            previous = _fake_session(session_id="fake-previous", owner_label="debug-order")
            try:
                with registry._lock:
                    registry._sessions[previous.id] = previous

                reopened = registry.reopen(previous.id)

                previous_events = previous.transcript.tail(10)
                reopened_info = reopened.info()
            finally:
                registry.close_all()
                previous._test_temp_dir.cleanup()
                if hasattr(registry, "created_session"):
                    registry.created_session._test_temp_dir.cleanup()

        self.assertEqual(registry.open_kwargs["owner_label"], "debug-order")
        self.assertEqual(registry.open_kwargs["previous_session_id"], "fake-previous")
        self.assertEqual(reopened_info["previous_session_id"], "fake-previous")
        self.assertEqual(reopened_info["previous_transcript_path"], str(previous.transcript.path))
        self.assertTrue(any(event["text"] == "reopen requested" for event in previous_events))
        self.assertTrue(any(event["text"] == "reopen succeeded" and event["new_session_id"] == "fake-reopened" for event in previous_events))


class KeyLoadingTests(unittest.TestCase):
    def test_rsa_pem_header_prefers_rsa_loader(self) -> None:
        class FakeParamiko:
            RSAKey = object()
            Ed25519Key = object()
            ECDSAKey = object()
            DSSKey = object()

        with tempfile.TemporaryDirectory() as temp_dir:
            key_path = Path(temp_dir) / "id_rsa"
            key_path.write_text(
                "-----BEGIN RSA PRIVATE KEY-----\nnot-a-real-key\n-----END RSA PRIVATE KEY-----\n",
                encoding="utf-8",
            )

            key_classes = _key_classes_for_file(FakeParamiko, key_path)

        self.assertEqual(key_classes, [FakeParamiko.RSAKey])


def _json_get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def _build_test_runtime(temp_dir: str, **kwargs):
    old_runtime = os.environ.get("SSH_MCP_RUNTIME_DIR")
    old_log = os.environ.pop("SSH_MCP_LOG_PATH", None)
    old_transcripts = os.environ.pop("SSH_MCP_TRANSCRIPTS_DIR", None)
    os.environ["SSH_MCP_RUNTIME_DIR"] = str(Path(temp_dir) / "runtime")
    try:
        return build_runtime(**kwargs)
    finally:
        _restore_env("SSH_MCP_RUNTIME_DIR", old_runtime)
        _restore_env("SSH_MCP_LOG_PATH", old_log)
        _restore_env("SSH_MCP_TRANSCRIPTS_DIR", old_transcripts)


class _FakeTransport:
    def __init__(self, active: bool = True) -> None:
        self.active = active
        self.keepalive_interval: int | None = None

    def is_active(self) -> bool:
        return self.active

    def set_keepalive(self, interval: int) -> None:
        self.keepalive_interval = interval


class _FakeClient:
    def __init__(self, transport_active: bool = True) -> None:
        self.transport = _FakeTransport(transport_active)
        self.closed = False

    def get_transport(self) -> _FakeTransport:
        return self.transport

    def close(self) -> None:
        self.closed = True


class _FakeChannel:
    closed = False

    def recv_ready(self) -> bool:
        return False

    def exit_status_ready(self) -> bool:
        return False

    def send(self, payload: str) -> int:
        return len(payload)

    def close(self) -> None:
        self.closed = True


def _fake_session(
    *,
    transport_active: bool = True,
    profile=None,
    session_id: str = "fake-session",
    owner_label: str | None = None,
    previous_session_id: str | None = None,
    previous_transcript_path: str | None = None,
):
    from ssh_mcp.config import SshProfile

    temp_dir = tempfile.TemporaryDirectory()
    profile = profile or SshProfile(name="fake", host="127.0.0.1", username="fake", keepalive_interval=3600)
    session = SshSession(
        session_id,
        profile,
        _FakeClient(transport_active),
        _FakeChannel(),
        TranscriptWriter(session_id, temp_dir.name),
        owner_label=owner_label,
        server_instance_id="server-health-test",
        previous_session_id=previous_session_id,
        previous_transcript_path=previous_transcript_path,
    )
    session._test_temp_dir = temp_dir
    return session


def _close_fake_session(session) -> None:
    session.close()
    session._test_temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
