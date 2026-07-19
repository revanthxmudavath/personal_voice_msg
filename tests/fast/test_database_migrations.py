from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from personal_voice_msg.database import Database, MigrationError
from personal_voice_msg.normalization import normalized_hash

EXPECTED_TABLES = {
    "schema_migrations",
    "sources",
    "inspiration_cards",
    "messages",
    "runs",
    "audio_artifacts",
    "deliveries",
}


def read_table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def downgrade_current_database_to_v2(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DROP INDEX IF EXISTS message_history_normalized_hash_unique_idx"
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 3")


@pytest.mark.fast
def test_migration_succeeds_on_empty_sqlite_file(tmp_path: Path) -> None:
    database_path = tmp_path / "assistant.sqlite3"
    database_path.touch()

    Database(database_path).migrate()

    assert EXPECTED_TABLES <= read_table_names(database_path)


@pytest.mark.fast
def test_rerunning_migration_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "assistant.sqlite3"
    database = Database(database_path)

    database.migrate()
    database.migrate()

    with sqlite3.connect(database_path) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert versions == [(1,), (2,), (3,)]


@pytest.mark.fast
def test_version_one_is_recorded_exactly_once(tmp_path: Path) -> None:
    database_path = tmp_path / "assistant.sqlite3"

    Database(database_path).migrate()

    with sqlite3.connect(database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 1"
        ).fetchone()
    assert count == (1,)


@pytest.mark.fast
def test_newer_migration_version_fails_without_altering_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "newer.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, "
            "applied_at TEXT NOT NULL"
            ")"
        )
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (4, "future-version"),
        )
        connection.execute(
            "CREATE TABLE newer_schema_sentinel (value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO newer_schema_sentinel (value) VALUES (?)",
            ("preserve-me",),
        )

    with sqlite3.connect(database_path) as connection:
        schema_before = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index') ORDER BY name"
        ).fetchall()
        versions_before = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()

    with pytest.raises(MigrationError):
        Database(database_path).migrate()

    with sqlite3.connect(database_path) as connection:
        schema_after = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index') ORDER BY name"
        ).fetchall()
        versions_after = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        sentinel = connection.execute(
            "SELECT value FROM newer_schema_sentinel"
        ).fetchone()

    assert schema_after == schema_before
    assert versions_after == versions_before
    assert sentinel == ("preserve-me",)


@pytest.mark.fast
def test_unknown_migration_version_fails_without_altering_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, "
            "applied_at TEXT NOT NULL"
            ")"
        )
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (0, "unknown-version"),
        )
        connection.execute(
            "CREATE TABLE unknown_schema_sentinel (value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO unknown_schema_sentinel (value) VALUES (?)",
            ("preserve-me",),
        )

    bytes_before = database_path.read_bytes()
    with sqlite3.connect(database_path) as connection:
        logical_before = (
            connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index') ORDER BY name"
            ).fetchall(),
            connection.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall(),
            connection.execute(
                "SELECT value FROM unknown_schema_sentinel"
            ).fetchall(),
        )

    with pytest.raises(MigrationError):
        Database(database_path).migrate()

    with sqlite3.connect(database_path) as connection:
        logical_after = (
            connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index') ORDER BY name"
            ).fetchall(),
            connection.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall(),
            connection.execute(
                "SELECT value FROM unknown_schema_sentinel"
            ).fetchall(),
        )

    assert database_path.read_bytes() == bytes_before
    assert logical_after == logical_before


@pytest.mark.fast
@pytest.mark.parametrize(
    "corruption_sql",
    [
        "DROP TABLE deliveries",
        "ALTER TABLE sources DROP COLUMN retrieved_at",
    ],
    ids=["missing-required-table", "missing-required-column"],
)
def test_claimed_current_version_with_incomplete_schema_fails_closed(
    tmp_path: Path,
    corruption_sql: str,
) -> None:
    database_path = tmp_path / "incomplete.sqlite3"
    database = Database(database_path)
    database.migrate()
    with sqlite3.connect(database_path) as connection:
        connection.execute(corruption_sql)

    bytes_before = database_path.read_bytes()
    with sqlite3.connect(database_path) as connection:
        schema_before = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index') ORDER BY name"
        ).fetchall()
        versions_before = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()

    with pytest.raises(MigrationError):
        database.migrate()

    with sqlite3.connect(database_path) as connection:
        schema_after = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index') ORDER BY name"
        ).fetchall()
        versions_after = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert database_path.read_bytes() == bytes_before
    assert schema_after == schema_before
    assert versions_after == versions_before


