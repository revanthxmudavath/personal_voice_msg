from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from personal_voice_msg.config import RuntimeProfile, Settings
from personal_voice_msg.database import Database, DisableReason, MessageState
from personal_voice_msg.delivery import run_daily_send
from personal_voice_msg.history import MessageHistory
from personal_voice_msg.redaction import SensitiveValue
from personal_voice_msg.scheduling import ScheduleKind, planned_triggers_for_date


def _send_trigger_bounds(pacific_date: date) -> tuple[datetime, datetime]:
    trigger = next(
        t for t in planned_triggers_for_date(pacific_date)
        if t.kind is ScheduleKind.DAILY_SEND
    )
    return trigger.scheduled_at, trigger.cutoff_at


def _settings(tmp_path: Path, chat_id: int) -> Settings:
    return Settings(
        profile=RuntimeProfile.DEVELOPMENT,
        telegram_chat_id=SensitiveValue(chat_id),
        telegram_bot_token=SensitiveValue("test-token"),
        voice_embedding=SensitiveValue(tmp_path / "embedding.safetensors"),
        sender_auth_key=SensitiveValue("test-sender-auth-key"),
    )


@pytest.mark.fast
def test_run_daily_send_rejects_a_call_before_the_send_window(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    start, _ = _send_trigger_bounds(date(2026, 8, 9))
    too_early = start - timedelta(seconds=1)

    async def call() -> None:
        await run_daily_send(
            database, None, None, "recipient_t16_window",  # type: ignore[arg-type]
            date(2026, 8, 9), Path("unused"), too_early,
        )

    with pytest.raises(ValueError, match="send window"):
        asyncio.run(call())


@pytest.mark.fast
def test_run_daily_send_rejects_a_call_at_or_after_the_cutoff(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    _, cutoff = _send_trigger_bounds(date(2026, 8, 9))

    async def call() -> None:
        await run_daily_send(
            database, None, None, "recipient_t16_window",  # type: ignore[arg-type]
            date(2026, 8, 9), Path("unused"), cutoff,
        )

    with pytest.raises(ValueError, match="send window"):
        asyncio.run(call())


@pytest.mark.fast
def test_run_daily_send_sending_on_entry_preserves_the_original_sending_time(
    tmp_path: Path,
) -> None:
    """T16 Task 13 fix, finding F2, carried forward under Telegram
    (T16b Task 4): a delivery found already in SENDING at orchestrator
    startup (standing in for a crashed prior process) must have its
    crash-recovery DELIVERY_UNKNOWN attempt stamped with the original
    SENDING-entry time, not this restart call's own real invocation time
    -- the stamped time is the delivery's permanent audit record of when
    the ambiguous send actually happened, even though DELIVERY_UNKNOWN is
    now terminal for the day (no reconciliation exists under Telegram; see
    Task 3/Task 4). This branch returns immediately without ever calling
    send_voice_note, so it needs no real Telegram/settings/session -- see
    the window-open tests above for the same no-network pattern.
    """
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    pacific_date = date(2026, 8, 9)
    start, cutoff = _send_trigger_bounds(pacific_date)
    sending_entered_at = start
    restart_at = start + timedelta(minutes=3)
    assert restart_at < cutoff

    decision = MessageHistory(database).evaluate_and_record(
        "A crash-restart timestamp test.", sending_entered_at
    )
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, sending_entered_at)
    recipient_key = "recipient_t16_sending_restart"
    reservation = database.reserve_next_message(
        recipient_key, pacific_date, sending_entered_at
    )
    assert reservation is not None
    database.mark_audio_ready(
        reservation.delivery_id, b"stale-audio-bytes", sending_entered_at
    )
    database.transition_delivery(
        reservation.delivery_id, MessageState.SENDING, sending_entered_at
    )

    async def call() -> MessageState:
        return await run_daily_send(
            database, None, None, recipient_key,  # type: ignore[arg-type]
            pacific_date, Path("unused"), restart_at,
        )

    result = asyncio.run(call())

    assert result is MessageState.DELIVERY_UNKNOWN
    recorded_window_start = database.get_delivery_updated_at(reservation.delivery_id)
    assert recorded_window_start == sending_entered_at
    assert recorded_window_start != restart_at


@pytest.mark.fast
def test_run_daily_send_returns_delivery_unknown_unresolved_with_no_reconciliation(
    tmp_path: Path,
) -> None:
    """T16b: Telegram's Bot API has no chat-history-read method for bots,
    so a delivery already in DELIVERY_UNKNOWN on entry must stay that way
    -- terminal for the Pacific day, not auto-resolved via a reconciliation
    call that no longer exists. This test passes `session=None` because a
    correct implementation never touches the network for this branch at
    all; if it did, this test would fail with an AttributeError on the
    None session before reaching the assertion below.
    """
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    pacific_date = date(2026, 8, 9)
    start, _ = _send_trigger_bounds(pacific_date)
    decision = MessageHistory(database).evaluate_and_record(
        "A delivery-unknown terminal-state test.", start
    )
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, start)
    recipient_key = "recipient_t16b_delivery_unknown"
    reservation = database.reserve_next_message(recipient_key, pacific_date, start)
    assert reservation is not None
    database.mark_audio_ready(reservation.delivery_id, b"stale-audio-bytes", start)
    database.transition_delivery(
        reservation.delivery_id, MessageState.SENDING, start
    )
    database.record_delivery_attempt(
        reservation.delivery_id, MessageState.DELIVERY_UNKNOWN, start
    )

    async def call() -> MessageState:
        return await run_daily_send(
            database, None, None, recipient_key,  # type: ignore[arg-type]
            pacific_date, Path("unused"), start,
        )

    result = asyncio.run(call())

    assert result is MessageState.DELIVERY_UNKNOWN
    final_state = database.get_delivery_state(reservation.delivery_id)
    assert final_state is MessageState.DELIVERY_UNKNOWN


