from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from personal_voice_msg.normalization import normalized_hash
from personal_voice_msg.scheduling import (
    PACIFIC,
    ScheduleKind,
    TriggerStatus,
    classify_trigger,
    planned_triggers_for_date,
)


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


class DailyRunState(StrEnum):
    CLAIMED = "claimed"
    COMPLETED = "completed"


class DisableReason(StrEnum):
    STOP_COMMAND = "stop_command"
    BLOCKED_BY_USER = "blocked_by_user"
    ADMIN_KILL_SWITCH = "admin_kill_switch"


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
    MessageState.FAILED: {MessageState.AUDIO_READY},
    MessageState.DELIVERY_UNKNOWN: {
        MessageState.AUDIO_READY,
        MessageState.SENT,
    },
}
ALL_STATES_SQL = ", ".join(f"'{state.value}'" for state in MessageState)
DELIVERY_STATES_SQL = ", ".join(
    f"'{state.value}'" for state in DELIVERY_TRANSITIONS
)
_ATTEMPT_OUTCOMES = {
    MessageState.SENT,
    MessageState.FAILED,
    MessageState.DELIVERY_UNKNOWN,
}
CURRENT_SCHEMA_VERSION = 8
OPAQUE_RECIPIENT_KEY = re.compile(r"recipient_[A-Za-z0-9][A-Za-z0-9_-]{2,119}")


def recipient_key_for_chat_id(chat_id: int) -> str:
    """Canonical recipient_key for a given enrolled telegram_chat_id --
    ties run_daily_send's idempotency boundary to the real delivery
    destination. See
    docs/superpowers/specs/2026-08-19-t17-telegram-consent-stop-killswitch-design.md.
    """

    return f"recipient_telegram_{chat_id}"


class DatabaseError(RuntimeError):
    """Base class for database boundary failures."""


class RecordNotFound(DatabaseError):
    """Raised when a requested record does not exist."""


class InvalidTransition(DatabaseError):
    """Raised when a state transition is not explicitly permitted."""


class DatabaseInvariantError(DatabaseError):
    """Raised when persisted delivery and message state disagree."""


class ReplayDetected(DatabaseError):
    """Raised when a sender-auth (idempotency_key, timestamp) pair repeats."""


class MigrationError(DatabaseError):
    """Raised when the database schema cannot be migrated safely."""


@dataclass(frozen=True, slots=True)
class Reservation:
    delivery_id: int
    message_id: int
    recipient_key: str
    pacific_date: date
    state: MessageState


