from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from personal_voice_msg.config import (
    ConfigurationError,
    RuntimeProfile,
    read_toml,
    runtime_profile,
    secret_file,
    secret_root,
)
from personal_voice_msg.redaction import SensitiveValue

GENERATION_REQUIRED_SETTINGS = {"profile", "secret_root", "gemini_api_key_file"}
MAX_GEMINI_API_KEY_CHARACTERS = 4_096


@dataclass(frozen=True, slots=True)
class GeminiSettings:
    profile: RuntimeProfile
    api_key: SensitiveValue[str]


def _api_key(path: Path) -> str:
    try:
        oversized = path.stat().st_size > MAX_GEMINI_API_KEY_CHARACTERS
    except OSError:
        raise ConfigurationError("Gemini API key file is unreadable") from None
    if oversized:
        raise ConfigurationError("Gemini API key file is too large")
    try:
        key = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise ConfigurationError("Gemini API key file is unreadable") from None
    if not key:
        raise ConfigurationError("Gemini API key is empty")
    return key


def load_gemini_settings(config_path: Path) -> GeminiSettings:
    """Load non-secret TOML settings and the Gemini API key from a bounded file."""

    path = config_path.resolve()
    document = read_toml(path, GENERATION_REQUIRED_SETTINGS)
    profile = runtime_profile(document["profile"])
    root = secret_root(path, document["secret_root"], profile)
    key_path = secret_file(
        root, document["gemini_api_key_file"], "gemini_api_key_file",
        profile=profile,
    )
    return GeminiSettings(profile=profile, api_key=SensitiveValue(_api_key(key_path)))
