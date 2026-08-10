from __future__ import annotations

import asyncio
import inspect
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.audio_pipeline import convert_to_opus, synthesize_to_wav
from personal_voice_msg.config import Settings, load_settings
from personal_voice_msg.database import Database
from personal_voice_msg.sender import (
    SenderError,
    SenderRejected,
    send_voice_note,
    sign_request,
)
from personal_voice_msg.voice_enrollment import enroll_voice

pytestmark = pytest.mark.e2e

VOICE_SAMPLE_ENV = "T13_VOICE_SAMPLE"
WAHA_SETTINGS_ENV = "T15_WAHA_SETTINGS"
_MISSING = [
    name
    for name in (VOICE_SAMPLE_ENV, WAHA_SETTINGS_ENV)
    if name not in os.environ
]
if _MISSING:
    pytestmark = [
        pytest.mark.e2e,
        pytest.mark.skip(
            reason=(
                "requires a real consented test voice sample and a real "
                f"paired WAHA session; set {', '.join(_MISSING)} "
                "(docs/task-logs/T15.md)"
            )
        ),
    ]


def new_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "assistant.sqlite3")
    database.migrate()
    return database


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings(Path(os.environ[WAHA_SETTINGS_ENV]))


@pytest.fixture(scope="module")
def valid_audio_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """Real Pocket TTS synthesis + real FFmpeg conversion, once per module."""

    workdir = tmp_path_factory.mktemp("t15_audio")
    raw_sample = workdir / "raw_sample.wav"
    shutil.copyfile(Path(os.environ[VOICE_SAMPLE_ENV]), raw_sample)
    embedding = workdir / "voice_embedding.safetensors"
    enroll_voice(raw_sample, embedding)

    wav_path = workdir / "synthesized.wav"
    synthesize_to_wav(
        embedding,
        "This is a real end to end test of the locked sender boundary.",
        wav_path,
    )
    ogg_path = workdir / "synthesized.ogg"
    convert_to_opus(wav_path, ogg_path)
    return ogg_path.read_bytes()


def signed_request(
    settings: Settings, idempotency_key: str, now: datetime
) -> tuple[int, str]:
    timestamp = int(now.timestamp())
    signature = sign_request(
        settings.sender_auth_key.reveal().encode(), idempotency_key, timestamp
    )
    return timestamp, signature


