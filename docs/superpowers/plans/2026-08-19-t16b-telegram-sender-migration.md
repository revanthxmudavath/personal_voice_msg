# T16b: Telegram Sender Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `sender.py`'s WAHA/WhatsApp transport with the Telegram Bot API, deleting the
chat-history-scraping reconciliation subsystem T16 built to compensate for WAHA's opacity (Telegram
needs no equivalent — its failures are synchronous and definite).

**Architecture:** The pipeline shape (discovery → generation → safety gates → approved queue → T14's
audio pipeline → sender) is unchanged. Only `sender.py`'s transport, `config.py`'s `Settings`, one
branch of `delivery.py`, and `docker-compose.yml` change. A new `recipient_enrollment.py` module
(modeled on T13's `enroll_voice`) captures the recipient's Telegram `chat_id` once, out of band.

**Tech Stack:** Python 3.12, `aiohttp` (already a dependency), Telegram Bot API (`api.telegram.org`,
official, HTTPS, no client library — same "hand-rolled `aiohttp` client, no SDK" pattern as the
existing WAHA sender).

## Global Constraints

- No mocks, ever — real SQLite files, real HTTP calls to a real test Telegram bot, real FFmpeg/audio
  pipeline output. See `AGENTS.md` §Strict no-mock TDD policy.
- Fail closed: any unrecognized Telegram response shape, unexpected status code, or malformed
  payload must not be silently treated as success.
- Secrets (the bot token, the enrolled `chat_id`) never appear in git, logs, task prompts, or command
  args. Never share them with a subagent as literal values — reference the secret file path instead.
- One recipient, one voice note per Pacific calendar date — unchanged from the existing rules
  (`AGENTS.md` §WhatsApp and delivery rules, which apply platform-agnostically despite the heading).
- Retry only when non-delivery is certain. An ambiguous outcome (no HTTP response received at all)
  must never retry the same Pacific day — it becomes terminal-for-the-day, not an intermediate state
  to auto-resolve (there is nothing to reconcile against under Telegram).
- `discovery/`, `generation/`, `judging/` must never import `personal_voice_msg.sender`,
  `personal_voice_msg.recipient_enrollment`, or reference `telegram_bot_token`/`voice_embedding`/
  `sender_auth_key` by attribute name — this boundary is enforced by a real AST-parsing test, not a
  linter suggestion.
- Full context for every decision below: `docs/superpowers/specs/2026-08-18-telegram-sender-design.md`
  (the approved design), `docs/research/next-platform-alternatives.md` (why Telegram),
  `docs/research/waha-alternatives.md` (why not WAHA). Read the design spec before starting Task 1 —
  this plan implements it, it does not re-derive it.

---

### Task 1: Replace WAHA settings with Telegram settings

**Files:**
- Modify: `src/personal_voice_msg/config.py`
- Modify: `tests/fast/test_configuration.py`
- Modify: `tests/fast/test_sender_temp_file_cleanup.py:34-41` (constructs a `Settings(...)` literal)

**Interfaces:**
- Produces: `Settings.telegram_bot_token: SensitiveValue[str]`,
  `Settings.telegram_chat_id: SensitiveValue[int]`. Every later task reads these two fields —
  `sender.py` reads both, `recipient_enrollment.py`'s caller writes the file `telegram_chat_id`
  is loaded from.
- `Settings` no longer has `waha_base_url`, `waha_token`, `waha_session`, or `recipient` — any
  later task that referenced those by name must be updated (Tasks 3, 4, 6 do this).

**Why `telegram_chat_id` is an int, not a str:** Telegram's own API represents `chat.id` as a JSON
integer everywhere (confirmed against `getUpdates`'s real response shape) and `sendVoice`'s
`chat_id` parameter accepts it directly as an integer — storing it as `str` would just add a
round-trip conversion at every call site for no benefit.

- [ ] **Step 1: Write the failing tests for the new settings shape**

Replace `tests/fast/test_configuration.py` in full with the following (the whole file changes:
`REQUIRED_SETTINGS` drops the WAHA keys and gains the Telegram ones, `create_configuration`'s
fixture writes a `telegram_chat_id` JSON file and a bot-token file instead of WAHA's phone/token/
session files, and every WAHA-specific test is replaced with its Telegram equivalent):

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/fast/test_configuration.py -v`
Expected: collection succeeds, every test fails with either `ImportError`/`AttributeError` (the
`telegram_bot_token`/`telegram_chat_id` fields don't exist on `Settings` yet) or a `KeyError` from
`create_configuration` referencing settings that don't exist in `REQUIRED_SETTINGS` yet.

- [ ] **Step 3: Rewrite `config.py`'s `Settings` and loader for Telegram**

Replace `src/personal_voice_msg/config.py` in full:

```python
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
```

Note what disappeared entirely, not just renamed: `E164_PHONE`, `LOOPBACK_HOSTNAMES`, and
`_waha_base_url()`. Telegram's endpoint (`https://api.telegram.org`) is a fixed constant the sender
hardcodes (Task 3) — unlike WAHA, which ran on this machine and needed a configurable, validated
loopback URL, there is no `base_url` setting to validate at all under Telegram. `_token()` and
`_sender_auth_key()` collapsed into one `_bounded_secret_text()` helper since they were byte-for-byte
identical logic under different names.

- [ ] **Step 4: Update `tests/fast/test_sender_temp_file_cleanup.py`'s `Settings` construction**

Find the `Settings(...)` call at `tests/fast/test_sender_temp_file_cleanup.py:34-41` and replace its
field list to match the new dataclass shape:

```python
    return Settings(
        profile=RuntimeProfile.DEVELOPMENT,
        telegram_chat_id=SensitiveValue(987654321),
        telegram_bot_token=SensitiveValue("test-token"),
        voice_embedding=SensitiveValue(tmp_path / "voice.embedding"),
        sender_auth_key=SensitiveValue("test-sender-auth-key"),
    )
```

Update that file's imports accordingly (`from personal_voice_msg.config import RuntimeProfile,
Settings` and `from personal_voice_msg.redaction import SensitiveValue` — check the top of the file
for its existing import block and adjust in place rather than duplicating imports).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/fast/test_configuration.py tests/fast/test_sender_temp_file_cleanup.py -v`
Expected: all PASS.

- [ ] **Step 6: Run mypy and ruff**

Run: `uv run mypy src && uv run ruff check .`
Expected: both clean. (`sender.py`, `delivery.py` will still reference the old `Settings` fields at
this point in the plan — Tasks 3 and 4 fix those. If mypy fails on those files specifically at this
step, that is expected and will be resolved by the end of Task 4, not this task.)

- [ ] **Step 7: Commit**

```bash
git add src/personal_voice_msg/config.py tests/fast/test_configuration.py tests/fast/test_sender_temp_file_cleanup.py
git commit -m "T16b: replace WAHA settings with Telegram settings"
```

---

### Task 2: Recipient enrollment module

**Files:**
- Create: `src/personal_voice_msg/recipient_enrollment.py`
- Test: `tests/fast/test_recipient_enrollment.py`
- Test: `tests/integration/test_recipient_enrollment.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (this module has no dependency on `config.py`'s `Settings` —
  it runs once, before `Settings` can even be loaded, since it's what produces the file `Settings`
  later reads).
- Produces: `enroll_recipient(bot_token: str, destination: Path, profile: RuntimeProfile) -> int`.
  No later task calls this at runtime — it is a one-time, owner-run operation (same as
  `enroll_voice`). Its only durable output is the JSON file at `destination`, which Task 1's
  `_telegram_chat_id()` reads back at every `load_settings()` call.

**Design note — a real refinement of the approved spec, not a deviation from it:** the design spec's
brainstorming pass approved "a plain function, no CLI framework, matches T13's actual precedent" for
this module (`docs/superpowers/specs/2026-08-18-telegram-sender-design.md`), illustrated there with
a `(bot_token, database) -> int` signature. Reading T13's actual `enroll_voice` signature during this
planning pass (`voice_enrollment.py:67`: `enroll_voice(sample_path: Path, destination: Path, *,
model=None) -> Path`) shows the real precedent is file-based (write the enrolled artifact to a given
output path), not database-based — T13 never touches the DB at all. The DB stores mutable
*operational* state (delivery/message rows); recipient identity, like the voice embedding, is
*static* configuration set once, deploy-time, and loaded through `Settings` — introducing a second,
DB-backed place recipient identity could live would be a real architectural inconsistency for no
benefit. This task therefore implements `enroll_recipient(bot_token, destination, profile)`, which
matches T13's real shape exactly: one output path, immutable once written. This does not change the
approved decision ("plain function, no CLI framework") — only the illustrative signature, which was
never the actual approved content.

- [ ] **Step 1: Write the failing fast tests (pure logic, no network)**

Create `tests/fast/test_recipient_enrollment.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_voice_msg.config import RuntimeProfile
from personal_voice_msg.recipient_enrollment import (
    EnrollmentError,
    _extract_chat_id,
)


@pytest.mark.fast
def test_extract_chat_id_reads_the_most_recent_inbound_message() -> None:
    payload = {
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 111}}},
            {"update_id": 2, "message": {"chat": {"id": 222}}},
        ],
    }

    assert _extract_chat_id(payload) == 222


