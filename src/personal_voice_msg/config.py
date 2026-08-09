from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from personal_voice_msg.redaction import Redactor, SensitiveValue

REQUIRED_SETTINGS = {
    "profile",
    "secret_root",
    "recipient_file",
    "waha_token_file",
    "voice_embedding_file",
    "waha_session_file",
    "waha_base_url",
    "sender_auth_key_file",
}
RECIPIENT_SETTINGS = {"profile", "phone_number"}
E164_PHONE = re.compile(r"\+[1-9][0-9]{7,14}")
MAX_WAHA_TOKEN_CHARACTERS = 4_096
MAX_SENDER_AUTH_KEY_CHARACTERS = 4_096
# T15's WAHA deployment binds its port to loopback only (never a public
# port -- see docs/task-logs/T15.md); real network exposure across hosts is
# T18's territory, so the sender is only ever configured to reach WAHA on
# the same machine.
LOOPBACK_HOSTNAMES = {"127.0.0.1", "localhost"}


class ConfigurationError(ValueError):
    """Raised when configuration cannot be loaded safely."""


class RuntimeProfile(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass(frozen=True, slots=True)
class Settings:
    profile: RuntimeProfile
    recipient: SensitiveValue[str]
    waha_token: SensitiveValue[str]
    voice_embedding: SensitiveValue[Path]
    waha_session: SensitiveValue[Path]
    waha_base_url: str
    sender_auth_key: SensitiveValue[str]

    def redactor(self) -> Redactor:
        return Redactor(
            (
                self.recipient.reveal(),
                self.waha_token.reveal(),
                str(self.voice_embedding.reveal()),
                str(self.waha_session.reveal()),
                self.sender_auth_key.reveal(),
            )
        )


def read_toml(config_path: Path, required_settings: set[str]) -> dict[str, Any]:
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ConfigurationError(
            "configuration file is unreadable or invalid"
        ) from None
    if set(document) != required_settings:
        raise ConfigurationError("configuration settings are missing or unknown")
    if not all(isinstance(document[key], str) for key in required_settings):
        raise ConfigurationError("configuration settings must be strings")
    return document


def runtime_profile(value: str) -> RuntimeProfile:
    try:
        return RuntimeProfile(value)
    except ValueError:
        raise ConfigurationError("runtime profile is invalid") from None


def _project_root(config_path: Path) -> Path:
    for parent in config_path.parents:
        if (parent / "pyproject.toml").is_file() or (parent / ".git").exists():
            return parent
    return config_path.parent


def secret_root(
    config_path: Path,
    value: str,
    profile: RuntimeProfile,
) -> Path:
    root = Path(value)
    if not root.is_absolute():
        root = config_path.parent / root
    try:
        resolved = root.resolve(strict=True)
    except OSError:
        raise ConfigurationError("secret root is missing") from None
    if not resolved.is_dir():
        raise ConfigurationError("secret root is not a directory")
    if profile is not RuntimeProfile.DEVELOPMENT:
        project_root = _project_root(config_path)
        if resolved.is_relative_to(project_root):
            raise ConfigurationError(
                "deployed secret root must be outside the project directory"
            )
    return resolved


def secret_file(root: Path, value: str, setting: str) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise ConfigurationError(f"{setting} must be relative to secret root")
    try:
        resolved = (root / relative).resolve(strict=True)
    except OSError:
        raise ConfigurationError(f"{setting} is missing") from None
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ConfigurationError(f"{setting} is outside secret root or not a file")
    return resolved


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ConfigurationError("recipient configuration has duplicate keys")
        document[key] = value
    return document


def _recipient(path: Path, profile: RuntimeProfile) -> str:
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ConfigurationError(
            "recipient configuration is unreadable or invalid"
        ) from None
    if not isinstance(document, dict) or set(document) != RECIPIENT_SETTINGS:
        raise ConfigurationError("recipient configuration schema is invalid")
    recipient_profile = document.get("profile")
    phone_number = document.get("phone_number")
    if recipient_profile != profile.value:
        raise ConfigurationError("recipient profile does not match runtime profile")
    if not isinstance(phone_number, str) or not E164_PHONE.fullmatch(phone_number):
        raise ConfigurationError("recipient phone number is invalid")
    return phone_number


def _token(path: Path) -> str:
    try:
        oversized = path.stat().st_size > MAX_WAHA_TOKEN_CHARACTERS
    except OSError:
        raise ConfigurationError("WAHA token file is unreadable") from None
    if oversized:
        raise ConfigurationError("WAHA token file is too large")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise ConfigurationError("WAHA token file is unreadable") from None
    if not token:
        raise ConfigurationError("WAHA token is empty")
    return token


def _sender_auth_key(path: Path) -> str:
    try:
        oversized = path.stat().st_size > MAX_SENDER_AUTH_KEY_CHARACTERS
    except OSError:
        raise ConfigurationError("sender auth key file is unreadable") from None
    if oversized:
        raise ConfigurationError("sender auth key file is too large")
    try:
        key = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise ConfigurationError("sender auth key file is unreadable") from None
    if not key:
        raise ConfigurationError("sender auth key is empty")
    return key


def _waha_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ConfigurationError("WAHA base URL must use http or https")
    if parsed.hostname not in LOOPBACK_HOSTNAMES:
        raise ConfigurationError("WAHA base URL must be a loopback address")
    if parsed.path not in ("", "/") or parsed.username or parsed.password:
        raise ConfigurationError("WAHA base URL must not contain extra components")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("WAHA base URL must not contain extra components")
    return value


def load_settings(config_path: Path) -> Settings:
    """Load non-secret TOML settings and secret values from bounded files."""

    path = config_path.resolve()
    document = read_toml(path, REQUIRED_SETTINGS)
    profile = runtime_profile(document["profile"])
    root = secret_root(path, document["secret_root"], profile)
    recipient_path = secret_file(root, document["recipient_file"], "recipient_file")
    token_path = secret_file(root, document["waha_token_file"], "waha_token_file")
    embedding_path = secret_file(
        root,
        document["voice_embedding_file"],
        "voice_embedding_file",
    )
    session_path = secret_file(
        root,
        document["waha_session_file"],
        "waha_session_file",
    )
    sender_auth_key_path = secret_file(
        root,
        document["sender_auth_key_file"],
        "sender_auth_key_file",
    )

    return Settings(
        profile=profile,
        recipient=SensitiveValue(_recipient(recipient_path, profile)),
        waha_token=SensitiveValue(_token(token_path)),
        voice_embedding=SensitiveValue(embedding_path),
        waha_session=SensitiveValue(session_path),
        waha_base_url=_waha_base_url(document["waha_base_url"]),
        sender_auth_key=SensitiveValue(_sender_auth_key(sender_auth_key_path)),
    )
