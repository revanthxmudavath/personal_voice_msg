from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from personal_voice_msg.daily_send_entrypoint import run_daily_entrypoint
from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.scheduling import ScheduleKind, planned_triggers_for_date


def _send_trigger_bounds(pacific_date: date) -> tuple[datetime, datetime]:
    trigger = next(
        t
        for t in planned_triggers_for_date(pacific_date)
        if t.kind is ScheduleKind.DAILY_SEND
    )
    return trigger.scheduled_at, trigger.cutoff_at


@pytest.mark.fast
def test_run_daily_entrypoint_before_the_window_is_a_pure_noop(
    tmp_path: Path,
) -> None:
    """session=None/settings=None proves no DB write and no network call is
    ever attempted outside the window -- a correct implementation that
    tried either would raise before reaching the assertion below."""
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    pacific_date = date(2026, 8, 9)
    start, _ = _send_trigger_bounds(pacific_date)
    too_early = start - timedelta(seconds=1)

    async def call() -> MessageState | None:
        return await run_daily_entrypoint(
            database, None, None, "recipient_t17b_window",  # type: ignore[arg-type]
            pacific_date, Path("unused"), too_early,
        )

    result = asyncio.run(call())

    assert result is None


@pytest.mark.fast
def test_run_daily_entrypoint_at_or_after_the_cutoff_is_a_pure_noop(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    pacific_date = date(2026, 8, 9)
    _, cutoff = _send_trigger_bounds(pacific_date)

    async def call() -> MessageState | None:
        return await run_daily_entrypoint(
            database, None, None, "recipient_t17b_window",  # type: ignore[arg-type]
            pacific_date, Path("unused"), cutoff,
        )

    result = asyncio.run(call())

    assert result is None