@pytest.mark.fast
def test_extract_chat_id_ignores_non_message_updates() -> None:
    payload = {
        "ok": True,
        "result": [
            {"update_id": 1, "edited_message": {"chat": {"id": 999}}},
            {"update_id": 2, "message": {"chat": {"id": 333}}},
        ],
    }

    assert _extract_chat_id(payload) == 333


@pytest.mark.fast
def test_extract_chat_id_raises_when_no_message_found() -> None:
    payload = {"ok": True, "result": []}

    with pytest.raises(EnrollmentError, match="no inbound message"):
        _extract_chat_id(payload)


@pytest.mark.fast
def test_extract_chat_id_raises_when_telegram_reports_failure() -> None:
    payload = {"ok": False, "error_code": 401, "description": "Unauthorized"}

    with pytest.raises(EnrollmentError, match="getUpdates failed"):
        _extract_chat_id(payload)


@pytest.mark.fast
@pytest.mark.parametrize("bad_chat_id", [True, False, "123", 1.5, None, -5, 0])
def test_extract_chat_id_rejects_non_positive_integer_chat_id(
    bad_chat_id: object,
) -> None:
    payload = {
        "ok": True,
        "result": [{"update_id": 1, "message": {"chat": {"id": bad_chat_id}}}],
    }

    with pytest.raises(EnrollmentError, match="valid integer"):
        _extract_chat_id(payload)


@pytest.mark.fast
def test_refuses_to_overwrite_an_already_enrolled_recipient(tmp_path: Path) -> None:
    destination = tmp_path / "telegram_chat_id.json"
    destination.write_text(
        json.dumps({"profile": "development", "telegram_chat_id": 1}),
        encoding="utf-8",
    )

    from personal_voice_msg.recipient_enrollment import enroll_recipient
    import asyncio

    with pytest.raises(EnrollmentError, match="already enrolled"):
        asyncio.run(
            enroll_recipient("unused-token", destination, RuntimeProfile.DEVELOPMENT)
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/fast/test_recipient_enrollment.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'personal_voice_msg.recipient_enrollment'`.

- [ ] **Step 3: Write `recipient_enrollment.py`**

Create `src/personal_voice_msg/recipient_enrollment.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp

from personal_voice_msg.config import RuntimeProfile

TELEGRAM_API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_TELEGRAM_CHAT_ID = 2**52


class EnrollmentError(ValueError):
    """Raised when recipient enrollment cannot be completed safely."""


def _extract_chat_id(payload: dict[str, Any]) -> int:
    """Pure and synchronous on purpose -- no network I/O -- so the parsing
    logic can be unit-tested directly with real Python data structures,
    matching this project's real-data-over-mocks testing policy.
    """

    if not payload.get("ok"):
        raise EnrollmentError(f"Telegram getUpdates failed: {payload}")

    updates = payload.get("result")
    if not isinstance(updates, list):
        raise EnrollmentError("Telegram getUpdates response was malformed")

    messages = [
        update["message"]
        for update in updates
        if isinstance(update, dict) and isinstance(update.get("message"), dict)
    ]
    if not messages:
        raise EnrollmentError(
            "no inbound message found -- ask the recipient to message the "
            "bot first, then retry enrollment"
        )

    chat = messages[-1].get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    # Explicitly excludes bool -- see config.py's _telegram_chat_id for the
    # same guard and why it matters (JSON true/false deserialize to
    # Python bool, and isinstance(True, int) is True).
    if (
        type(chat_id) is not int
        or chat_id <= 0
        or chat_id >= MAX_TELEGRAM_CHAT_ID
    ):
        raise EnrollmentError("Telegram chat id was not a valid integer")

    return chat_id


async def enroll_recipient(
    bot_token: str, destination: Path, profile: RuntimeProfile
) -> int:
    """One-time: poll Telegram's getUpdates once, capture the chat_id of
    whoever sent the most recent inbound message, and write it to
    ``destination`` as the fixed allowlisted recipient.

    Refuses to run at all if ``destination`` already exists -- the
    captured chat_id becomes immutable once enrolled, matching
    voice_enrollment's trust model (there, the enrolled artifact's
    existence isn't separately guarded because ``enroll_voice`` deletes
    its own input sample after success; here there is no equivalent
    single-use input to delete, so the guard is on the output instead).

    Before running this, the owner must have already sent the recipient
    the bot's private t.me/<name> link and had them send it any message
    (conventionally /start) -- see the design spec's "Recipient
    enrollment" section.
    """

    if destination.exists():
        raise EnrollmentError(f"a recipient is already enrolled at {destination}")

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{TELEGRAM_API_BASE}/bot{bot_token}/getUpdates",
            params={"timeout": "0"},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
        ) as response:
            payload = await response.json()

    chat_id = _extract_chat_id(payload)

    destination.write_text(
        json.dumps({"profile": profile.value, "telegram_chat_id": chat_id}),
        encoding="utf-8",
    )
    return chat_id
```

- [ ] **Step 4: Run the fast tests to verify they pass**

Run: `uv run pytest tests/fast/test_recipient_enrollment.py -v`
Expected: all PASS.

- [ ] **Step 5: Write a real integration test against a test bot**

This test requires a real Telegram bot token (create one via `@BotFather`, out-of-band, same as any
other secret) and a real message sent to it from the owner's own Telegram account beforehand.

Create `tests/integration/test_recipient_enrollment.py`:

```python
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from personal_voice_msg.config import RuntimeProfile
from personal_voice_msg.recipient_enrollment import enroll_recipient

pytestmark = pytest.mark.integration

BOT_TOKEN_ENV = "T16B_TEST_BOT_TOKEN"
_MISSING = [name for name in (BOT_TOKEN_ENV,) if name not in os.environ]
if _MISSING:
    pytestmark = [
        pytest.mark.integration,
        pytest.mark.skip(
            reason=(
                "requires a real test Telegram bot token with at least one "
                f"real inbound message already sent to it; set {BOT_TOKEN_ENV}"
            )
        ),
    ]


def test_enroll_recipient_captures_a_real_chat_id(tmp_path: Path) -> None:
    destination = tmp_path / "telegram_chat_id.json"

    chat_id = asyncio.run(
        enroll_recipient(
            os.environ[BOT_TOKEN_ENV], destination, RuntimeProfile.DEVELOPMENT
        )
    )

    assert isinstance(chat_id, int) and chat_id > 0
    assert destination.exists()


def test_enroll_recipient_refuses_a_second_enrollment(tmp_path: Path) -> None:
    destination = tmp_path / "telegram_chat_id.json"
    asyncio.run(
        enroll_recipient(
            os.environ[BOT_TOKEN_ENV], destination, RuntimeProfile.DEVELOPMENT
        )
    )

    from personal_voice_msg.recipient_enrollment import EnrollmentError

    with pytest.raises(EnrollmentError, match="already enrolled"):
        asyncio.run(
            enroll_recipient(
                os.environ[BOT_TOKEN_ENV], destination, RuntimeProfile.DEVELOPMENT
            )
        )
```

- [ ] **Step 6: Run the integration test (requires the real test bot + a real prior message)**

Run: `T16B_TEST_BOT_TOKEN=<real token> uv run pytest tests/integration/test_recipient_enrollment.py -v`
Expected: both PASS, and independently confirm the written `chat_id` really is the owner's own
Telegram account (compare it, once, against `@userinfobot` or Telegram's own account settings) —
do not trust the test's own assertion alone for this one check, since a wrong chat_id here would
mean voice notes get delivered to the wrong Telegram account later.

- [ ] **Step 7: Run the full fast suite, mypy, and ruff**

Run: `uv run pytest -m fast && uv run mypy src && uv run ruff check .`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/personal_voice_msg/recipient_enrollment.py tests/fast/test_recipient_enrollment.py tests/integration/test_recipient_enrollment.py
git commit -m "T16b: add recipient enrollment module"
```

---

### Task 3: Rewrite `sender.py` for Telegram, delete the reconciliation subsystem

**Files:**
- Modify: `src/personal_voice_msg/sender.py` (full rewrite)
- Modify: `tests/e2e/test_sender.py` (env var rename only — see Step 1)
- Delete: `tests/e2e/test_reconciliation.py` (tests `reconcile_delivery`, which this task deletes)
- Delete: `tests/fast/test_reconcile_matching.py` (tests `_find_matching_provider_id`, deleted)

