from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from personal_voice_msg.database import (
    OPAQUE_RECIPIENT_KEY,
    Database,
    DisableReason,
    recipient_key_for_chat_id,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def new_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    return database


@pytest.mark.fast
def test_sending_is_enabled_by_default_on_a_fresh_database(tmp_path: Path) -> None:
    database = new_database(tmp_path)
    assert database.is_sending_enabled() is True


@pytest.mark.fast
def test_disable_sending_durably_disables_and_records_the_reason(
    tmp_path: Path,
) -> None:
    database = new_database(tmp_path)

    database.disable_sending(DisableReason.STOP_COMMAND, NOW)

    assert database.is_sending_enabled() is False
    with sqlite3.connect(database.path) as connection:
        row = connection.execute(
            "SELECT enabled, reason FROM sending_control WHERE id = 1"
        ).fetchone()
    assert row == (0, "stop_command")


@pytest.mark.fast
def test_disable_sending_is_idempotent_first_reason_wins(tmp_path: Path) -> None:
    database = new_database(tmp_path)

    database.disable_sending(DisableReason.STOP_COMMAND, NOW)
    database.disable_sending(DisableReason.ADMIN_KILL_SWITCH, NOW)

    with sqlite3.connect(database.path) as connection:
        row = connection.execute(
            "SELECT reason FROM sending_control WHERE id = 1"
        ).fetchone()
        event_count = connection.execute(
            "SELECT COUNT(*) FROM sending_control_events"
        ).fetchone()
    assert row == ("stop_command",)
    assert event_count == (1,)


@pytest.mark.fast
def test_enable_sending_requires_a_non_empty_note(tmp_path: Path) -> None:
    database = new_database(tmp_path)
    database.disable_sending(DisableReason.STOP_COMMAND, NOW)

    with pytest.raises(ValueError, match="non-empty note"):
        database.enable_sending("   ", NOW)
    with pytest.raises(ValueError, match="non-empty note"):
        database.enable_sending("", NOW)


@pytest.mark.fast
def test_enable_sending_re_enables_and_records_the_note(tmp_path: Path) -> None:
    database = new_database(tmp_path)
    database.disable_sending(DisableReason.STOP_COMMAND, NOW)

    database.enable_sending("recipient confirmed re-consent by phone", NOW)

    assert database.is_sending_enabled() is True
    with sqlite3.connect(database.path) as connection:
        events = connection.execute(
            "SELECT enabled, reason, note FROM sending_control_events ORDER BY id"
        ).fetchall()
    assert events == [
        (0, "stop_command", None),
        (1, None, "recipient confirmed re-consent by phone"),
    ]


@pytest.mark.fast
def test_enable_sending_is_idempotent_when_already_enabled(tmp_path: Path) -> None:
    database = new_database(tmp_path)

    database.enable_sending("no-op, already enabled", NOW)

    with sqlite3.connect(database.path) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM sending_control_events"
        ).fetchone()
    assert database.is_sending_enabled() is True
    assert event_count == (0,)


@pytest.mark.fast
def test_disabled_state_survives_reopening_the_database(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    first = Database(database_path)
    first.migrate()
    first.disable_sending(DisableReason.BLOCKED_BY_USER, NOW)

    second = Database(database_path)
    second.migrate()

    assert second.is_sending_enabled() is False


@pytest.mark.fast
def test_telegram_inbound_offset_round_trips_and_defaults_to_none(
    tmp_path: Path,
) -> None:
    database = new_database(tmp_path)

    assert database.get_telegram_inbound_offset() is None

    database.set_telegram_inbound_offset(42, NOW)

    assert database.get_telegram_inbound_offset() == 42

    database.set_telegram_inbound_offset(43, NOW)

    assert database.get_telegram_inbound_offset() == 43


@pytest.mark.fast
def test_recipient_key_for_chat_id_is_deterministic_and_opaque_key_shaped() -> None:
    key = recipient_key_for_chat_id(987654321)

    assert key == "recipient_telegram_987654321"
    assert OPAQUE_RECIPIENT_KEY.fullmatch(key)
