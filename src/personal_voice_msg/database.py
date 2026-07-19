from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

from personal_voice_msg.normalization import normalized_hash


class MessageState(StrEnum):
    DISCOVERED = "discovered"
    VALIDATED = "validated"
    APPROVED = "approved"
    QUEUED = "queued"
    RESERVED = "reserved"
    AUDIO_READY = "audio_ready"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    DELIVERY_UNKNOWN = "delivery_unknown"


CONTENT_TRANSITIONS = {
    MessageState.DISCOVERED: MessageState.VALIDATED,
    MessageState.VALIDATED: MessageState.APPROVED,
    MessageState.APPROVED: MessageState.QUEUED,
}
DELIVERY_TRANSITIONS = {
    MessageState.RESERVED: {MessageState.AUDIO_READY},
    MessageState.AUDIO_READY: {MessageState.SENDING},
    MessageState.SENDING: {
        MessageState.SENT,
        MessageState.FAILED,
        MessageState.DELIVERY_UNKNOWN,
    },
    MessageState.SENT: set(),
    MessageState.FAILED: set(),
    MessageState.DELIVERY_UNKNOWN: set(),
}
ALL_STATES_SQL = ", ".join(f"'{state.value}'" for state in MessageState)
DELIVERY_STATES_SQL = ", ".join(
    f"'{state.value}'" for state in DELIVERY_TRANSITIONS
)
CURRENT_SCHEMA_VERSION = 2
OPAQUE_RECIPIENT_KEY = re.compile(r"recipient_[A-Za-z0-9][A-Za-z0-9_-]{2,119}")


class DatabaseError(RuntimeError):
    """Base class for database boundary failures."""


class RecordNotFound(DatabaseError):
    """Raised when a requested record does not exist."""


class InvalidTransition(DatabaseError):
    """Raised when a state transition is not explicitly permitted."""


class DatabaseInvariantError(DatabaseError):
    """Raised when persisted delivery and message state disagree."""


class MigrationError(DatabaseError):
    """Raised when the database schema cannot be migrated safely."""


@dataclass(frozen=True, slots=True)
class Reservation:
    delivery_id: int
    message_id: int
    recipient_key: str
    pacific_date: date
    state: MessageState


