from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from personal_voice_msg.database import Database, ReplayDetected

NOW = datetime(2026, 8, 8, 15, 0, tzinfo=UTC)
EXPIRES_AT = NOW + timedelta(minutes=5)


def new_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "assistant.sqlite3")
    database.migrate()
    return database


@pytest.mark.fast
def test_record_sender_nonce_succeeds_for_a_new_pair(tmp_path: Path) -> None:
    database = new_database(tmp_path)

    database.record_sender_nonce("delivery-1", 1_700_000_000, EXPIRES_AT)

    with sqlite3.connect(database.path) as connection:
        row = connection.execute(
            "SELECT idempotency_key, timestamp FROM sender_auth_nonces"
        ).fetchone()
    assert row == ("delivery-1", 1_700_000_000)


@pytest.mark.fast
def test_record_sender_nonce_rejects_an_exact_replay(tmp_path: Path) -> None:
    database = new_database(tmp_path)
    database.record_sender_nonce("delivery-1", 1_700_000_000, EXPIRES_AT)

    with pytest.raises(ReplayDetected):
        database.record_sender_nonce("delivery-1", 1_700_000_000, EXPIRES_AT)


@pytest.mark.fast
def test_record_sender_nonce_allows_same_key_with_a_different_timestamp(
    tmp_path: Path,
) -> None:
    database = new_database(tmp_path)
    database.record_sender_nonce("delivery-1", 1_700_000_000, EXPIRES_AT)

    database.record_sender_nonce("delivery-1", 1_700_000_060, EXPIRES_AT)

    with sqlite3.connect(database.path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM sender_auth_nonces"
        ).fetchone()
    assert count == (2,)


@pytest.mark.fast
def test_record_sender_nonce_allows_same_timestamp_with_a_different_key(
    tmp_path: Path,
) -> None:
    database = new_database(tmp_path)
    database.record_sender_nonce("delivery-1", 1_700_000_000, EXPIRES_AT)

    database.record_sender_nonce("delivery-2", 1_700_000_000, EXPIRES_AT)

    with sqlite3.connect(database.path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM sender_auth_nonces"
        ).fetchone()
    assert count == (2,)
