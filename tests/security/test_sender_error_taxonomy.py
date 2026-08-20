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
from personal_voice_msg.sender import (
    SenderAmbiguous,
    SenderRejected,
    send_voice_note,
    sign_request,
)
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
        "HTTP/1.1 402 Payment Required",
        "HTTP/1.1 405 Method Not Allowed",
        "HTTP/1.1 418 I'm a teapot",
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


def test_a_200_response_with_malformed_result_raises_sender_ambiguous(
    valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    """Telegram's contract is ok:true plus a well-shaped result.message_id
    -- a 200 with ok:true but a missing/malformed result must not be
    silently treated as success either."""
    server = _FixedStatusServer("HTTP/1.1 200 OK", body=b'{"ok":true,"result":{}}')
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


def test_a_blocked_by_user_403_raises_sender_blocked_not_generic_rejected(
    valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    """T17: Telegram's specific blocked-by-user 403 description must be
    distinguishable from any other definite rejection, so the caller can
    treat it as a durable stop signal alongside STOP."""
    from personal_voice_msg.sender import SenderBlocked

    server = _FixedStatusServer(
        "HTTP/1.1 403 Forbidden",
        body=(
            b'{"ok":false,"error_code":403,'
            b'"description":"Forbidden: bot was blocked by the user"}'
        ),
    )
    try:
        settings = _settings(tmp_path)
        database = Database(tmp_path / "state.sqlite3")
        database.migrate()

        with pytest.raises(SenderBlocked):
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


def test_a_different_403_reason_raises_plain_sender_rejected_not_blocked(
    valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    """A 403 for a different reason (e.g. a revoked token) must not be
    misclassified as blocked-by-user -- exact type check, not isinstance,
    since SenderBlocked is itself a SenderRejected subclass."""
    from personal_voice_msg.sender import SenderBlocked

    server = _FixedStatusServer(
        "HTTP/1.1 403 Forbidden",
        body=b'{"ok":false,"error_code":403,"description":"Forbidden: unauthorized"}',
    )
    try:
        settings = _settings(tmp_path)
        database = Database(tmp_path / "state.sqlite3")
        database.migrate()

        with pytest.raises(SenderRejected) as exc_info:
            asyncio.run(
                _send(
                    settings,
                    valid_audio_bytes,
                    database,
                    f"http://127.0.0.1:{server.port}",
                )
            )
        assert type(exc_info.value) is SenderRejected
        assert not isinstance(exc_info.value, SenderBlocked)
    finally:
        server.stop()
