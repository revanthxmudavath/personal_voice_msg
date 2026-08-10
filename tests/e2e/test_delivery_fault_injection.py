from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.audio_pipeline import produce_voice_note
from personal_voice_msg.config import Settings, load_settings
from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.delivery import run_daily_send
from personal_voice_msg.history import MessageHistory
from personal_voice_msg.scheduling import (
    PACIFIC,
    ScheduleKind,
    planned_triggers_for_date,
)

pytestmark = pytest.mark.e2e

VOICE_SAMPLE_ENV = "T13_VOICE_SAMPLE"
WAHA_SETTINGS_ENV = "T15_WAHA_SETTINGS"
WAHA_CONTAINER_ENV = "T15_WAHA_CONTAINER"
_MISSING = [
    name
    for name in (VOICE_SAMPLE_ENV, WAHA_SETTINGS_ENV, WAHA_CONTAINER_ENV)
    if name not in os.environ
]
if _MISSING:
    pytestmark = [
        pytest.mark.e2e,
        pytest.mark.skip(
            reason=(
                "requires a real consented test voice sample, a real paired "
                "WAHA session, and real container control; set "
                f"{', '.join(_MISSING)} (docs/task-logs/T16.md)"
            )
        ),
    ]


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings(Path(os.environ[WAHA_SETTINGS_ENV]))


# See tests/e2e/test_delivery.py for the full rationale on why `now` is
# anchored to today's real Pacific date rather than a fixed historical one
# when exercising run_daily_send against the real, shared WAHA session.
PACIFIC_DATE = datetime.now(PACIFIC).date()


def _in_send_window(pacific_date: date) -> datetime:
    trigger = next(
        t
        for t in planned_triggers_for_date(pacific_date)
        if t.kind is ScheduleKind.DAILY_SEND
    )
    return trigger.scheduled_at


def approved_message(database: Database, text: str, now: datetime) -> None:
    database.migrate()
    decision = MessageHistory(database).evaluate_and_record(text, now)
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, now)


def _pause_container(container: str) -> None:
    subprocess.run(["docker", "pause", container], check=True)


def _unpause_container(container: str) -> None:
    subprocess.run(["docker", "unpause", container], check=True)


def test_a_paused_waha_container_produces_delivery_unknown_not_a_duplicate(
    settings: Settings, tmp_path: Path
) -> None:
    """Real fault injection: pause the real WAHA container mid-request so
    the client-side timeout fires with the request already in flight --
    exactly the "timeout after possible submission" scenario."""
    container = os.environ[WAHA_CONTAINER_ENV]
    database = Database(tmp_path / "state.sqlite3")
    now = _in_send_window(PACIFIC_DATE)
    text = (
        f"A paused-container fault injection test at "
        f"{datetime.now(UTC).timestamp()}."
    )
    approved_message(database, text, now)
    embedding_path = settings.voice_embedding.reveal()

    _pause_container(container)
    try:
        async def run() -> MessageState:
            async with aiohttp.ClientSession() as session:
                return await run_daily_send(
                    database, settings, session, "recipient_t16_fault",
                    PACIFIC_DATE, embedding_path, text, now,
                )

        result = asyncio.run(run())
    finally:
        _unpause_container(container)

    assert result is MessageState.DELIVERY_UNKNOWN

    delivery_id = database.get_delivery_for_date(
        "recipient_t16_fault", PACIFIC_DATE
    )
    assert delivery_id is not None

    # Reconciliation may need more than one pass: reconcile_delivery's own
    # internal poll only smooths a small, common indexing lag (see
    # RECONCILE_POLL_ATTEMPTS in sender.py); it is not sized to guarantee
    # an answer within one call, and the caller (this loop, standing in
    # for a real scheduled re-invocation of run_daily_send within the same
    # send window) is documented to retry on DELIVERY_UNKNOWN -- exactly
    # what tests/e2e/test_reconciliation.py's SENT-outcome test already
    # establishes as the correct calling pattern against this same real,
    # sometimes-laggy WAHA session (see docs/task-logs/T16.md's "Outstanding
    # concerns").
    deadline = now + timedelta(seconds=90)
    step_now = now + timedelta(seconds=5)
    final_result = MessageState.DELIVERY_UNKNOWN

    async def step(step_now: datetime) -> MessageState:
        async with aiohttp.ClientSession() as session:
            return await run_daily_send(
                database, settings, session, "recipient_t16_fault",
                PACIFIC_DATE, embedding_path, text, step_now,
            )

    while step_now <= deadline:
        final_result = asyncio.run(step(step_now))
        if final_result is not MessageState.DELIVERY_UNKNOWN:
            break
        step_now = step_now + timedelta(seconds=5)

    # Either the paused container's original request eventually landed
    # (SENT after reconciliation finds it) or it definitely did not
    # (retried and freshly SENT) -- either way, exactly one attempt row
    # has outcome='sent', proving no duplicate voice note.
    assert final_result is MessageState.SENT
    with sqlite3.connect(database.path) as connection:
        sent_count = connection.execute(
            "SELECT COUNT(*) FROM delivery_attempts "
            "WHERE delivery_id = ? AND outcome = 'sent'",
            (delivery_id,),
        ).fetchone()
    assert sent_count == (1,)


