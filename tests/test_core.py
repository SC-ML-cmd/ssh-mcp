from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from ssh_mcp.config import load_profile, load_profiles
from ssh_mcp.session import TerminalBuffer, _key_classes_for_file, build_log_search_command
from ssh_mcp.transcript import TranscriptWriter


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

        self.assertEqual(events[0]["text"], "[REDACTED]")
        self.assertTrue(events[0]["sensitive"])
        self.assertEqual(events[1]["dir"], "recv")
        self.assertEqual(events[1]["text"], "ok\n")


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


if __name__ == "__main__":
    unittest.main()
