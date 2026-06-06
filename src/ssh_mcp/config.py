from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("config/profiles.json")


class ConfigError(ValueError):
    """Raised when the SSH MCP configuration is missing or invalid."""


@dataclass(frozen=True)
class SshProfile:
    name: str
    host: str
    username: str
    port: int = 22
    password: str | None = None
    password_env: str | None = None
    key_filename: str | None = None
    passphrase: str | None = None
    passphrase_env: str | None = None
    timeout: float = 15.0
    banner_timeout: float = 15.0
    auth_timeout: float = 15.0
    allow_agent: bool = True
    look_for_keys: bool = True
    auto_add_host_key: bool = True
    term: str = "xterm-256color"
    width: int = 120
    height: int = 40
    keepalive_interval: float = 30.0

    def resolved_password(self, override: str | None = None) -> str | None:
        return override if override is not None else _secret_from_value_or_env(self.password, self.password_env)

    def resolved_passphrase(self, override: str | None = None) -> str | None:
        return override if override is not None else _secret_from_value_or_env(self.passphrase, self.passphrase_env)


def get_config_path(path: str | None = None) -> Path:
    raw_path = path or os.getenv("SSH_MCP_CONFIG")
    return Path(raw_path) if raw_path else DEFAULT_CONFIG_PATH


def load_profiles(path: str | Path | None = None) -> dict[str, SshProfile]:
    config_path = get_config_path(str(path) if path is not None else None)
    if not config_path.exists():
        raise ConfigError(
            f"Config file not found: {config_path}. Copy config/profiles.example.json to config/profiles.json."
        )

    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    raw_profiles = data.get("profiles", data)
    if isinstance(raw_profiles, list):
        entries = {item.get("name"): item for item in raw_profiles if isinstance(item, dict)}
    elif isinstance(raw_profiles, dict):
        entries = raw_profiles
    else:
        raise ConfigError("Config must contain a 'profiles' object or list.")

    profiles: dict[str, SshProfile] = {}
    for name, raw_profile in entries.items():
        if not name or not isinstance(raw_profile, dict):
            raise ConfigError("Each profile must be an object with a non-empty name.")
        profiles[str(name)] = _parse_profile(str(name), raw_profile)
    return profiles


def load_profile(name: str, path: str | Path | None = None) -> SshProfile:
    profiles = load_profiles(path)
    try:
        return profiles[name]
    except KeyError as exc:
        available = ", ".join(sorted(profiles)) or "<none>"
        raise ConfigError(f"Profile '{name}' not found. Available profiles: {available}.") from exc


def _parse_profile(name: str, data: dict[str, Any]) -> SshProfile:
    missing = [field for field in ("host", "username") if not data.get(field)]
    if missing:
        raise ConfigError(f"Profile '{name}' is missing required field(s): {', '.join(missing)}.")

    return SshProfile(
        name=name,
        host=str(data["host"]),
        username=str(data["username"]),
        port=int(data.get("port", 22)),
        password=_optional_str(data.get("password")),
        password_env=_optional_str(data.get("password_env")),
        key_filename=_optional_str(data.get("key_filename")),
        passphrase=_optional_str(data.get("passphrase")),
        passphrase_env=_optional_str(data.get("passphrase_env")),
        timeout=float(data.get("timeout", 15.0)),
        banner_timeout=float(data.get("banner_timeout", 15.0)),
        auth_timeout=float(data.get("auth_timeout", 15.0)),
        allow_agent=bool(data.get("allow_agent", True)),
        look_for_keys=bool(data.get("look_for_keys", True)),
        auto_add_host_key=bool(data.get("auto_add_host_key", True)),
        term=str(data.get("term", "xterm-256color")),
        width=int(data.get("width", 120)),
        height=int(data.get("height", 40)),
        keepalive_interval=float(data.get("keepalive_interval", os.getenv("SSH_MCP_KEEPALIVE_INTERVAL") or 30.0)),
    )


def _secret_from_value_or_env(value: str | None, env_name: str | None) -> str | None:
    if _is_real_secret(value):
        return value
    if env_name:
        env_value = os.getenv(env_name)
        return env_value if _is_real_secret(env_value) else None
    return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _is_real_secret(value: str | None) -> bool:
    if value is None:
        return False
    text = value.strip()
    if not text:
        return False
    return not (text.startswith("${") and text.endswith("}"))
