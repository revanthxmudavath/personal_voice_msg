from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import aiohttp

from personal_voice_msg.database import Database
from personal_voice_msg.judging.pipeline import evaluate_message_safety
from personal_voice_msg.redaction import SensitiveValue

# "Maintain at least 30 approved messages" -- IMPLEMENTATION_PLAN.md's T12
# section. The reserve buffer is a computed threshold over QUEUED rows, not
# a separate pool: reserve_next_message already only ever draws from
# QUEUED, so this is the one number that defines queue health.
MIN_QUEUE_SIZE = 30


@dataclass(frozen=True, slots=True)
class QueueHealth:
    queued_count: int
    minimum: int
    below_minimum: bool


@dataclass(frozen=True, slots=True)
class RefillResult:
    approved: int
    rejected: int
    health: QueueHealth


async def refill_queue(
    database: Database,
    session: aiohttp.ClientSession,
    api_key: SensitiveValue[str],
    now: datetime,
    *,
    target: int = MIN_QUEUE_SIZE,
) -> RefillResult:
    """Top up the QUEUED pool to `target` by judging pending candidates.

    Stops as soon as the target is met or no more candidates remain
    (`database.next_unjudged_message()` returns `None`) -- a discovery
    shortfall leaves the existing QUEUED rows untouched rather than
    reprocessing or evicting them.
    """

    approved = 0
    rejected = 0
    while database.count_queued_messages() < target:
        candidate = database.next_unjudged_message()
        if candidate is None:
            break
        message_id, text = candidate
        decision = await evaluate_message_safety(session, api_key, text)
        if decision.approved:
            database.approve_message(message_id, now)
            approved += 1
        else:
            assert decision.reason is not None
            database.reject_message(message_id, decision.reason, now)
            rejected += 1

    queued_count = database.count_queued_messages()
    health = QueueHealth(
        queued_count=queued_count,
        minimum=target,
        below_minimum=queued_count < target,
    )
    return RefillResult(approved=approved, rejected=rejected, health=health)
