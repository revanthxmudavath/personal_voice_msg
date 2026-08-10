from __future__ import annotations

import tempfile
from datetime import date, datetime
from pathlib import Path

import aiohttp

from personal_voice_msg.audio_pipeline import produce_voice_note
from personal_voice_msg.config import Settings
from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.sender import (
    SenderAmbiguous,
    SenderRejected,
    reconcile_delivery,
    send_voice_note,
    sign_request,
)


async def run_daily_send(
    database: Database,
    settings: Settings,
    session: aiohttp.ClientSession,
    recipient_key: str,
    pacific_date: date,
    embedding_path: Path,
    text: str,
    now: datetime,
) -> MessageState:
    """Advance today's delivery by one orchestration step from wherever it
    currently sits, and return its resulting state. Callers loop this
    within the send window -- see Task 11.
    """
    reservation = database.reserve_next_message(recipient_key, pacific_date, now)
    if reservation is not None:
        delivery_id = reservation.delivery_id
        state = reservation.state
    else:
        existing_id = database.get_delivery_for_date(recipient_key, pacific_date)
        if existing_id is None:
            return MessageState.QUEUED  # nothing reserved, nothing queued
        delivery_id = existing_id
        state = database.get_delivery_state(delivery_id)

    if state is MessageState.SENDING:
        # This process did not just set SENDING itself in this call --
        # a prior attempt (possibly a crashed process) may or may not
        # have reached WAHA. Reclassify as ambiguous rather than guessing.
        database.record_delivery_attempt(
            delivery_id, MessageState.DELIVERY_UNKNOWN, now
        )
        return MessageState.DELIVERY_UNKNOWN

    if state is MessageState.FAILED:
        database.transition_delivery(delivery_id, MessageState.AUDIO_READY, now)
        state = MessageState.AUDIO_READY

    if state is MessageState.DELIVERY_UNKNOWN:
        latest_attempt_at = database.get_latest_attempt_time(delivery_id)
        outcome, provider_message_id = await reconcile_delivery(
            session, settings, latest_attempt_at, now
        )
        if outcome is MessageState.DELIVERY_UNKNOWN:
            return MessageState.DELIVERY_UNKNOWN  # still inconclusive
        database.record_delivery_attempt(
            delivery_id, outcome, now, provider_message_id=provider_message_id
        )
        if outcome is MessageState.SENT:
            return MessageState.SENT
        state = MessageState.AUDIO_READY

    if state is MessageState.RESERVED:
        temp_destination = Path(tempfile.gettempdir()) / f"t16-{delivery_id}.ogg"
        produce_voice_note(
            database, delivery_id, embedding_path, text, temp_destination, now
        )
        state = MessageState.AUDIO_READY

    if state is MessageState.AUDIO_READY:
        audio_bytes = database.get_audio_data(delivery_id)
        database.transition_delivery(delivery_id, MessageState.SENDING, now)
        idempotency_key = f"delivery-{delivery_id}"
        timestamp = int(now.timestamp())
        signature = sign_request(
            settings.sender_auth_key.reveal().encode(), idempotency_key, timestamp
        )
        try:
            provider_message_id = await send_voice_note(
                session, database, settings, audio_bytes,
                idempotency_key, timestamp, signature, now,
            )
        except SenderRejected:
            database.record_delivery_attempt(delivery_id, MessageState.FAILED, now)
            return MessageState.FAILED
        except SenderAmbiguous:
            database.record_delivery_attempt(
                delivery_id, MessageState.DELIVERY_UNKNOWN, now
            )
            return MessageState.DELIVERY_UNKNOWN
        else:
            database.record_delivery_attempt(
                delivery_id, MessageState.SENT, now,
                provider_message_id=provider_message_id,
            )
            return MessageState.SENT

    return state