@pytest.mark.parametrize(
    "interrupt_state",
    [
        MessageState.RESERVED,
        MessageState.AUDIO_READY,
        MessageState.SENDING,
        MessageState.FAILED,
        MessageState.DELIVERY_UNKNOWN,
    ],
)
def test_restart_at_every_delivery_state_never_duplicates_a_send(
    settings: Settings, tmp_path: Path, interrupt_state: MessageState
) -> None:
    """Simulates a process restart by constructing a fresh Database handle
    from the same file and resuming from each persisted state in turn."""
    database_path = tmp_path / "state.sqlite3"
    database = Database(database_path)
    now = _in_send_window(PACIFIC_DATE)
    text = (
        f"A restart-at-{interrupt_state.value} test at "
        f"{datetime.now(UTC).timestamp()}."
    )
    approved_message(database, text, now)
    recipient_key = f"recipient_t16_restart_{interrupt_state.value}"
    reservation = database.reserve_next_message(recipient_key, PACIFIC_DATE, now)
    assert reservation is not None
    embedding_path = settings.voice_embedding.reveal()

    if interrupt_state is not MessageState.RESERVED:
        # Real synthesis, not placeholder bytes: send_voice_note's real
        # audio validation (Task 6) rejects malformed audio with
        # SenderRejected -> FAILED, so a fixture using fake bytes here
        # would make every AUDIO_READY/SENDING/FAILED/DELIVERY_UNKNOWN
        # case fail deterministically for a reason unrelated to restart
        # handling. produce_voice_note also performs the real
        # RESERVED -> AUDIO_READY transition + durable BLOB persistence
        # (Task 2/5) that these states must already have crossed.
        temp_destination = tmp_path / f"t16-restart-{reservation.delivery_id}.ogg"
        produce_voice_note(
            database, reservation.delivery_id, embedding_path, text,
            temp_destination, now,
        )
    if interrupt_state in (
        MessageState.SENDING,
        MessageState.FAILED,
        MessageState.DELIVERY_UNKNOWN,
    ):
        database.transition_delivery(
            reservation.delivery_id, MessageState.SENDING, now
        )
    if interrupt_state is MessageState.FAILED:
        database.record_delivery_attempt(
            reservation.delivery_id, MessageState.FAILED, now
        )
    if interrupt_state is MessageState.DELIVERY_UNKNOWN:
        database.record_delivery_attempt(
            reservation.delivery_id, MessageState.DELIVERY_UNKNOWN, now
        )

    # "Restart": a fresh Database instance over the same file, a fresh
    # run_daily_send call -- nothing carried over in memory.
    resumed_database = Database(database_path)

    async def resume(step_now: datetime) -> MessageState:
        async with aiohttp.ClientSession() as session:
            return await run_daily_send(
                resumed_database, settings, session,
                recipient_key,
                PACIFIC_DATE, embedding_path, text, step_now,
            )

    result = asyncio.run(resume(now))
    step_now = now
    # Ambiguity found on restart (the SENDING/DELIVERY_UNKNOWN-on-entry
    # case, and possibly the DELIVERY_UNKNOWN-interrupt-state fixture
    # itself) -- keep re-invoking exactly as a real scheduled re-run
    # within the send window would, until a terminal outcome or a
    # generous real-world bound is hit.
    deadline = now + timedelta(seconds=90)
    while result is MessageState.DELIVERY_UNKNOWN and step_now <= deadline:
        step_now = step_now + timedelta(seconds=5)
        result = asyncio.run(resume(step_now))

    assert result is MessageState.SENT
    with sqlite3.connect(database_path) as connection:
        sent_count = connection.execute(
            "SELECT COUNT(*) FROM delivery_attempts "
            "WHERE delivery_id = ? AND outcome = 'sent'",
            (reservation.delivery_id,),
        ).fetchone()
    assert sent_count == (1,)
