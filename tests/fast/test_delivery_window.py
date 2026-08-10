from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from personal_voice_msg.database import Database
from personal_voice_msg.delivery import run_daily_send
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
            date(2026, 8, 9), Path("unused"), "unused text", too_early,
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
            date(2026, 8, 9), Path("unused"), "unused text", cutoff,
        )

    with pytest.raises(ValueError, match="send window"):
        asyncio.run(call())
