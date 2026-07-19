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
    "recipient_file",
    "waha_token_file",
    "voice_embedding_file",
    "waha_session_file",
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
) -> tuple[Path, dict[str, str], dict[str, str]]:
    secret_root = root / "secrets"
    secret_root.mkdir()

    phone_number = "+1" + "5550001111"
    token = "waha-" + "integration-token"
    embedding_data = "consented-test-voice-embedding"
    session_data = "non-production-session-data"

    (secret_root / "recipient.json").write_text(
        json.dumps(
            {
                "profile": recipient_profile or profile,
                "phone_number": phone_number,
            }
        ),
        encoding="utf-8",
    )
    (secret_root / "waha-token.txt").write_text(f"{token}\n", encoding="utf-8")
    (secret_root / "voice.embedding").write_bytes(embedding_data.encode())
    (secret_root / "waha-session.bin").write_bytes(session_data.encode())

    values = {
        "profile": profile,
        "secret_root": secret_root.as_posix(),
        "recipient_file": "recipient.json",
        "waha_token_file": "waha-token.txt",
        "voice_embedding_file": "voice.embedding",
        "waha_session_file": "waha-session.bin",
    }
    config_path = root / "settings.toml"
    write_toml(config_path, values)

    sensitive = {
        "phone_number": phone_number,
        "token": token,
        "embedding_path": str((secret_root / "voice.embedding").resolve()),
        "embedding_name": "voice.embedding",
        "embedding_data": embedding_data,
        "session_path": str((secret_root / "waha-session.bin").resolve()),
        "session_name": "waha-session.bin",
        "session_data": session_data,
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
    values["unexpected_setting"] = "must-not-be-ignored"
    write_toml(config_path, values)

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
@pytest.mark.parametrize("bad_profile", ["", "test", "Production"])
def test_unknown_runtime_profile_fails_closed(tmp_path: Path, bad_profile: str) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    values["profile"] = bad_profile
    write_toml(config_path, values)

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
@pytest.mark.parametrize("recipient_change", ["missing", "unknown"])
def test_recipient_file_requires_exact_schema(
    tmp_path: Path, recipient_change: str
) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    recipient_path = Path(values["secret_root"]) / values["recipient_file"]
    recipient: dict[str, Any] = {
        "profile": "development",
        "phone_number": "+1" + "5550001111",
    }
    if recipient_change == "missing":
        del recipient["phone_number"]
    else:
        recipient["display_name"] = "not-allowed"
    recipient_path.write_text(json.dumps(recipient), encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
@pytest.mark.parametrize(
    "recipient_json",
    [
        '{"profile":"production","profile":"staging",'
        '"phone_number":"+15550001111"}',
        '{"profile":"staging","phone_number":"+15550001111",'
        '"phone_number":"+15550002222"}',
    ],
)
def test_recipient_file_rejects_duplicate_keys(
    tmp_path: Path, recipient_json: str
) -> None:
    config_path, values, _ = create_configuration(tmp_path, profile="staging")
    recipient_path = Path(values["secret_root"]) / values["recipient_file"]
    recipient_path.write_text(recipient_json, encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
def test_configuration_error_traceback_hides_sensitive_file_path(
    tmp_path: Path,
) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    sensitive_name = "owner-private-voice.embedding"
    values["voice_embedding_file"] = sensitive_name
    write_toml(config_path, values)

    try:
        load_settings(config_path)
    except ConfigurationError as error:
        rendered = "".join(traceback.format_exception(error))
    else:
        pytest.fail("missing secret file did not fail closed")

    assert sensitive_name not in rendered
    assert str(Path(values["secret_root"]) / sensitive_name) not in rendered


@pytest.mark.fast
def test_staging_rejects_production_recipient_configuration(tmp_path: Path) -> None:
    config_path, _, _ = create_configuration(
        tmp_path,
        profile="staging",
        recipient_profile="production",
    )

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
@pytest.mark.parametrize("escaping_name", ["../outside-token.txt", "absolute"])
def test_secret_file_cannot_escape_secret_root(
    tmp_path: Path, escaping_name: str
) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    outside_token = tmp_path / "outside-token.txt"
    outside_token.write_text("outside-secret\n", encoding="utf-8")
    values["waha_token_file"] = (
        outside_token.as_posix() if escaping_name == "absolute" else escaping_name
    )
    write_toml(config_path, values)

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
@pytest.mark.parametrize(
    "file_setting",
    [
        "recipient_file",
        "waha_token_file",
        "voice_embedding_file",
        "waha_session_file",
    ],
)
def test_configured_secret_file_must_exist(tmp_path: Path, file_setting: str) -> None:
    config_path, values, _ = create_configuration(tmp_path)
    (Path(values["secret_root"]) / values[file_setting]).unlink()

    with pytest.raises(ConfigurationError):
        load_settings(config_path)


@pytest.mark.fast
def test_sensitive_values_use_redacting_wrappers_and_do_not_leak_to_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_path, _, sensitive = create_configuration(tmp_path)
    settings = load_settings(config_path)

    protected_values = [
        settings.recipient,
        settings.waha_token,
        settings.voice_embedding,
        settings.waha_session,
    ]
    for protected in protected_values:
        assert not isinstance(protected, (str, Path, bytes))

    logger = logging.getLogger("personal_voice_msg.config.test")
    with caplog.at_level(logging.INFO):
        logger.info("loaded configuration: %s", settings)

    rendered = "\n".join(
        [
            str(settings),
            repr(settings),
            *(str(value) for value in protected_values),
            *(repr(value) for value in protected_values),
            caplog.text,
        ]
    )
    for plaintext in sensitive.values():
        assert plaintext not in rendered