SCHEMA_V1_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY,
        source_url TEXT NOT NULL,
        retrieved_at TEXT NOT NULL,
        rights_category TEXT NOT NULL,
        rights_evidence TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inspiration_cards (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY,
        inspiration_card_id INTEGER
            REFERENCES inspiration_cards(id) ON DELETE RESTRICT,
        text TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ({ALL_STATES_SQL})),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY,
        run_kind TEXT NOT NULL,
        pacific_date TEXT,
        state TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audio_artifacts (
        id INTEGER PRIMARY KEY,
        message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
        state TEXT NOT NULL,
        storage_key TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS deliveries (
        id INTEGER PRIMARY KEY,
        message_id INTEGER NOT NULL UNIQUE
            REFERENCES messages(id) ON DELETE RESTRICT,
        recipient_key TEXT NOT NULL,
        pacific_date TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ({DELIVERY_STATES_SQL})),
        provider_message_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (recipient_key, pacific_date)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS messages_state_id_idx
    ON messages(state, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS deliveries_recipient_date_idx
    ON deliveries(recipient_key, pacific_date)
    """,
)
SCHEMA_V2_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS message_history (
        message_id INTEGER PRIMARY KEY
            REFERENCES messages(id) ON DELETE CASCADE,
        normalized_hash TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS message_history_normalized_hash_idx
    ON message_history(normalized_hash)
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS message_history_fts USING fts5(
        text,
        content='messages',
        content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_history_ai AFTER INSERT ON messages BEGIN
        INSERT INTO message_history_fts(rowid, text) VALUES (new.id, new.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_history_ad AFTER DELETE ON messages BEGIN
        INSERT INTO message_history_fts(message_history_fts, rowid, text)
        VALUES ('delete', old.id, old.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_text_immutable
    BEFORE UPDATE OF text ON messages
    WHEN new.text IS NOT old.text BEGIN
        SELECT RAISE(ABORT, 'message text is immutable');
    END
    """,
)
EXPECTED_SCHEMA_V1_OBJECTS = {
    ("table", "schema_migrations"): SCHEMA_V1_STATEMENTS[0],
    ("table", "sources"): SCHEMA_V1_STATEMENTS[1],
    ("table", "inspiration_cards"): SCHEMA_V1_STATEMENTS[2],
    ("table", "messages"): SCHEMA_V1_STATEMENTS[3],
    ("table", "runs"): SCHEMA_V1_STATEMENTS[4],
    ("table", "audio_artifacts"): SCHEMA_V1_STATEMENTS[5],
    ("table", "deliveries"): SCHEMA_V1_STATEMENTS[6],
    ("index", "messages_state_id_idx"): SCHEMA_V1_STATEMENTS[7],
    ("index", "deliveries_recipient_date_idx"): SCHEMA_V1_STATEMENTS[8],
}
EXPECTED_SCHEMA_V2_OBJECTS = {
    **EXPECTED_SCHEMA_V1_OBJECTS,
    ("table", "message_history"): SCHEMA_V2_STATEMENTS[0],
    ("index", "message_history_normalized_hash_idx"): SCHEMA_V2_STATEMENTS[1],
    ("table", "message_history_fts"): SCHEMA_V2_STATEMENTS[2],
    ("trigger", "messages_history_ai"): SCHEMA_V2_STATEMENTS[3],
    ("trigger", "messages_history_ad"): SCHEMA_V2_STATEMENTS[4],
    ("trigger", "messages_text_immutable"): SCHEMA_V2_STATEMENTS[5],
}


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _migration_versions(connection: sqlite3.Connection) -> set[int]:
    table = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'schema_migrations'
        """
    ).fetchone()
    if table is None:
        return set()
    try:
        rows = connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    except sqlite3.DatabaseError:
        raise MigrationError("migration metadata is invalid") from None
    return {int(row[0]) for row in rows}


def _normalize_schema_sql(value: str) -> str:
    without_guard = value.casefold().replace("if not exists", "")
    return " ".join(without_guard.split())


def _validate_schema(
    connection: sqlite3.Connection,
    expected_objects: dict[tuple[str, str], str],
) -> None:
    for (object_type, name), expected_sql in expected_objects.items():
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
            (object_type, name),
        ).fetchone()
        if (
            row is None
            or not isinstance(row[0], str)
            or _normalize_schema_sql(row[0]) != _normalize_schema_sql(expected_sql)
        ):
            raise MigrationError(f"database schema object {name} is invalid")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            versions = _migration_versions(connection)
            if versions not in (set(), {1}, {1, CURRENT_SCHEMA_VERSION}):
                raise MigrationError("database has an unknown migration version")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(SCHEMA_V1_STATEMENTS[0])
            versions = _migration_versions(connection)
            if versions not in (set(), {1}, {1, CURRENT_SCHEMA_VERSION}):
                raise MigrationError("database has an unknown migration version")
            if not versions:
                for statement in SCHEMA_V1_STATEMENTS[1:]:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (1)"
                )
                versions = {1}

            _validate_schema(connection, EXPECTED_SCHEMA_V1_OBJECTS)
            if versions == {1}:
                for statement in SCHEMA_V2_STATEMENTS:
                    connection.execute(statement)
                rows = connection.execute("SELECT id, text FROM messages").fetchall()
                connection.executemany(
                    """
                    INSERT INTO message_history (message_id, normalized_hash)
                    VALUES (?, ?)
                    """,
                    ((int(row[0]), normalized_hash(str(row[1]))) for row in rows),
                )
                connection.execute(
                    "INSERT INTO message_history_fts(message_history_fts) "
                    "VALUES ('rebuild')"
                )
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (CURRENT_SCHEMA_VERSION,),
                )

            _validate_schema(connection, EXPECTED_SCHEMA_V2_OBJECTS)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_message(self, text: str, now: datetime) -> int:
        if not text.strip():
            raise ValueError("message text must not be empty")
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO messages (text, state, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (text, MessageState.DISCOVERED.value, timestamp, timestamp),
            )
            if cursor.lastrowid is None:
                raise DatabaseInvariantError("message insert did not return an id")
            message_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO message_history (message_id, normalized_hash)
                VALUES (?, ?)
                """,
                (message_id, normalized_hash(text)),
            )
            return message_id

    def transition_message(
        self,
        message_id: int,
        target: MessageState,
        now: datetime,
    ) -> None:
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("message does not exist")
            current = MessageState(row[0])
            if CONTENT_TRANSITIONS.get(current) is not target:
                raise InvalidTransition(
                    f"message cannot transition from {current.value} to {target.value}"
                )
            updated = connection.execute(
                """
                UPDATE messages
                SET state = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (target.value, timestamp, message_id, current.value),
            )
            if updated.rowcount != 1:
                raise DatabaseInvariantError("message state changed concurrently")

    def reserve_next_message(
        self,
        recipient_key: str,
        pacific_date: date,
        now: datetime,
    ) -> Reservation | None:
        if not OPAQUE_RECIPIENT_KEY.fullmatch(recipient_key):
            raise ValueError("recipient key must be an opaque identifier")
        if not isinstance(pacific_date, date) or isinstance(pacific_date, datetime):
            raise ValueError("Pacific date must be a date without a time")
        date_value = pacific_date.isoformat()
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT 1 FROM deliveries
                WHERE recipient_key = ? AND pacific_date = ?
                """,
                (recipient_key, date_value),
            ).fetchone()
            if existing is not None:
                return None

            message = connection.execute(
                """
                SELECT id FROM messages
                WHERE state = ?
                ORDER BY id
                LIMIT 1
                """,
                (MessageState.QUEUED.value,),
            ).fetchone()
            if message is None:
                return None
            message_id = int(message[0])

            updated = connection.execute(
                """
                UPDATE messages
                SET state = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    MessageState.RESERVED.value,
                    timestamp,
                    message_id,
                    MessageState.QUEUED.value,
                ),
            )
            if updated.rowcount != 1:
                raise DatabaseInvariantError("queued message changed concurrently")

            cursor = connection.execute(
                """
                INSERT INTO deliveries (
                    message_id,
                    recipient_key,
                    pacific_date,
                    state,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    recipient_key,
                    date_value,
                    MessageState.RESERVED.value,
                    timestamp,
                    timestamp,
                ),
            )
            if cursor.lastrowid is None:
                raise DatabaseInvariantError("delivery insert did not return an id")
            return Reservation(
                delivery_id=int(cursor.lastrowid),
                message_id=message_id,
                recipient_key=recipient_key,
                pacific_date=pacific_date,
                state=MessageState.RESERVED,
            )

    def transition_delivery(
        self,
        delivery_id: int,
        target: MessageState,
        now: datetime,
    ) -> None:
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT deliveries.state, deliveries.message_id, messages.state
                FROM deliveries
                JOIN messages ON messages.id = deliveries.message_id
                WHERE deliveries.id = ?
                """,
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("delivery does not exist")
            current = MessageState(row[0])
            message_id = int(row[1])
            message_state = MessageState(row[2])
            if message_state is not current:
                raise DatabaseInvariantError("message and delivery state disagree")
            if target not in DELIVERY_TRANSITIONS.get(current, set()):
                raise InvalidTransition(
                    f"delivery cannot transition from {current.value} to {target.value}"
                )

            delivery_update = connection.execute(
                """
                UPDATE deliveries
                SET state = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (target.value, timestamp, delivery_id, current.value),
            )
            message_update = connection.execute(
                """
                UPDATE messages
                SET state = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (target.value, timestamp, message_id, current.value),
            )
            if delivery_update.rowcount != 1 or message_update.rowcount != 1:
                raise DatabaseInvariantError("delivery state changed concurrently")

    def get_message_state(self, message_id: int) -> MessageState:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT state FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("message does not exist")
        return MessageState(row[0])

    def get_delivery_state(self, delivery_id: int) -> MessageState:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT state FROM deliveries WHERE id = ?",
                (delivery_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("delivery does not exist")
        return MessageState(row[0])

    def count_deliveries(self, recipient_key: str, pacific_date: date) -> int:
        connection = self.connect()
        try:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM deliveries
                WHERE recipient_key = ? AND pacific_date = ?
                """,
                (recipient_key, pacific_date.isoformat()),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise DatabaseInvariantError("delivery count query returned no row")
        return int(row[0])