@pytest.mark.fast
def test_run_daily_send_does_not_progress_a_reserved_delivery_while_disabled(
    tmp_path: Path,
) -> None:
    """T17: the admin kill switch (or a prior STOP/blocked-by-user event)
    must stop a RESERVED delivery before any audio production or network
    call -- session=None and settings=None prove the disabled gate is
    checked before either is ever touched."""
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    pacific_date = date(2026, 8, 9)
    start, _ = _send_trigger_bounds(pacific_date)

    decision = MessageHistory(database).evaluate_and_record(
        "A kill-switch reserved-send test.", start
    )
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, start)
    recipient_key = "recipient_t17_kill_switch"
    reservation = database.reserve_next_message(recipient_key, pacific_date, start)
    assert reservation is not None

    database.disable_sending(DisableReason.ADMIN_KILL_SWITCH, start)

    async def call() -> MessageState:
        return await run_daily_send(
            database, None, None, recipient_key,  # type: ignore[arg-type]
            pacific_date, Path("unused"), start,
        )

    result = asyncio.run(call())

    assert result is MessageState.RESERVED
    assert (
        database.get_delivery_state(reservation.delivery_id)
        is MessageState.RESERVED
    )


@pytest.mark.fast
def test_run_daily_send_rejects_a_recipient_key_that_does_not_match_the_enrolled_chat_id(  # noqa: E501
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    pacific_date = date(2026, 8, 9)
    start, _ = _send_trigger_bounds(pacific_date)

    decision = MessageHistory(database).evaluate_and_record(
        "A recipient key mismatch test.", start
    )
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, start)
    mismatched_key = "recipient_some_other_key"
    reservation = database.reserve_next_message(mismatched_key, pacific_date, start)
    assert reservation is not None
    database.mark_audio_ready(reservation.delivery_id, b"stale-audio-bytes", start)
    settings = _settings(tmp_path, chat_id=555)

    async def call() -> MessageState:
        return await run_daily_send(
            database, settings, None, mismatched_key,  # type: ignore[arg-type]
            pacific_date, Path("unused"), start,
        )

    with pytest.raises(ValueError, match="does not match the enrolled chat id"):
        asyncio.run(call())

    assert (
        database.get_delivery_state(reservation.delivery_id)
        is MessageState.AUDIO_READY
    )
