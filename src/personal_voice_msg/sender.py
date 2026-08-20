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


class SenderBlocked(SenderRejected):
    """Telegram's specific 403 'Forbidden: bot was blocked by the user'
    response -- the closest thing to a proactive block signal Telegram
    offers, though still necessarily reactive (learned only by attempting
    a send). Callers must treat this as a durable stop signal alongside
    STOP -- see delivery.py and
    docs/superpowers/specs/2026-08-19-t17-telegram-consent-stop-killswitch-design.md.
    """


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
        retry_after = (
            parameters.get("retry_after") if isinstance(parameters, dict) else None
        )
    except (json.JSONDecodeError, AttributeError):
        return "unparseable response body"
    detail = f"error_code={code} description={description!r}"
    if retry_after is not None:
        detail += f" retry_after={retry_after}"
    return detail


def _is_blocked_by_user(body: bytes) -> bool:
    """True only for Telegram's specific blocked-by-user 403 description
    -- a narrow, exact substring check, not a general 403 assumption
    (other 403 reasons exist in principle, even for a private one-to-one
    chat)."""

    try:
        payload = json.loads(body)
        description = payload.get("description")
    except (ValueError, AttributeError):
        return False
    return isinstance(description, str) and "blocked by the user" in description


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
    if status == 403 and _is_blocked_by_user(body):
        raise SenderBlocked(
            f"Telegram rejected the request: {_describe_rejection(body)}"
        )
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
