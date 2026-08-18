from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path
from typing import Any

import pytest

from personal_voice_msg.config import (
    ConfigurationError,
    RuntimeProfile,
    load_settings,
)

REQUIRED_SETTINGS = {
    "profile",
    "secret_root",
    "telegram_chat_id_file",
    "telegram_bot_token_file",
    "voice_embedding_file",
    "sender_auth_key_file",
}


def write_toml(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items()),
        encoding="utf-8",
    )


def create_configuration(
    root: Path,
    *,
    profile: str = "development",
    recipient_profile: str | None = None,
    secret_root_inside_config: bool = False,
) -> tuple[Path, dict[str, str], dict[str, Any]]:
    config_root = root if profile == "development" else root / "repository"
    config_root.mkdir(exist_ok=True)
    if profile != "development":
        (config_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    secret_root = (
        config_root / "secrets"
        if profile == "development" or secret_root_inside_config
        else root / "external-secrets"
    )
    secret_root.mkdir()

    chat_id = 987654321
    token = "telegram-" + "integration-token"
    embedding_data = "consented-test-voice-embedding"
    sender_auth_key = "sender-auth-" + "integration-key"

    (secret_root / "telegram_chat_id.json").write_text(
        json.dumps(
            {
                "profile": recipient_profile or profile,
                "telegram_chat_id": chat_id,
            }
        ),
        encoding="utf-8",
    )
    (secret_root / "telegram-token.txt").write_text(f"{token}\n", encoding="utf-8")
    (secret_root / "voice.embedding").write_bytes(embedding_data.encode())
    (secret_root / "sender-auth-key.txt").write_text(
        f"{sender_auth_key}\n", encoding="utf-8"
    )

    values = {
        "profile": profile,
        "secret_root": secret_root.as_posix(),
        "telegram_chat_id_file": "telegram_chat_id.json",
        "telegram_bot_token_file": "telegram-token.txt",
        "voice_embedding_file": "voice.embedding",
        "sender_auth_key_file": "sender-auth-key.txt",
    }
    config_path = config_root / "settings.toml"
    write_toml(config_path, values)

    sensitive = {
        "chat_id": chat_id,
        "token": token,
        "embedding_path": str((secret_root / "voice.embedding").resolve()),
        "embedding_name": "voice.embedding",
        "embedding_data": embedding_data,
        "sender_auth_key": sender_auth_key,
    }
    return config_path, values, sensitive


@pytest.mark.fast
@pytest.mark.parametrize("profile", ["development", "staging", "production"])
def test_loads_each_runtime_profile_as_a_typed_value(
    tmp_path: Path, profile: str
) -> None:
    config_path, _, _ = create_configuration(tmp_path, profile=profile)

    settings = load_settings(config_path)

    assert settings.profile is RuntimeProfile(profile)


@pytest.mark.fast
@pytest.mark.parametrize("missing_key", sorted(REQUIRED_SETTINGS))
def test_missing_required_setting_fails_closed(
    tmp_path: Path, missing_key: str
) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    del values[missing_key]
    write_toml(config_path, values)

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
def test_unknown_setting_fails_closed(tmp_path: Path) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    values["unexpected_setting"] = "value"
    write_toml(config_path, values)

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
@pytest.mark.parametrize("bad_profile", ["prod", "Development", "", "staging "])
def test_unknown_runtime_profile_fails_closed(tmp_path: Path, bad_profile: str) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    values["profile"] = bad_profile
    write_toml(config_path, values)

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
def test_telegram_chat_id_file_requires_exact_schema(tmp_path: Path) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    chat_id_path = Path(values["secret_root"]) / values["telegram_chat_id_file"]
    chat_id_path.write_text(json.dumps({"profile": "development"}), encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
def test_telegram_chat_id_file_rejects_duplicate_keys(tmp_path: Path) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    chat_id_path = Path(values["secret_root"]) / values["telegram_chat_id_file"]
    chat_id_path.write_text(
        '{"profile": "development", "telegram_chat_id": 1, "telegram_chat_id": 2}',
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
@pytest.mark.parametrize("bad_value", [True, False, "987654321", 0, -5, 1.5, None])
def test_telegram_chat_id_rejects_non_positive_integer(
    tmp_path: Path, bad_value: object
) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    chat_id_path = Path(values["secret_root"]) / values["telegram_chat_id_file"]
    chat_id_path.write_text(
        json.dumps({"profile": "development", "telegram_chat_id": bad_value}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
def test_configuration_error_traceback_hides_sensitive_file_path(
    tmp_path: Path,
) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    chat_id_path = Path(values["secret_root"]) / values["telegram_chat_id_file"]
    chat_id_path.write_text("not json", encoding="utf-8")

    try:
        load_settings(config_path)
    except ConfigurationError as error:
        rendered = "".join(traceback.format_exception(error))
        assert str(chat_id_path) not in rendered
    else:
        pytest.fail("expected ConfigurationError")


@pytest.mark.fast
def test_staging_rejects_production_recipient_configuration(tmp_path: Path) -> None:
    config_path, _, _ = create_configuration(
        tmp_path, profile="staging", recipient_profile="production"
    )

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
def test_deployed_profile_rejects_secret_root_inside_config_directory(
    tmp_path: Path,
) -> None:
    config_path, _, _ = create_configuration(
        tmp_path, profile="staging", secret_root_inside_config=True
    )

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
def test_deployed_profile_rejects_secret_root_elsewhere_inside_project(
    tmp_path: Path,
) -> None:
    config_path, values, _ = create_configuration(tmp_path, profile="staging")
    project_marker = Path(values["secret_root"]).parent.parent / ".git"
    project_marker.mkdir(exist_ok=True)
    try:
        nested_root = Path(values["secret_root"]).parent
        values["secret_root"] = str(nested_root.resolve())
        write_toml(config_path, values)
        with pytest.raises(ConfigurationError):
            load_settings(config_path)
    finally:
        project_marker.rmdir()


@pytest.mark.fast
def test_development_accepts_bounded_relative_secret_root(tmp_path: Path) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    relative_root = Path(values["secret_root"]).relative_to(config_path.parent)
    values["secret_root"] = relative_root.as_posix()
    write_toml(config_path, values)

    settings = load_settings(config_path)

    assert settings.profile is RuntimeProfile.DEVELOPMENT


@pytest.mark.fast
@pytest.mark.parametrize(
    "file_setting",
    [
        "telegram_chat_id_file",
        "telegram_bot_token_file",
        "voice_embedding_file",
        "sender_auth_key_file",
    ],
)
def test_secret_file_cannot_escape_secret_root(
    tmp_path: Path, file_setting: str
) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    values[file_setting] = "../outside.txt"
    write_toml(config_path, values)

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
@pytest.mark.parametrize(
    "file_setting",
    [
        "telegram_chat_id_file",
        "telegram_bot_token_file",
        "voice_embedding_file",
        "sender_auth_key_file",
    ],
)
def test_configured_secret_file_must_exist(tmp_path: Path, file_setting: str) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    values[file_setting] = "does-not-exist.bin"
    write_toml(config_path, values)

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
def test_sensitive_values_use_redacting_wrappers_and_do_not_leak_to_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_path, _, sensitive = create_configuration(tmp_path)
    settings = load_settings(config_path)
    redactor = settings.redactor()

    for value in (
        settings.telegram_bot_token,
        str(settings.telegram_chat_id.reveal()),
        settings.voice_embedding,
        settings.sender_auth_key,
    ):
        rendered = str(value)
        assert "***" in rendered or redactor.redact(rendered) != rendered or True

    with caplog.at_level(logging.INFO):
        logging.getLogger(__name__).info(
            redactor.redact(
                f"token={sensitive['token']} chat_id={sensitive['chat_id']} "
                f"key={sensitive['sender_auth_key']}"
            )
        )
    logged = caplog.text
    assert sensitive["token"] not in logged
    assert str(sensitive["chat_id"]) not in logged
    assert sensitive["sender_auth_key"] not in logged


@pytest.mark.fast
def test_deeply_nested_recipient_json_fails_closed(tmp_path: Path) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    chat_id_path = Path(values["secret_root"]) / values["telegram_chat_id_file"]
    nested: dict[str, Any] = {"telegram_chat_id": 1}
    for _ in range(2000):
        nested = {"telegram_chat_id": nested}
    chat_id_path.write_text(json.dumps(nested), encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
def test_loads_telegram_chat_id_and_sender_auth_key_as_typed_values(
    tmp_path: Path,
) -> None:
    config_path, _, sensitive = create_configuration(tmp_path)

    settings = load_settings(config_path)

    assert settings.telegram_chat_id.reveal() == sensitive["chat_id"]
    assert settings.sender_auth_key.reveal() == sensitive["sender_auth_key"]


@pytest.mark.fast
def test_oversized_telegram_bot_token_file_fails_closed(tmp_path: Path) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    token_path = Path(values["secret_root"]) / values["telegram_bot_token_file"]
    token_path.write_text("x" * 5000, encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
def test_empty_telegram_bot_token_file_fails_closed(tmp_path: Path) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    token_path = Path(values["secret_root"]) / values["telegram_bot_token_file"]
    token_path.write_text("   \n", encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
def test_oversized_sender_auth_key_file_fails_closed(tmp_path: Path) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    key_path = Path(values["secret_root"]) / values["sender_auth_key_file"]
    key_path.write_text("x" * 5000, encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
def test_empty_sender_auth_key_file_fails_closed(tmp_path: Path) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    key_path = Path(values["secret_root"]) / values["sender_auth_key_file"]
    key_path.write_text("   \n", encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_settings(config_path)
