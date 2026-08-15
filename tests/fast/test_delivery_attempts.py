from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from personal_voice_msg.database import (
    Database,
    InvalidTransition,
    MessageState,
    RecordNotFound,
)
from personal_voice_msg.history import MessageHistory

NOW = datetime(2026, 8, 9, 14, 0, tzinfo=UTC)
RECIPIENT = "recipient_t16_test"


def reserved_and_audio_ready(database: Database, text: str) -> int:
    database.migrate()
    decision = MessageHistory(database).evaluate_and_record(text, NOW)
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, NOW)
    reservation = database.reserve_next_message(RECIPIENT, date(2026, 8, 9), NOW)
    assert reservation is not None
    database.mark_audio_ready(reservation.delivery_id, b"note-bytes", NOW)
    database.transition_delivery(reservation.delivery_id, MessageState.SENDING, NOW)
    return reservation.delivery_id


@pytest.mark.fast
def test_record_delivery_attempt_sent_persists_id_and_transitions(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    delivery_id = reserved_and_audio_ready(database, "A warm original sentence.")

    database.record_delivery_attempt(
        delivery_id, MessageState.SENT, NOW, provider_message_id="waha-msg-1"
    )

    assert database.get_delivery_state(delivery_id) is MessageState.SENT
    with sqlite3.connect(database.path) as connection:
        provider_id = connection.execute(
            "SELECT provider_message_id FROM deliveries WHERE id = ?",
            (delivery_id,),
        ).fetchone()
        attempt = connection.execute(
            "SELECT outcome, provider_message_id FROM delivery_attempts "
            "WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
    assert provider_id == ("waha-msg-1",)
    assert attempt == ("sent", "waha-msg-1")


@pytest.mark.fast
def test_record_delivery_attempt_failed_transitions_without_provider_id(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    delivery_id = reserved_and_audio_ready(database, "A warm original sentence.")

    database.record_delivery_attempt(delivery_id, MessageState.FAILED, NOW)

    assert database.get_delivery_state(delivery_id) is MessageState.FAILED
    with sqlite3.connect(database.path) as connection:
        attempt = connection.execute(
            "SELECT outcome, provider_message_id FROM delivery_attempts "
            "WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
    assert attempt == ("failed", None)


@pytest.mark.fast
def test_record_delivery_attempt_delivery_unknown(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    delivery_id = reserved_and_audio_ready(database, "A warm original sentence.")

    database.record_delivery_attempt(delivery_id, MessageState.DELIVERY_UNKNOWN, NOW)

    assert database.get_delivery_state(delivery_id) is MessageState.DELIVERY_UNKNOWN


@pytest.mark.fast
def test_record_delivery_attempt_rejects_a_non_outcome_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    delivery_id = reserved_and_audio_ready(database, "A warm original sentence.")

    with pytest.raises(ValueError, match="outcome"):
        database.record_delivery_attempt(delivery_id, MessageState.AUDIO_READY, NOW)


@pytest.mark.fast
def test_record_delivery_attempt_rejects_an_illegal_current_state(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    delivery_id = reserved_and_audio_ready(database, "A warm original sentence.")
    database.record_delivery_attempt(delivery_id, MessageState.SENT, NOW)

    with pytest.raises(InvalidTransition):
        database.record_delivery_attempt(delivery_id, MessageState.FAILED, NOW)


@pytest.mark.fast
def test_record_delivery_attempt_raises_for_a_missing_delivery(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()

    with pytest.raises(RecordNotFound):
        database.record_delivery_attempt(999, MessageState.SENT, NOW)


@pytest.mark.fast
def test_clear_audio_data_nulls_the_column_once_sent(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    delivery_id = reserved_and_audio_ready(database, "A warm original sentence.")
    database.record_delivery_attempt(delivery_id, MessageState.SENT, NOW)

    database.clear_audio_data(delivery_id, NOW)

    with sqlite3.connect(database.path) as connection:
        audio_data = connection.execute(
            "SELECT audio_data FROM deliveries WHERE id = ?", (delivery_id,)
        ).fetchone()
    assert audio_data == (None,)


@pytest.mark.fast
def test_clear_audio_data_refuses_before_sent(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    delivery_id = reserved_and_audio_ready(database, "A warm original sentence.")

    with pytest.raises(InvalidTransition):
        database.clear_audio_data(delivery_id, NOW)

    assert database.get_audio_data(delivery_id) == b"note-bytes"


@pytest.mark.fast
def test_get_delivery_for_date_returns_none_when_nothing_reserved(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()

    assert database.get_delivery_for_date(RECIPIENT, date(2026, 8, 9)) is None


@pytest.mark.fast
def test_get_delivery_for_date_finds_an_existing_reservation(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    delivery_id = reserved_and_audio_ready(database, "A warm original sentence.")

    assert database.get_delivery_for_date(RECIPIENT, date(2026, 8, 9)) == delivery_id
