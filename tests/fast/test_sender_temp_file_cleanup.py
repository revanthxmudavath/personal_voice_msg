from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.config import RuntimeProfile, Settings
from personal_voice_msg.database import Database
from personal_voice_msg.redaction import SensitiveValue
from personal_voice_msg.sender import send_voice_note, sign_request

# Real HMAC key used to sign the request below -- not a production secret.
KEY = b"test-sender-auth-key"


def new_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "assistant.sqlite3")
    database.migrate()
    return database


def new_settings() -> Settings:
    """A real Settings value built directly, without a TOML file.

    WAHA is never reached in this test -- the write failure below happens
    before send_voice_note gets anywhere near audio validation or the
    network call, so these values only need to be well-typed, not real.
    """

    return Settings(
        profile=RuntimeProfile.DEVELOPMENT,
        recipient=SensitiveValue("+15550000000"),
        waha_token=SensitiveValue("unused-token"),
        voice_embedding=SensitiveValue(Path("unused.safetensors")),
        waha_session=SensitiveValue(Path("unused-session.bin")),
        waha_base_url="http://127.0.0.1:3000",
        sender_auth_key=SensitiveValue(KEY.decode()),
    )


@pytest.mark.fast
def test_temp_file_is_not_orphaned_when_writing_audio_bytes_fails(
    tmp_path: Path,
) -> None:
    """A real, unmocked write failure must not leave an orphaned temp file.

    ``audio_bytes`` here is a ``str``, not ``bytes`` -- a file opened in
    binary mode genuinely cannot write it, so Python itself raises a real
    TypeError inside send_voice_note's write step. No mocking is involved;
    this exercises the exact code path a disk-full or OS-level write error
    would take. See docs/task-logs/T15.md's temp-file handling and the
    pre-T16 readiness review that found this gap.
    """

    database = new_database(tmp_path)
    settings = new_settings()
    now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    timestamp = int(now.timestamp())
    idempotency_key = "temp-file-leak-test"
    signature = sign_request(KEY, idempotency_key, timestamp)

    temp_dir = tmp_path / "sender-temp"
    temp_dir.mkdir()
    real_tempdir = tempfile.tempdir
    tempfile.tempdir = str(temp_dir)
    try:
        async def send() -> str:
            async with aiohttp.ClientSession() as session:
                return await send_voice_note(
                    session,
                    database,
                    settings,
                    "not-real-audio-bytes",  # type: ignore[arg-type]
                    idempotency_key,
                    timestamp,
                    signature,
                    now,
                )

        with pytest.raises(TypeError):
            asyncio.run(send())
    finally:
        tempfile.tempdir = real_tempdir

    leftover = list(temp_dir.iterdir())
    assert leftover == [], f"orphaned temp file(s) left behind: {leftover}"
