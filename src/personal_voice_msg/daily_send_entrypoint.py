"""Minimal daily-send entrypoint -- gives poll_inbound_stop and
run_daily_send their first real caller. See
docs/superpowers/specs/2026-08-20-t17b-daily-send-entrypoint-design.md.

A short-lived function, not a daemon: it does whatever's due, once, and
returns. scripts/run_daily_entrypoint.py is the process an external timer
(cron inside the container, or a systemd timer -- T18's concern) invokes
every 1-2 minutes; nothing here loops or sleeps.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import aiohttp

from personal_voice_msg.config import Settings
from personal_voice_msg.consent import TelegramPollError, poll_inbound_stop
from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.delivery import run_daily_send
from personal_voice_msg.scheduling import (
    ScheduleKind,
    TriggerStatus,
    classify_trigger,
    planned_triggers_for_date,
)
from personal_voice_msg.sender import TELEGRAM_API_BASE


async def run_daily_entrypoint(
    database: Database,
    settings: Settings,
    session: aiohttp.ClientSession,
    recipient_key: str,
    pacific_date: date,
    embedding_path: Path,
    now: datetime,
    *,
    api_base: str = TELEGRAM_API_BASE,
) -> MessageState | None:
    """Advance today's delivery by one step, after giving a pending STOP a
    chance to take effect first. Returns ``None`` -- touching neither the
    database nor the network -- outside the DAILY_SEND window, so this is
    safe to call on every cron tick all day.
    """

    send_trigger = next(
        trigger
        for trigger in planned_triggers_for_date(pacific_date)
        if trigger.kind is ScheduleKind.DAILY_SEND
    )
    if classify_trigger(send_trigger, now) is not TriggerStatus.DUE:
        return None

    try:
        await poll_inbound_stop(session, database, settings, now, api_base=api_base)
    except TelegramPollError:
        # Poll fragility must never block a legitimate send attempt -- see
        # the design spec's "The entrypoint function" section. No
        # structured logging exists yet (T19), so this is deliberately a
        # bare pass, not dressed up as more than it is.
        pass

    return await run_daily_send(
        database,
        settings,
        session,
        recipient_key,
        pacific_date,
        embedding_path,
        now,
        api_base=api_base,
    )
