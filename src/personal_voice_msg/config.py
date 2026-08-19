from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from personal_voice_msg.redaction import Redactor, SensitiveValue

REQUIRED_SETTINGS = {
    "profile",
    "secret_root",
    "telegram_chat_id_file",
    "telegram_bot_token_file",
    "voice_embedding_file",
    "sender_auth_key_file",
}
CHAT_ID_SETTINGS = {"profile", "telegram_chat_id"}
MAX_TELEGRAM_BOT_TOKEN_CHARACTERS = 4_096
MAX_SENDER_AUTH_KEY_CHARACTERS = 4_096
# Telegram user/chat IDs are documented as fitting within a 52-bit signed
# integer as of the current Bot API -- this is a sanity bound, not a
# protocol requirement, so a real future ID near this ceiling would need
# this constant raised, not treated as a security boundary.
MAX_TELEGRAM_CHAT_ID = 2**52


class ConfigurationError(ValueError):
    """Raised when configuration cannot be loaded safely."""


class RuntimeProfile(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass(frozen=True, slots=True)
class Settings:
    profile: RuntimeProfile
    telegram_chat_id: SensitiveValue[int]
    telegram_bot_token: SensitiveValue[str]
    voice_embedding: SensitiveValue[Path]
    sender_auth_key: SensitiveValue[str]

    def redactor(self) -> Redactor:
        return Redactor(
            (
                str(self.telegram_chat_id.reveal()),
                self.telegram_bot_token.reveal(),
                str(self.voice_embedding.reveal()),
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


def _telegram_chat_id(path: Path, profile: RuntimeProfile) -> int:
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ConfigurationError(
            "recipient configuration is unreadable or invalid"
        ) from None
    if not isinstance(document, dict) or set(document) != CHAT_ID_SETTINGS:
        raise ConfigurationError("recipient configuration schema is invalid")
    recipient_profile = document.get("profile")
    chat_id = document.get("telegram_chat_id")
    if recipient_profile != profile.value:
        raise ConfigurationError("recipient profile does not match runtime profile")
    # Explicitly excludes bool: JSON `true`/`false` deserialize to Python
    # `bool`, and `isinstance(True, int)` is True in Python, which would
    # otherwise let a malformed `"telegram_chat_id": true` silently pass
    # as chat_id=1.
    if (
        type(chat_id) is not int
        or chat_id <= 0
        or chat_id >= MAX_TELEGRAM_CHAT_ID
    ):
        raise ConfigurationError("Telegram chat id is invalid")
    return chat_id


def _bounded_secret_text(path: Path, setting: str, max_characters: int) -> str:
    try:
        oversized = path.stat().st_size > max_characters
    except OSError:
        raise ConfigurationError(f"{setting} is unreadable") from None
    if oversized:
        raise ConfigurationError(f"{setting} is too large")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise ConfigurationError(f"{setting} is unreadable") from None
    if not value:
        raise ConfigurationError(f"{setting} is empty")
    return value


def load_settings(config_path: Path) -> Settings:
    """Load non-secret TOML settings and secret values from bounded files."""

    path = config_path.resolve()
    document = read_toml(path, REQUIRED_SETTINGS)
    profile = runtime_profile(document["profile"])
    root = secret_root(path, document["secret_root"], profile)
    chat_id_path = secret_file(
        root, document["telegram_chat_id_file"], "telegram_chat_id_file"
    )
    token_path = secret_file(
        root, document["telegram_bot_token_file"], "telegram_bot_token_file"
    )
    embedding_path = secret_file(
        root,
        document["voice_embedding_file"],
        "voice_embedding_file",
    )
    sender_auth_key_path = secret_file(
        root,
        document["sender_auth_key_file"],
        "sender_auth_key_file",
    )

    return Settings(
        profile=profile,
        telegram_chat_id=SensitiveValue(_telegram_chat_id(chat_id_path, profile)),
        telegram_bot_token=SensitiveValue(
            _bounded_secret_text(
                token_path,
                "telegram_bot_token_file",
                MAX_TELEGRAM_BOT_TOKEN_CHARACTERS,
            )
        ),
        voice_embedding=SensitiveValue(embedding_path),
        sender_auth_key=SensitiveValue(
            _bounded_secret_text(
                sender_auth_key_path,
                "sender_auth_key_file",
                MAX_SENDER_AUTH_KEY_CHARACTERS,
            )
        ),
    )