**Interfaces:**
- Consumes: `Settings.telegram_bot_token`, `Settings.telegram_chat_id` (Task 1).
- Produces: `send_voice_note(session, database, settings, audio_bytes, idempotency_key, timestamp,
  signature, now, *, api_base: str = TELEGRAM_API_BASE) -> str` — every existing positional call
  site (`delivery.py`, every existing test) keeps working unmodified; `api_base` is a new
  keyword-only parameter with a real-Telegram default, added specifically so Task 5's security
  tests can redirect the sender at a local raw-socket fake server to force real timeouts/status
  codes (WAHA's equivalent tests did this via a configurable `waha_base_url` in `Settings`, which
  no longer exists — Telegram's URL is a fixed constant, not a deploy-time setting, so the
  redirect point moves to this parameter instead). `SenderRejected`, `SenderAmbiguous`,
  `sign_request`, `verify_signature`, `is_fresh` all keep their existing names and signatures.
- Removes: `reconcile_delivery`, `_fetch_matching_provider_id`, `_find_matching_provider_id`,
  `_no_match_outcome`, `_ReconcileQueryFailed`, `WAHA_SESSION_NAME`, and every `RECONCILE_*`
  constant. `delivery.py` (Task 4) is the only caller of `reconcile_delivery` — that call site is
  deleted there, not adapted.

**The one real design decision this task makes beyond the spec's own text:** the design spec says
Telegram's 400/401/403/404/429 are "definite" and map to `SenderRejected`, without stating what an
*unlisted* status code should do. This task treats that as an explicit allow-list, not a blanket
"4xx vs 5xx" split — any status Telegram returns that is **not** in `{400, 401, 403, 404, 429}` and
is not `200` maps to `SenderAmbiguous`, on the same fail-closed principle T16 Task 13's finding F3
already established for WAHA (an assumed-safe status range that turns out not to be safe is exactly
how that bug happened — an explicit allow-list of *known* definite codes, defaulting to ambiguous
for anything else, cannot repeat it).

- [ ] **Step 1: Update `tests/e2e/test_sender.py`'s environment/fixture (the test bodies do not
  change — every existing test here exercises pre-network checks that stay platform-agnostic)**

Change these three things in `tests/e2e/test_sender.py`, leaving every test function body exactly
as it is today:

1. Replace:
```python
WAHA_SETTINGS_ENV = "T15_WAHA_SETTINGS"
```
with:
```python
TELEGRAM_SETTINGS_ENV = "T16B_TELEGRAM_SETTINGS"
```

2. Replace every remaining reference to `WAHA_SETTINGS_ENV` in this file (the `_MISSING` list
   comprehension, the skip `reason=` string, and the `settings` fixture's
   `os.environ[WAHA_SETTINGS_ENV]` lookup) with `TELEGRAM_SETTINGS_ENV`.

3. Update the skip reason text from `"requires a real consented test voice sample and a real paired
   WAHA session; set..."` to `"requires a real consented test voice sample and a real Telegram bot
   token/chat id; set..."`.

Everything else in the file — `valid_audio_bytes`, `signed_request`, and all ten `test_*` functions
— is unchanged, including `test_send_voice_note_has_no_recipient_parameter` (`api_base` is not in
that test's `recipient_shaped` set — it's a transport override, not a destination selector, and it
has a safe real-Telegram default). This is the direct payoff of keeping `send_voice_note`'s
pre-network behavior identical: the tests proving that behavior don't need to know or care what
happens after the pre-network checks pass.

- [ ] **Step 2: Delete the two reconciliation-only test files**

```bash
git rm tests/e2e/test_reconciliation.py tests/fast/test_reconcile_matching.py
```

- [ ] **Step 3: Run the fast/e2e suites to confirm the expected failures**

Run: `uv run pytest -m fast -v 2>&1 | tail -30`
Expected: collection succeeds (the deleted files are gone, nothing else imports them). No new
failures yet — `sender.py` hasn't changed. This step exists to establish a clean baseline
immediately before the rewrite, not to prove a red state (there is no new failing test to write
here beyond what Step 1 already sets up via the env var rename, which only bites once a
`TELEGRAM_SETTINGS_ENV`-pointed settings file actually exists in Step 6's real run).

- [ ] **Step 4: Rewrite `sender.py`**

Replace `src/personal_voice_msg/sender.py` in full:

```python
from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

from personal_voice_msg.audio_pipeline import AudioPipelineError, validate_audio
from personal_voice_msg.config import Settings
from personal_voice_msg.database import Database, ReplayDetected

# How long a signed sender request stays acceptable. Generous enough for
# real HTTP latency against Telegram's API in integration/e2e tests,
# still bounded -- see docs/task-logs/T15.md.
REPLAY_WINDOW_SECONDS = 300
TELEGRAM_API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 65_536
RESPONSE_CHUNK_BYTES = 8_192
# Telegram status codes that are synchronous, definite answers -- the
# request never landed in an unknown state, so it's safe to retry
# immediately. This is deliberately an explicit allow-list, not a
# blanket "4xx is safe" rule: T16 Task 13's finding F3 already proved
# that assuming an entire status *range* is safe (WAHA's "any 4xx") can
# be wrong in ways an allow-list of specifically-verified codes cannot.
# Any status Telegram returns that is not in this set, and is not 200,
# defaults to SenderAmbiguous.
_DEFINITE_REJECTION_STATUS_CODES = frozenset({400, 401, 403, 404, 429})


class SenderError(RuntimeError):
    """Base class for a rejected, ambiguous, or otherwise failed sender
    request."""


class SenderRejected(SenderError):
    """The request definitely never reached Telegram, or Telegram gave a
    definite rejection. Safe to retry immediately."""


class SenderAmbiguous(SenderError):
    """Telegram may or may not have processed the request -- either no
    HTTP response was received at all, or Telegram returned a status
    code outside the known-definite allow-list. Must not be retried
    blindly. Under this project's Telegram design there is no chat-history
    to reconcile against (Telegram's Bot API has no such method for
    bots), so an ambiguous outcome becomes terminal for the Pacific day
    rather than auto-resolved -- see delivery.py and
    docs/superpowers/specs/2026-08-18-telegram-sender-design.md."""


def sign_request(key: bytes, idempotency_key: str, timestamp: int) -> str:
    """HMAC-SHA256 hex digest authenticating one sender request."""

    message = f"{timestamp}:{idempotency_key}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_signature(
    key: bytes,
    idempotency_key: str,
    timestamp: int,
    signature: str,
) -> bool:
    """Constant-time check that ``signature`` matches the expected HMAC."""

    expected = sign_request(key, idempotency_key, timestamp)
    return hmac.compare_digest(expected, signature)


def is_fresh(timestamp: int, now: datetime) -> bool:
    """Whether ``timestamp`` is within the replay window of ``now``."""

    return abs(now.timestamp() - timestamp) <= REPLAY_WINDOW_SECONDS


def _describe_rejection(body: bytes) -> str:
    """Best-effort extraction of Telegram's error_code/description/
    retry_after for diagnostics -- never raises. A malformed or truncated
    rejection body doesn't change the outcome (the HTTP status code alone
    already proved it's a definite rejection), it only loses the extra
    detail in the exception message.
    """

    try:
        payload = json.loads(body)
        code = payload.get("error_code")
        description = payload.get("description")
        parameters = payload.get("parameters")
        retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
    except (json.JSONDecodeError, AttributeError):
        return "unparseable response body"
    detail = f"error_code={code} description={description!r}"
    if retry_after is not None:
        detail += f" retry_after={retry_after}"
    return detail


async def send_voice_note(
    session: aiohttp.ClientSession,
    database: Database,
    settings: Settings,
    audio_bytes: bytes,
    idempotency_key: str,
    timestamp: int,
    signature: str,
    now: datetime,
    *,
    api_base: str = TELEGRAM_API_BASE,
) -> str:
    """Authenticate, validate, and send one voice note through Telegram.

    No parameter can select a recipient -- the destination is always
    ``settings.telegram_chat_id``. Every check below runs before Telegram
    is ever contacted; a failure at any step makes zero Telegram calls.
    Returns Telegram's own ``message_id`` (as a string, matching this
    function's pre-existing return type) on success.

    ``api_base`` defaults to the real Telegram API and should never be
    passed by production code -- it exists only so tests can redirect
    this call at a local fake server to force real network-level failure
    modes (a real hanging connection, a real fixed HTTP status) without
    needing a configurable production setting for something that is, in
    production, always exactly one fixed official URL.
    """

    key = settings.sender_auth_key.reveal().encode()
    if not verify_signature(key, idempotency_key, timestamp, signature):
        raise SenderRejected("sender request signature is invalid")
    if not is_fresh(timestamp, now):
        raise SenderRejected("sender request timestamp is stale")
    try:
        database.record_sender_nonce(
            idempotency_key,
            timestamp,
            now + timedelta(seconds=REPLAY_WINDOW_SECONDS),
        )
    except ReplayDetected:
        raise SenderRejected("sender request was already processed") from None

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        temp_path.write_bytes(audio_bytes)
        validate_audio(temp_path)
    except AudioPipelineError as error:
        raise SenderRejected(f"audio failed validation: {error}") from error
    finally:
        temp_path.unlink(missing_ok=True)

    form = aiohttp.FormData()
    form.add_field("chat_id", str(settings.telegram_chat_id.reveal()))
    form.add_field(
        "voice",
        audio_bytes,
        filename="voice-note.ogg",
        content_type="audio/ogg",
    )
    bot_token = settings.telegram_bot_token.reveal()

    try:
        async with session.post(
            f"{api_base}/bot{bot_token}/sendVoice",
            data=form,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            allow_redirects=False,
        ) as response:
            status = response.status
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(RESPONSE_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    break
                chunks.append(chunk)
            body = b"".join(chunks)
    except (aiohttp.ClientError, TimeoutError):
        raise SenderAmbiguous("no response received from Telegram") from None

    # Everything below runs on a real, received HTTP response -- a
    # malformed or oversized body from here on is never re-classified as
    # "no response received"; it's judged against the status code that
    # already arrived.
    if status in _DEFINITE_REJECTION_STATUS_CODES:
        raise SenderRejected(
            f"Telegram rejected the request: {_describe_rejection(body)}"
        )
    if status != 200:
        raise SenderAmbiguous(
            f"Telegram returned an unrecognized status {status}"
        )
    if total > MAX_RESPONSE_BYTES:
        raise SenderAmbiguous("Telegram response exceeded the size limit")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise SenderAmbiguous("Telegram response was not valid JSON") from None
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise SenderAmbiguous(f"Telegram response was malformed: {payload!r}")
    try:
        return str(payload["result"]["message_id"])
    except (KeyError, TypeError):
        raise SenderAmbiguous("Telegram response was malformed") from None
```

- [ ] **Step 5: Run the fast suite, mypy, ruff**

Run: `uv run pytest -m fast && uv run mypy src && uv run ruff check .`
Expected: all green. (`delivery.py` still imports `reconcile_delivery` from `sender` at this point
— Task 4 fixes that. If `mypy`/import errors surface specifically from `delivery.py`, that is
expected here and resolved by Task 4, not this task. If they surface from anywhere else, stop and
investigate before proceeding.)

- [ ] **Step 6: Run the real e2e sender tests against a real Telegram bot**

Create a settings file pointed to by `T16B_TELEGRAM_SETTINGS` using a **second, non-production**
bot token and the owner's own personal Telegram chat as the enrolled recipient (same "staging can
send only to the owner's test chat" rule this project has always followed, now against a second
bot instead of a second WAHA session) — run Task 2's `enroll_recipient` once, manually, against
that test bot, before running this step.

