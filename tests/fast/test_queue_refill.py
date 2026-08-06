from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.history import MessageHistory
from personal_voice_msg.queue_refill import MIN_QUEUE_SIZE, refill_queue
from personal_voice_msg.redaction import SensitiveValue

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
RECIPIENT = "recipient_staging_test"
# A gate-violating sentence: the deterministic gate rejects it before the
# judge is ever called, so a fast test can exercise the real rejection path
# with a fake API key and no network call. See
# tests/fast/test_judging_pipeline_short_circuit.py for the same technique.
GATE_VIOLATING_TEXT = "Will you marry me and also send me money right now?"
FAKE_API_KEY = SensitiveValue("not-a-real-api-key")


def record_message(database: Database, text: str, now: datetime = NOW) -> int:
    decision = MessageHistory(database).evaluate_and_record(text, now)
    assert decision.accepted
    assert decision.recorded_message_id is not None
    return decision.recorded_message_id


def queue_message(database: Database, text: str, now: datetime = NOW) -> int:
    message_id = record_message(database, text, now)
    database.approve_message(message_id, now)
    return message_id


def send_message(database: Database, text: str, now: datetime = NOW) -> int:
    message_id = queue_message(database, text, now)
    reservation = database.reserve_next_message(RECIPIENT, date(2026, 8, 6), now)
    assert reservation is not None
    for state in (
        MessageState.AUDIO_READY,
        MessageState.SENDING,
        MessageState.SENT,
    ):
        database.transition_delivery(reservation.delivery_id, state, now)
    return message_id


def run_refill(
    database: Database, *, target: int = MIN_QUEUE_SIZE
) -> object:
    async def _run() -> object:
        async with aiohttp.ClientSession() as session:
            return await refill_queue(
                database, session, FAKE_API_KEY, NOW, target=target
            )

    return asyncio.run(_run())


@pytest.mark.fast
def test_discovery_failure_preserves_the_existing_queue(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    queued_id = queue_message(database, "Your kindness lights up every room.")

    result = run_refill(database, target=MIN_QUEUE_SIZE)

    assert result.approved == 0
    assert result.rejected == 0
    assert database.get_message_state(queued_id) is MessageState.QUEUED
    assert result.health.queued_count == 1
    assert result.health.below_minimum is True


@pytest.mark.fast
def test_rejected_candidates_never_enter_the_queue(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    message_id = record_message(database, GATE_VIOLATING_TEXT)

    result = run_refill(database, target=1)

    assert result.approved == 0
    assert result.rejected == 1
    assert database.get_message_state(message_id) is MessageState.VALIDATED
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        row = connection.execute(
            "SELECT reason FROM message_rejections WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    assert row == ("gate_violation",)


@pytest.mark.fast
def test_rejected_message_is_never_resubmitted_on_a_later_refill_pass(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    message_id = record_message(database, GATE_VIOLATING_TEXT)
    first_result = run_refill(database, target=1)
    assert first_result.rejected == 1

    second_result = run_refill(database, target=1)

    assert second_result.approved == 0
    assert second_result.rejected == 0
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM message_rejections WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    assert count == (1,)


@pytest.mark.fast
def test_queue_refill_cannot_modify_sent_records(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    sent_id = send_message(database, "Your kindness lights up every room.")
    record_message(database, GATE_VIOLATING_TEXT)

    run_refill(database, target=1)

    assert database.get_message_state(sent_id) is MessageState.SENT
    assert (
        database.get_message_text(sent_id) == "Your kindness lights up every room."
    )


@pytest.mark.fast
def test_exhaustion_selects_only_the_pre_approved_reserve_buffer(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    buffered_id = queue_message(database, "Your kindness lights up every room.")

    result = run_refill(database, target=MIN_QUEUE_SIZE)

    assert result.health.queued_count == 1
    assert result.health.below_minimum is True
    reservation = database.reserve_next_message(RECIPIENT, date(2026, 8, 6), NOW)
    assert reservation is not None
    assert reservation.message_id == buffered_id


@pytest.mark.fast
def test_no_reserve_buffer_means_no_send(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()

    result = run_refill(database, target=MIN_QUEUE_SIZE)

    assert result.health.queued_count == 0
    assert result.health.below_minimum is True
    reservation = database.reserve_next_message(RECIPIENT, date(2026, 8, 6), NOW)
    assert reservation is None


@pytest.mark.fast
def test_refill_is_a_no_op_once_the_target_is_already_met(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    queue_message(database, "Your kindness lights up every room.")
    queue_message(database, "Quiet mornings feel warmer beside you.")

    result = run_refill(database, target=2)

    assert result.approved == 0
    assert result.rejected == 0
    assert result.health.queued_count == 2
    assert result.health.below_minimum is False


@pytest.mark.fast
def test_refill_recovers_a_message_left_validated_after_a_simulated_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    database = Database(database_path)
    database.migrate()
    message_id = record_message(database, GATE_VIOLATING_TEXT)
    # Simulate a crash after the DISCOVERED -> VALIDATED transition but
    # before the judge call completed: the message is left at VALIDATED
    # with no rejection row, same as a real interrupted refill pass would
    # leave it.
    database.transition_message(message_id, MessageState.VALIDATED, NOW)
    restarted_database = Database(database_path)

    result = run_refill(restarted_database, target=1)

    assert result.rejected == 1
    assert restarted_database.get_message_state(message_id) is MessageState.VALIDATED
