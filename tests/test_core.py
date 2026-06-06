from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
import urllib.request

from ssh_mcp.config import load_profile, load_profiles
from ssh_mcp.session import SessionRegistry, TerminalBuffer, _key_classes_for_file, build_log_search_command
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


class ViewerTests(unittest.TestCase):
    def test_viewer_serves_sessions_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = TranscriptWriter("session-1", temp_dir)
            writer.record("session_meta", "session metadata", extra={"profile": "dev", "owner_label": "codex-test"})
            writer.record("recv", "hello\n")
            registry = SessionRegistry()
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
        registry = SessionRegistry()
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


if __name__ == "__main__":
    unittest.main()
