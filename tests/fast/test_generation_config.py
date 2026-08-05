from __future__ import annotations

from pathlib import Path

import pytest

from personal_voice_msg.config import ConfigurationError, RuntimeProfile
from personal_voice_msg.generation.config import (
    GENERATION_REQUIRED_SETTINGS,
    load_gemini_settings,
)


def write_generation_toml(path: Path, values: dict[str, str]) -> None:
    import json

    path.write_text(
        "".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items()),
        encoding="utf-8",
    )


def create_generation_configuration(
    root: Path, *, key_content: str = "test-gemini-key"
) -> Path:
    secret_root_dir = root / "secrets"
    secret_root_dir.mkdir()
    (secret_root_dir / "gemini_api_key.txt").write_text(key_content, encoding="utf-8")

    config_path = root / "generation-settings.toml"
    write_generation_toml(
        config_path,
        {
            "profile": "development",
            "secret_root": secret_root_dir.as_posix(),
            "gemini_api_key_file": "gemini_api_key.txt",
        },
    )
    return config_path


@pytest.mark.fast
def test_loads_gemini_api_key_as_sensitive_value(tmp_path: Path) -> None:
    config_path = create_generation_configuration(
        tmp_path, key_content="real-looking-key-value"
    )

    settings = load_gemini_settings(config_path)

    assert settings.profile is RuntimeProfile.DEVELOPMENT
    assert not isinstance(settings.api_key, str)
    assert settings.api_key.reveal() == "real-looking-key-value"


@pytest.mark.fast
def test_missing_gemini_api_key_file_fails_closed(tmp_path: Path) -> None:
    config_path = create_generation_configuration(tmp_path)
    (Path(tmp_path) / "secrets" / "gemini_api_key.txt").unlink()

    with pytest.raises(ConfigurationError):
        load_gemini_settings(config_path)


@pytest.mark.fast
def test_oversized_gemini_api_key_file_fails_closed(tmp_path: Path) -> None:
    config_path = create_generation_configuration(tmp_path)
    key_path = tmp_path / "secrets" / "gemini_api_key.txt"
    key_path.write_text("a" * 10_000_000, encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_gemini_settings(config_path)


@pytest.mark.fast
def test_unknown_generation_setting_fails_closed(tmp_path: Path) -> None:
    config_path = create_generation_configuration(tmp_path)
    with open(config_path, "a", encoding="utf-8") as f:
        f.write('extra_setting = "not-allowed"\n')

    with pytest.raises(ConfigurationError):
        load_gemini_settings(config_path)


@pytest.mark.fast
def test_generation_required_settings_are_exactly_three() -> None:
    assert GENERATION_REQUIRED_SETTINGS == {
        "profile",
        "secret_root",
        "gemini_api_key_file",
    }