def test_valid_signed_request_sends_a_real_voice_note(
    settings: Settings, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t15-done-when-gate-{now.timestamp()}"
    timestamp, signature = signed_request(settings, idempotency_key, now)

    async def send() -> str:
        async with aiohttp.ClientSession() as session:
            return await send_voice_note(
                session,
                database,
                settings,
                valid_audio_bytes,
                idempotency_key,
                timestamp,
                signature,
                now,
            )

    message_id = asyncio.run(send())

    assert isinstance(message_id, str) and message_id


def test_send_voice_note_has_no_recipient_parameter() -> None:
    """Structural guarantee: no caller input can select a destination."""

    parameters = set(inspect.signature(send_voice_note).parameters)
    recipient_shaped = {"recipient", "phone_number", "chat_id", "chatId", "to"}
    assert parameters.isdisjoint(recipient_shaped)


def test_text_bytes_are_rejected_without_calling_waha(
    settings: Settings, tmp_path: Path
) -> None:
    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t15-text-rejected-{now.timestamp()}"
    timestamp, signature = signed_request(settings, idempotency_key, now)

    async def send() -> str:
        async with aiohttp.ClientSession() as session:
            return await send_voice_note(
                session,
                database,
                settings,
                b"this is plain text, not audio",
                idempotency_key,
                timestamp,
                signature,
                now,
            )

    with pytest.raises(SenderError, match="audio failed validation"):
        asyncio.run(send())


def test_corrupt_audio_is_rejected(
    settings: Settings, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t15-corrupt-rejected-{now.timestamp()}"
    timestamp, signature = signed_request(settings, idempotency_key, now)
    corrupt_bytes = valid_audio_bytes[:200]

    async def send() -> str:
        async with aiohttp.ClientSession() as session:
            return await send_voice_note(
                session,
                database,
                settings,
                corrupt_bytes,
                idempotency_key,
                timestamp,
                signature,
                now,
            )

    with pytest.raises(SenderError, match="audio failed validation"):
        asyncio.run(send())


def test_replayed_request_is_rejected_before_reaching_waha(
    settings: Settings, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t15-replay-rejected-{now.timestamp()}"
    timestamp, signature = signed_request(settings, idempotency_key, now)
    # Pre-record the exact (idempotency_key, timestamp) pair directly via
    # the real database method -- this proves replay rejection happens
    # without needing a prior real WAHA send.
    database.record_sender_nonce(
        idempotency_key, timestamp, now + timedelta(minutes=5)
    )

    async def send() -> str:
        async with aiohttp.ClientSession() as session:
            return await send_voice_note(
                session,
                database,
                settings,
                valid_audio_bytes,
                idempotency_key,
                timestamp,
                signature,
                now,
            )

    with pytest.raises(SenderError, match="already processed"):
        asyncio.run(send())


def test_stale_timestamp_is_rejected(
    settings: Settings, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t15-stale-rejected-{now.timestamp()}"
    stale_timestamp, signature = signed_request(
        settings, idempotency_key, now - timedelta(seconds=400)
    )

    async def send() -> str:
        async with aiohttp.ClientSession() as session:
            return await send_voice_note(
                session,
                database,
                settings,
                valid_audio_bytes,
                idempotency_key,
                stale_timestamp,
                signature,
                now,
            )

    with pytest.raises(SenderError, match="stale"):
        asyncio.run(send())


def test_invalid_signature_is_rejected(
    settings: Settings, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t15-bad-signature-{now.timestamp()}"
    timestamp = int(now.timestamp())

    async def send() -> str:
        async with aiohttp.ClientSession() as session:
            return await send_voice_note(
                session,
                database,
                settings,
                valid_audio_bytes,
                idempotency_key,
                timestamp,
                "0" * 64,
                now,
            )

    with pytest.raises(SenderError, match="signature is invalid"):
        asyncio.run(send())


def test_invalid_audio_raises_sender_rejected_not_ambiguous(
    settings: Settings, tmp_path: Path
) -> None:
    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t16-taxonomy-audio-{now.timestamp()}"
    timestamp, signature = signed_request(settings, idempotency_key, now)

    async def send() -> str:
        async with aiohttp.ClientSession() as session:
            return await send_voice_note(
                session,
                database,
                settings,
                b"this is plain text, not audio",
                idempotency_key,
                timestamp,
                signature,
                now,
            )

    with pytest.raises(SenderRejected):
        asyncio.run(send())


def test_invalid_signature_raises_sender_rejected(
    settings: Settings, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t16-taxonomy-signature-{now.timestamp()}"
    timestamp = int(now.timestamp())

    async def send() -> str:
        async with aiohttp.ClientSession() as session:
            return await send_voice_note(
                session,
                database,
                settings,
                valid_audio_bytes,
                idempotency_key,
                timestamp,
                "0" * 64,
                now,
            )

    with pytest.raises(SenderRejected):
        asyncio.run(send())


def test_replayed_request_raises_sender_rejected(
    settings: Settings, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t16-taxonomy-replay-{now.timestamp()}"
    timestamp, signature = signed_request(settings, idempotency_key, now)
    database.record_sender_nonce(
        idempotency_key, timestamp, now + timedelta(minutes=5)
    )

    async def send() -> str:
        async with aiohttp.ClientSession() as session:
            return await send_voice_note(
                session,
                database,
                settings,
                valid_audio_bytes,
                idempotency_key,
                timestamp,
                signature,
                now,
            )

    with pytest.raises(SenderRejected):
        asyncio.run(send())


def test_unauthenticated_request_is_rejected(
    settings: Settings, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t15-no-signature-{now.timestamp()}"
    timestamp = int(now.timestamp())

    async def send() -> str:
        async with aiohttp.ClientSession() as session:
            return await send_voice_note(
                session,
                database,
                settings,
                valid_audio_bytes,
                idempotency_key,
                timestamp,
                "",
                now,
            )

    with pytest.raises(SenderError, match="signature is invalid"):
        asyncio.run(send())