@pytest.mark.fast
def test_claimed_current_version_with_weakened_constraints_fails_closed(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "weakened.sqlite3"
    database = Database(database_path)
    database.migrate()
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE deliveries")
        connection.execute(
            "CREATE TABLE deliveries ("
            "id INTEGER PRIMARY KEY, "
            "message_id INTEGER NOT NULL, "
            "recipient_key TEXT NOT NULL, "
            "pacific_date TEXT NOT NULL, "
            "state TEXT NOT NULL, "
            "provider_message_id TEXT, "
            "created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL"
            ")"
        )
        connection.execute(
            "CREATE INDEX deliveries_recipient_date_idx "
            "ON deliveries(message_id)"
        )

    bytes_before = database_path.read_bytes()
    with sqlite3.connect(database_path) as connection:
        schema_before = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE tbl_name = 'deliveries' ORDER BY name"
        ).fetchall()
        versions_before = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()

    with pytest.raises(MigrationError):
        database.migrate()

    with sqlite3.connect(database_path) as connection:
        schema_after = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE tbl_name = 'deliveries' ORDER BY name"
        ).fetchall()
        versions_after = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert database_path.read_bytes() == bytes_before
    assert schema_after == schema_before
    assert versions_after == versions_before


@pytest.mark.fast
def test_operational_connections_enforce_foreign_keys(tmp_path: Path) -> None:
    database_path = tmp_path / "assistant.sqlite3"
    database = Database(database_path)
    database.migrate()
    connection = database.connect()

    try:
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()
        assert enabled == (1,)

        connection.execute(
            "CREATE TABLE foreign_key_probe ("
            "source_id INTEGER NOT NULL REFERENCES sources(id)"
            ")"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO foreign_key_probe (source_id) VALUES (?)", (999_999,)
            )
    finally:
        connection.close()


@pytest.mark.fast
def test_new_database_instance_preserves_schema_and_data(tmp_path: Path) -> None:
    database_path = tmp_path / "assistant.sqlite3"
    first_database = Database(database_path)
    first_database.migrate()
    first_connection = first_database.connect()

    try:
        first_connection.execute(
            "CREATE TABLE reopen_probe (value TEXT NOT NULL)"
        )
        first_connection.execute(
            "INSERT INTO reopen_probe (value) VALUES (?)", ("preserved",)
        )
        first_connection.commit()
    finally:
        first_connection.close()

    second_database = Database(database_path)
    second_database.migrate()
    second_connection = second_database.connect()
    try:
        row = second_connection.execute("SELECT value FROM reopen_probe").fetchone()
        tables = {
            str(result[0])
            for result in second_connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        second_connection.close()

    assert row == ("preserved",)
    assert EXPECTED_TABLES <= tables


@pytest.mark.fast
def test_version_two_database_upgrades_to_unique_normalized_hashes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "version-two.sqlite3"
    database = Database(database_path)
    database.migrate()
    downgrade_current_database_to_v2(database_path)

    database.migrate()

    with sqlite3.connect(database_path) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        indexes = connection.execute(
            "PRAGMA index_list(message_history)"
        ).fetchall()
    assert versions == [(1,), (2,), (3,)]
    assert any(
        row[1] == "message_history_normalized_hash_unique_idx" and row[2] == 1
        for row in indexes
    )


@pytest.mark.fast
def test_version_three_migration_fails_closed_on_existing_exact_duplicates(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "duplicate-version-two.sqlite3"
    database = Database(database_path)
    database.migrate()
    downgrade_current_database_to_v2(database_path)
    duplicate_hash = normalized_hash("Your smile makes every morning brighter.")
    connection = database.connect()
    try:
        for text in ("First stored sentence.", "Second stored sentence."):
            cursor = connection.execute(
                """
                INSERT INTO messages (text, state, created_at, updated_at)
                VALUES (?, 'discovered', '2026-07-19T12:00:00+00:00',
                        '2026-07-19T12:00:00+00:00')
                """,
                (text,),
            )
            assert cursor.lastrowid is not None
            connection.execute(
                """
                INSERT INTO message_history (message_id, normalized_hash)
                VALUES (?, ?)
                """,
                (int(cursor.lastrowid), duplicate_hash),
            )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(MigrationError, match="duplicate"):
        database.migrate()

    with sqlite3.connect(database_path) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        history_rows = connection.execute(
            "SELECT message_id, normalized_hash FROM message_history "
            "ORDER BY message_id"
        ).fetchall()
        indexes = connection.execute(
            "PRAGMA index_list(message_history)"
        ).fetchall()
    assert versions == [(1,), (2,)]
    assert history_rows == [(1, duplicate_hash), (2, duplicate_hash)]
    assert not any(
        row[1] == "message_history_normalized_hash_unique_idx" for row in indexes
    )
