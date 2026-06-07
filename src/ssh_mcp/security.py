from __future__ import annotations

from dataclasses import dataclass
import os
import re
import shlex
from typing import Any


SECURITY_MODES = {"unrestricted", "readonly", "restricted"}
SENSITIVE_KEYS = ("password", "passphrase", "secret", "token", "apikey", "api_key", "authorization")
REDACTED = "[REDACTED]"

READONLY_COMMANDS = {
    "awk",
    "cat",
    "cut",
    "date",
    "df",
    "du",
    "echo",
    "env",
    "find",
    "free",
    "grep",
    "head",
    "hostname",
    "id",
    "journalctl",
    "kubectl",
    "less",
    "ls",
    "netstat",
    "pgrep",
    "ps",
    "pwd",
    "sed",
    "ss",
    "stat",
    "tail",
    "top",
    "uname",
    "wc",
    "which",
    "whoami",
    "xargs",
}

READONLY_KUBECTL_VERBS = {"api-resources", "api-versions", "describe", "explain", "get", "logs", "top", "version"}

DANGEROUS_PATTERNS = (
    r"(?i)(^|[;&|]\s*)rm\s+.*(-r|-R|--recursive)",
    r"(?i)(^|[;&|]\s*)rm\s+.*\s/(?:\s|$)",
    r"(?i)(^|[;&|]\s*)(shutdown|reboot|halt|poweroff)\b",
    r"(?i)(^|[;&|]\s*)(mkfs|fdisk|parted|dd)\b",
    r"(?i)(^|[;&|]\s*)chmod\s+(-R|.*\s777\b)",
    r"(?i)(^|[;&|]\s*)chown\s+-R\b",
    r"(?i)(^|[;&|]\s*)systemctl\s+(stop|restart|disable|mask)\b",
    r"(?i)(^|[;&|]\s*)docker\s+(rm|rmi|stop|kill|prune)\b",
    r"(?i)(^|[;&|]\s*)kubectl\s+(delete|drain|cordon|uncordon|apply|replace|patch|scale|rollout\s+restart)\b",
    r"(?i)(curl|wget)\b.*\|\s*(sh|bash)\b",
    r"(?i)\b(eval|exec)\s+\$\(",
    r":\s*\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",
)

MUTATING_TOKENS_RE = re.compile(r"(^|[^<])>(?![>])|>>|<<|(^|[;&|]\s*)(sudo|su|mv|cp|touch|mkdir|rmdir|tee)\b", re.I)
SHELL_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||[;|])\s*")
GENERIC_SECRET_RE = re.compile(
    r"(?i)\b(password|passphrase|token|secret|api[_-]?key|authorization)\b\s*[:=]\s*([^\s\"']+)"
    r"|Bearer\s+[A-Za-z0-9._~+/=-]+"
)


class SecurityError(RuntimeError):
    """Raised when a local security policy refuses a tool call before it reaches the remote shell."""


@dataclass(frozen=True)
class SecurityDecision:
    allowed: bool
    mode: str
    reason: str
    matched_rule: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "mode": self.mode,
            "reason": self.reason,
            "matched_rule": self.matched_rule,
        }


@dataclass(frozen=True)
class SecurityPolicy:
    mode: str = "unrestricted"
    allow_patterns: tuple[str, ...] = ()
    deny_patterns: tuple[str, ...] = ()
    allow_interactive_text: bool = True
    redact_transcripts: bool = True
    transcript_retention_days: int | None = None
    transcript_max_files: int | None = None

    def evaluate_command(self, command: str, *, tool: str = "execute_command") -> SecurityDecision:
        mode = normalize_mode(self.mode)
        text = command.strip()
        if not text:
            return SecurityDecision(False, mode, "Empty command is not allowed.")

        deny = _first_matching_pattern([*DANGEROUS_PATTERNS, *self.deny_patterns], text)
        if deny:
            return SecurityDecision(False, mode, "Command matched deny/dangerous policy.", deny)

        if mode == "unrestricted":
            return SecurityDecision(True, mode, "Unrestricted mode allows command.")

        if tool == "search_logs":
            return SecurityDecision(True, mode, "search_logs is treated as a read-only tool.")

        if mode == "readonly":
            return self._evaluate_readonly(text)

        allow = _first_matching_pattern(self.allow_patterns, text)
        if allow:
            return SecurityDecision(True, mode, "Command matched restricted allow pattern.", allow)
        return SecurityDecision(False, mode, "Restricted mode requires an allow pattern match.")

    def evaluate_text(self, text: str, *, tool: str = "send_text") -> SecurityDecision:
        mode = normalize_mode(self.mode)
        stripped = text.strip()
        deny = _first_matching_pattern([*DANGEROUS_PATTERNS, *self.deny_patterns], stripped)
        if deny:
            return SecurityDecision(False, mode, "Text matched deny/dangerous policy.", deny)
        if mode == "unrestricted":
            return SecurityDecision(True, mode, "Unrestricted mode allows text input.")
        if self.allow_interactive_text and _looks_like_menu_input(stripped):
            return SecurityDecision(True, mode, "Interactive menu-like input allowed.")
        if mode == "readonly":
            return self._evaluate_readonly(stripped)
        allow = _first_matching_pattern(self.allow_patterns, stripped)
        if allow:
            return SecurityDecision(True, mode, "Text matched restricted allow pattern.", allow)
        return SecurityDecision(False, mode, f"{tool} input is not allowed by {mode} policy.")

    def _evaluate_readonly(self, command: str) -> SecurityDecision:
        if MUTATING_TOKENS_RE.search(command):
            return SecurityDecision(False, "readonly", "Readonly mode blocks mutating shell syntax.")
        for part in SHELL_SPLIT_RE.split(command):
            if not part.strip() or part.strip() in {"then", "else", "fi", "do", "done"}:
                continue
            name = _command_name(part)
            if not name:
                continue
            if name not in READONLY_COMMANDS and name not in {"if", "test", "["}:
                return SecurityDecision(False, "readonly", f"Readonly mode does not allow command '{name}'.")
            if name == "kubectl" and not _kubectl_is_readonly(part):
                return SecurityDecision(False, "readonly", "Readonly mode only allows read-only kubectl verbs.")
        return SecurityDecision(True, "readonly", "Readonly command allowed.")