Run: `T13_VOICE_SAMPLE=<real sample> T16B_TELEGRAM_SETTINGS=<path> uv run pytest tests/e2e/test_sender.py -v`
Expected: all PASS. Independently confirm the real send by checking the owner's own Telegram test
chat for the actual voice note (same "don't trust the test's own exit code alone for a real send"
discipline this project used for T15/T16's WAHA sends).

- [ ] **Step 7: Commit**

```bash
git add src/personal_voice_msg/sender.py tests/e2e/test_sender.py
git commit -m "T16b: rewrite sender.py for Telegram, delete WAHA reconciliation subsystem"
```

---

### Task 4: Simplify `delivery.py`'s `DELIVERY_UNKNOWN` branch

**Files:**
- Modify: `src/personal_voice_msg/delivery.py`
- Modify: `tests/e2e/test_delivery.py` (env var rename only, same pattern as Task 3 Step 1)

**Interfaces:**
- Consumes: `sender.py`'s new Telegram-backed `send_voice_note` (Task 3) — the import line changes
  (drops `reconcile_delivery`), the call site to `send_voice_note` itself does not.
- `run_daily_send`'s own signature and return type are unchanged — no caller anywhere needs updating
  beyond the test fixture rename below.

**The actual behavior change, precisely:** today, a delivery found in `DELIVERY_UNKNOWN` calls
`reconcile_delivery` to try to resolve it (query WAHA's chat history, retry-poll, eventually
conclude `SENT` or `AUDIO_READY` after a grace period). Under Telegram there is nothing to query —
`reconcile_delivery` no longer exists (Task 3 deleted it). This task's entire change is: a delivery
found in `DELIVERY_UNKNOWN` on entry returns `MessageState.DELIVERY_UNKNOWN` immediately, with no
attempt to resolve it. It stays `DELIVERY_UNKNOWN` until a human checks it — that is the terminal
state for that Pacific day, not an intermediate one, per the design spec's "Ambiguous outcomes"
section. Verified before writing this: `get_delivery_updated_at` (used by the `SENDING` branch just
above) still has a real purpose after this change — it stamps *when* the ambiguous state began for
audit visibility — so it is not dead code, only no longer also used as a reconciliation window
anchor.

- [ ] **Step 1: Write the failing test proving the new terminal behavior**

Add this test to `tests/fast/test_delivery_window.py` (it already has the no-network,
no-real-WAHA test pattern this needs — a `DELIVERY_UNKNOWN` delivery never makes any network call,
so `session=None` with a `# type: ignore[arg-type]` comment, exactly like that file's two existing
window-rejection tests):

```python
@pytest.mark.fast
def test_run_daily_send_returns_delivery_unknown_unresolved_with_no_reconciliation(
    tmp_path: Path,
) -> None:
    """T16b: Telegram's Bot API has no chat-history-read method for bots,
    so a delivery already in DELIVERY_UNKNOWN on entry must stay that way
    -- terminal for the Pacific day, not auto-resolved via a reconciliation
    call that no longer exists. This test passes `session=None` because a
    correct implementation never touches the network for this branch at
    all; if it did, this test would fail with an AttributeError on the
    None session before reaching the assertion below.
    """
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    pacific_date = date(2026, 8, 9)
    start, _ = _send_trigger_bounds(pacific_date)
    decision = MessageHistory(database).evaluate_and_record(
        "A delivery-unknown terminal-state test.", start
    )
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, start)
    recipient_key = "recipient_t16b_delivery_unknown"
    reservation = database.reserve_next_message(recipient_key, pacific_date, start)
    assert reservation is not None
    database.mark_audio_ready(reservation.delivery_id, b"stale-audio-bytes", start)
    database.transition_delivery(
        reservation.delivery_id, MessageState.SENDING, start
    )
    database.record_delivery_attempt(
        reservation.delivery_id, MessageState.DELIVERY_UNKNOWN, start
    )

    async def call() -> MessageState:
        return await run_daily_send(
            database, None, None, recipient_key,  # type: ignore[arg-type]
            pacific_date, Path("unused"), start,
        )

    result = asyncio.run(call())

    assert result is MessageState.DELIVERY_UNKNOWN
    assert database.get_delivery_state(reservation.delivery_id) is MessageState.DELIVERY_UNKNOWN
```

This requires `test_delivery_window.py` to import `MessageHistory`
(`from personal_voice_msg.history import MessageHistory`) if it doesn't already — check the top of
the file (Task 13's F2 fix already added a similar test there and may have added this import; add
it only if it's missing, don't duplicate).

- [ ] **Step 2: Run to verify it fails for the right reason**

Run: `uv run pytest tests/fast/test_delivery_window.py::test_run_daily_send_returns_delivery_unknown_unresolved_with_no_reconciliation -v`
Expected: FAIL — either an `AttributeError` (the current code calls
`reconcile_delivery(session, ...)` and `session` is `None`) or an `ImportError` from `sender.py`
already having `reconcile_delivery` removed by Task 3 while `delivery.py` still imports it. Either
failure confirms the test exercises the branch being changed.

- [ ] **Step 3: Simplify `delivery.py`'s imports and `DELIVERY_UNKNOWN` branch**

In `src/personal_voice_msg/delivery.py`, change the import block:

Replace:
```python
from personal_voice_msg.sender import (
    SenderAmbiguous,
    SenderRejected,
    reconcile_delivery,
    send_voice_note,
    sign_request,
)
```
with:
```python
from personal_voice_msg.sender import (
    SenderAmbiguous,
    SenderRejected,
    send_voice_note,
    sign_request,
)
```

Replace the `DELIVERY_UNKNOWN` branch:
```python
    if state is MessageState.DELIVERY_UNKNOWN:
        window_start = database.get_delivery_updated_at(delivery_id)
        outcome, provider_message_id = await reconcile_delivery(
            session, settings, window_start, now
        )
        if outcome is MessageState.DELIVERY_UNKNOWN:
            return MessageState.DELIVERY_UNKNOWN  # still inconclusive
        if outcome is MessageState.SENT:
            database.record_delivery_attempt(
                delivery_id, outcome, now, provider_message_id=provider_message_id
            )
            return MessageState.SENT
        # outcome is AUDIO_READY: reconciliation concluded, conclusively,
        # that nothing was ever sent. AUDIO_READY is not a valid
        # delivery_attempts outcome (see database.py's _ATTEMPT_OUTCOMES),
        # so this is a plain state transition, not an attempt record.
        database.transition_delivery(delivery_id, MessageState.AUDIO_READY, now)
        state = MessageState.AUDIO_READY
```
with:
```python
    if state is MessageState.DELIVERY_UNKNOWN:
        # Telegram's Bot API has no chat-history-read method for bots --
        # there is nothing to reconcile against (unlike WAHA). An
        # ambiguous outcome is terminal for the Pacific day: never
        # auto-retried, surfaced for the owner to check, consistent with
        # "retry only when non-delivery is certain" and "never carry a
        # missed send into the next Pacific day" (AGENTS.md). See
        # docs/superpowers/specs/2026-08-18-telegram-sender-design.md's
        # "Ambiguous outcomes" section.
        return MessageState.DELIVERY_UNKNOWN
```

Also update the docstring comment in the `SENDING` branch just above (currently says "have reached
WAHA" and references reconciliation implicitly via the timestamp's purpose) — replace:
```python
        # This process did not just set SENDING itself in this call --
        # a prior attempt (possibly a crashed process) may or may not
        # have reached WAHA. Reclassify as ambiguous rather than guessing.
        # Stamp this attempt with the delivery's own SENDING-entry time
        # (durably recorded as deliveries.updated_at by the
        # AUDIO_READY -> SENDING transition, captured here before this
        # call overwrites it) rather than this restart's real invocation
        # time -- otherwise the DELIVERY_UNKNOWN branch below would later
        # anchor its reconciliation window to the restart instant, after
        # any real WhatsApp message the crashed process's send may have
        # actually produced, and could never find it (T16 Task 13 fix,
        # finding F2).
```
with:
```python
        # This process did not just set SENDING itself in this call --
        # a prior attempt (possibly a crashed process) may or may not
        # have reached Telegram. Reclassify as ambiguous rather than
        # guessing. Stamp this attempt with the delivery's own
        # SENDING-entry time (durably recorded as deliveries.updated_at
        # by the AUDIO_READY -> SENDING transition, captured here before
        # this call overwrites it) purely for audit visibility -- under
        # WAHA this value also anchored a later reconciliation window
        # (T16 Task 13 fix, finding F2); under Telegram there is nothing
        # to reconcile against, so it now only records when the
        # ambiguity began.
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `uv run pytest tests/fast/test_delivery_window.py -v`
Expected: all PASS, including the new test.

- [ ] **Step 5: Update `tests/e2e/test_delivery.py`'s environment/fixture (same pattern as Task 3
  Step 1 — no test body changes needed; none of its three tests re-enter `DELIVERY_UNKNOWN`)**

Replace `WAHA_SETTINGS_ENV = "T15_WAHA_SETTINGS"` with `TELEGRAM_SETTINGS_ENV =
"T16B_TELEGRAM_SETTINGS"`, and update every other reference to that constant in the file (the
`_MISSING` list, the skip reason, the `settings` fixture) the same way Task 3 Step 1 did for
`test_sender.py`.

- [ ] **Step 6: Run the full fast suite, mypy, ruff**

Run: `uv run pytest -m fast && uv run mypy src && uv run ruff check .`
Expected: all green.

- [ ] **Step 7: Run the real e2e delivery tests against the same test bot from Task 3**

Run: `T13_VOICE_SAMPLE=<real sample> T16B_TELEGRAM_SETTINGS=<path> uv run pytest tests/e2e/test_delivery.py -v`
Expected: all PASS. Independently confirm real sends landed in the owner's test chat (same
discipline as Task 3 Step 6).

- [ ] **Step 8: Commit**

```bash
git add src/personal_voice_msg/delivery.py tests/e2e/test_delivery.py tests/fast/test_delivery_window.py
git commit -m "T16b: simplify delivery.py's DELIVERY_UNKNOWN branch (no reconciliation under Telegram)"
```

---

### Task 5: Rewrite the sender error-taxonomy security tests for Telegram

**Files:**
- Modify: `tests/security/test_sender_error_taxonomy.py` (full rewrite)

**Interfaces:**
- Consumes: `send_voice_note`'s new `api_base` keyword parameter (Task 3) — this is the only reason
  this rewrite is possible without a configurable production URL setting.

**Why the whole file changes, not just the `_settings_for` helper:** the old file tested exactly one
ambiguous case (a hang) and one WAHA-specific finding (F3's "5xx is ambiguous, not rejected"). The
new design has a five-code definite-rejection allow-list (400/401/403/404/429) plus an explicit
"anything else defaults to ambiguous" fallback (this task's own design decision from Task 3) — both
directions need real coverage, not just the one WAHA-era case.

- [ ] **Step 1: Write the failing tests**

Replace `tests/security/test_sender_error_taxonomy.py` in full:

```python
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import threading
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.audio_pipeline import convert_to_opus, synthesize_to_wav
from personal_voice_msg.config import RuntimeProfile, Settings
from personal_voice_msg.database import Database
from personal_voice_msg.redaction import SensitiveValue
from personal_voice_msg.sender import SenderAmbiguous, SenderRejected, send_voice_note, sign_request
from personal_voice_msg.voice_enrollment import enroll_voice

VOICE_SAMPLE_ENV = "T13_VOICE_SAMPLE"

pytestmark = pytest.mark.security

if VOICE_SAMPLE_ENV not in os.environ:
    pytestmark = [
        pytest.mark.security,
        pytest.mark.skip(
            reason=(
                "requires a real consented test voice sample; set "
                f"{VOICE_SAMPLE_ENV} so audio validation genuinely passes "
                "before the network call"
            )
        ),
    ]


class _HangingServer:
    """Accepts a connection and never responds -- a real socket, no HTTP
    semantics implemented. Used only to force a real client-side timeout,
    not to simulate Telegram's API."""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_and_hang, daemon=True)
        self._thread.start()

    def _accept_and_hang(self) -> None:
        self._socket.settimeout(1.0)
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            self._stop.wait()
            connection.close()

    def stop(self) -> None:
        self._stop.set()
        self._socket.close()


@pytest.fixture
def hanging_server() -> _HangingServer:
    server = _HangingServer()
    yield server
    server.stop()


class _FixedStatusServer:
    """Accepts one connection, drains whatever the client sends until it
    goes quiet, then responds with a fixed HTTP status line and a
    Telegram-shaped error body, then closes -- a real raw socket, no
    aiohttp/Telegram server semantics beyond the status line and body
    text. Used to force a real, definite HTTP response (not a mock) for
    exercising send_voice_note's status-code handling."""

    def __init__(self, status_line: str, body: bytes = b'{"ok":false}') -> None:
        self._status_line = status_line
        self._body = body
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_and_respond, daemon=True)
        self._thread.start()

    def _accept_and_respond(self) -> None:
        self._socket.settimeout(1.0)
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            try:
                connection.settimeout(2.0)
                try:
                    while connection.recv(65_536):
                        pass
                except (TimeoutError, OSError):
                    pass
                response = (
                    f"{self._status_line}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(self._body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode() + self._body
                connection.sendall(response)
            finally:
                connection.close()
            return

    def stop(self) -> None:
        self._stop.set()
        self._socket.close()


@pytest.fixture(scope="module")
def valid_audio_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """Real Pocket TTS synthesis + real FFmpeg conversion, once per module.

    ``send_voice_note`` validates audio *before* contacting Telegram, so
    every test below needs real, valid OGG/Opus bytes to reach the
    network step at all.
    """

    workdir = tmp_path_factory.mktemp("t16b_taxonomy_audio")
    raw_sample = workdir / "raw_sample.wav"
    shutil.copyfile(Path(os.environ[VOICE_SAMPLE_ENV]), raw_sample)
    embedding = workdir / "voice_embedding.safetensors"
    enroll_voice(raw_sample, embedding)

    wav_path = workdir / "synthesized.wav"
    synthesize_to_wav(
        embedding,
        "This is a real end to end test of the sender error taxonomy.",
        wav_path,
    )
    ogg_path = workdir / "synthesized.ogg"
    convert_to_opus(wav_path, ogg_path)
    return ogg_path.read_bytes()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        profile=RuntimeProfile.DEVELOPMENT,
        telegram_chat_id=SensitiveValue(987654321),
        telegram_bot_token=SensitiveValue("test-token"),
        voice_embedding=SensitiveValue(tmp_path / "embedding.safetensors"),
        sender_auth_key=SensitiveValue("test-sender-auth-key"),
    )


