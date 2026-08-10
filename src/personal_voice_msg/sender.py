from __future__ import annotations

import asyncio
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
from personal_voice_msg.database import Database, MessageState, ReplayDetected

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
# How many of the most recent chat messages reconcile_delivery inspects.
# Generous enough to cover normal chat traffic between reconciliation
# attempts, small enough to keep the request/response cheap.
RECONCILE_MESSAGE_LIMIT = 20
# Chat history responses carry a lot more per-message data than sendVoice's
# reply (WhatsApp protocol internals under "_data"), so this bound is wider
# than MAX_RESPONSE_BYTES -- see docs/task-logs/T16.md.
RECONCILE_MAX_RESPONSE_BYTES = 262_144
# A real WAHA send either completes or times out within
# REQUEST_TIMEOUT_SECONDS, so once more than that has elapsed since
# attempt_window_start with no matching message in chat history, the
# original request is conclusively over -- nothing will appear later. The
# margin above the raw timeout absorbs WAHA-side echo/indexing latency
# beyond what RECONCILE_POLL_ATTEMPTS already covers. See docs/task-logs/T16.md.
RECONCILE_GRACE_SECONDS = 45.0
# A message sent through WAHA is not always immediately visible via the
# chats/messages endpoint -- verified against the real live session: a
# just-completed send under light load typically took ~0.5-1.3s to appear
# in chat history (isolated single-send measurements). This small bounded
# retry smooths over that common case before falling through to the
# elapsed-time-based outcome below. It is deliberately NOT sized to
# absorb multi-second indexing backlogs (observed only under this task's
# own rapid back-to-back test sends, not representative of this
# application's one-send-per-day production cadence) -- reconcile_delivery
# must stay cheap to call repeatedly, since a slower real backlog is
# exactly what DELIVERY_UNKNOWN + the caller's own retry-later contract is
# for (see the function's docstring). See docs/task-logs/T16.md.
RECONCILE_POLL_ATTEMPTS = 3
RECONCILE_POLL_DELAY_SECONDS = 1.0


class SenderError(RuntimeError):
    """Base class for a rejected, ambiguous, or otherwise failed sender
    request."""


class SenderRejected(SenderError):
    """The request definitely never reached WAHA, or WAHA gave a definite
    rejection. Safe to retry immediately -- see docs/task-logs/T16.md."""


