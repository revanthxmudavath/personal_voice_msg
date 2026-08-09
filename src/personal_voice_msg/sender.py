from __future__ import annotations

import base64
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
# real HTTP latency against a real WAHA container in integration/e2e tests,
# still bounded -- see docs/task-logs/T15.md.
REPLAY_WINDOW_SECONDS = 300
# T15 sends through a single fixed WAHA session; the sender never accepts a
# caller-supplied session name.
WAHA_SESSION_NAME = "default"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 65_536
RESPONSE_CHUNK_BYTES = 8_192


class SenderError(RuntimeError):
    """Raised when a sender request is unauthenticated, stale, replayed,
    carries invalid audio, or WAHA rejects the send."""


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


async def send_voice_note(
    session: aiohttp.ClientSession,
    database: Database,
    settings: Settings,
    audio_bytes: bytes,
    idempotency_key: str,
    timestamp: int,
    signature: str,
    now: datetime,
) -> str:
    """Authenticate, validate, and send one voice note through WAHA.

    No parameter can select a recipient -- the destination is always
    ``settings.recipient``. Every check below runs before WAHA is ever
    contacted; a failure at any step makes zero WAHA calls. Returns WAHA's
    own message id on success. See docs/task-logs/T15.md.
    """

    key = settings.sender_auth_key.reveal().encode()
    if not verify_signature(key, idempotency_key, timestamp, signature):
        raise SenderError("sender request signature is invalid")
    if not is_fresh(timestamp, now):
        raise SenderError("sender request timestamp is stale")
    try:
        database.record_sender_nonce(
            idempotency_key,
            timestamp,
            now + timedelta(seconds=REPLAY_WINDOW_SECONDS),
        )
    except ReplayDetected:
        raise SenderError("sender request was already processed") from None

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(audio_bytes)
    try:
        validate_audio(temp_path)
    except AudioPipelineError as error:
        raise SenderError(f"audio failed validation: {error}") from error
    finally:
        temp_path.unlink(missing_ok=True)

    phone_number = settings.recipient.reveal().removeprefix("+")
    body = {
        "chatId": f"{phone_number}@c.us",
        "session": WAHA_SESSION_NAME,
        "file": {
            "mimetype": "audio/ogg; codecs=opus",
            "filename": "voice-note.ogg",
            "data": base64.b64encode(audio_bytes).decode("ascii"),
        },
    }
    try:
        async with session.post(
            f"{settings.waha_base_url}/api/sendVoice",
            json=body,
            headers={"X-Api-Key": settings.waha_token.reveal()},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            allow_redirects=False,
        ) as response:
            if response.status >= 400:
                raise SenderError("WAHA send request failed")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(RESPONSE_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise SenderError("WAHA response exceeded the size limit")
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
        raise SenderError("WAHA send request failed") from None

    try:
        return str(payload["key"]["id"])
    except (KeyError, TypeError):
        raise SenderError("WAHA response was malformed") from None
