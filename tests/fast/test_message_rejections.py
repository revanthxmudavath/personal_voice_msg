from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from personal_voice_msg.database import (
    Database,
    DatabaseInvariantError,
    InvalidTransition,
    MessageState,
    RecordNotFound,
)
from personal_voice_msg.history import MessageHistory

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def record_message(database: Database, text: str, now: datetime = NOW) -> int:
    decision = MessageHistory(database).evaluate_and_record(text, now)
    assert decision.accepted
    assert decision.recorded_message_id is not None
    return decision.recorded_message_id


@pytest.mark.fast
def test_reject_message_from_discovered_moves_to_validated_and_records_reason(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    message_id = record_message(database, "A warm original sentence.")

    database.reject_message(message_id, "gate_violation", NOW)

    assert database.get_message_state(message_id) is MessageState.VALIDATED
    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        row = connection.execute(
            "SELECT message_id, reason FROM message_rejections WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    assert row == (message_id, "gate_violation")


@pytest.mark.fast
def test_reject_message_from_validated_records_reason_without_changing_state(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    message_id = record_message(database, "A warm original sentence.")
    database.transition_message(message_id, MessageState.VALIDATED, NOW)

    database.reject_message(message_id, "judge_score_floor", NOW)

    assert database.get_message_state(message_id) is MessageState.VALIDATED


@pytest.mark.fast
def test_reject_message_records_exactly_one_row_and_rejects_a_second_call(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    message_id = record_message(database, "A warm original sentence.")
    database.reject_message(message_id, "gate_violation", NOW)

    with pytest.raises(DatabaseInvariantError):
        database.reject_message(message_id, "gate_violation", NOW)

    with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM message_rejections WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    assert count == (1,)


@pytest.mark.fast
def test_reject_message_from_queued_raises_invalid_transition(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    message_id = record_message(database, "A warm original sentence.")
    for state in (MessageState.VALIDATED, MessageState.APPROVED, MessageState.QUEUED):
        database.transition_message(message_id, state, NOW)

    with pytest.raises(InvalidTransition):
        database.reject_message(message_id, "gate_violation", NOW)

    assert database.get_message_state(message_id) is MessageState.QUEUED


@pytest.mark.fast
def test_reject_message_raises_record_not_found_for_missing_id(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()

    with pytest.raises(RecordNotFound):
        database.reject_message(1, "gate_violation", NOW)


@pytest.mark.fast
def test_approve_message_from_discovered_walks_to_queued(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    message_id = record_message(database, "A warm original sentence.")

    database.approve_message(message_id, NOW)

    assert database.get_message_state(message_id) is MessageState.QUEUED


@pytest.mark.fast
def test_approve_message_from_validated_walks_to_queued(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    message_id = record_message(database, "A warm original sentence.")
    database.transition_message(message_id, MessageState.VALIDATED, NOW)

    database.approve_message(message_id, NOW)

    assert database.get_message_state(message_id) is MessageState.QUEUED


@pytest.mark.fast
def test_approve_message_from_queued_raises_invalid_transition(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    message_id = record_message(database, "A warm original sentence.")
    database.approve_message(message_id, NOW)

    with pytest.raises(InvalidTransition):
        database.approve_message(message_id, NOW)

    assert database.get_message_state(message_id) is MessageState.QUEUED


@pytest.mark.fast
def test_approve_message_raises_record_not_found_for_missing_id(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()

    with pytest.raises(RecordNotFound):
        database.approve_message(1, NOW)


@pytest.mark.fast
def test_next_unjudged_message_returns_none_when_no_candidates(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()

    assert database.next_unjudged_message() is None


@pytest.mark.fast
def test_next_unjudged_message_returns_oldest_discovered_first(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    first_id = record_message(database, "Your kindness lights up every room.")
    record_message(database, "Quiet mornings feel warmer beside you.")

    candidate = database.next_unjudged_message()

    assert candidate == (first_id, "Your kindness lights up every room.")


@pytest.mark.fast
def test_next_unjudged_message_includes_validated_not_yet_judged(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    message_id = record_message(database, "A warm original sentence.")
    database.transition_message(message_id, MessageState.VALIDATED, NOW)

    candidate = database.next_unjudged_message()

    assert candidate == (message_id, "A warm original sentence.")


@pytest.mark.fast
def test_next_unjudged_message_skips_rejected_messages(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    rejected_id = record_message(database, "Your kindness lights up every room.")
    database.reject_message(rejected_id, "gate_violation", NOW)
    second_id = record_message(database, "Quiet mornings feel warmer beside you.")

    candidate = database.next_unjudged_message()

    assert candidate == (second_id, "Quiet mornings feel warmer beside you.")


@pytest.mark.fast
def test_next_unjudged_message_excludes_queued_and_sent_messages(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    message_id = record_message(database, "A warm original sentence.")
    database.approve_message(message_id, NOW)

    assert database.next_unjudged_message() is None


@pytest.mark.fast
def test_count_queued_messages_counts_only_queued_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    assert database.count_queued_messages() == 0

    approved_id = record_message(database, "Approved sentence.")
    database.approve_message(approved_id, NOW)
    discovered_id = record_message(database, "Still discovered sentence.")
    database.reject_message(discovered_id, "gate_violation", NOW)

    assert database.count_queued_messages() == 1
