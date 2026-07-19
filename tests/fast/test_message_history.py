from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from personal_voice_msg.database import Database
from personal_voice_msg.history import DuplicateReason, MessageHistory

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def create_history(path: Path) -> MessageHistory:
    database = Database(path)
    database.migrate()
    return MessageHistory(database)


def create_v1_database(path: Path, text: str) -> int:
    states = (
        "'discovered', 'validated', 'approved', 'queued', 'reserved', "
        "'audio_ready', 'sending', 'sent', 'failed', 'delivery_unknown'"
    )
    delivery_states = (
        "'reserved', 'audio_ready', 'sending', 'sent', 'failed', "
        "'delivery_unknown'"
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(
            f"""
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE sources (
                id INTEGER PRIMARY KEY,
                source_url TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                rights_category TEXT NOT NULL,
                rights_evidence TEXT
            );
            CREATE TABLE inspiration_cards (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL
                    REFERENCES sources(id) ON DELETE RESTRICT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                inspiration_card_id INTEGER
                    REFERENCES inspiration_cards(id) ON DELETE RESTRICT,
                text TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ({states})),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY,
                run_kind TEXT NOT NULL,
                pacific_date TEXT,
                state TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE TABLE audio_artifacts (
                id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL
                    REFERENCES messages(id) ON DELETE RESTRICT,
                state TEXT NOT NULL,
                storage_key TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT
            );
            CREATE TABLE deliveries (
                id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL UNIQUE
                    REFERENCES messages(id) ON DELETE RESTRICT,
                recipient_key TEXT NOT NULL,
                pacific_date TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ({delivery_states})),
                provider_message_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (recipient_key, pacific_date)
            );
            CREATE INDEX messages_state_id_idx ON messages(state, id);
            CREATE INDEX deliveries_recipient_date_idx
                ON deliveries(recipient_key, pacific_date);
            INSERT INTO schema_migrations (version) VALUES (1);
            """
        )
        cursor = connection.execute(
            """
            INSERT INTO messages (text, state, created_at, updated_at)
            VALUES (?, 'sent', ?, ?)
            """,
            (text, NOW.isoformat(), NOW.isoformat()),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


@pytest.mark.fast
@pytest.mark.parametrize(
    "variant",
    [
        "Your smile makes every morning brighter",
        "YOUR SMILE MAKES EVERY MORNING BRIGHTER",
        "  Your   smile makes\n every morning brighter  ",
        "Your smile---makes every morning brighter!!!",
    ],
    ids=["exact", "case", "whitespace", "punctuation"],
)
def test_exact_normalized_variants_are_rejected(
    tmp_path: Path, variant: str
) -> None:
    history = create_history(tmp_path / "history.sqlite3")
    message_id = history.record("Your smile makes every morning brighter", NOW)

    decision = history.evaluate(variant)

    assert not decision.accepted
    assert decision.reason is DuplicateReason.EXACT
    assert decision.matched_message_id == message_id
    assert decision.score == 100.0


@pytest.mark.fast
@pytest.mark.parametrize(
    ("existing", "candidate"),
    [
        (
            "Your laughter makes every ordinary moment feel brighter.",
            "Every ordinary moment feels brighter because of your laughter.",
        ),
        (
            "I love how your kindness makes every day feel warmer.",
            "Your kindness makes every day feel warmer, and I love that.",
        ),
        (
            "Your smile makes even the quietest mornings feel full of light.",
            "Even the quietest mornings feel full of light when you smile.",
        ),
        (
            "Your kindness makes every morning brighter.",
            "Y0ur kindnes makez evry mornng brightr.",
        ),
    ],
    ids=["laughter", "kindness", "morning-smile", "typo-obfuscated"],
)
def test_curated_near_duplicate_paraphrases_are_rejected(
    tmp_path: Path, existing: str, candidate: str
) -> None:
    history = create_history(tmp_path / "history.sqlite3")
    message_id = history.record(existing, NOW)

    decision = history.evaluate(candidate)

    assert not decision.accepted
    assert decision.reason is DuplicateReason.NEAR
    assert decision.matched_message_id == message_id
    assert decision.score is not None
    assert 0.0 < decision.score <= 100.0


@pytest.mark.fast
def test_clearly_distinct_message_remains_accepted(tmp_path: Path) -> None:
    history = create_history(tmp_path / "history.sqlite3")
    history.record(
        "Your laughter makes every ordinary moment feel brighter.", NOW
    )

    decision = history.evaluate(
        "I hope tonight wraps you in calm and lets you rest deeply."
    )

    assert decision.accepted
    assert decision.reason is None
    assert decision.matched_message_id is None


@pytest.mark.fast
def test_six_consecutive_source_words_are_rejected_without_storing_source(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    history = create_history(path)
    source = "The moon keeps a silver promise above the quiet sleeping city"

    decision = history.evaluate(
        "Tonight, a silver promise above the quiet sleeping city makes me smile.",
        source_text=source,
    )

    assert not decision.accepted
    assert decision.reason is DuplicateReason.SOURCE_COPY
    assert decision.matched_message_id is None
    with sqlite3.connect(path) as connection:
        stored_source = connection.execute(
            "SELECT 1 FROM messages WHERE text = ?", (source,)
        ).fetchone()
    assert stored_source is None


@pytest.mark.fast
def test_history_stores_text_once_and_uses_real_external_content_fts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    history = create_history(path)
    text = "Your calm presence makes each morning feel hopeful."
    message_id = history.record(text, NOW)

    with sqlite3.connect(path) as connection:
        history_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(message_history)")
        }
        stored = connection.execute(
            "SELECT messages.text, message_history.normalized_hash "
            "FROM message_history JOIN messages "
            "ON messages.id = message_history.message_id "
            "WHERE messages.id = ?",
            (message_id,),
        ).fetchone()
        fts_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'message_history_fts'"
        ).fetchone()
        fts_matches = connection.execute(
            "SELECT rowid FROM message_history_fts "
            "WHERE message_history_fts MATCH 'morning'"
        ).fetchall()

    assert history_columns == {"message_id", "normalized_hash"}
    assert stored is not None
    assert stored[0] == text
    assert isinstance(stored[1], str) and len(stored[1]) == 64
    assert fts_sql is not None
    assert "fts5" in str(fts_sql[0]).casefold()
    assert "content='messages'" in str(fts_sql[0]).casefold()
    assert fts_matches == [(message_id,)]


@pytest.mark.fast
def test_recorded_message_text_is_immutable(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    history = create_history(path)
    text = "Your calm presence makes each morning feel hopeful."
    message_id = history.record(text, NOW)

    connection = history.database.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE messages SET text = ? WHERE id = ?",
                ("Changed text", message_id),
            )
    finally:
        connection.close()

    decision = history.evaluate(text)
    assert decision.reason is DuplicateReason.EXACT
    assert decision.matched_message_id == message_id


@pytest.mark.fast
def test_v1_migration_backfills_history_without_changing_message_text(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1.sqlite3"
    text = "Your gentle heart makes ordinary days feel special."
    message_id = create_v1_database(path, text)

    Database(path).migrate()

    with sqlite3.connect(path) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        message = connection.execute(
            "SELECT text, state FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        history_row = connection.execute(
            "SELECT message_id, normalized_hash FROM message_history "
            "WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        fts_match = connection.execute(
            "SELECT rowid FROM message_history_fts "
            "WHERE message_history_fts MATCH 'gentle'"
        ).fetchone()

    assert versions == [(1,), (2,)]
    assert message == (text, "sent")
    assert history_row is not None
    assert history_row[0] == message_id
    assert isinstance(history_row[1], str) and len(history_row[1]) == 64
    assert fts_match == (message_id,)