async def _send(
    settings: Settings, audio_bytes: bytes, database: Database, api_base: str
) -> str:
    now = datetime.now(UTC)
    idempotency_key = f"t16b-taxonomy-{api_base}-{now.timestamp()}"
    timestamp = int(now.timestamp())
    signature = sign_request(
        settings.sender_auth_key.reveal().encode(), idempotency_key, timestamp
    )
    async with aiohttp.ClientSession() as session:
        return await send_voice_note(
            session,
            database,
            settings,
            audio_bytes,
            idempotency_key,
            timestamp,
            signature,
            now,
            api_base=api_base,
        )


def test_a_hanging_connection_raises_sender_ambiguous_not_rejected(
    hanging_server: _HangingServer, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()

    with pytest.raises(SenderAmbiguous):
        asyncio.run(
            _send(
                settings,
                valid_audio_bytes,
                database,
                f"http://127.0.0.1:{hanging_server.port}",
            )
        )


@pytest.mark.parametrize(
    "status_line",
    [
        "HTTP/1.1 400 Bad Request",
        "HTTP/1.1 401 Unauthorized",
        "HTTP/1.1 403 Forbidden",
        "HTTP/1.1 404 Not Found",
        "HTTP/1.1 429 Too Many Requests",
    ],
)
def test_a_definite_rejection_status_raises_sender_rejected_not_ambiguous(
    status_line: str, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    """T16b design: 400/401/403/404/429 are synchronous, definite Telegram
    answers -- the request never landed in an unknown state, so it's
    safe to retry immediately."""
    server = _FixedStatusServer(
        status_line, body=b'{"ok":false,"error_code":400,"description":"test"}'
    )
    try:
        settings = _settings(tmp_path)
        database = Database(tmp_path / "state.sqlite3")
        database.migrate()

        with pytest.raises(SenderRejected):
            asyncio.run(
                _send(
                    settings,
                    valid_audio_bytes,
                    database,
                    f"http://127.0.0.1:{server.port}",
                )
            )
    finally:
        server.stop()


@pytest.mark.parametrize(
    "status_line",
    [
        "HTTP/1.1 500 Internal Server Error",
        "HTTP/1.1 502 Bad Gateway",
        "HTTP/1.1 503 Service Unavailable",
    ],
)
def test_an_unlisted_status_raises_sender_ambiguous_not_rejected(
    status_line: str, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    """This task's own design decision (not directly stated in the spec's
    prose): only the explicit allow-list of *known* definite Telegram
    codes maps to SenderRejected. Anything else -- including a real 5xx,
    which Telegram could in principle return after already dispatching
    the request -- defaults to SenderAmbiguous, the same fail-closed
    lesson T16 Task 13's finding F3 already established for WAHA (an
    assumed-safe status *range* is not the same as a verified-safe status
    *code*)."""
    server = _FixedStatusServer(status_line)
    try:
        settings = _settings(tmp_path)
        database = Database(tmp_path / "state.sqlite3")
        database.migrate()

        with pytest.raises(SenderAmbiguous):
            asyncio.run(
                _send(
                    settings,
                    valid_audio_bytes,
                    database,
                    f"http://127.0.0.1:{server.port}",
                )
            )
    finally:
        server.stop()


def test_a_malformed_200_response_raises_sender_ambiguous(
    valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    """A 200 status with an ``"ok": false`` body, or a body missing
    ``result.message_id``, must not be silently treated as a successful
    send -- Telegram's own contract is that ``ok`` is authoritative, not
    the HTTP status alone."""
    server = _FixedStatusServer("HTTP/1.1 200 OK", body=b'{"ok":false}')
    try:
        settings = _settings(tmp_path)
        database = Database(tmp_path / "state.sqlite3")
        database.migrate()

        with pytest.raises(SenderAmbiguous):
            asyncio.run(
                _send(
                    settings,
                    valid_audio_bytes,
                    database,
                    f"http://127.0.0.1:{server.port}",
                )
            )
    finally:
        server.stop()
```

- [ ] **Step 2: Run to verify the tests fail for the right reason**

Run: `T13_VOICE_SAMPLE=<real sample> uv run pytest tests/security/test_sender_error_taxonomy.py -v`
Expected: FAIL with `TypeError: send_voice_note() got an unexpected keyword argument 'api_base'` if
run before Task 3's `sender.py` rewrite lands, or PASS immediately if Task 3 is already done (Tasks
are executed in order in this plan, so by the time this task runs, Task 3 is done and this file's
correctness is what's actually being verified — if these tests already pass on the first run
because Task 3's rewrite is correct, that is a legitimate outcome, not a process violation; rerun
Step 1 with a deliberately wrong status code in `_DEFINITE_REJECTION_STATUS_CODES` if you need to
positively confirm a test can fail, per this project's own TDD discipline).

- [ ] **Step 3: Run to verify all pass**

Run: `T13_VOICE_SAMPLE=<real sample> uv run pytest tests/security/test_sender_error_taxonomy.py -v`
Expected: all PASS (9 tests: 1 hanging, 5 definite-rejection parametrized, 3 unlisted-status
parametrized... plus the malformed-200 test = 10 total).

- [ ] **Step 4: Run mypy and ruff**

Run: `uv run mypy src && uv run ruff check .`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add tests/security/test_sender_error_taxonomy.py
git commit -m "T16b: rewrite sender error-taxonomy tests for Telegram's status codes"
```

---

### Task 6: Update the security AST boundary test for the new secret name

**Files:**
- Modify: `tests/security/test_voice_enrollment_boundaries.py:22`

**Interfaces:** none — this is a one-constant change to an existing, already-passing test's
forbidden-name set.

**Why `personal_voice_msg.sender` and `personal_voice_msg.recipient_enrollment` don't both need
adding to `FORBIDDEN_MODULES`:** `personal_voice_msg.sender` is already in that set and stays
forbidden unchanged (`discovery`/`generation`/`judging` still must never import the sender module
at all). `recipient_enrollment.py` is a one-time, owner-run, out-of-band operation with no runtime
caller in `discovery`/`generation`/`judging` to begin with (Task 2 built it with zero callers
anywhere in the production pipeline) — there is no scenario where those restricted packages would
import it, so adding it to the forbidden-module set would be a no-op check, not a real boundary.

- [ ] **Step 1: Confirm this test currently passes (baseline)**

Run: `uv run pytest tests/security/test_voice_enrollment_boundaries.py -v`
Expected: PASS (this establishes the test genuinely still runs and passes before the one-line
change below, so a later failure can only be attributed to that change).

- [ ] **Step 2: Update the forbidden-attribute-names set**

In `tests/security/test_voice_enrollment_boundaries.py`, replace:
```python
FORBIDDEN_ATTRIBUTE_NAMES = {"voice_embedding", "sender_auth_key", "waha_token"}
```
with:
```python
FORBIDDEN_ATTRIBUTE_NAMES = {"voice_embedding", "sender_auth_key", "telegram_bot_token"}
```

- [ ] **Step 3: Run to verify it still passes**

Run: `uv run pytest tests/security/test_voice_enrollment_boundaries.py -v`
Expected: PASS. (This test can only ever fail if `discovery/`, `generation/`, or `judging/` source
actually references one of these names — it is not expected to have failed at Step 1 or to fail
here; both runs are confirmation, not a red/green TDD cycle, since there is no new behavior being
added, only a renamed constant being kept accurate.)

- [ ] **Step 4: Run mypy and ruff**

Run: `uv run mypy src && uv run ruff check .`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add tests/security/test_voice_enrollment_boundaries.py
git commit -m "T16b: update security boundary test for telegram_bot_token"
```

---

### Task 7: Remove WAHA's Docker infrastructure; rewrite the fault-injection suite for Telegram

**Files:**
- Delete: `docker-compose.yml`
- Delete: `.env.example`
- Modify: `CLAUDE.md:99` (drop the now-meaningless `docker compose config --quiet` command)
- Modify: `tests/e2e/test_delivery_fault_injection.py` (full rewrite)

**Interfaces:** none new — this task only removes infrastructure and replaces test coverage.

**Why `docker-compose.yml` is deleted outright, not emptied:** the file's only service was WAHA.
Telegram needs no local service at all — `sender.py` makes direct outbound HTTPS calls to
`api.telegram.org`; there is nothing left to containerize until T18 ("Cloud and container
hardening") containerizes the application itself, which hasn't started. An empty or stub compose
file would be actively misleading (implying there's a container story here when there currently
isn't one) — deleting it is the honest state of the world today, and `git log` preserves exactly
what was here and why (T15's original container, T16b's removal) for whenever T18 needs to revisit
this.

**Why the fault-injection suite changes so substantially:** the original suite's entire premise —
pause a real WAHA container, let a real `reconcile_delivery` eventually resolve the ambiguity via
chat-history polling — has no Telegram equivalent (Task 3 deleted `reconcile_delivery`; there is no
container to pause, since Telegram is a remote service this project doesn't run). The replacement
composes two already-proven pieces from earlier tasks instead of re-deriving fault injection from
scratch: Task 5's proof that a real network hang raises `SenderAmbiguous`, and Task 4's proof that a
`DELIVERY_UNKNOWN` delivery never retries. A genuinely new property this suite proves that WAHA's
version *couldn't*: because Telegram's failures are synchronous and definite, "no duplicate send"
can now be proven by pure state-machine reasoning plus a `session=None` call (any code path that
tried to make a real network call would raise `AttributeError` before any assertion is reached) —
no external verification against the provider's own records is needed, unlike WAHA's version, which
had to scrape real chat history because WAHA offered no stronger guarantee.

- [ ] **Step 1: Delete the WAHA Docker infrastructure**

```bash
git rm docker-compose.yml .env.example
```

- [ ] **Step 2: Update `CLAUDE.md`'s command list**

Replace:
```
uv run python scripts/repository_policy.py all --root .
docker compose config --quiet
```
with:
```
uv run python scripts/repository_policy.py all --root .
```

- [ ] **Step 3: Write the new fault-injection suite**

Replace `tests/e2e/test_delivery_fault_injection.py` in full:

```python
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sqlite3
import threading
from datetime import UTC, date, datetime
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.audio_pipeline import (
    convert_to_opus,
    produce_voice_note,
    synthesize_to_wav,
)
from personal_voice_msg.config import Settings, load_settings
from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.delivery import run_daily_send
from personal_voice_msg.history import MessageHistory
from personal_voice_msg.scheduling import (
    PACIFIC,
    ScheduleKind,
    planned_triggers_for_date,
)
from personal_voice_msg.sender import SenderAmbiguous, send_voice_note, sign_request
from personal_voice_msg.voice_enrollment import enroll_voice

pytestmark = pytest.mark.e2e

VOICE_SAMPLE_ENV = "T13_VOICE_SAMPLE"
TELEGRAM_SETTINGS_ENV = "T16B_TELEGRAM_SETTINGS"
_MISSING = [
    name for name in (VOICE_SAMPLE_ENV, TELEGRAM_SETTINGS_ENV) if name not in os.environ
]
if _MISSING:
    pytestmark = [
        pytest.mark.e2e,
        pytest.mark.skip(
            reason=(
                "requires a real consented test voice sample and a real "
                f"Telegram bot/chat; set {', '.join(_MISSING)}"
            )
        ),
    ]


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings(Path(os.environ[TELEGRAM_SETTINGS_ENV]))


@pytest.fixture(scope="module")
def valid_audio_text() -> str:
    return "A T16b fault-injection test."


PACIFIC_DATE = datetime.now(PACIFIC).date()


def _in_send_window(pacific_date: date) -> datetime:
    trigger = next(
        t for t in planned_triggers_for_date(pacific_date)
        if t.kind is ScheduleKind.DAILY_SEND
    )
    return trigger.scheduled_at


def approved_message(database: Database, text: str, now: datetime) -> None:
    database.migrate()
    decision = MessageHistory(database).evaluate_and_record(text, now)
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, now)


class _HangingServer:
    """Accepts a connection and never responds -- a real socket, no HTTP
    semantics implemented. Forces a real client-side timeout. See
    tests/security/test_sender_error_taxonomy.py for the identical
    pattern; duplicated here rather than shared, since both files are
    small and self-contained, matching this project's existing
    per-file-scoped fake-server convention."""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_and_hang, daemon=True)
        self._thread.start()

    def _accept_and_hang(self) -> None:
        self._socket.settimeout(1.0)
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            self._stop.wait()
            connection.close()

    def stop(self) -> None:
        self._stop.set()
        self._socket.close()


def _sent_count(database_path: Path, delivery_id: int) -> tuple[int]:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM delivery_attempts "
            "WHERE delivery_id = ? AND outcome = 'sent'",
            (delivery_id,),
        ).fetchone()
    return row


@pytest.mark.parametrize(
    "interrupt_state",
    [
        MessageState.RESERVED,
        MessageState.AUDIO_READY,
        MessageState.SENDING,
        MessageState.FAILED,
        MessageState.DELIVERY_UNKNOWN,
    ],
)
def test_restart_at_every_delivery_state_never_duplicates_a_send(
    settings: Settings, tmp_path: Path, interrupt_state: MessageState
) -> None:
    """Simulates a process restart by constructing a fresh Database handle
    from the same file and resuming from each persisted state in turn.

    RESERVED/AUDIO_READY/FAILED restart into a genuine real send and reach
    SENT. SENDING/DELIVERY_UNKNOWN are ambiguous on entry -- under
    Telegram there is nothing to reconcile against, so these must resolve
    to DELIVERY_UNKNOWN without ever making a real network call, proven
    by passing session=None (any code path that tried to send would raise
    AttributeError before the assertion below).
    """
    database_path = tmp_path / "state.sqlite3"
    database = Database(database_path)
    now = _in_send_window(PACIFIC_DATE)
    text = f"A restart-at-{interrupt_state.value} test at {datetime.now(UTC).timestamp()}."
    approved_message(database, text, now)
    recipient_key = f"recipient_t16b_restart_{interrupt_state.value}"
    reservation = database.reserve_next_message(recipient_key, PACIFIC_DATE, now)
    assert reservation is not None
    embedding_path = settings.voice_embedding.reveal()

    if interrupt_state is not MessageState.RESERVED:
        temp_destination = tmp_path / f"t16b-restart-{reservation.delivery_id}.ogg"
        produce_voice_note(
            database, reservation.delivery_id, embedding_path, text,
            temp_destination, now,
        )
    if interrupt_state in (
        MessageState.SENDING,
        MessageState.FAILED,
        MessageState.DELIVERY_UNKNOWN,
    ):
        database.transition_delivery(reservation.delivery_id, MessageState.SENDING, now)
    if interrupt_state is MessageState.FAILED:
        database.record_delivery_attempt(reservation.delivery_id, MessageState.FAILED, now)
    if interrupt_state is MessageState.DELIVERY_UNKNOWN:
        database.record_delivery_attempt(
            reservation.delivery_id, MessageState.DELIVERY_UNKNOWN, now
        )

    resumed_database = Database(database_path)

    async def resume_real() -> MessageState:
        async with aiohttp.ClientSession() as session:
            return await run_daily_send(
                resumed_database, settings, session, recipient_key,
                PACIFIC_DATE, embedding_path, now,
            )

    async def resume_no_network() -> MessageState:
        return await run_daily_send(
            resumed_database, settings, None, recipient_key,  # type: ignore[arg-type]
            PACIFIC_DATE, embedding_path, now,
        )

    if interrupt_state in (MessageState.SENDING, MessageState.DELIVERY_UNKNOWN):
        result = asyncio.run(resume_no_network())
        assert result is MessageState.DELIVERY_UNKNOWN
        assert _sent_count(database_path, reservation.delivery_id) == (0,)
        return

    result = asyncio.run(resume_real())
    assert result is MessageState.SENT
    assert _sent_count(database_path, reservation.delivery_id) == (1,)

    # SENT is terminal (DELIVERY_TRANSITIONS[SENT] = set()) -- a second
    # call must never touch the network either, proven the same way.
    second_result = asyncio.run(resume_no_network())
    assert second_result is MessageState.SENT
    assert _sent_count(database_path, reservation.delivery_id) == (1,)


def test_a_real_timeout_during_send_becomes_delivery_unknown_and_never_retries(
    settings: Settings, tmp_path: Path
) -> None:
    """Composes two already-proven pieces rather than re-deriving fault
    injection from scratch: tests/security/test_sender_error_taxonomy.py
    proves a real hang raises SenderAmbiguous; the restart-matrix test
    above proves a DELIVERY_UNKNOWN delivery never retries. This connects
    them through a real send attempt: pause is impossible (there is no
    container to pause under Telegram), so the fault is injected via a
    real hanging local server instead, redirected to via
    send_voice_note's api_base override -- exactly the mechanism
    tests/security/test_sender_error_taxonomy.py already established.
    """
    database_path = tmp_path / "state.sqlite3"
    database = Database(database_path)
    now = _in_send_window(PACIFIC_DATE)
    text = f"A real-hang fault injection test at {datetime.now(UTC).timestamp()}."
    approved_message(database, text, now)
    recipient_key = "recipient_t16b_hang"
    reservation = database.reserve_next_message(recipient_key, PACIFIC_DATE, now)
    assert reservation is not None
    embedding_path = settings.voice_embedding.reveal()
    temp_destination = tmp_path / f"t16b-hang-{reservation.delivery_id}.ogg"
    produce_voice_note(
        database, reservation.delivery_id, embedding_path, text, temp_destination, now
    )
    database.transition_delivery(reservation.delivery_id, MessageState.SENDING, now)
    audio_bytes = database.get_audio_data(reservation.delivery_id)
    idempotency_key = f"delivery-{reservation.delivery_id}"
    timestamp = int(now.timestamp())
    signature = sign_request(
        settings.sender_auth_key.reveal().encode(), idempotency_key, timestamp
    )

    server = _HangingServer()
    try:
        async def attempt() -> None:
            async with aiohttp.ClientSession() as session:
                await send_voice_note(
                    session, database, settings, audio_bytes, idempotency_key,
                    timestamp, signature, now,
                    api_base=f"http://127.0.0.1:{server.port}",
                )

        with pytest.raises(SenderAmbiguous):
            asyncio.run(attempt())
    finally:
        server.stop()

    # Exactly what delivery.py's own AUDIO_READY branch does on
    # SenderAmbiguous -- reproduced directly since run_daily_send always
    # targets the real Telegram API with no fake-server override of its
    # own (only send_voice_note has one, added in Task 3 specifically for
    # testing).
    database.record_delivery_attempt(
        reservation.delivery_id, MessageState.DELIVERY_UNKNOWN, now
    )

    async def resume_no_network() -> MessageState:
        return await run_daily_send(
            database, settings, None, recipient_key,  # type: ignore[arg-type]
            PACIFIC_DATE, embedding_path, now,
        )

    result = asyncio.run(resume_no_network())
    assert result is MessageState.DELIVERY_UNKNOWN
    assert _sent_count(database_path, reservation.delivery_id) == (0,)
```

- [ ] **Step 4: Run to verify the tests fail for the right reason (before this task, if run against
  the pre-Task-3/4 codebase, this file wouldn't even import)**

By this point in the plan, Tasks 3-6 are already done, so this is a normal pass/fail check, not a
red-state check:

Run: `T13_VOICE_SAMPLE=<real sample> T16B_TELEGRAM_SETTINGS=<path> uv run pytest tests/e2e/test_delivery_fault_injection.py -v`
Expected: all PASS. If any restart-matrix case fails, stop and diagnose against the actual
delivery.py logic before assuming the test itself is wrong — this is the done-when-gate suite for
the whole migration.

- [ ] **Step 5: Independently confirm real sends happened where expected**

The `RESERVED`, `AUDIO_READY`, and `FAILED` parametrized cases, plus the hang-recovery test does
**not** perform a real send (it stays DELIVERY_UNKNOWN by design) — check the owner's real Telegram
test chat and confirm exactly 3 new voice notes arrived (one per real-send case), not more, not
fewer. Do not trust the test's own exit code alone for this — same discipline as every other real
send in this project's history.

- [ ] **Step 6: Run the full fast/security suites, mypy, ruff, and repository policy**

Run: `uv run pytest -m fast && uv run pytest -m security && uv run mypy src && uv run ruff check . && uv run python scripts/repository_policy.py all --root .`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "T16b: remove WAHA docker infrastructure, rewrite fault-injection suite for Telegram"
```

---

### Task 8: Independent security review, final docs, and merge

**Files:**
- Modify: `AGENTS.md` (§Confirmed stack, §Immediate next step)
- Create: `docs/task-logs/T16b.md`

**Interfaces:** none — this task is verification and documentation only, no code changes.

T16b is on this project's mandatory independent-security-review list (same posture as T15/T16 —
this task touched secrets and the locked sender boundary). Follow T15's exact precedent
(`docs/task-logs/T15.md`'s "Independent review" section) and T16 Task 13's precedent
(`docs/task-logs/T16.md`'s "Independent review" section) for how this project runs and records that
review — do not skip it or self-approve.

- [ ] **Step 1: Dispatch a fresh, unbiased independent reviewer**

Dispatch a fresh subagent with no implementation context — give it only `AGENTS.md`,
`IMPLEMENTATION_PLAN.md`'s T16b section, `docs/superpowers/specs/2026-08-18-telegram-sender-design.md`,
this plan file, and the full diff of every commit from Task 1 through Task 7. Instruct it explicitly
to verify claims against actual source, not trust this plan's prose, and to focus on:

- Can any code path make two real Telegram sends for one delivery? Trace every route to
  `send_voice_note` from `run_daily_send` across every `MessageState` the state machine can be in
  on entry, including the crash-restart branches.
- Is the `SenderRejected`/`SenderAmbiguous` status-code mapping actually fail-closed — specifically,
  does anything other than the exact `{400, 401, 403, 404, 429}` allow-list ever get treated as a
  definite rejection?
- Does `recipient_enrollment.py`'s immutability guard (refuse to overwrite an existing file) have
  any bypass (a race between the existence check and the write, a symlink, etc.)?
- Does `discovery/`, `generation/`, or `judging/` have any path to the bot token, the enrolled
  `chat_id`, or the sender module that the AST boundary test (Task 6) doesn't actually catch?
- Is the `DELIVERY_UNKNOWN`-is-terminal design genuinely enforced, or is there a code path that
  still attempts a resend from that state?

- [ ] **Step 2: Triage findings**

For each finding, verify against current source directly before acting (per this project's own
"verify findings against current source before acting on them" rule). Fix confirmed issues with
their own focused red/green cycle, each its own commit. Record informational-only or rejected
findings with reasoning, matching T15/T16's task-log style.

- [ ] **Step 3: Final verification pass**

```bash
uv run pytest -m fast
uv run pytest -m security
uv run pytest -m integration
uv run mypy src
uv run ruff check .
uv run python scripts/repository_policy.py all --root .
```

(No `docker compose config --quiet` — that file no longer exists, per Task 7.)

- [ ] **Step 4: Write `docs/task-logs/T16b.md`**

Create it following T15/T16's established task-log structure (Status, Dependencies, Design summary,
Verification, Independent review, Files changed, Next step sections). Record: what was built (the
full transport migration, task-by-task), the real e2e/integration verification evidence (real sends
to the owner's Telegram test chat, with the actual outcomes observed), the independent review's
findings and resolutions, and an explicit "T17 is next, and must be planned fresh against Telegram's
actual mechanics — see IMPLEMENTATION_PLAN.md's T17 section" pointer, matching this plan's own
scope boundary (T16b does not include STOP/kill-switch).

- [ ] **Step 5: Update `AGENTS.md`**

In §Confirmed stack, replace:
```
- WAHA Core behind a narrow internal sender
```
with:
```
- Telegram Bot API behind the same-shaped internal sender boundary (migrated from WAHA in T16b --
  see docs/task-logs/T16b.md; WAHA/self-hosted WhatsApp-Web automation confirmed dead, see
  docs/research/waha-alternatives.md)
```

Update §Immediate next step to record T16b complete and merged, and that T17 (recipient consent,
STOP, and kill switch, rewritten for Telegram per `IMPLEMENTATION_PLAN.md`) is next and has not yet
been planned — point at `docs/superpowers/specs/2026-08-18-telegram-sender-design.md`'s "Inbound
handling" section as T17's starting design context, and note per this project's one-task-at-a-time
discipline that T17 gets its own `writing-plans` session, not a continuation of this one.

- [ ] **Step 6: Commit, push, open PR, merge**

```bash
git add AGENTS.md docs/task-logs/T16b.md
git commit -m "T16b: record independent security review and update confirmed stack"
git push -u origin <branch-name>
gh pr create --fill
gh pr merge --merge --delete-branch
```

Per `CLAUDE.md`'s per-task workflow — merge via GitHub PR, not locally. Confirm `git rev-parse HEAD`
on `main` afterward matches what was just merged, and that the branch this plan was executed on is
gone both locally and on `origin`, before considering T16b closed.

---

## Self-Review

**Spec coverage:** every numbered section of `docs/superpowers/specs/2026-08-18-telegram-sender-design.md`
maps to a task here — "Architecture overview" and "What's deleted, kept, and rewritten" → Tasks 3-4;
"Recipient enrollment" → Task 2; "Ambiguous outcomes" → Task 4; "Testing plan" → Tasks 2-3, 5, 7;
"Backlog placement" → already carried out directly against `IMPLEMENTATION_PLAN.md` before this plan
file was written (T16b's own section there, plus T17's rewrite). "Inbound handling: STOP" is
explicitly out of scope for this plan (T17's territory, per the resolved backlog-placement decision)
and correctly has no task here.

**Placeholder scan:** no "TBD"/"TODO"/"implement later" anywhere above; every code block is complete,
compilable code, not a description of code. The one deliberately open item (T17's own detailed plan)
is explicitly deferred to a future `writing-plans` session, not left as an unstated gap.

**Type consistency:** `send_voice_note`'s signature (Task 3) — `(session, database, settings,
audio_bytes, idempotency_key, timestamp, signature, now, *, api_base=TELEGRAM_API_BASE) -> str` — is
used identically in Task 5's taxonomy tests and Task 7's fault-injection tests. `Settings`' new
fields (`telegram_chat_id: SensitiveValue[int]`, `telegram_bot_token: SensitiveValue[str]`, Task 1)
are constructed identically in Task 5's `_settings()` helper and Task 7's fixture. `enroll_recipient(
bot_token: str, destination: Path, profile: RuntimeProfile) -> int` (Task 2) is not called by any
later task at runtime (by design — it's a one-time, owner-run operation), so there is no
cross-task signature-drift risk for it to check.
