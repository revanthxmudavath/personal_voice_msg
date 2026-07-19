from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from personal_voice_msg.database import Database, InvalidTransition, MessageState

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
RECIPIENT = "recipient_staging_test"


def queue_message(database: Database, text: str, now: datetime = NOW) -> int:
    message_id = database.create_message(text, now)
    for state in (
        MessageState.VALIDATED,
        MessageState.APPROVED,
        MessageState.QUEUED,
    ):
        database.transition_message(message_id, state, now)
    return message_id


def reserve_concurrently(
    database_path: Path, pacific_date: date, now: datetime
) -> list[object | None]:
    ready = Barrier(2)

    def reserve() -> object | None:
        worker_database = Database(database_path)
        ready.wait(timeout=5)
        return worker_database.reserve_next_message(RECIPIENT, pacific_date, now)

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(reserve) for _ in range(2)]
        return [future.result(timeout=10) for future in futures]


@pytest.mark.fast
def test_migrate_creates_the_t03_schema_in_a_real_sqlite_file(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"

    Database(database_path).migrate()

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "sources",
        "inspiration_cards",
        "messages",
        "runs",
        "audio_artifacts",
        "deliveries",
    } <= tables


@pytest.mark.fast
def test_message_state_is_persisted_after_each_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    database = Database(database_path)
    database.migrate()
    message_id = database.create_message("A warm original sentence.", NOW)

    assert (
        Database(database_path).get_message_state(message_id) is MessageState.DISCOVERED
    )

    for state in (
        MessageState.VALIDATED,
        MessageState.APPROVED,
        MessageState.QUEUED,
    ):
        Database(database_path).transition_message(message_id, state, NOW)
        assert Database(database_path).get_message_state(message_id) is state

    reservation = Database(database_path).reserve_next_message(
        RECIPIENT, date(2026, 7, 18), NOW
    )
    assert reservation is not None
    assert reservation.state is MessageState.RESERVED
    assert (
        Database(database_path).get_message_state(message_id) is MessageState.RESERVED
    )
    assert (
        Database(database_path).get_delivery_state(reservation.delivery_id)
        is MessageState.RESERVED
    )

    for state in (
        MessageState.AUDIO_READY,
        MessageState.SENDING,
        MessageState.SENT,
    ):
        Database(database_path).transition_delivery(reservation.delivery_id, state, NOW)
        assert (
            Database(database_path).get_delivery_state(reservation.delivery_id) is state
        )
        assert Database(database_path).get_message_state(message_id) is state


@pytest.mark.fast
def test_message_transitions_reject_skips_and_backwards_moves(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    message_id = database.create_message("A warm original sentence.", NOW)

    with pytest.raises(InvalidTransition):
        database.transition_message(message_id, MessageState.APPROVED, NOW)

    database.transition_message(message_id, MessageState.VALIDATED, NOW)
    with pytest.raises(InvalidTransition):
        database.transition_message(message_id, MessageState.DISCOVERED, NOW)

    database.transition_message(message_id, MessageState.APPROVED, NOW)
    database.transition_message(message_id, MessageState.QUEUED, NOW)
    with pytest.raises(InvalidTransition):
        database.transition_message(message_id, MessageState.APPROVED, NOW)


@pytest.mark.fast
def test_delivery_transitions_reject_skips_and_backwards_moves(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    queue_message(database, "A warm original sentence.")
    reservation = database.reserve_next_message(RECIPIENT, date(2026, 7, 18), NOW)
    assert reservation is not None

    with pytest.raises(InvalidTransition):
        database.transition_delivery(reservation.delivery_id, MessageState.SENDING, NOW)

    database.transition_delivery(reservation.delivery_id, MessageState.AUDIO_READY, NOW)
    with pytest.raises(InvalidTransition):
        database.transition_delivery(
            reservation.delivery_id, MessageState.RESERVED, NOW
        )


@pytest.mark.fast
@pytest.mark.parametrize(
    ("terminal_state", "next_state"),
    [
        (MessageState.SENT, MessageState.FAILED),
        (MessageState.FAILED, MessageState.SENT),
        (MessageState.DELIVERY_UNKNOWN, MessageState.FAILED),
    ],
)
def test_terminal_delivery_states_cannot_transition(
    tmp_path: Path, terminal_state: MessageState, next_state: MessageState
) -> None:
    database = Database(tmp_path / f"{terminal_state.value}.sqlite3")
    database.migrate()
    queue_message(database, "A warm original sentence.")
    reservation = database.reserve_next_message(RECIPIENT, date(2026, 7, 18), NOW)
    assert reservation is not None
    database.transition_delivery(reservation.delivery_id, MessageState.AUDIO_READY, NOW)
    database.transition_delivery(reservation.delivery_id, MessageState.SENDING, NOW)
    database.transition_delivery(reservation.delivery_id, terminal_state, NOW)

    with pytest.raises(InvalidTransition):
        database.transition_delivery(reservation.delivery_id, next_state, NOW)


@pytest.mark.fast
def test_two_workers_reserve_one_message_for_one_recipient_date(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    database = Database(database_path)
    database.migrate()
    message_id = queue_message(database, "A warm original sentence.")
    pacific_date = date(2026, 7, 18)

    results = reserve_concurrently(database_path, pacific_date, NOW)

    reservations = [result for result in results if result is not None]
    assert len(reservations) == 1
    reservation = reservations[0]
    assert reservation.message_id == message_id
    assert reservation.recipient_key == RECIPIENT
    assert reservation.pacific_date == pacific_date
    assert reservation.state is MessageState.RESERVED
    assert database.count_deliveries(RECIPIENT, pacific_date) == 1


@pytest.mark.fast
@pytest.mark.parametrize(
    ("recipient_key", "pacific_date"),
    [
        ("+14155550123", date(2026, 7, 18)),
        ("14155550123", date(2026, 7, 18)),
        (RECIPIENT, datetime(2026, 7, 18, 7, 0, tzinfo=UTC)),
    ],
)
def test_reservation_rejects_non_opaque_recipient_or_datetime_date(
    tmp_path: Path,
    recipient_key: str,
    pacific_date: date,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    message_id = queue_message(database, "A warm original sentence.")

    with pytest.raises(ValueError):
        database.reserve_next_message(recipient_key, pacific_date, NOW)

    assert database.get_message_state(message_id) is MessageState.QUEUED
    count_date = (
        pacific_date.date()
        if isinstance(pacific_date, datetime)
        else pacific_date
    )
    assert database.count_deliveries(recipient_key, count_date) == 0


@pytest.mark.fast
def test_thirty_dates_have_one_atomic_reservation_without_reusing_messages(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state.sqlite3"
    database = Database(database_path)
    database.migrate()
    for index in range(30):
        queue_message(database, f"Original queued sentence {index}.")

    start_date = date(2026, 7, 18)
    reserved_message_ids: list[int] = []
    for offset in range(30):
        pacific_date = start_date + timedelta(days=offset)
        now = NOW + timedelta(days=offset)
        results = reserve_concurrently(database_path, pacific_date, now)
        reservations = [result for result in results if result is not None]

        assert len(reservations) == 1
        reservation = reservations[0]
        assert reservation.pacific_date == pacific_date
        assert database.count_deliveries(RECIPIENT, pacific_date) == 1
        reserved_message_ids.append(reservation.message_id)

    assert len(set(reserved_message_ids)) == 30
