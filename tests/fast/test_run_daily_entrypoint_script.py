from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from personal_voice_msg.audio_pipeline import AudioPipelineError, synthesize_to_wav
from personal_voice_msg.config import RuntimeProfile, Settings
from personal_voice_msg.database import MessageState
from personal_voice_msg.redaction import REDACTED, SensitiveValue

SCRIPT = Path(__file__).parents[2] / "scripts" / "run_daily_entrypoint.py"


def _load_script_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_daily_entrypoint_script", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_valid_config(root: Path) -> Path:
    """A minimal, fully valid (development-profile) configuration that
    load_settings accepts -- enough for the script to get past settings
    loading, which Fix 1 deliberately leaves outside the redaction try/except.
    """
    secret_root = root / "secrets"
    secret_root.mkdir()
    (secret_root / "chat_id.json").write_text(
        json.dumps({"profile": "development", "telegram_chat_id": 111222333}),
        encoding="utf-8",
    )
    (secret_root / "token.txt").write_text("test-bot-token\n", encoding="utf-8")
    (secret_root / "embedding.bin").write_bytes(b"not-a-real-embedding")
    (secret_root / "sender-auth.txt").write_text(
        "test-sender-auth-key\n", encoding="utf-8"
    )

    values = {
        "profile": "development",
        "secret_root": secret_root.as_posix(),
        "telegram_chat_id_file": "chat_id.json",
        "telegram_bot_token_file": "token.txt",
        "voice_embedding_file": "embedding.bin",
        "sender_auth_key_file": "sender-auth.txt",
    }
    config_path = root / "settings.toml"
    config_path.write_text(
        "".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    return config_path


@pytest.mark.fast
def test_run_daily_entrypoint_script_requires_config_and_database() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "--config" in result.stderr
    assert "--database" in result.stderr


@pytest.mark.fast
def test_run_daily_entrypoint_script_requires_database_when_only_config_given(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config", str(tmp_path / "settings.toml")],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "--database" in result.stderr


@pytest.mark.fast
def test_run_daily_entrypoint_script_exits_nonzero_without_a_raw_traceback_on_failure(
    tmp_path: Path,
) -> None:
    """Fix 1's wiring, exercised end to end via the real CLI: a real,
    deterministic, DAILY_SEND-window-independent failure inside _run
    (database.migrate() failing because its parent path is a file, not a
    directory -- reached unconditionally before any window check) must be
    caught by main(), never let Python's default unhandled-exception
    traceback reach stderr, and must exit non-zero.

    This does not reach the specific AudioPipelineError/voice-embedding-path
    leak the reviewer found -- reproducing that through the real script
    requires being inside the real 07:00-07:05 Pacific DAILY_SEND window,
    which this suite cannot control without monkeypatching wall-clock time
    (forbidden by this project's no-mock policy). That specific
    secret-redaction property is proven directly, with a real
    AudioPipelineError, by
    test_settings_redactor_scrubs_a_real_audio_pipeline_error below; this
    test proves the surrounding catch/redact/exit-1 plumbing that fires no
    matter which exception reaches it.
    """
    config_path = _write_valid_config(tmp_path)
    parent_as_file = tmp_path / "database_parent_is_a_file"
    parent_as_file.write_text("not a directory", encoding="utf-8")
    database_path = parent_as_file / "state.sqlite3"

    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--config", str(config_path),
            "--database", str(database_path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert result.stderr.strip() != ""
    assert "Traceback (most recent call last)" not in result.stderr


@pytest.mark.fast
def test_settings_redactor_scrubs_a_real_audio_pipeline_error(tmp_path: Path) -> None:
    """The exact property Fix 1 depends on: a real AudioPipelineError from
    audio_pipeline.py (no mocks -- real synthesize_to_wav call), whose
    message embeds the raw voice-embedding path, is fully scrubbed by
    settings.redactor() -- the same call main() makes on any top-level
    failure.

    A garbage-but-existing embedding file (the exact shape config
    validation allows) produces a safetensors header-parsing error that,
    in the pocket_tts version pinned here, does not itself embed the path.
    A path that was never created reproduces the reviewer's actual leak
    deterministically: model.get_state_for_audio_prompt raises a real
    Python FileNotFoundError-shaped error whose text embeds the exact
    path string (verified empirically -- a path deleted after being
    created instead hits a different, Rust-level "System error" message
    with platform-specific backslash-escaping on Windows, so this test
    uses the never-created form to stay platform-independent). Settings
    is built directly (as tests/security/test_daily_send_entrypoint_fault_injection.py's
    _settings() helper does) rather than via load_settings, since
    load_settings requires the configured file to already exist and this
    test needs it not to.
    """
    embedding_path = tmp_path / "never_created_embedding.safetensors"
    settings = Settings(
        profile=RuntimeProfile.DEVELOPMENT,
        telegram_chat_id=SensitiveValue(111222333),
        telegram_bot_token=SensitiveValue("test-bot-token"),
        voice_embedding=SensitiveValue(embedding_path),
        sender_auth_key=SensitiveValue("test-sender-auth-key"),
    )

    try:
        synthesize_to_wav(embedding_path, "hello there", tmp_path / "out.wav")
        pytest.fail("expected AudioPipelineError")
    except AudioPipelineError as exc:
        raw_message = str(exc)
        # Sanity check: this really does reproduce the reviewer's leak.
        assert str(embedding_path) in raw_message
        redacted = settings.redactor().redact(raw_message)

    assert str(embedding_path) not in redacted
    assert REDACTED in redacted


@pytest.mark.fast
def test_exit_code_for_signals_failure_states_only() -> None:
    """Fix 2's dispatch logic, tested directly at the smallest possible
    grain: run_daily_entrypoint already correctly returns each MessageState
    (proven elsewhere); this proves the script's own state-to-exit-code
    decision, for every state, without needing a real DAILY_SEND window
    (which this suite cannot control deterministically without
    monkeypatching wall-clock time)."""
    module = _load_script_module()

    assert module._exit_code_for(None) == 0
    failure_states = (MessageState.FAILED, MessageState.DELIVERY_UNKNOWN)
    for state in MessageState:
        expected = 1 if state in failure_states else 0
        assert module._exit_code_for(state) == expected, state
