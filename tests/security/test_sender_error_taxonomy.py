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
from personal_voice_msg.sender import SenderAmbiguous, send_voice_note, sign_request
from personal_voice_msg.voice_enrollment import enroll_voice

VOICE_SAMPLE_ENV = "T13_VOICE_SAMPLE"

pytestmark = pytest.mark.security

if VOICE_SAMPLE_ENV not in os.environ:
    pytestmark = [
        pytest.mark.security,
        pytest.mark.skip(
            reason=(
                "requires a real consented test voice sample; set "
                f"{VOICE_SAMPLE_ENV} (docs/task-logs/T15.md) so audio "
                "validation genuinely passes before the network hang"
            )
        ),
    ]


class _HangingServer:
    """Accepts a connection and never responds -- a real socket, no HTTP
    or WAHA semantics implemented. Used only to force a real client-side
    timeout, not to simulate WhatsApp's API."""

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
            # Accept and hold the connection open without ever writing a
            # response -- the client's own request timeout must fire.
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


@pytest.fixture(scope="module")
def valid_audio_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """Real Pocket TTS synthesis + real FFmpeg conversion, once per module.

    ``send_voice_note`` validates audio *before* contacting WAHA, so the
    hanging-server test needs real, valid OGG/Opus bytes to reach the
    network step at all -- see the brief's note in task-6-brief.md.
    """

    workdir = tmp_path_factory.mktemp("t16_taxonomy_audio")
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


def _settings_for(port: int, tmp_path: Path) -> Settings:
    return Settings(
        profile=RuntimeProfile.DEVELOPMENT,
        recipient=SensitiveValue("+14155550100"),
        waha_token=SensitiveValue("test-token"),
        voice_embedding=SensitiveValue(tmp_path / "embedding.safetensors"),
        waha_session=SensitiveValue(tmp_path / "session.bin"),
        waha_base_url=f"http://127.0.0.1:{port}",
        sender_auth_key=SensitiveValue("test-sender-auth-key"),
    )


def test_a_hanging_connection_raises_sender_ambiguous_not_rejected(
    hanging_server: _HangingServer, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    settings = _settings_for(hanging_server.port, tmp_path)
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    now = datetime.now(UTC)
    idempotency_key = f"t16-ambiguous-{now.timestamp()}"
    timestamp = int(now.timestamp())
    signature = sign_request(
        settings.sender_auth_key.reveal().encode(), idempotency_key, timestamp
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

    with pytest.raises(SenderAmbiguous):
        asyncio.run(send())
