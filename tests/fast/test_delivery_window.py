from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.delivery import run_daily_send
from personal_voice_msg.history import MessageHistory
from personal_voice_msg.scheduling import ScheduleKind, planned_triggers_for_date


def _send_trigger_bounds(pacific_date: date) -> tuple[datetime, datetime]:
    trigger = next(
        t for t in planned_triggers_for_date(pacific_date)
        if t.kind is ScheduleKind.DAILY_SEND
    )
    return trigger.scheduled_at, trigger.cutoff_at


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
    """T16 Task 13 fix, finding F2: a delivery found already in SENDING at
    orchestrator startup (standing in for a crashed prior process) must
    have its crash-recovery DELIVERY_UNKNOWN attempt stamped with the
    original SENDING-entry time, not this restart call's own real
    invocation time -- otherwise the next DELIVERY_UNKNOWN branch's
    reconciliation window would start after any real WhatsApp message the
    crashed process's send may have actually produced, and could never
    find it. This branch returns before ever calling reconcile_delivery,
    so it needs no real WAHA/settings/session -- see the window-open
    tests above for the same no-network pattern.
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