@dataclass(frozen=True, slots=True)
class DailyRun:
    run_id: int
    recipient_key: str
    pacific_date: date
    state: DailyRunState
    started_at: datetime
    finished_at: datetime | None


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
SCHEMA_V3_STATEMENTS = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS
        message_history_normalized_hash_unique_idx
    ON message_history(normalized_hash)
    """,
)
SCHEMA_V4_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS daily_runs (
        id INTEGER PRIMARY KEY,
        recipient_key TEXT NOT NULL,
        pacific_date TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('claimed', 'completed')),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        UNIQUE (recipient_key, pacific_date),
        CHECK (
            (state = 'claimed' AND finished_at IS NULL)
            OR (state = 'completed' AND finished_at IS NOT NULL)
        )
    )
    """,
)
SCHEMA_V5_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS message_rejections (
        id INTEGER PRIMARY KEY,
        message_id INTEGER NOT NULL UNIQUE
            REFERENCES messages(id) ON DELETE RESTRICT,
        reason TEXT NOT NULL,
        rejected_at TEXT NOT NULL
    )
    """,
)
# T15's sender authentication-layer replay protection (docs/task-logs/T15.md)
# -- distinct from T16's exactly-once delivery/retry bookkeeping.
SCHEMA_V6_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS sender_auth_nonces (
        idempotency_key TEXT NOT NULL,
        timestamp INTEGER NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (idempotency_key, timestamp)
    )
    """,
)
# T16's durable audio storage and delivery attempt records (docs/task-logs/T16.md)
SCHEMA_V7_STATEMENTS = (
    "ALTER TABLE deliveries ADD COLUMN audio_data BLOB",
    """
    CREATE TABLE IF NOT EXISTS delivery_attempts (
        id INTEGER PRIMARY KEY,
        delivery_id INTEGER NOT NULL
            REFERENCES deliveries(id) ON DELETE RESTRICT,
        attempted_at TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (
            outcome IN ('sent', 'failed', 'delivery_unknown')
        ),
        provider_message_id TEXT
    )
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
EXPECTED_SCHEMA_V3_OBJECTS = {
    **EXPECTED_SCHEMA_V2_OBJECTS,
    (
        "index",
        "message_history_normalized_hash_unique_idx",
    ): SCHEMA_V3_STATEMENTS[0],
}
EXPECTED_SCHEMA_V4_OBJECTS = {
    **EXPECTED_SCHEMA_V3_OBJECTS,
    ("table", "daily_runs"): SCHEMA_V4_STATEMENTS[0],
}
EXPECTED_SCHEMA_V5_OBJECTS = {
    **EXPECTED_SCHEMA_V4_OBJECTS,
    ("table", "message_rejections"): SCHEMA_V5_STATEMENTS[0],
}
EXPECTED_SCHEMA_V6_OBJECTS = {
    **EXPECTED_SCHEMA_V5_OBJECTS,
    ("table", "sender_auth_nonces"): SCHEMA_V6_STATEMENTS[0],
}
# Build V7 objects by extending V6
EXPECTED_SCHEMA_V7_OBJECTS = {
    **EXPECTED_SCHEMA_V6_OBJECTS,
    ("table", "delivery_attempts"): SCHEMA_V7_STATEMENTS[1],
}
# Override deliveries table with post-ALTER schema
# Literal schema text from real migration run (noqa: E501 for captured text)
_v7_deliveries_sql = (  # noqa: E501
    "CREATE TABLE deliveries (\n"
    "        id INTEGER PRIMARY KEY,\n"
    "        message_id INTEGER NOT NULL UNIQUE\n"
    "            REFERENCES messages(id) ON DELETE RESTRICT,\n"
    "        recipient_key TEXT NOT NULL,\n"
    "        pacific_date TEXT NOT NULL,\n"
    "        state TEXT NOT NULL CHECK (state IN ('reserved', 'audio_ready', "
    "'sending', 'sent', 'failed', 'delivery_unknown')),\n"
    "        provider_message_id TEXT,\n"
    "        created_at TEXT NOT NULL,\n"
    "        updated_at TEXT NOT NULL, audio_data BLOB,\n"
    "        UNIQUE (recipient_key, pacific_date)\n"
    "    )"
)
EXPECTED_SCHEMA_V7_OBJECTS[("table", "deliveries")] = _v7_deliveries_sql

# T17's durable sending-control state, audit trail, and inbound-poll offset
# cursor. See: docs/superpowers/specs/
# 2026-08-19-t17-telegram-consent-stop-killswitch-design.md
SCHEMA_V8_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS sending_control (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        enabled INTEGER NOT NULL,
        reason TEXT,
        changed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sending_control_events (
        id INTEGER PRIMARY KEY,
        enabled INTEGER NOT NULL,
        reason TEXT,
        note TEXT,
        changed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS telegram_inbound_offset (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        next_offset INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)
EXPECTED_SCHEMA_V8_OBJECTS = {
    **EXPECTED_SCHEMA_V7_OBJECTS,
    ("table", "sending_control"): SCHEMA_V8_STATEMENTS[0],
    ("table", "sending_control_events"): SCHEMA_V8_STATEMENTS[1],
    ("table", "telegram_inbound_offset"): SCHEMA_V8_STATEMENTS[2],
}


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _daily_run_from_row(row: sqlite3.Row | tuple[object, ...]) -> DailyRun:
    finished_at = None if row[5] is None else datetime.fromisoformat(str(row[5]))
    return DailyRun(
        run_id=cast(int, row[0]),
        recipient_key=str(row[1]),
        pacific_date=date.fromisoformat(str(row[2])),
        state=DailyRunState(str(row[3])),
        started_at=datetime.fromisoformat(str(row[4])),
        finished_at=finished_at,
    )


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


def _stage_schema_objects(
    expected_objects: dict[tuple[str, str], str],
    deliveries_already_altered: bool,
) -> dict[tuple[str, str], str]:
    """Return expected_objects with post-ALTER deliveries schema if V7 was reached."""
    if not deliveries_already_altered:
        return expected_objects
    return {
        **expected_objects,
        ("table", "deliveries"): EXPECTED_SCHEMA_V7_OBJECTS[("table", "deliveries")],
    }


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
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
        connection = self._connect()
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
        connection = self._connect()
        try:
            versions = _migration_versions(connection)
            versions_at_entry = versions  # Save initial state for schema validation
            v7_reached = 7 in versions_at_entry
            if versions not in (
                set(),
                {1},
                {1, 2},
                {1, 2, 3},
                {1, 2, 3, 4},
                {1, 2, 3, 4, 5},
                {1, 2, 3, 4, 5, 6},
                {1, 2, 3, 4, 5, 6, 7},
                {1, 2, 3, 4, 5, 6, 7, CURRENT_SCHEMA_VERSION},
            ):
                raise MigrationError("database has an unknown migration version")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(SCHEMA_V1_STATEMENTS[0])
            versions = _migration_versions(connection)
            if versions not in (
                set(),
                {1},
                {1, 2},
                {1, 2, 3},
                {1, 2, 3, 4},
                {1, 2, 3, 4, 5},
                {1, 2, 3, 4, 5, 6},
                {1, 2, 3, 4, 5, 6, 7},
                {1, 2, 3, 4, 5, 6, 7, CURRENT_SCHEMA_VERSION},
            ):
                raise MigrationError("database has an unknown migration version")
            if not versions:
                for statement in SCHEMA_V1_STATEMENTS[1:]:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (1)"
                )
                versions = {1}

            staged_objects_v1 = _stage_schema_objects(
                EXPECTED_SCHEMA_V1_OBJECTS, v7_reached
            )
            _validate_schema(connection, staged_objects_v1)
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
                    (2,),
                )
                versions = {1, 2}

            staged_objects_v2 = _stage_schema_objects(
                EXPECTED_SCHEMA_V2_OBJECTS, v7_reached
            )
            _validate_schema(connection, staged_objects_v2)
            if versions == {1, 2}:
                try:
                    for statement in SCHEMA_V3_STATEMENTS:
                        connection.execute(statement)
                except sqlite3.IntegrityError:
                    raise MigrationError(
                        "database contains duplicate normalized message hashes"
                    ) from None
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (3,),
                )
                versions = {1, 2, 3}

            staged_objects_v3 = _stage_schema_objects(
                EXPECTED_SCHEMA_V3_OBJECTS, v7_reached
            )
            _validate_schema(connection, staged_objects_v3)
            if versions == {1, 2, 3}:
                for statement in SCHEMA_V4_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (4,),
                )
                versions = {1, 2, 3, 4}

            staged_objects_v4 = _stage_schema_objects(
                EXPECTED_SCHEMA_V4_OBJECTS, v7_reached
            )
            _validate_schema(connection, staged_objects_v4)
            if versions == {1, 2, 3, 4}:
                for statement in SCHEMA_V5_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (5,),
                )
                versions = {1, 2, 3, 4, 5}

            staged_objects_v5 = _stage_schema_objects(
                EXPECTED_SCHEMA_V5_OBJECTS, v7_reached
            )
            _validate_schema(connection, staged_objects_v5)
            if versions == {1, 2, 3, 4, 5}:
                for statement in SCHEMA_V6_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (6,),
                )
                versions = {1, 2, 3, 4, 5, 6}

            staged_objects_v6 = _stage_schema_objects(
                EXPECTED_SCHEMA_V6_OBJECTS, v7_reached
            )
            _validate_schema(connection, staged_objects_v6)
            if versions == {1, 2, 3, 4, 5, 6}:
                for statement in SCHEMA_V7_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (7,),
                )
                versions = {1, 2, 3, 4, 5, 6, 7}

            _validate_schema(connection, EXPECTED_SCHEMA_V7_OBJECTS)
            if versions == {1, 2, 3, 4, 5, 6, 7}:
                for statement in SCHEMA_V8_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (CURRENT_SCHEMA_VERSION,),
                )

            _validate_schema(connection, EXPECTED_SCHEMA_V8_OBJECTS)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_daily_run(
        self,
        recipient_key: str,
        pacific_date: date,
        now: datetime,
    ) -> DailyRun | None:
        if not OPAQUE_RECIPIENT_KEY.fullmatch(recipient_key):
            raise ValueError("recipient key must be an opaque identifier")
        if not isinstance(pacific_date, date) or isinstance(pacific_date, datetime):
            raise ValueError("Pacific date must be a date without a time")
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT 1 FROM daily_runs
                WHERE recipient_key = ? AND pacific_date = ?
                """,
                (recipient_key, pacific_date.isoformat()),
            ).fetchone()
            if existing is not None:
                return None
            prepare_trigger = next(
                trigger
                for trigger in planned_triggers_for_date(pacific_date)
                if trigger.kind is ScheduleKind.DAILY_PREPARE
            )
            if classify_trigger(prepare_trigger, now) is not TriggerStatus.DUE:
                raise ValueError(
                    "daily run can only be claimed in the prepare window"
                )
            row = connection.execute(
                """
                INSERT INTO daily_runs (
                    recipient_key,
                    pacific_date,
                    state,
                    started_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT (recipient_key, pacific_date) DO NOTHING
                RETURNING id, recipient_key, pacific_date, state,
                          started_at, finished_at
                """,
                (
                    recipient_key,
                    pacific_date.isoformat(),
                    DailyRunState.CLAIMED.value,
                    timestamp,
                ),
            ).fetchone()
        return None if row is None else _daily_run_from_row(row)

    def get_daily_run(
        self,
        recipient_key: str,
        pacific_date: date,
    ) -> DailyRun | None:
        if not OPAQUE_RECIPIENT_KEY.fullmatch(recipient_key):
            raise ValueError("recipient key must be an opaque identifier")
        if not isinstance(pacific_date, date) or isinstance(pacific_date, datetime):
            raise ValueError("Pacific date must be a date without a time")
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT id, recipient_key, pacific_date, state,
                       started_at, finished_at
                FROM daily_runs
                WHERE recipient_key = ? AND pacific_date = ?
                """,
                (recipient_key, pacific_date.isoformat()),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else _daily_run_from_row(row)

    def complete_daily_run(self, run_id: int, now: datetime) -> DailyRun:
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT id, recipient_key, pacific_date, state,
                       started_at, finished_at
                FROM daily_runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("daily run does not exist")
            if DailyRunState(str(row[3])) is not DailyRunState.CLAIMED:
                raise InvalidTransition("daily run is already completed")
            started_at = datetime.fromisoformat(str(row[4]))
            completed_at = datetime.fromisoformat(timestamp)
            if completed_at < started_at:
                raise ValueError(
                    "daily run completion cannot be before its start"
                )
            pacific_date = date.fromisoformat(str(row[2]))
            if completed_at.astimezone(PACIFIC).date() != pacific_date:
                raise ValueError(
                    "daily run completion must be on its Pacific date"
                )
            updated = connection.execute(
                """
                UPDATE daily_runs
                SET state = ?, finished_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    DailyRunState.COMPLETED.value,
                    timestamp,
                    run_id,
                    DailyRunState.CLAIMED.value,
                ),
            )
            if updated.rowcount != 1:
                raise DatabaseInvariantError("daily run state changed concurrently")
            completed_row = (
                row[0],
                row[1],
                row[2],
                DailyRunState.COMPLETED.value,
                row[4],
                timestamp,
            )
        return _daily_run_from_row(completed_row)

    def _create_message_in_transaction(
        self,
        connection: sqlite3.Connection,
        text: str,
        now: datetime,
    ) -> int:
        if not connection.in_transaction:
            raise DatabaseInvariantError(
                "message insertion requires an active transaction"
            )
        if not text.strip():
            raise ValueError("message text must not be empty")
        timestamp = _timestamp(now)
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

    def _transition_message_in_transaction(
        self,
        connection: sqlite3.Connection,
        message_id: int,
        target: MessageState,
        timestamp: str,
    ) -> None:
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

    def transition_message(
        self,
        message_id: int,
        target: MessageState,
        now: datetime,
    ) -> None:
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            self._transition_message_in_transaction(
                connection, message_id, target, timestamp
            )

    def reject_message(self, message_id: int, reason: str, now: datetime) -> None:
        """Atomically record a safety rejection without deleting the message.

        A message that fails safety review stays at `VALIDATED` (walking
        there first from `DISCOVERED` if needed, in the same transaction)
        with a matching `message_rejections` row -- see
        `IMPLEMENTATION_PLAN.md`'s T12 "Pre-T12 decisions" block.
        """
        if not reason.strip():
            raise ValueError("rejection reason must not be empty")
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("message does not exist")
            current = MessageState(row[0])
            if current is MessageState.DISCOVERED:
                self._transition_message_in_transaction(
                    connection, message_id, MessageState.VALIDATED, timestamp
                )
            elif current is not MessageState.VALIDATED:
                raise InvalidTransition(
                    f"message cannot be rejected from state {current.value}"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO message_rejections (message_id, reason, rejected_at)
                    VALUES (?, ?, ?)
                    """,
                    (message_id, reason, timestamp),
                )
            except sqlite3.IntegrityError:
                raise DatabaseInvariantError(
                    "message already has a rejection recorded"
                ) from None

    def approve_message(self, message_id: int, now: datetime) -> None:
        """Atomically walk a message from its current content state to `QUEUED`."""
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("message does not exist")
            current = MessageState(row[0])
            if current is MessageState.QUEUED:
                raise InvalidTransition("message is already queued")
            while current is not MessageState.QUEUED:
                target = CONTENT_TRANSITIONS.get(current)
                if target is None:
                    raise InvalidTransition(
                        f"message cannot be approved from state {current.value}"
                    )
                self._transition_message_in_transaction(
                    connection, message_id, target, timestamp
                )
                current = target

    def next_unjudged_message(self) -> tuple[int, str] | None:
        """Return the oldest message still awaiting a safety decision.

        Candidates are `DISCOVERED` (never processed) or `VALIDATED`
        without a `message_rejections` row (approved-in-progress or
        interrupted before judging finished); a `VALIDATED` message that
        already has a rejection row is permanently excluded.
        """
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT id, text FROM messages
                WHERE state IN (?, ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM message_rejections
                      WHERE message_rejections.message_id = messages.id
                  )
                ORDER BY id
                LIMIT 1
                """,
                (MessageState.DISCOVERED.value, MessageState.VALIDATED.value),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else (int(row[0]), str(row[1]))

    def count_queued_messages(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE state = ?",
                (MessageState.QUEUED.value,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise DatabaseInvariantError("queued message count query returned no row")
        return int(row[0])

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

    def mark_audio_ready(
        self, delivery_id: int, audio_bytes: bytes, now: datetime
    ) -> None:
        """Atomically persist produced audio and flip RESERVED -> AUDIO_READY.

        Storing the bytes and the state transition in one transaction means a
        delivery can never be left AUDIO_READY with no recoverable audio --
        see docs/superpowers/specs/2026-08-09-t16-exactly-once-delivery-design.md.
        """
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT deliveries.state, deliveries.message_id, messages.state "
                "FROM deliveries JOIN messages ON messages.id = deliveries.message_id "
                "WHERE deliveries.id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("delivery does not exist")
            current = MessageState(row[0])
            message_id = int(row[1])
            message_state = MessageState(row[2])
            if message_state is not current:
                raise DatabaseInvariantError("message and delivery state disagree")
            target = MessageState.AUDIO_READY
            if current is not MessageState.RESERVED:
                raise InvalidTransition(
                    f"delivery cannot transition from {current.value} "
                    f"to {target.value}"
                )
            delivery_update = connection.execute(
                """
                UPDATE deliveries
                SET state = ?, audio_data = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (target.value, audio_bytes, timestamp, delivery_id, current.value),
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

    def record_delivery_attempt(
        self,
        delivery_id: int,
        outcome: MessageState,
        now: datetime,
        provider_message_id: str | None = None,
    ) -> None:
        """Atomically record one concluded WAHA send attempt.

        Inserts a ``delivery_attempts`` audit row, transitions the delivery
        (and its message) to ``outcome``, and -- only when ``outcome`` is
        ``SENT`` -- writes ``deliveries.provider_message_id``, all in one
        transaction. This is the literal "persist WAHA message identifiers
        and attempt records transactionally" requirement from
        IMPLEMENTATION_PLAN.md's T16 section.
        """
        if outcome not in _ATTEMPT_OUTCOMES:
            raise ValueError(f"{outcome.value} is not a valid attempt outcome")
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT deliveries.state, deliveries.message_id, messages.state "
                "FROM deliveries JOIN messages ON messages.id = deliveries.message_id "
                "WHERE deliveries.id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound("delivery does not exist")
            current = MessageState(row[0])
            message_id = int(row[1])
            message_state = MessageState(row[2])
            if message_state is not current:
                raise DatabaseInvariantError("message and delivery state disagree")
            if outcome not in DELIVERY_TRANSITIONS.get(current, set()):
                raise InvalidTransition(
                    f"delivery cannot transition from {current.value} "
                    f"to {outcome.value}"
                )

            connection.execute(
                """
                INSERT INTO delivery_attempts (
                    delivery_id, attempted_at, outcome, provider_message_id
                )
                VALUES (?, ?, ?, ?)
                """,
                (delivery_id, timestamp, outcome.value, provider_message_id),
            )
            if outcome is MessageState.SENT:
                delivery_update = connection.execute(
                    """
                    UPDATE deliveries
                    SET state = ?, provider_message_id = ?, updated_at = ?
                    WHERE id = ? AND state = ?
                    """,
                    (
                        outcome.value,
                        provider_message_id,
                        timestamp,
                        delivery_id,
                        current.value,
                    ),
                )
            else:
                delivery_update = connection.execute(
                    """
                    UPDATE deliveries SET state = ?, updated_at = ?
                    WHERE id = ? AND state = ?
                    """,
                    (outcome.value, timestamp, delivery_id, current.value),
                )
            message_update = connection.execute(
                """
                UPDATE messages
                SET state = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (outcome.value, timestamp, message_id, current.value),
            )
            if delivery_update.rowcount != 1 or message_update.rowcount != 1:
                raise DatabaseInvariantError("delivery state changed concurrently")

    def clear_audio_data(self, delivery_id: int, now: datetime) -> None:
        """Null out a delivery's stored audio once it is confirmed SENT."""
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound("delivery does not exist")
            if MessageState(row[0]) is not MessageState.SENT:
                raise InvalidTransition(
                    "audio can only be cleared once the delivery is sent"
                )
            updated = connection.execute(
                "UPDATE deliveries SET audio_data = NULL, updated_at = ? "
                "WHERE id = ? AND state = ?",
                (timestamp, delivery_id, MessageState.SENT.value),
            )
            if updated.rowcount != 1:
                raise DatabaseInvariantError("delivery state changed concurrently")

    def get_audio_data(self, delivery_id: int) -> bytes:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT audio_data FROM deliveries WHERE id = ?",
                (delivery_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("delivery does not exist")
        if row[0] is None:
            raise DatabaseInvariantError("delivery has no stored audio data")
        return bytes(row[0])

    def get_message_state(self, message_id: int) -> MessageState:
        connection = self._connect()
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

    def get_delivery_for_date(
        self, recipient_key: str, pacific_date: date
    ) -> int | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT id FROM deliveries
                WHERE recipient_key = ? AND pacific_date = ?
                """,
                (recipient_key, pacific_date.isoformat()),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else int(row[0])

    def get_delivery_state(self, delivery_id: int) -> MessageState:
        connection = self._connect()
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

    def get_delivery_updated_at(self, delivery_id: int) -> datetime:
        """Return ``deliveries.updated_at`` -- the timestamp of this
        delivery's most recent committed state transition.

        Used by the ``DELIVERY_UNKNOWN`` reconciliation path (T16 Task 13
        fix, finding F2) as the reconciliation window start. That window
        must anchor to when the delivery actually entered ``SENDING``,
        not to whatever real wall-clock instant later discovered it there
        after a crash -- see ``delivery.py``'s ``SENDING``-on-entry branch,
        which stamps its crash-recovery ``DELIVERY_UNKNOWN`` attempt with
        this same value (captured before the transition) instead of the
        restart's own ``now``, so this column keeps reflecting the
        original ``SENDING``-entry instant through that transition.
        """
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT updated_at FROM deliveries WHERE id = ?",
                (delivery_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("delivery does not exist")
        return datetime.fromisoformat(str(row[0]))

    def get_delivery_message_id(self, delivery_id: int) -> int:
        """Return the ``message_id`` a delivery is associated with.

        Used by ``run_daily_send`` (T16 Task 13 fix, finding F4) to read
        the delivery's own bound message text from the database instead
        of trusting a caller-supplied string, when resuming an existing
        delivery that has no fresh ``Reservation`` this call.
        """
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT message_id FROM deliveries WHERE id = ?",
                (delivery_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("delivery does not exist")
        return int(row[0])

    def get_latest_attempt_time(self, delivery_id: int) -> datetime:
        """Return the ``attempted_at`` of the most recent recorded attempt.

        Used by the ``DELIVERY_UNKNOWN`` reconciliation path (T16) as the
        start of the window to search WAHA's chat history for a matching
        send. Raises ``ValueError`` if no attempt row exists for this
        delivery -- a ``DELIVERY_UNKNOWN`` delivery always has at least one,
        since ``record_delivery_attempt`` is what puts it there.
        """
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT attempted_at FROM delivery_attempts "
                "WHERE delivery_id = ? ORDER BY id DESC LIMIT 1",
                (delivery_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("no attempt recorded for a delivery_unknown delivery")
        return datetime.fromisoformat(str(row[0]))

    def get_message_text(self, message_id: int) -> str:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT text FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RecordNotFound("message does not exist")
        return str(row[0])

    def count_deliveries(self, recipient_key: str, pacific_date: date) -> int:
        connection = self._connect()
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

    def record_sender_nonce(
        self,
        idempotency_key: str,
        timestamp: int,
        expires_at: datetime,
    ) -> None:
        """Record a sender-auth (idempotency_key, timestamp) pair.

        Authentication-layer replay protection for T15's sender boundary
        (docs/task-logs/T15.md) -- distinct from T16's exactly-once
        delivery/retry bookkeeping. Raises ``ReplayDetected`` if the exact
        pair was already recorded.
        """

        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO sender_auth_nonces (
                        idempotency_key,
                        timestamp,
                        expires_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (idempotency_key, timestamp, _timestamp(expires_at)),
                )
            except sqlite3.IntegrityError:
                raise ReplayDetected(
                    "sender-auth request was already recorded"
                ) from None

    def disable_sending(self, reason: DisableReason, now: datetime) -> None:
        """Durably disable sending. Idempotent: a no-op if sending is
        already disabled -- the first trigger's reason and timestamp are
        preserved as the audit record; a repeated trigger (a second STOP,
        a blocked-by-user 403 on a later day, the admin kill switch after
        STOP already fired) doesn't overwrite it or spam the event log.
        """
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT enabled FROM sending_control WHERE id = 1"
            ).fetchone()
            if row is not None and not bool(row[0]):
                return
            connection.execute(
                """
                INSERT INTO sending_control (id, enabled, reason, changed_at)
                VALUES (1, 0, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    enabled = 0, reason = excluded.reason,
                    changed_at = excluded.changed_at
                """,
                (reason.value, timestamp),
            )
            connection.execute(
                """
                INSERT INTO sending_control_events
                    (enabled, reason, note, changed_at)
                VALUES (0, ?, NULL, ?)
                """,
                (reason.value, timestamp),
            )

    def enable_sending(self, note: str, now: datetime) -> None:
        """The audited re-enable procedure: requires a non-empty note
        durably recording why. Idempotent: a no-op if sending is already
        enabled. See
        docs/superpowers/specs/2026-08-19-t17-telegram-consent-stop-killswitch-design.md.
        """
        if not note.strip():
            raise ValueError("enable_sending requires a non-empty note")
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT enabled FROM sending_control WHERE id = 1"
            ).fetchone()
            if row is None or bool(row[0]):
                return
            connection.execute(
                """
                INSERT INTO sending_control (id, enabled, reason, changed_at)
                VALUES (1, 1, NULL, ?)
                ON CONFLICT (id) DO UPDATE SET
                    enabled = 1, reason = NULL, changed_at = excluded.changed_at
                """,
                (timestamp,),
            )
            connection.execute(
                """
                INSERT INTO sending_control_events
                    (enabled, reason, note, changed_at)
                VALUES (1, NULL, ?, ?)
                """,
                (note, timestamp),
            )

    def is_sending_enabled(self) -> bool:
        """A missing row means sending was never disabled -- the default
        enabled state, matching a fresh SCHEMA_V8 migration."""
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT enabled FROM sending_control WHERE id = 1"
            ).fetchone()
        finally:
            connection.close()
        return True if row is None else bool(row[0])

    def get_telegram_inbound_offset(self) -> int | None:
        """None means no getUpdates poll has ever completed -- the caller
        should omit Telegram's offset parameter on the next call."""
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT next_offset FROM telegram_inbound_offset WHERE id = 1"
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else int(row[0])

    def set_telegram_inbound_offset(self, offset: int, now: datetime) -> None:
        timestamp = _timestamp(now)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO telegram_inbound_offset (id, next_offset, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    next_offset = excluded.next_offset,
                    updated_at = excluded.updated_at
                """,
                (offset, timestamp),
            )
