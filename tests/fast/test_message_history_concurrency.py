from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.history import (
    DuplicateDecision,
    DuplicateReason,
    MessageHistory,
)

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def evaluate_concurrently(
    database_path: Path, candidates: tuple[str, str]
) -> list[DuplicateDecision]:
    ready = Barrier(len(candidates))

    def evaluate(candidate: str) -> DuplicateDecision:
        history = MessageHistory(Database(database_path))
        ready.wait(timeout=5)
        return history.evaluate_and_record(candidate, NOW)

    with ThreadPoolExecutor(max_workers=len(candidates)) as workers:
        futures = [workers.submit(evaluate, candidate) for candidate in candidates]
        return [future.result(timeout=10) for future in futures]


def assert_one_persisted_winner(
    database_path: Path,
    decisions: list[DuplicateDecision],
    rejection_reason: DuplicateReason,
) -> None:
    accepted = [decision for decision in decisions if decision.accepted]
    rejected = [decision for decision in decisions if not decision.accepted]

    assert len(accepted) == 1
    assert accepted[0].reason is None
    assert accepted[0].recorded_message_id is not None
    assert len(rejected) == 1
    assert rejected[0].reason is rejection_reason
    assert rejected[0].recorded_message_id is None

    with sqlite3.connect(database_path) as connection:
        message_rows = connection.execute("SELECT id FROM messages").fetchall()
        history_rows = connection.execute(
            "SELECT message_id FROM message_history"
        ).fetchall()

    assert message_rows == [(accepted[0].recorded_message_id,)]
    assert history_rows == [(accepted[0].recorded_message_id,)]


@pytest.mark.fast
def test_exact_normalized_concurrent_submissions_record_only_one(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "exact-race.sqlite3"
    Database(database_path).migrate()

    decisions = evaluate_concurrently(
        database_path,
        (
            "Your smile makes every morning brighter.",
            "  YOUR smile---makes every morning brighter!!!  ",
        ),
    )

    assert_one_persisted_winner(database_path, decisions, DuplicateReason.EXACT)


@pytest.mark.fast
def test_near_duplicate_concurrent_submissions_record_only_one(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "near-race.sqlite3"
    Database(database_path).migrate()

    decisions = evaluate_concurrently(
        database_path,
        (
            "Your laughter makes every ordinary moment feel brighter.",
            "Every ordinary moment feels brighter because of your laughter.",
        ),
    )

    assert_one_persisted_winner(database_path, decisions, DuplicateReason.NEAR)


@pytest.mark.fast
def test_migrated_schema_enforces_unique_normalized_hashes(tmp_path: Path) -> None:
    database_path = tmp_path / "unique-hash.sqlite3"
    database = Database(database_path)
    database.migrate()
    first_message_id = database.create_message(
        "Your smile makes every morning brighter.", NOW
    )

    connection = database.connect()
    try:
        duplicate_hash = connection.execute(
            "SELECT normalized_hash FROM message_history WHERE message_id = ?",
            (first_message_id,),
        ).fetchone()
        assert duplicate_hash is not None
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            INSERT INTO messages (text, state, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "A clearly different message.",
                MessageState.DISCOVERED.value,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        assert cursor.lastrowid is not None

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO message_history (message_id, normalized_hash)
                VALUES (?, ?)
                """,
                (int(cursor.lastrowid), str(duplicate_hash[0])),
            )
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.fast
def test_duplicate_database_insert_rolls_back_message_hash_and_fts(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "duplicate-rollback.sqlite3"
    database = Database(database_path)
    database.migrate()
    first_message_id = database.create_message(
        "Your smile makes every morning brighter.", NOW
    )

    with pytest.raises(sqlite3.IntegrityError):
        database.create_message(
            "YOUR smile---makes every morning brighter!!!",
            NOW,
        )

    with sqlite3.connect(database_path) as connection:
        messages = connection.execute("SELECT id FROM messages").fetchall()
        history = connection.execute(
            "SELECT message_id FROM message_history"
        ).fetchall()
        fts = connection.execute(
            "SELECT rowid FROM message_history_fts "
            "WHERE message_history_fts MATCH 'morning'"
        ).fetchall()
    assert messages == [(first_message_id,)]
    assert history == [(first_message_id,)]
    assert fts == [(first_message_id,)]