class SenderAmbiguous(SenderError):
    """WAHA may or may not have processed the request. Must be
    reconciled before any retry -- see docs/task-logs/T16.md."""


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
                raise SenderRejected("WAHA send request failed")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(RESPONSE_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise SenderAmbiguous("WAHA response exceeded the size limit")
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
        raise SenderAmbiguous("WAHA send request failed") from None

    try:
        return str(payload["key"]["id"])
    except (KeyError, TypeError):
        raise SenderAmbiguous("WAHA response was malformed") from None


class _ReconcileQueryFailed(Exception):
    """Internal signal: the WAHA chat-history query itself could not be
    completed or answered a malformed payload. Never escapes
    reconcile_delivery -- it always maps to DELIVERY_UNKNOWN."""


async def _fetch_matching_provider_id(
    session: aiohttp.ClientSession,
    settings: Settings,
    chat_id: str,
    window_start_timestamp: float,
) -> str | None:
    """One real GET against WAHA's chat history. Returns the matching
    outgoing voice message's provider_message_id (``_data.key.id``), or
    ``None`` if the query succeeded but nothing matched. Raises
    ``_ReconcileQueryFailed`` if the query itself failed.
    """

    try:
        async with session.get(
            f"{settings.waha_base_url}/api/{WAHA_SESSION_NAME}/chats/"
            f"{chat_id}/messages",
            params={"limit": str(RECONCILE_MESSAGE_LIMIT)},
            headers={"X-Api-Key": settings.waha_token.reveal()},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            allow_redirects=False,
        ) as response:
            if response.status >= 400:
                raise _ReconcileQueryFailed("WAHA chat-history request failed")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(RESPONSE_CHUNK_BYTES):
                total += len(chunk)
                if total > RECONCILE_MAX_RESPONSE_BYTES:
                    raise _ReconcileQueryFailed(
                        "WAHA chat-history response exceeded the size limit"
                    )
                chunks.append(chunk)
            messages = json.loads(b"".join(chunks))
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as error:
        raise _ReconcileQueryFailed("WAHA chat-history request failed") from error

    if not isinstance(messages, list):
        raise _ReconcileQueryFailed("WAHA chat-history response was malformed")

    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("fromMe") is not True:
            continue
        if message.get("hasMedia") is not True:
            continue
        media = message.get("media")
        mimetype = media.get("mimetype") if isinstance(media, dict) else None
        if not isinstance(mimetype, str) or not mimetype.startswith("audio/"):
            continue
        timestamp = message.get("timestamp")
        if not isinstance(timestamp, int | float):
            continue
        if timestamp < window_start_timestamp:
            continue
        try:
            return str(message["_data"]["key"]["id"])
        except (KeyError, TypeError):
            continue

    return None


async def reconcile_delivery(
    session: aiohttp.ClientSession,
    settings: Settings,
    attempt_window_start: datetime,
    now: datetime,
) -> tuple[MessageState, str | None]:
    """Resolve an ambiguous submission by checking WAHA's own record of
    what happened, since WAHA has no client-supplied idempotent message
    ID to dedupe against.

    Returns ``(MessageState.SENT, provider_message_id)`` if a matching
    outgoing voice message is found in the recipient's chat history at or
    after ``attempt_window_start``. Returns ``(MessageState.AUDIO_READY,
    None)`` if no matching message was found *and* every query succeeded,
    once enough time has passed since ``attempt_window_start`` that a real
    send would already be visible. Otherwise returns
    ``(MessageState.DELIVERY_UNKNOWN, None)`` -- covering both "not enough
    time has passed to be sure" and "the WAHA query itself failed," so a
    real WAHA outage can never be silently treated as "conclusively not
    sent."

    A message that really was sent is not always immediately visible via
    WAHA's chat-history endpoint (confirmed against the real live session
    -- see docs/task-logs/T16.md): this function retries the query a small
    bounded number of times (``RECONCILE_POLL_ATTEMPTS``,
    ``RECONCILE_POLL_DELAY_SECONDS`` apart) to smooth over the common
    sub-2s indexing lag before falling through to the elapsed-time-based
    outcome below. This retry is deliberately small -- it is not sized to
    absorb multi-second indexing backlogs, since reconcile_delivery must
    stay cheap for a caller to invoke repeatedly. A slower real backlog is
    exactly what the ``DELIVERY_UNKNOWN`` outcome and "the caller retries
    reconciliation later" contract below are for. A hard query failure
    (bad status, network error, malformed payload) short-circuits
    immediately to ``DELIVERY_UNKNOWN`` without retrying -- that is a
    WAHA-reachability problem, not an indexing-lag problem, and retrying
    it internally would just delay an already-known "unknown" answer.

    ``DELIVERY_UNKNOWN`` is a sentinel, not a legal transition target:
    ``DELIVERY_TRANSITIONS[MessageState.DELIVERY_UNKNOWN]`` only contains
    ``AUDIO_READY``/``SENT`` (see ``database.py``), so
    ``record_delivery_attempt`` raises ``InvalidTransition`` if handed
    ``DELIVERY_UNKNOWN`` while the delivery is already in that state.
    Callers (Task 8's ``delivery.py`` orchestrator) must check for this
    third outcome explicitly and skip calling ``record_delivery_attempt``
    entirely when it's returned -- retry reconciliation later instead of
    passing it through. See docs/task-logs/T16.md.
    """

    phone_number = settings.recipient.reveal().removeprefix("+")
    chat_id = f"{phone_number}@c.us"
    window_start_timestamp = attempt_window_start.timestamp()

    for attempt in range(RECONCILE_POLL_ATTEMPTS):
        try:
            provider_message_id = await _fetch_matching_provider_id(
                session, settings, chat_id, window_start_timestamp
            )
        except _ReconcileQueryFailed:
            return MessageState.DELIVERY_UNKNOWN, None
        if provider_message_id is not None:
            return MessageState.SENT, provider_message_id
        if attempt < RECONCILE_POLL_ATTEMPTS - 1:
            await asyncio.sleep(RECONCILE_POLL_DELAY_SECONDS)

    return _no_match_outcome(attempt_window_start, now)


def _no_match_outcome(
    attempt_window_start: datetime, now: datetime
) -> tuple[MessageState, str | None]:
    """Every query succeeded but no matching send was found anywhere in
    the retry loop. Only conclude AUDIO_READY once enough time has passed
    that a real send would already be over one way or another -- see
    RECONCILE_GRACE_SECONDS. Before that, stay DELIVERY_UNKNOWN.
    """

    elapsed = (now - attempt_window_start).total_seconds()
    if elapsed >= RECONCILE_GRACE_SECONDS:
        return MessageState.AUDIO_READY, None
    return MessageState.DELIVERY_UNKNOWN, None
