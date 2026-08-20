from __future__ import annotations

import tempfile
from datetime import date, datetime
from pathlib import Path

import aiohttp

from personal_voice_msg.audio_pipeline import produce_voice_note
from personal_voice_msg.config import Settings
from personal_voice_msg.database import (
    Database,
    DisableReason,
    MessageState,
    recipient_key_for_chat_id,
)
from personal_voice_msg.scheduling import (
    ScheduleKind,
    TriggerStatus,
    classify_trigger,
    planned_triggers_for_date,
)
from personal_voice_msg.sender import (
    TELEGRAM_API_BASE,
    SenderAmbiguous,
    SenderBlocked,
    SenderRejected,
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
    *,
    api_base: str = TELEGRAM_API_BASE,
) -> MessageState:
    """Advance today's delivery by one orchestration step from wherever it
    currently sits, and return its resulting state. Callers loop this
    within the send window -- see Task 11.

    ``api_base`` defaults to the real Telegram API and should never be
    passed by production code -- it exists only so tests can redirect the
    one real network call this function can make (inside the
    ``AUDIO_READY`` branch) at a local fake server, mirroring
    ``send_voice_note``'s own identical parameter (Task 3).
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
        # have reached Telegram. Reclassify as ambiguous rather than
        # guessing. Stamp this attempt with the delivery's own
        # SENDING-entry time (durably recorded as deliveries.updated_at
        # by the AUDIO_READY -> SENDING transition, captured here before
        # this call overwrites it) purely for audit visibility -- under
        # WAHA this value also anchored a later reconciliation window
        # (T16 Task 13 fix, finding F2); under Telegram there is nothing
        # to reconcile against, so it now only records when the
        # ambiguity began.
        sending_started_at = database.get_delivery_updated_at(delivery_id)
        database.record_delivery_attempt(
            delivery_id, MessageState.DELIVERY_UNKNOWN, sending_started_at
        )
        return MessageState.DELIVERY_UNKNOWN

    if not database.is_sending_enabled():
        # A STOP, a blocked-by-user 403, or the admin kill switch already
        # disabled sending -- stop before any production or network work,
        # for every state this could still progress from (FAILED retry,
        # RESERVED production, AUDIO_READY send). The delivery is left
        # exactly where it was; nothing here mutates it.
        return state

    if state is MessageState.FAILED:
        database.transition_delivery(delivery_id, MessageState.AUDIO_READY, now)
        state = MessageState.AUDIO_READY

    if state is MessageState.DELIVERY_UNKNOWN:
        # Telegram's Bot API has no chat-history-read method for bots --
        # there is nothing to reconcile against (unlike WAHA). An
        # ambiguous outcome is terminal for the Pacific day: never
        # auto-retried, surfaced for the owner to check, consistent with
        # "retry only when non-delivery is certain" and "never carry a
        # missed send into the next Pacific day" (AGENTS.md). See
        # docs/superpowers/specs/2026-08-18-telegram-sender-design.md's
        # "Ambiguous outcomes" section.
        return MessageState.DELIVERY_UNKNOWN

    if state is MessageState.RESERVED:
        text = database.get_message_text(message_id)
        temp_destination = Path(tempfile.gettempdir()) / f"t16-{delivery_id}.ogg"
        produce_voice_note(
            database, delivery_id, embedding_path, text, temp_destination, now
        )
        state = MessageState.AUDIO_READY

    if state is MessageState.AUDIO_READY:
        if recipient_key != recipient_key_for_chat_id(
            settings.telegram_chat_id.reveal()
        ):
            raise ValueError(
                "recipient_key does not match the enrolled chat id"
            )
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
                api_base=api_base,
            )
        except SenderBlocked:
            database.disable_sending(DisableReason.BLOCKED_BY_USER, now)
            database.record_delivery_attempt(delivery_id, MessageState.FAILED, now)
            return MessageState.FAILED
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
