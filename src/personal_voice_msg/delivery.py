from __future__ import annotations

import tempfile
from datetime import date, datetime
from pathlib import Path

import aiohttp

from personal_voice_msg.audio_pipeline import produce_voice_note
from personal_voice_msg.config import Settings
from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.scheduling import (
    ScheduleKind,
    TriggerStatus,
    classify_trigger,
    planned_triggers_for_date,
)
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
    now: datetime,
) -> MessageState:
    """Advance today's delivery by one orchestration step from wherever it
    currently sits, and return its resulting state. Callers loop this
    within the send window -- see Task 11.
    """
    send_trigger = next(
        trigger
        for trigger in planned_triggers_for_date(pacific_date)
        if trigger.kind is ScheduleKind.DAILY_SEND
    )
    if classify_trigger(send_trigger, now) is not TriggerStatus.DUE:
        raise ValueError(
            "run_daily_send can only run inside the daily send window"
        )

    reservation = database.reserve_next_message(recipient_key, pacific_date, now)
    if reservation is not None:
        delivery_id = reservation.delivery_id
        message_id = reservation.message_id
        state = reservation.state
    else:
        existing_id = database.get_delivery_for_date(recipient_key, pacific_date)
        if existing_id is None:
            return MessageState.QUEUED  # nothing reserved, nothing queued
        delivery_id = existing_id
        message_id = database.get_delivery_message_id(delivery_id)
        state = database.get_delivery_state(delivery_id)

    if state is MessageState.SENDING:
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
        sending_started_at = database.get_delivery_updated_at(delivery_id)
        database.record_delivery_attempt(
            delivery_id, MessageState.DELIVERY_UNKNOWN, sending_started_at
        )
        return MessageState.DELIVERY_UNKNOWN

    if state is MessageState.FAILED:
        database.transition_delivery(delivery_id, MessageState.AUDIO_READY, now)
        state = MessageState.AUDIO_READY

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

    if state is MessageState.RESERVED:
        text = database.get_message_text(message_id)
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