def security_policy_from_config(data: dict[str, Any]) -> SecurityPolicy:
    raw = data.get("security", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("security must be an object when provided.")

    mode = normalize_mode(str(raw.get("mode") or data.get("security_mode") or os.getenv("SSH_MCP_SECURITY_MODE") or "unrestricted"))
    return SecurityPolicy(
        mode=mode,
        allow_patterns=tuple(str(item) for item in raw.get("allow_patterns", data.get("command_allow_patterns", [])) or []),
        deny_patterns=tuple(str(item) for item in raw.get("deny_patterns", data.get("command_deny_patterns", [])) or []),
        allow_interactive_text=bool(raw.get("allow_interactive_text", data.get("allow_interactive_text", True))),
        redact_transcripts=bool(
            _env_bool("SSH_MCP_REDACT_TRANSCRIPTS", raw.get("redact_transcripts", data.get("redact_transcripts", True)))
        ),
        transcript_retention_days=_optional_int(
            raw.get("transcript_retention_days", data.get("transcript_retention_days", os.getenv("SSH_MCP_TRANSCRIPT_RETENTION_DAYS")))
        ),
        transcript_max_files=_optional_int(
            raw.get("transcript_max_files", data.get("transcript_max_files", os.getenv("SSH_MCP_TRANSCRIPT_MAX_FILES")))
        ),
    )


def normalize_mode(mode: str) -> str:
    text = mode.strip().lower()
    if text == "permissive":
        text = "unrestricted"
    if text not in SECURITY_MODES:
        raise ValueError(f"Unknown security mode '{mode}'. Expected one of: {', '.join(sorted(SECURITY_MODES))}.")
    return text


def redact_text(text: str, *, force: bool = False) -> str:
    if force:
        return REDACTED
    return GENERIC_SECRET_RE.sub(_redact_match, text)


def redact_extra(data: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in data.items():
        if any(secret_key in key.lower() for secret_key in SENSITIVE_KEYS):
            redacted[key] = REDACTED
        elif isinstance(value, str):
            redacted[key] = redact_text(value)
        else:
            redacted[key] = value
    return redacted


def _first_matching_pattern(patterns: list[str] | tuple[str, ...], text: str) -> str | None:
    for pattern in patterns:
        try:
            if re.search(pattern, text):
                return pattern
        except re.error:
            if pattern in text:
                return pattern
    return None


def _command_name(part: str) -> str:
    try:
        tokens = shlex.split(part, posix=True)
    except ValueError:
        tokens = part.strip().split()
    if not tokens:
        return ""
    token = tokens[0]
    if "=" in token and not token.startswith("="):
        tokens = tokens[1:]
        if not tokens:
            return ""
        token = tokens[0]
    return token.rsplit("/", 1)[-1]


def _kubectl_is_readonly(part: str) -> bool:
    try:
        tokens = shlex.split(part, posix=True)
    except ValueError:
        tokens = part.strip().split()
    if not tokens:
        return True
    args = [token for token in tokens[1:] if not token.startswith("-")]
    return bool(args and args[0] in READONLY_KUBECTL_VERBS)


def _looks_like_menu_input(text: str) -> bool:
    if not text or len(text) > 256 or "\n" in text or "\r" in text:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_./:@+=,\- ]+", text))


def _redact_match(match: re.Match[str]) -> str:
    text = match.group(0)
    if text.lower().startswith("bearer "):
        return "Bearer " + REDACTED
    key = match.group(1) or "secret"
    separator = "=" if "=" in text else ":"
    return f"{key}{separator}{REDACTED}"


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _env_bool(name: str, default: Any) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.strip().lower() not in {"0", "false", "no", "off"}
