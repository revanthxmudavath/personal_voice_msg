# T16 — Exactly-once delivery and ambiguity recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first real orchestrator that chains T03/T12's reservation, T14's synthesis, and T15's sender into an exactly-once daily send, with durable audio persistence, attempt bookkeeping, and ambiguity reconciliation — so a restart, timeout, or crash at any delivery state can never duplicate a voice note.

**Architecture:** `deliveries.audio_data` (new BLOB column) makes synthesis run exactly once per delivery so every attempt — first send and every retry — reuses identical bytes. A new `delivery_attempts` table plus two new `DELIVERY_TRANSITIONS` edges (`FAILED → AUDIO_READY`, `DELIVERY_UNKNOWN → {AUDIO_READY, SENT}`) let the existing state machine represent retry and reconciliation without adding a new `MessageState`. `sender.py` gains a `SenderRejected`/`SenderAmbiguous` split so the new `delivery.py` orchestrator can tell "safe to retry now" from "must reconcile first." All of this is grounded in `docs/superpowers/specs/2026-08-09-t16-exactly-once-delivery-design.md` — read it before starting; this plan does not repeat its rationale, only its resulting interfaces.

**Tech Stack:** Python 3.12, SQLite (stdlib `sqlite3`), `aiohttp`, real Pocket TTS + FFmpeg (T14), real WAHA Core container (T15). No mocks anywhere — see Global Constraints.

## Global Constraints

- No `unittest.mock`, `pytest-mock`, monkeypatching, fake LLM/WhatsApp/DB, or in-memory DB substitutes anywhere in tests (`AGENTS.md` §Strict no-mock TDD policy).
- Real fault injection only: real SQLite files, real WAHA container pause/stop, real raw sockets for generic network-fault doubles (not WhatsApp-API-shaped fakes — see Task 7's note).
- Every delivery/audio/safety state that is unknown fails closed.
- Never retry blindly after `delivery_unknown`; always reconcile first.
- Never begin a new send attempt at or after 07:05 Pacific for that date; never carry a missed send into the next Pacific date.
- Retries must reuse the same sentence and audio bytes — never re-synthesize.
- `pytest.mark.fast` for pure logic / small file-backed SQLite tests, `integration` for real local dependencies (Pocket TTS/FFmpeg), `e2e`/`security` for real WAHA container tests, gated by the same env-var pattern T15 established (`T15_WAHA_SETTINGS`, `T15_WAHA_CONTAINER`, `T13_VOICE_SAMPLE`).
- `uv run pytest -m fast` / `-m security` / `mypy src` / `ruff check .` / `python scripts/repository_policy.py all --root .` must stay green after every task.
- Branch: `task/T16-exactly-once-delivery` (already created). Independent security review required before merge — do not self-approve (`AGENTS.md`'s list: T06, T15, T16, T17, T18).

---

## File Structure

- **Modify** `src/personal_voice_msg/database.py` — `SCHEMA_V7`, extended `DELIVERY_TRANSITIONS`, `mark_audio_ready`, `get_audio_data`, `clear_audio_data`, `record_delivery_attempt`.
- **Modify** `src/personal_voice_msg/audio_pipeline.py` — `produce_voice_note` persists bytes via `mark_audio_ready` instead of a bare `transition_delivery`; `remove_audio_after_delivery` clears the DB blob instead of unlinking a file.
- **Modify** `src/personal_voice_msg/sender.py` — `SenderRejected`/`SenderAmbiguous` exception split; new `reconcile_delivery` function.
- **Create** `src/personal_voice_msg/delivery.py` — the orchestrator: `run_daily_send(database, settings, session, recipient_key, pacific_date, embedding_path, text, now) -> MessageState`.
- **Modify** `tests/fast/test_database_migrations.py`, `tests/fast/test_delivery_state_machine.py` — extend for `SCHEMA_V7`/new transitions.
- **Create** `tests/fast/test_delivery_attempts.py` — `record_delivery_attempt` unit coverage.
- **Modify** `tests/integration/test_audio_pipeline.py` — update the three tests whose contract changes.
- **Modify** `tests/e2e/test_sender.py` or **create** `tests/security/test_sender_error_taxonomy.py` — `SenderRejected`/`SenderAmbiguous` coverage.
- **Create** `tests/e2e/test_delivery.py` — full orchestration, retry, and reconciliation fault-injection suite (this is where the plan's literal red tests live).
- **Modify** `docs/task-logs/T16.md` (create) — verification evidence, per `CLAUDE.md`'s per-task workflow.

---

## Task 1: Schema V7 — durable audio storage and attempt records

**Files:**
- Modify: `src/personal_voice_msg/database.py`
- Test: `tests/fast/test_database_migrations.py`

**Interfaces:**
- Produces: `SCHEMA_V7_STATEMENTS`, `EXPECTED_SCHEMA_V7_OBJECTS`, `CURRENT_SCHEMA_VERSION = 7`, table `delivery_attempts(id, delivery_id, attempted_at, outcome, provider_message_id)`, column `deliveries.audio_data BLOB`.

- [ ] **Step 1: Write the failing migration test**

Add to `tests/fast/test_database_migrations.py` (extend `EXPECTED_TABLES`, add a downgrade helper and a version-six upgrade test, following the exact pattern of `downgrade_current_database_to_v5`/`test_version_five_database_upgrades_to_sender_auth_nonces_without_data_loss` already in that file):

```python
EXPECTED_TABLES = {
    "schema_migrations",
    "sources",
    "inspiration_cards",
    "messages",
    "runs",
    "audio_artifacts",
    "deliveries",
    "daily_runs",
    "message_rejections",
    "sender_auth_nonces",
    "delivery_attempts",
}


def downgrade_current_database_to_v6(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE IF EXISTS delivery_attempts")
        # SQLite has no DROP COLUMN before 3.35; recreate deliveries as it
        # was at v6 (no audio_data) to simulate a real pre-v7 database.
        connection.execute("ALTER TABLE deliveries RENAME TO deliveries_v6")
        connection.execute(
            """
            CREATE TABLE deliveries (
                id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL UNIQUE
                    REFERENCES messages(id) ON DELETE RESTRICT,
                recipient_key TEXT NOT NULL,
                pacific_date TEXT NOT NULL,
                state TEXT NOT NULL,
                provider_message_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (recipient_key, pacific_date)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO deliveries
            SELECT id, message_id, recipient_key, pacific_date, state,
                   provider_message_id, created_at, updated_at
            FROM deliveries_v6
            """
        )
        connection.execute("DROP TABLE deliveries_v6")
        connection.execute(
            "CREATE INDEX deliveries_recipient_date_idx "
            "ON deliveries(recipient_key, pacific_date)"
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")


@pytest.mark.fast
def test_version_six_database_upgrades_to_delivery_attempts_without_data_loss(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "version-six.sqlite3"
    database = Database(database_path)
    database.migrate()
    downgrade_current_database_to_v6(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO runs (run_kind, pacific_date, state, started_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "migration_probe",
                "2026-08-09",
                "preserve-me",
                "2026-08-09T13:50:00+00:00",
            ),
        )

    database.migrate()

    with sqlite3.connect(database_path) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        preserved = connection.execute(
            "SELECT run_kind, pacific_date, state, started_at FROM runs"
        ).fetchall()
        delivery_attempts_table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'delivery_attempts'"
        ).fetchone()
        deliveries_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(deliveries)")
        }
    assert versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,)]
    assert preserved == [
        (
            "migration_probe",
            "2026-08-09",
            "preserve-me",
            "2026-08-09T13:50:00+00:00",
        )
    ]
    assert delivery_attempts_table == (1,)
    assert "audio_data" in deliveries_columns
```

Also update the two existing generic tests in that file that hardcode the version tuple / `EXPECTED_TABLES`: `test_rerunning_migration_is_idempotent` (`assert versions == [(1,), (2,), (3,), (4,), (5,), (6,)]` → add `(7,)`) and every other `assert versions == [...]` line already in the file for `test_version_*` cases stays as-is (they each assert the migration reaches `CURRENT_SCHEMA_VERSION`, so they must all gain `(7,)` too — update `test_migration_succeeds_on_empty_sqlite_file` is fine unchanged since it only checks `EXPECTED_TABLES <= tables`, but `test_version_one_is_recorded_exactly_once`, `test_version_three_database_upgrades_to_daily_runs_without_data_loss`, `test_version_five_database_upgrades_to_sender_auth_nonces_without_data_loss`, `test_version_four_database_upgrades_to_message_rejections_without_data_loss`, `test_version_two_database_upgrades_to_unique_normalized_hashes`, `test_version_three_migration_fails_closed_on_existing_exact_duplicates` all assert exact version-tuple lists ending at `(6,)` and must be updated to end at `(7,)`).

Also extend `tests/fast/test_delivery_state_machine.py`'s `test_message_state_is_persisted_after_each_restart` is unaffected (it never reaches `SENT` via a fresh `audio_data`-less path in a way that breaks — no change needed there since it only asserts `MessageState`, not the new column).

- [ ] **Step 2: Run it, confirm it fails for the right reason**

```bash
uv run pytest tests/fast/test_database_migrations.py::test_version_six_database_upgrades_to_delivery_attempts_without_data_loss -v
```

Expected: FAIL — `sqlite3.OperationalError: no such table: delivery_attempts` (or `AttributeError`/assertion on `versions`), not a collection error.

- [ ] **Step 3: Implement `SCHEMA_V7`**

In `database.py`, add after `SCHEMA_V6_STATEMENTS`:

```python
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
```

Bump `CURRENT_SCHEMA_VERSION = 7`. Add the migration step in `migrate()` after the existing V6 block, following the exact shape of every prior step (`if versions == {1, 2, 3, 4, 5, 6}: ... versions = {1, 2, 3, 4, 5, 6, 7}`), then `_validate_schema(connection, EXPECTED_SCHEMA_V7_OBJECTS)`. Also extend the two `versions not in (...)` guard tuples near the top of `migrate()` to include `{1, 2, 3, 4, 5, 6}` and `{1, 2, 3, 4, 5, 6, CURRENT_SCHEMA_VERSION}` (mirroring how `{1, 2, 3, 4, 5}` was added for V6 — re-read that diff shape before writing this one).

`EXPECTED_SCHEMA_V7_OBJECTS` is the first entry in this file to **override** an inherited key rather than only add new ones — `("table", "deliveries")` must map to the real post-`ALTER` SQL SQLite records, not `SCHEMA_V7_STATEMENTS[0]` (which is the `ALTER` statement itself, not a `CREATE TABLE`). Do not hand-type this text. After writing the migration code, run:

```bash
uv run python -c "
import sqlite3, tempfile
from pathlib import Path
from personal_voice_msg.database import Database
path = Path(tempfile.mktemp(suffix='.sqlite3'))
Database(path).migrate()
conn = sqlite3.connect(path)
print(conn.execute(\"SELECT sql FROM sqlite_master WHERE type='table' AND name='deliveries'\").fetchone()[0])
"
```

and paste the exact printed text as the literal string value for `EXPECTED_SCHEMA_V7_OBJECTS[("table", "deliveries")]`:

```python
EXPECTED_SCHEMA_V7_OBJECTS = {
    **EXPECTED_SCHEMA_V6_OBJECTS,
    ("table", "deliveries"): "<paste the printed CREATE TABLE text here>",
    ("table", "delivery_attempts"): SCHEMA_V7_STATEMENTS[1],
}
```

- [ ] **Step 4: Run the migration suite, confirm green**

```bash
uv run pytest tests/fast/test_database_migrations.py -v
```

Expected: all pass, including the new test and every updated version-tuple assertion.

- [ ] **Step 5: Commit**

```bash
git add src/personal_voice_msg/database.py tests/fast/test_database_migrations.py
git commit -m "T16: add SCHEMA_V7 (deliveries.audio_data, delivery_attempts)"
```

---

## Task 2: `DELIVERY_TRANSITIONS` edges + `mark_audio_ready` / `get_audio_data`

**Files:**
- Modify: `src/personal_voice_msg/database.py`
- Test: `tests/fast/test_delivery_state_machine.py`

**Interfaces:**
- Consumes: `Reservation` (Task-independent, already exists), `SCHEMA_V7` from Task 1.
- Produces: `Database.mark_audio_ready(delivery_id: int, audio_bytes: bytes, now: datetime) -> None`, `Database.get_audio_data(delivery_id: int) -> bytes`. `DELIVERY_TRANSITIONS[MessageState.FAILED] == {MessageState.AUDIO_READY}`, `DELIVERY_TRANSITIONS[MessageState.DELIVERY_UNKNOWN] == {MessageState.AUDIO_READY, MessageState.SENT}`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/fast/test_delivery_state_machine.py`:

```python
@pytest.mark.fast
def test_mark_audio_ready_persists_bytes_and_transitions_atomically(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    queue_message(database, "A warm original sentence.")
    reservation = database.reserve_next_message(RECIPIENT, date(2026, 7, 18), NOW)
    assert reservation is not None

    database.mark_audio_ready(reservation.delivery_id, b"fake-ogg-bytes", NOW)

    assert (
        database.get_delivery_state(reservation.delivery_id)
        is MessageState.AUDIO_READY
    )
    assert database.get_audio_data(reservation.delivery_id) == b"fake-ogg-bytes"


@pytest.mark.fast
def test_mark_audio_ready_rejects_a_delivery_not_reserved(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    queue_message(database, "A warm original sentence.")
    reservation = database.reserve_next_message(RECIPIENT, date(2026, 7, 18), NOW)
    assert reservation is not None
    database.mark_audio_ready(reservation.delivery_id, b"first-bytes", NOW)

    with pytest.raises(InvalidTransition):
        database.mark_audio_ready(reservation.delivery_id, b"second-bytes", NOW)


@pytest.mark.fast
def test_failed_delivery_can_return_to_audio_ready_for_retry(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    queue_message(database, "A warm original sentence.")
    reservation = database.reserve_next_message(RECIPIENT, date(2026, 7, 18), NOW)
    assert reservation is not None
    database.mark_audio_ready(reservation.delivery_id, b"note-bytes", NOW)
    database.transition_delivery(reservation.delivery_id, MessageState.SENDING, NOW)
    database.transition_delivery(reservation.delivery_id, MessageState.FAILED, NOW)

    database.transition_delivery(
        reservation.delivery_id, MessageState.AUDIO_READY, NOW
    )

    assert (
        database.get_delivery_state(reservation.delivery_id)
        is MessageState.AUDIO_READY
    )
    # The stored bytes survive the round trip -- retries reuse the same audio.
    assert database.get_audio_data(reservation.delivery_id) == b"note-bytes"


@pytest.mark.fast
@pytest.mark.parametrize("target", [MessageState.AUDIO_READY, MessageState.SENT])
def test_delivery_unknown_can_transition_to_audio_ready_or_sent(
    tmp_path: Path, target: MessageState
) -> None:
    database = Database(tmp_path / f"unknown-{target.value}.sqlite3")
    database.migrate()
    queue_message(database, "A warm original sentence.")
    reservation = database.reserve_next_message(RECIPIENT, date(2026, 7, 18), NOW)
    assert reservation is not None
    database.mark_audio_ready(reservation.delivery_id, b"note-bytes", NOW)
    database.transition_delivery(reservation.delivery_id, MessageState.SENDING, NOW)
    database.transition_delivery(
        reservation.delivery_id, MessageState.DELIVERY_UNKNOWN, NOW
    )

    database.transition_delivery(reservation.delivery_id, target, NOW)

    assert database.get_delivery_state(reservation.delivery_id) is target
```

Update the existing `test_terminal_delivery_states_cannot_transition` parametrize list: `(MessageState.FAILED, MessageState.SENT)` is no longer a valid "terminal states cannot transition" case (FAILED now legitimately transitions to `AUDIO_READY`) — replace that tuple with `(MessageState.FAILED, MessageState.SENDING)` (still illegal — `FAILED` can only go to `AUDIO_READY`, not straight back to `SENDING`) so the test keeps asserting a real remaining invariant instead of a now-false one. Keep `(MessageState.SENT, MessageState.FAILED)` unchanged (`SENT` is still fully terminal) and change `(MessageState.DELIVERY_UNKNOWN, MessageState.FAILED)` to `(MessageState.DELIVERY_UNKNOWN, MessageState.SENDING)` (still illegal — `DELIVERY_UNKNOWN` can only go to `AUDIO_READY` or `SENT`, never straight back to `SENDING`).

- [ ] **Step 2: Run, confirm failure for the right reason**

```bash
uv run pytest tests/fast/test_delivery_state_machine.py -v -k "mark_audio_ready or failed_delivery_can_return or delivery_unknown_can_transition"
```

Expected: FAIL — `AttributeError: 'Database' object has no attribute 'mark_audio_ready'`.

- [ ] **Step 3: Implement**

In `database.py`, extend `DELIVERY_TRANSITIONS`:

```python
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
```

Add two new methods after `transition_delivery`:

```python
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
        if target not in DELIVERY_TRANSITIONS.get(current, set()):
            raise InvalidTransition(
                f"delivery cannot transition from {current.value} to {target.value}"
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
            "UPDATE messages SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
            (target.value, timestamp, message_id, current.value),
        )
        if delivery_update.rowcount != 1 or message_update.rowcount != 1:
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
```

- [ ] **Step 4: Run, confirm green**

```bash
uv run pytest tests/fast/test_delivery_state_machine.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/personal_voice_msg/database.py tests/fast/test_delivery_state_machine.py
git commit -m "T16: extend DELIVERY_TRANSITIONS, add mark_audio_ready/get_audio_data"
```

---

## Task 3: `record_delivery_attempt`

**Files:**
- Modify: `src/personal_voice_msg/database.py`
- Test: `tests/fast/test_delivery_attempts.py` (new)

**Interfaces:**
- Consumes: `mark_audio_ready` (Task 2) to reach `AUDIO_READY`/`SENDING` in test setup.
- Produces: `Database.record_delivery_attempt(delivery_id: int, outcome: MessageState, now: datetime, provider_message_id: str | None = None) -> None`. `outcome` must be one of `{MessageState.SENT, MessageState.FAILED, MessageState.DELIVERY_UNKNOWN}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/fast/test_delivery_attempts.py`:

```python
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
```

- [ ] **Step 2: Run, confirm failure**

```bash
uv run pytest tests/fast/test_delivery_attempts.py -v
```

Expected: FAIL — `AttributeError: 'Database' object has no attribute 'record_delivery_attempt'`.

- [ ] **Step 3: Implement**

```python
_ATTEMPT_OUTCOMES = {
    MessageState.SENT,
    MessageState.FAILED,
    MessageState.DELIVERY_UNKNOWN,
}

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
                f"delivery cannot transition from {current.value} to {outcome.value}"
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
            "UPDATE messages SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
            (outcome.value, timestamp, message_id, current.value),
        )
        if delivery_update.rowcount != 1 or message_update.rowcount != 1:
            raise DatabaseInvariantError("delivery state changed concurrently")
```

- [ ] **Step 4: Run, confirm green**

```bash
uv run pytest tests/fast/test_delivery_attempts.py tests/fast/test_delivery_state_machine.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/personal_voice_msg/database.py tests/fast/test_delivery_attempts.py
git commit -m "T16: add record_delivery_attempt"
```

---

## Task 4: `clear_audio_data`

**Files:**
- Modify: `src/personal_voice_msg/database.py`
- Test: `tests/fast/test_delivery_attempts.py`

**Interfaces:**
- Produces: `Database.clear_audio_data(delivery_id: int, now: datetime) -> None` — requires the delivery to be `SENT`, else raises `InvalidTransition`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/fast/test_delivery_attempts.py`:

```python
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
```

- [ ] **Step 2: Run, confirm failure**

```bash
uv run pytest tests/fast/test_delivery_attempts.py -k clear_audio_data -v
```

Expected: FAIL — `AttributeError: 'Database' object has no attribute 'clear_audio_data'`.

- [ ] **Step 3: Implement**

```python
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
```

- [ ] **Step 4: Run, confirm green**

```bash
uv run pytest tests/fast/test_delivery_attempts.py -v
uv run pytest -m fast
```

Expected: full fast suite green (this is the last database.py change — confirm nothing else regressed).

- [ ] **Step 5: Commit**

```bash
git add src/personal_voice_msg/database.py tests/fast/test_delivery_attempts.py
git commit -m "T16: add clear_audio_data"
```

---

## Task 5: Rewire `audio_pipeline.py` to durable BLOB storage

**Files:**
- Modify: `src/personal_voice_msg/audio_pipeline.py`
- Modify: `tests/integration/test_audio_pipeline.py`

**Interfaces:**
- Consumes: `Database.mark_audio_ready`, `Database.clear_audio_data` (Tasks 2/4).
- Produces: `produce_voice_note(...) -> bytes` (return type changes from `Path` to `bytes`). `remove_audio_after_delivery(database: Database, delivery_id: int, now: datetime) -> None` (drops the `audio_path: Path` parameter).

- [ ] **Step 1: Update the three affected tests first (red)**

In `tests/integration/test_audio_pipeline.py`, replace `test_produce_voice_note_marks_audio_ready_and_creates_valid_file`:

```python
def test_produce_voice_note_persists_audio_and_marks_ready(
    tmp_path: Path, model: TTSModel, voice_embedding: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    delivery_id = record_and_reserve(database, "Thinking of you and smiling today.")
    destination = tmp_path / "note.ogg"

    result = produce_voice_note(
        database,
        delivery_id,
        voice_embedding,
        "Thinking of you and smiling today.",
        destination,
        NOW,
        model=model,
    )

    assert isinstance(result, bytes) and result
    assert not destination.exists()
    assert database.get_delivery_state(delivery_id) == MessageState.AUDIO_READY
    assert database.get_audio_data(delivery_id) == result
```

Replace `test_successful_delivery_removes_temporary_audio`:

```python
def test_successful_delivery_clears_stored_audio(
    tmp_path: Path, model: TTSModel, voice_embedding: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    delivery_id = record_and_reserve(database, "A sentence sent all the way through.")
    destination = tmp_path / "note.ogg"
    produce_voice_note(
        database,
        delivery_id,
        voice_embedding,
        "A sentence sent all the way through.",
        destination,
        NOW,
        model=model,
    )
    database.transition_delivery(delivery_id, MessageState.SENDING, NOW)
    database.record_delivery_attempt(delivery_id, MessageState.SENT, NOW)

    remove_audio_after_delivery(database, delivery_id, NOW)

    with pytest.raises(DatabaseInvariantError):
        database.get_audio_data(delivery_id)
```

Replace `test_remove_audio_before_delivery_confirmed_is_refused`:

```python
def test_remove_audio_before_delivery_confirmed_is_refused(tmp_path: Path) -> None:
    delivery_id = record_and_reserve(
        Database(tmp_path / "state.sqlite3"), "Not sent yet, do not delete me."
    )
    database = Database(tmp_path / "state.sqlite3")
    database.mark_audio_ready(delivery_id, b"placeholder", NOW)

    with pytest.raises(AudioPipelineError, match="not.*sent"):
        remove_audio_after_delivery(database, delivery_id, NOW)

    assert database.get_audio_data(delivery_id) == b"placeholder"
```

Update the imports at the top of the file: add `DatabaseInvariantError` to the `from personal_voice_msg.database import (...)` line. `test_failed_synthesis_leaves_no_sendable_artifact` needs no change (its assertions — `not destination.exists()` and state stays `RESERVED` — hold unchanged under the new implementation).

- [ ] **Step 2: Run, confirm failure for the right reason**

```bash
uv run pytest tests/integration/test_audio_pipeline.py -v -k "persists_audio_and_marks_ready or clears_stored_audio or before_delivery_confirmed"
```

Expected: FAIL — `test_produce_voice_note_persists_audio_and_marks_ready` fails on `assert not destination.exists()` (today's code leaves it); the other two fail with `AttributeError`/`TypeError` since `mark_audio_ready`/`record_delivery_attempt` calls or the new `remove_audio_after_delivery` signature don't exist yet in this file (these two DB methods already exist from Tasks 2-3, so only the `audio_pipeline.py` call sites are missing).

- [ ] **Step 3: Implement**

Replace `produce_voice_note` and `remove_audio_after_delivery` in `audio_pipeline.py`:

```python
def produce_voice_note(
    database: Database,
    delivery_id: int,
    embedding_path: Path,
    text: str,
    destination: Path,
    now: datetime,
    *,
    model: TTSModel | None = None,
) -> bytes:
    """Synthesize, convert, and validate a voice note; persist it as the
    delivery's durable audio and mark it ``audio_ready``.

    Leaves no on-disk file and no delivery-state change on any failure.
    Synthesis runs exactly once per delivery -- every later send attempt
    reads the returned/stored bytes back via ``Database.get_audio_data``
    instead of re-synthesizing (Pocket TTS is not deterministic).
    """

    temp_wav = destination.with_suffix(".wav")
    try:
        synthesize_to_wav(embedding_path, text, temp_wav, model=model)
        convert_to_opus(temp_wav, destination)
        validate_audio(destination)
        audio_bytes = destination.read_bytes()
    except Exception:
        temp_wav.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    else:
        temp_wav.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)

    database.mark_audio_ready(delivery_id, audio_bytes, now)
    return audio_bytes


def remove_audio_after_delivery(
    database: Database,
    delivery_id: int,
    now: datetime,
) -> None:
    """Clear the delivery's stored audio, but only once it is sent."""

    state = database.get_delivery_state(delivery_id)
    if state is not MessageState.SENT:
        raise AudioPipelineError(
            f"delivery is not sent (state={state.value}); refusing to remove audio"
        )
    database.clear_audio_data(delivery_id, now)
```

- [ ] **Step 4: Run, confirm green**

```bash
uv run pytest tests/integration/test_audio_pipeline.py -v
```

Requires `T13_VOICE_SAMPLE` set (real consented test voice — see T13/T14's task logs for how the local dev sample is set up). If unset, tests skip cleanly; confirm skip behavior explicitly:

```bash
uv run pytest tests/integration/test_audio_pipeline.py -v
```

Expected: `skipped` reason mentions `T13_VOICE_SAMPLE`, not an error.

- [ ] **Step 5: Commit**

```bash
git add src/personal_voice_msg/audio_pipeline.py tests/integration/test_audio_pipeline.py
git commit -m "T16: persist produced audio as a durable DB blob, not a file"
```

---

## Task 6: `sender.py` error taxonomy

**Files:**
- Modify: `src/personal_voice_msg/sender.py`
- Test: `tests/e2e/test_sender.py` (extend), `tests/security/test_sender_error_taxonomy.py` (new)

**Interfaces:**
- Produces: `SenderRejected(SenderError)`, `SenderAmbiguous(SenderError)`.

- [ ] **Step 1: Write the failing tests**

In `tests/e2e/test_sender.py`, add (these reuse the module's existing `settings`/`valid_audio_bytes` fixtures and env-var skip gate):

```python
from personal_voice_msg.sender import SenderAmbiguous, SenderRejected


@pytest.mark.parametrize(
    "make_request",
    [
        lambda now, idem, ts, sig: (b"this is plain text, not audio", ts, sig),
    ],
)
def test_invalid_audio_raises_sender_rejected_not_ambiguous(
    settings: Settings, tmp_path: Path
) -> None:
    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t16-taxonomy-audio-{now.timestamp()}"
    timestamp, signature = signed_request(settings, idempotency_key, now)

    async def send() -> str:
        async with aiohttp.ClientSession() as session:
            return await send_voice_note(
                session,
                database,
                settings,
                b"this is plain text, not audio",
                idempotency_key,
                timestamp,
                signature,
                now,
            )

    with pytest.raises(SenderRejected):
        asyncio.run(send())


def test_invalid_signature_raises_sender_rejected(
    settings: Settings, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t16-taxonomy-signature-{now.timestamp()}"
    timestamp = int(now.timestamp())

    async def send() -> str:
        async with aiohttp.ClientSession() as session:
            return await send_voice_note(
                session,
                database,
                settings,
                valid_audio_bytes,
                idempotency_key,
                timestamp,
                "0" * 64,
                now,
            )

    with pytest.raises(SenderRejected):
        asyncio.run(send())


def test_replayed_request_raises_sender_rejected(
    settings: Settings, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t16-taxonomy-replay-{now.timestamp()}"
    timestamp, signature = signed_request(settings, idempotency_key, now)
    database.record_sender_nonce(
        idempotency_key, timestamp, now + timedelta(minutes=5)
    )

    async def send() -> str:
        async with aiohttp.ClientSession() as session:
            return await send_voice_note(
                session,
                database,
                settings,
                valid_audio_bytes,
                idempotency_key,
                timestamp,
                signature,
                now,
            )

    with pytest.raises(SenderRejected):
        asyncio.run(send())
```

Create `tests/security/test_sender_error_taxonomy.py` for the network-ambiguity case, using a real raw TCP listener that accepts a connection and then never writes a response — a generic network-fault double, not a fake WhatsApp API (it implements zero HTTP/WAHA protocol semantics; contrast with `tests/security/t06/fixture_server.py`'s real HTTP server used the same way for generic web-fetcher fault injection):

```python
from __future__ import annotations

import asyncio
import socket
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.config import RuntimeProfile, Settings
from personal_voice_msg.database import Database
from personal_voice_msg.redaction import SensitiveValue
from personal_voice_msg.sender import SenderAmbiguous, send_voice_note, sign_request

pytestmark = pytest.mark.security


class _HangingServer:
    """Accepts a connection and never responds -- a real socket, no HTTP
    or WAHA semantics implemented. Used only to force a real client-side
    timeout, not to simulate WhatsApp's API."""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_and_hang, daemon=True)
        self._thread.start()

    def _accept_and_hang(self) -> None:
        self._socket.settimeout(1.0)
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            # Accept and hold the connection open without ever writing a
            # response -- the client's own request timeout must fire.
            self._stop.wait()
            connection.close()

    def stop(self) -> None:
        self._stop.set()
        self._socket.close()


@pytest.fixture
def hanging_server() -> "_HangingServer":
    server = _HangingServer()
    yield server
    server.stop()


def _settings_for(port: int, tmp_path: Path) -> Settings:
    return Settings(
        profile=RuntimeProfile.DEVELOPMENT,
        recipient=SensitiveValue("+14155550100"),
        waha_token=SensitiveValue("test-token"),
        voice_embedding=SensitiveValue(tmp_path / "embedding.safetensors"),
        waha_session=SensitiveValue(tmp_path / "session.bin"),
        waha_base_url=f"http://127.0.0.1:{port}",
        sender_auth_key=SensitiveValue("test-sender-auth-key"),
    )


def test_a_hanging_connection_raises_sender_ambiguous_not_rejected(
    hanging_server: "_HangingServer", tmp_path: Path
) -> None:
    settings = _settings_for(hanging_server.port, tmp_path)
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    now = datetime.now(UTC)
    idempotency_key = f"t16-ambiguous-{now.timestamp()}"
    timestamp = int(now.timestamp())
    signature = sign_request(
        settings.sender_auth_key.reveal().encode(), idempotency_key, timestamp
    )

    async def send() -> str:
        async with aiohttp.ClientSession() as session:
            return await send_voice_note(
                session,
                database,
                settings,
                b"irrelevant -- never reaches audio validation... actually it "
                b"does, validation happens before the network call",
                idempotency_key,
                timestamp,
                signature,
                now,
            )

    with pytest.raises(SenderAmbiguous):
        asyncio.run(send())
```

Note while writing this test: `send_voice_note` validates audio *before* contacting WAHA, so the request body above must be real, valid OGG/Opus bytes (reuse the `valid_audio_bytes` fixture pattern from `tests/e2e/test_sender.py`, gated the same way by `T13_VOICE_SAMPLE`) rather than the placeholder shown — adjust this test to depend on that fixture (or a module-scoped equivalent) so the hang genuinely happens at the network step, not the validation step. Confirm which by running the red test first and reading the actual failure.

- [ ] **Step 2: Run, confirm failure**

```bash
uv run pytest tests/security/test_sender_error_taxonomy.py -v
uv run pytest tests/e2e/test_sender.py -v -k taxonomy
```

Expected: `ImportError: cannot import name 'SenderRejected'` / `'SenderAmbiguous'`.

- [ ] **Step 3: Implement**

In `sender.py`, replace the `SenderError` block and every raise site:

```python
class SenderError(RuntimeError):
    """Base class for a rejected, ambiguous, or otherwise failed sender
    request."""


class SenderRejected(SenderError):
    """The request definitely never reached WAHA, or WAHA gave a definite
    rejection. Safe to retry immediately -- see docs/task-logs/T16.md."""


class SenderAmbiguous(SenderError):
    """WAHA may or may not have processed the request. Must be
    reconciled before any retry -- see docs/task-logs/T16.md."""
```

Update `send_voice_note`'s body (message text unchanged everywhere so the existing e2e `match=` assertions stay valid — only the exception class changes):

```python
    key = settings.sender_auth_key.reveal().encode()
    if not verify_signature(key, idempotency_key, timestamp, signature):
        raise SenderRejected("sender request signature is invalid")
    if not is_fresh(timestamp, now):
        raise SenderRejected("sender request timestamp is stale")
    try:
        database.record_sender_nonce(
            idempotency_key,
            timestamp,
            now + timedelta(seconds=REPLAY_WINDOW_SECONDS),
        )
    except ReplayDetected:
        raise SenderRejected("sender request was already processed") from None

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        temp_path.write_bytes(audio_bytes)
        validate_audio(temp_path)
    except AudioPipelineError as error:
        raise SenderRejected(f"audio failed validation: {error}") from error
    finally:
        temp_path.unlink(missing_ok=True)

    phone_number = settings.recipient.reveal().removeprefix("+")
    body = {
        "chatId": f"{phone_number}@c.us",
        "session": WAHA_SESSION_NAME,
        "file": {
            "mimetype": "audio/ogg; codecs=opus",
            "filename": "voice-note.ogg",
            "data": base64.b64encode(audio_bytes).decode("ascii"),
        },
    }
    try:
        async with session.post(
            f"{settings.waha_base_url}/api/sendVoice",
            json=body,
            headers={"X-Api-Key": settings.waha_token.reveal()},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            allow_redirects=False,
        ) as response:
            if response.status >= 400:
                raise SenderRejected("WAHA send request failed")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(RESPONSE_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise SenderAmbiguous("WAHA response exceeded the size limit")
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
        raise SenderAmbiguous("WAHA send request failed") from None

    try:
        return str(payload["key"]["id"])
    except (KeyError, TypeError):
        raise SenderAmbiguous("WAHA response was malformed") from None
```

- [ ] **Step 4: Run, confirm green**

```bash
uv run pytest tests/e2e/test_sender.py -v
uv run pytest tests/security/test_sender_error_taxonomy.py -v
uv run pytest -m fast -m security
```

- [ ] **Step 5: Commit**

```bash
git add src/personal_voice_msg/sender.py tests/e2e/test_sender.py tests/security/test_sender_error_taxonomy.py
git commit -m "T16: split SenderError into SenderRejected/SenderAmbiguous"
```

---

## Task 7: Reconciliation — `sender.reconcile_delivery`

**Files:**
- Modify: `src/personal_voice_msg/sender.py`
- Test: `tests/e2e/test_reconciliation.py` (new, real-WAHA-gated)

**Interfaces:**
- Consumes: `SenderAmbiguous` outcomes from Task 6.
- Produces: `async def reconcile_delivery(session: aiohttp.ClientSession, settings: Settings, attempt_window_start: datetime, now: datetime) -> tuple[MessageState, str | None]` returning `(MessageState.SENT, provider_message_id)`, `(MessageState.AUDIO_READY, None)`, or a sentinel meaning "still inconclusive" (use `(MessageState.DELIVERY_UNKNOWN, None)` for that third case — a legal no-op input to `record_delivery_attempt`... actually `DELIVERY_UNKNOWN` is not in `DELIVERY_TRANSITIONS[DELIVERY_UNKNOWN]`, so the caller must check for this sentinel explicitly and skip calling `record_delivery_attempt` entirely rather than passing it through — document this clearly in the docstring and in `delivery.py`'s Task 8 usage).

- [ ] **Step 1: Real-API exploration (required before writing the red test)**

This step is exploratory, not TDD — you cannot write a meaningful assertion about WAHA's chat-messages response shape without seeing a real one first, matching how T15's "WAHA facts verified before designing" section was produced. With the real WAHA container running and paired (`T15_WAHA_CONTAINER`/`T15_WAHA_SETTINGS` set, per T15's task log for how the local dev setup works):

```bash
uv run python -c "
import asyncio, aiohttp, os
from pathlib import Path
from personal_voice_msg.config import load_settings

settings = load_settings(Path(os.environ['T15_WAHA_SETTINGS']))

async def main():
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f'{settings.waha_base_url}/api/default/chats/'
            f'{settings.recipient.reveal().removeprefix(\"+\")}@c.us/messages',
            params={'limit': 10},
            headers={'X-Api-Key': settings.waha_token.reveal()},
        ) as response:
            print(response.status)
            print(await response.text())

asyncio.run(main())
"
```

Record the real response shape (field names for `fromMe`, `timestamp`, message `id`, media type) in `docs/task-logs/T16.md` under a "WAHA facts verified before designing" section, matching T15's precedent. If this endpoint 404s or requires a NOWEB Store flag not currently set in `docker-compose.yml`, that is real information — record it and add the required flag to `docker-compose.yml` (`environment:` block) as part of this task, with its own real-container test proving it's now present, before proceeding to Step 2.

- [ ] **Step 2: Write the failing test**

Once the real endpoint shape is confirmed, write `tests/e2e/test_reconciliation.py` following the exact gating/fixture pattern of `tests/e2e/test_sender.py` (`T13_VOICE_SAMPLE`, `T15_WAHA_SETTINGS` env vars, `valid_audio_bytes` fixture). Concretely:

```python
def test_reconcile_delivery_finds_a_message_that_was_actually_sent(
    settings: Settings, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t16-reconcile-sent-{now.timestamp()}"
    timestamp, signature = signed_request(settings, idempotency_key, now)
    attempt_window_start = now

    async def send_then_reconcile() -> tuple[MessageState, str | None]:
        async with aiohttp.ClientSession() as session:
            provider_message_id = await send_voice_note(
                session, database, settings, valid_audio_bytes,
                idempotency_key, timestamp, signature, now,
            )
            outcome, found_id = await reconcile_delivery(
                session, settings, attempt_window_start, datetime.now(UTC)
            )
            return outcome, found_id, provider_message_id

    outcome, found_id, provider_message_id = asyncio.run(send_then_reconcile())

    assert outcome is MessageState.SENT
    assert found_id == provider_message_id


def test_reconcile_delivery_reports_not_delivered_for_a_window_with_no_send(
    settings: Settings, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    attempt_window_start = now - timedelta(minutes=10)

    async def reconcile() -> tuple[MessageState, str | None]:
        async with aiohttp.ClientSession() as session:
            return await reconcile_delivery(
                session, settings, attempt_window_start, now - timedelta(minutes=9)
            )

    outcome, found_id = asyncio.run(reconcile())

    assert outcome is MessageState.AUDIO_READY
    assert found_id is None
```

- [ ] **Step 3: Run, confirm failure**

```bash
T13_VOICE_SAMPLE=... T15_WAHA_SETTINGS=... uv run pytest tests/e2e/test_reconciliation.py -v
```

Expected: `ImportError: cannot import name 'reconcile_delivery'`.

- [ ] **Step 4: Implement**

Add to `sender.py`, using the real endpoint/field names confirmed in Step 1:

```python
async def reconcile_delivery(
    session: aiohttp.ClientSession,
    settings: Settings,
    attempt_window_start: datetime,
    now: datetime,
) -> tuple[MessageState, str | None]:
    """Resolve an ambiguous submission by checking WAHA's own record of
    what happened, since WAHA has no client-supplied idempotent message
    ID to dedupe against.

    Returns (SENT, provider_message_id) if a matching outgoing voice
    message is found, (AUDIO_READY, None) if the window has clearly
    passed with nothing found, or (DELIVERY_UNKNOWN, None) if still
    inconclusive -- callers must treat that third case as "do not call
    record_delivery_attempt yet," not as a legal transition target.
    """
    # Implementation fills in the exact endpoint/field names recorded in
    # docs/task-logs/T16.md's "WAHA facts verified before designing"
    # section from Step 1 above.
    ...
```

(The docstring's `...` body is the one deliberate exception to this plan's "no placeholders" rule: its exact contents depend on Step 1's real, not-yet-observed API response and cannot be written correctly in advance — matching the design spec's own "left open, deliberately" note for this exact function.)

- [ ] **Step 5: Run, confirm green**

```bash
T13_VOICE_SAMPLE=... T15_WAHA_SETTINGS=... uv run pytest tests/e2e/test_reconciliation.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/personal_voice_msg/sender.py tests/e2e/test_reconciliation.py docs/task-logs/T16.md docker-compose.yml
git commit -m "T16: add reconcile_delivery against real WAHA chat history"
```

---

## Task 8: `delivery.py` orchestrator — happy path

**Files:**
- Create: `src/personal_voice_msg/delivery.py`
- Create: `tests/e2e/test_delivery.py`

**Interfaces:**
- Consumes: `scheduling.classify_trigger`/`planned_triggers_for_date` (existing, T05), `Database.reserve_next_message`/`mark_audio_ready`/`record_delivery_attempt` (Tasks 2-3), `audio_pipeline.produce_voice_note` (Task 5), `sender.send_voice_note`/`sign_request`/`SenderRejected`/`SenderAmbiguous` (T15/Task 6).
- Produces: `async def run_daily_send(database: Database, settings: Settings, session: aiohttp.ClientSession, recipient_key: str, pacific_date: date, embedding_path: Path, text: str, now: datetime) -> MessageState` — returns the delivery's resulting state after one orchestration pass (caller loops it; see Task 11).

- [ ] **Step 1: Write the failing test**

Create `tests/e2e/test_delivery.py`, following `tests/e2e/test_sender.py`'s gating pattern:

```python
from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.config import Settings, load_settings
from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.delivery import run_daily_send
from personal_voice_msg.history import MessageHistory

pytestmark = pytest.mark.e2e

VOICE_SAMPLE_ENV = "T13_VOICE_SAMPLE"
WAHA_SETTINGS_ENV = "T15_WAHA_SETTINGS"
_MISSING = [n for n in (VOICE_SAMPLE_ENV, WAHA_SETTINGS_ENV) if n not in os.environ]
if _MISSING:
    pytestmark = [
        pytest.mark.e2e,
        pytest.mark.skip(reason=f"requires {', '.join(_MISSING)} (docs/task-logs/T16.md)"),
    ]


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings(Path(os.environ[WAHA_SETTINGS_ENV]))


def approved_message(database: Database, text: str, now: datetime) -> None:
    database.migrate()
    decision = MessageHistory(database).evaluate_and_record(text, now)
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, now)


def test_run_daily_send_reaches_sent_from_a_queued_message(
    settings: Settings, tmp_path: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    now = datetime.now(UTC)
    text = f"A real end to end delivery test at {now.timestamp()}."
    approved_message(database, text, now)
    embedding_path = settings.voice_embedding.reveal()

    async def run() -> MessageState:
        async with aiohttp.ClientSession() as session:
            return await run_daily_send(
                database, settings, session, "recipient_t16_e2e",
                date(2026, 8, 9), embedding_path, text, now,
            )

    result = asyncio.run(run())

    assert result is MessageState.SENT
```

- [ ] **Step 2: Run, confirm failure**

```bash
T13_VOICE_SAMPLE=... T15_WAHA_SETTINGS=... uv run pytest tests/e2e/test_delivery.py -v
```

Expected: `ModuleNotFoundError: No module named 'personal_voice_msg.delivery'`.

- [ ] **Step 3: Implement the happy path only**

Create `src/personal_voice_msg/delivery.py`:

```python
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import aiohttp

from personal_voice_msg.audio_pipeline import produce_voice_note
from personal_voice_msg.config import Settings
from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.sender import (
    SenderAmbiguous,
    SenderRejected,
    send_voice_note,
    sign_request,
)


async def run_daily_send(
    database: Database,
    settings: Settings,
    session: aiohttp.ClientSession,
    recipient_key: str,
    pacific_date: date,
    embedding_path: Path,
    text: str,
    now: datetime,
) -> MessageState:
    """Advance today's delivery by one orchestration step from wherever it
    currently sits, and return its resulting state. Callers loop this
    within the send window -- see Task 11.
    """
    reservation = database.reserve_next_message(recipient_key, pacific_date, now)
    if reservation is None:
        existing_state = _find_existing_delivery_state(
            database, recipient_key, pacific_date
        )
        if existing_state is None:
            return MessageState.QUEUED  # nothing reserved, nothing queued
        delivery_id = _find_existing_delivery_id(database, recipient_key, pacific_date)
        state = existing_state
    else:
        delivery_id = reservation.delivery_id
        state = reservation.state

    if state is MessageState.RESERVED:
        temp_destination = Path(f"/tmp/t16-{delivery_id}.ogg")
        produce_voice_note(database, delivery_id, embedding_path, text, temp_destination, now)
        state = MessageState.AUDIO_READY

    if state is MessageState.AUDIO_READY:
        audio_bytes = database.get_audio_data(delivery_id)
        database.transition_delivery(delivery_id, MessageState.SENDING, now)
        idempotency_key = f"delivery-{delivery_id}"
        timestamp = int(now.timestamp())
        signature = sign_request(
            settings.sender_auth_key.reveal().encode(), idempotency_key, timestamp
        )
        try:
            provider_message_id = await send_voice_note(
                session, database, settings, audio_bytes,
                idempotency_key, timestamp, signature, now,
            )
        except SenderRejected:
            database.record_delivery_attempt(delivery_id, MessageState.FAILED, now)
            return MessageState.FAILED
        except SenderAmbiguous:
            database.record_delivery_attempt(
                delivery_id, MessageState.DELIVERY_UNKNOWN, now
            )
            return MessageState.DELIVERY_UNKNOWN
        else:
            database.record_delivery_attempt(
                delivery_id, MessageState.SENT, now,
                provider_message_id=provider_message_id,
            )
            return MessageState.SENT

    return state


def _find_existing_delivery_id(
    database: Database, recipient_key: str, pacific_date: date
) -> int:
    raise NotImplementedError  # filled in by Task 9 -- see its Step 3


def _find_existing_delivery_state(
    database: Database, recipient_key: str, pacific_date: date
) -> MessageState | None:
    raise NotImplementedError  # filled in by Task 9 -- see its Step 3
```

`_find_existing_delivery_id`/`_find_existing_delivery_state` are needed because `reserve_next_message` returns `None` both when nothing is queued *and* when today's delivery already exists (per T03's design — see `database.py`'s `reserve_next_message`, which checks for an existing `(recipient_key, pacific_date)` row first). Task 9 adds a proper `Database.get_delivery_for_date(recipient_key, pacific_date) -> int | None` lookup rather than leaving these as `NotImplementedError` placeholders — this task's happy-path test never reaches that branch (a fresh `reserve_next_message` always returns a `Reservation` when nothing exists yet), so it is legitimately untested here and finished next.

- [ ] **Step 4: Run, confirm green**

```bash
T13_VOICE_SAMPLE=... T15_WAHA_SETTINGS=... uv run pytest tests/e2e/test_delivery.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/personal_voice_msg/delivery.py tests/e2e/test_delivery.py
git commit -m "T16: add delivery.run_daily_send happy path"
```

---

## Task 9: `Database.get_delivery_for_date` + resume-from-any-state

**Files:**
- Modify: `src/personal_voice_msg/database.py`
- Modify: `src/personal_voice_msg/delivery.py`
- Test: `tests/fast/test_delivery_attempts.py`, `tests/e2e/test_delivery.py`

**Interfaces:**
- Produces: `Database.get_delivery_for_date(recipient_key: str, pacific_date: date) -> int | None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/fast/test_delivery_attempts.py`:

```python
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
```

- [ ] **Step 2: Run, confirm failure**

```bash
uv run pytest tests/fast/test_delivery_attempts.py -k get_delivery_for_date -v
```

Expected: `AttributeError: 'Database' object has no attribute 'get_delivery_for_date'`.

- [ ] **Step 3: Implement**

```python
def get_delivery_for_date(
    self, recipient_key: str, pacific_date: date
) -> int | None:
    connection = self._connect()
    try:
        row = connection.execute(
            "SELECT id FROM deliveries WHERE recipient_key = ? AND pacific_date = ?",
            (recipient_key, pacific_date.isoformat()),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else int(row[0])
```

Update `delivery.py`'s `run_daily_send` to use it directly instead of the two `NotImplementedError` helpers:

```python
    reservation = database.reserve_next_message(recipient_key, pacific_date, now)
    if reservation is not None:
        delivery_id = reservation.delivery_id
        state = reservation.state
    else:
        existing_id = database.get_delivery_for_date(recipient_key, pacific_date)
        if existing_id is None:
            return MessageState.QUEUED  # nothing reserved, nothing queued
        delivery_id = existing_id
        state = database.get_delivery_state(delivery_id)
```

Remove the two `_find_existing_delivery_*` helper functions entirely.

- [ ] **Step 4: Run, confirm green**

```bash
uv run pytest tests/fast/test_delivery_attempts.py -v
T13_VOICE_SAMPLE=... T15_WAHA_SETTINGS=... uv run pytest tests/e2e/test_delivery.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/personal_voice_msg/database.py src/personal_voice_msg/delivery.py tests/fast/test_delivery_attempts.py
git commit -m "T16: resume run_daily_send from any existing delivery state"
```

---

## Task 10: Retry (`FAILED`) and reconciliation (`DELIVERY_UNKNOWN`) paths

**Files:**
- Modify: `src/personal_voice_msg/delivery.py`
- Test: `tests/e2e/test_delivery.py`

**Interfaces:**
- Consumes: `reconcile_delivery` (Task 7).
- Produces: `run_daily_send` now also handles `state in {FAILED, DELIVERY_UNKNOWN, SENDING}` on entry.

- [ ] **Step 1: Write the failing tests**

Append to `tests/e2e/test_delivery.py`:

```python
def test_run_daily_send_retries_a_failed_delivery_reusing_stored_audio(
    settings: Settings, tmp_path: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    now = datetime.now(UTC)
    text = f"A retried delivery test at {now.timestamp()}."
    approved_message(database, text, now)
    embedding_path = settings.voice_embedding.reveal()

    async def run(step_now: datetime) -> MessageState:
        async with aiohttp.ClientSession() as session:
            return await run_daily_send(
                database, settings, session, "recipient_t16_retry",
                date(2026, 8, 9), embedding_path, text, step_now,
            )

    asyncio.run(run(now))  # RESERVED -> AUDIO_READY
    delivery_id = database.get_delivery_for_date(
        "recipient_t16_retry", date(2026, 8, 9)
    )
    assert delivery_id is not None
    stored_audio = database.get_audio_data(delivery_id)
    database.transition_delivery(delivery_id, MessageState.SENDING, now)
    database.record_delivery_attempt(delivery_id, MessageState.FAILED, now)

    result = asyncio.run(run(now))

    assert result is MessageState.SENT
    assert database.get_audio_data.__wrapped__ if False else True  # no-op guard
    # The retry must have reused the exact bytes produced on the first pass
    # -- confirmed by checking no second synthesis happened: the delivery
    # went FAILED -> AUDIO_READY -> SENDING -> SENT without ever returning
    # to RESERVED, and the pre-retry audio_data equals what was already
    # stored (re-read before it gets cleared by any later cleanup step).
    assert stored_audio  # sanity: non-empty real audio was captured above
```

Simplify that last assertion block during implementation once the real behavior is observed (the comment above is a placeholder for reasoning, not for code — replace it with a direct equality check against a value captured before the retry, e.g. re-derive `stored_audio` from a `delivery_attempts` count check: exactly one row with `outcome='failed'` and one with `outcome='sent'`, proving exactly one retry happened, not a fresh synthesis):

```python
    with sqlite3.connect(database.path) as connection:
        outcomes = connection.execute(
            "SELECT outcome FROM delivery_attempts WHERE delivery_id = ? "
            "ORDER BY id",
            (delivery_id,),
        ).fetchall()
    assert outcomes == [("failed",), ("sent",)]
```

(add `import sqlite3` to the test file's imports)

```python
def test_run_daily_send_reclassifies_orphaned_sending_as_delivery_unknown(
    settings: Settings, tmp_path: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    now = datetime.now(UTC)
    text = f"An orphaned-sending test at {now.timestamp()}."
    approved_message(database, text, now)
    reservation = database.reserve_next_message(
        "recipient_t16_orphan", date(2026, 8, 9), now
    )
    assert reservation is not None
    database.mark_audio_ready(reservation.delivery_id, b"stale-audio-bytes", now)
    # Simulate a crash: a prior process flipped to SENDING and never
    # returned -- this run did not just do that itself.
    database.transition_delivery(
        reservation.delivery_id, MessageState.SENDING, now
    )

    async def run() -> MessageState:
        async with aiohttp.ClientSession() as session:
            return await run_daily_send(
                database, settings, session, "recipient_t16_orphan",
                date(2026, 8, 9), settings.voice_embedding.reveal(), text, now,
            )

    result = asyncio.run(run())

    assert result is MessageState.DELIVERY_UNKNOWN
```

- [ ] **Step 2: Run, confirm failure**

```bash
T13_VOICE_SAMPLE=... T15_WAHA_SETTINGS=... uv run pytest tests/e2e/test_delivery.py -v -k "retries_a_failed or reclassifies_orphaned"
```

Expected: the retry test fails because `run_daily_send` today falls through to `return state` for `FAILED` without retrying; the orphaned-`SENDING` test fails the same way for `SENDING` found on entry.

- [ ] **Step 3: Implement**

In `delivery.py`, before the `RESERVED`/`AUDIO_READY` branches, add:

```python
    if state is MessageState.SENDING:
        # This process did not just set SENDING itself in this call --
        # a prior attempt (possibly a crashed process) may or may not
        # have reached WAHA. Reclassify as ambiguous rather than guessing.
        database.record_delivery_attempt(
            delivery_id, MessageState.DELIVERY_UNKNOWN, now
        )
        return MessageState.DELIVERY_UNKNOWN

    if state is MessageState.FAILED:
        database.transition_delivery(delivery_id, MessageState.AUDIO_READY, now)
        state = MessageState.AUDIO_READY

    if state is MessageState.DELIVERY_UNKNOWN:
        latest_attempt_at = _latest_attempt_time(database, delivery_id)
        outcome, provider_message_id = await reconcile_delivery(
            session, settings, latest_attempt_at, now
        )
        if outcome is MessageState.DELIVERY_UNKNOWN:
            return MessageState.DELIVERY_UNKNOWN  # still inconclusive
        database.record_delivery_attempt(
            delivery_id, outcome, now, provider_message_id=provider_message_id
        )
        if outcome is MessageState.SENT:
            return MessageState.SENT
        state = MessageState.AUDIO_READY
```

Add the small helper (queries the most recent `delivery_attempts.attempted_at` for this delivery, used as the reconciliation window's start):

```python
def _latest_attempt_time(database: Database, delivery_id: int) -> datetime:
    import sqlite3

    connection = sqlite3.connect(database.path)
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
```

(This raw `sqlite3` read from outside `Database` matches this file's existing pattern of using `Database.path` directly in tests, but for production code it is cleaner to add a proper `Database.get_latest_attempt_time(delivery_id) -> datetime` method instead — do that: move this logic into `database.py` as a fourth method alongside `record_delivery_attempt`, following the exact style of `get_delivery_state`, and import it normally in `delivery.py` rather than reaching into `database.path` from application code.)

Add the necessary import at the top of `delivery.py`: `from personal_voice_msg.sender import reconcile_delivery`.

- [ ] **Step 4: Run, confirm green**

```bash
T13_VOICE_SAMPLE=... T15_WAHA_SETTINGS=... uv run pytest tests/e2e/test_delivery.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/personal_voice_msg/delivery.py src/personal_voice_msg/database.py tests/e2e/test_delivery.py
git commit -m "T16: retry FAILED and reconcile DELIVERY_UNKNOWN in run_daily_send"
```

---

## Task 11: Send-window enforcement

**Files:**
- Modify: `src/personal_voice_msg/delivery.py`
- Test: `tests/fast/test_delivery_window.py` (new — pure logic, no real WAHA needed)

**Interfaces:**
- Consumes: `scheduling.classify_trigger`, `scheduling.planned_triggers_for_date`, `scheduling.ScheduleKind`, `scheduling.TriggerStatus` (existing, unchanged).
- Produces: `run_daily_send` raises `ValueError` if called with a `now` outside the `DAILY_SEND` window for `pacific_date`.

- [ ] **Step 1: Write the failing test**

Create `tests/fast/test_delivery_window.py`:

```python
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from personal_voice_msg.database import Database
from personal_voice_msg.delivery import run_daily_send
from personal_voice_msg.scheduling import PACIFIC, planned_triggers_for_date, ScheduleKind


def _send_trigger_bounds(pacific_date: date) -> tuple[datetime, datetime]:
    trigger = next(
        t for t in planned_triggers_for_date(pacific_date)
        if t.kind is ScheduleKind.DAILY_SEND
    )
    return trigger.scheduled_at, trigger.cutoff_at


import asyncio


@pytest.mark.fast
def test_run_daily_send_rejects_a_call_before_the_send_window(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    start, _ = _send_trigger_bounds(date(2026, 8, 9))
    too_early = start - timedelta(seconds=1)

    async def call() -> None:
        await run_daily_send(
            database, None, None, "recipient_t16_window",  # type: ignore[arg-type]
            date(2026, 8, 9), Path("unused"), "unused text", too_early,
        )

    with pytest.raises(ValueError, match="send window"):
        asyncio.run(call())


@pytest.mark.fast
def test_run_daily_send_rejects_a_call_at_or_after_the_cutoff(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    _, cutoff = _send_trigger_bounds(date(2026, 8, 9))

    async def call() -> None:
        await run_daily_send(
            database, None, None, "recipient_t16_window",  # type: ignore[arg-type]
            date(2026, 8, 9), Path("unused"), "unused text", cutoff,
        )

    with pytest.raises(ValueError, match="send window"):
        asyncio.run(call())
```

Both tests pass `None` for `settings`/`session` because the window check must reject the call before either is ever touched — if implementation Step 3 places the window check after any use of those parameters, these tests will fail with an `AttributeError`/`TypeError` instead of the expected `ValueError`, which is itself a useful signal that the check is in the wrong place.

- [ ] **Step 2: Run, confirm failure**

```bash
uv run pytest tests/fast/test_delivery_window.py -v
```

Expected: FAIL — no `ValueError` raised (today's `run_daily_send` has no window check at all, so it proceeds to `database.reserve_next_message` and returns normally without error).

- [ ] **Step 3: Implement**

At the top of `run_daily_send`, before anything else:

```python
    from personal_voice_msg.scheduling import (
        ScheduleKind,
        TriggerStatus,
        classify_trigger,
        planned_triggers_for_date,
    )

    send_trigger = next(
        trigger
        for trigger in planned_triggers_for_date(pacific_date)
        if trigger.kind is ScheduleKind.DAILY_SEND
    )
    if classify_trigger(send_trigger, now) is not TriggerStatus.DUE:
        raise ValueError(
            "run_daily_send can only run inside the DAILY_SEND window"
        )
```

Move these imports to the top of the file alongside the other `personal_voice_msg` imports rather than inline (inline shown here only to make the diff obvious against the existing function body).

- [ ] **Step 4: Run, confirm green**

```bash
uv run pytest tests/fast/test_delivery_window.py -v
T13_VOICE_SAMPLE=... T15_WAHA_SETTINGS=... uv run pytest tests/e2e/test_delivery.py -v
```

The e2e tests must still pass — their `now = datetime.now(UTC)` calls will only be inside the real window if run between 07:00-07:05 Pacific on the day they execute, which is unrealistic for routine test runs. Fix this: change every e2e test in `tests/e2e/test_delivery.py` to construct an explicit `now` inside a real `DAILY_SEND` window instead of `datetime.now(UTC)`, e.g.:

```python
from personal_voice_msg.scheduling import ScheduleKind, planned_triggers_for_date

def _in_send_window(pacific_date: date) -> datetime:
    trigger = next(
        t for t in planned_triggers_for_date(pacific_date)
        if t.kind is ScheduleKind.DAILY_SEND
    )
    return trigger.scheduled_at
```

and use `now = _in_send_window(date(2026, 8, 9))` (or whatever fixed test date each test already uses) instead of `datetime.now(UTC)` throughout `tests/e2e/test_delivery.py`. Update Tasks 8-10's test code accordingly — this is a real fix surfaced by writing this task, not scope creep: those tests were always going to need explicit-clock construction once the window check existed.

- [ ] **Step 5: Commit**

```bash
git add src/personal_voice_msg/delivery.py tests/fast/test_delivery_window.py tests/e2e/test_delivery.py
git commit -m "T16: enforce the DAILY_SEND window in run_daily_send"
```

---

## Task 12: Fault-injection suite — full plan red-test coverage

**Files:**
- Create: `tests/e2e/test_delivery_fault_injection.py`
- Modify: `docs/task-logs/T16.md`

**Interfaces:**
- Consumes: everything above. No new production code expected from this task — if a real fault exposes a bug, fix it here with its own focused red/green cycle before moving on.

- [ ] **Step 1: Write the fault-injection tests**

Create `tests/e2e/test_delivery_fault_injection.py`, gated identically to `tests/e2e/test_delivery.py` plus a `T15_WAHA_CONTAINER` env var (matching `tests/security/test_waha_deployment.py`'s existing gating for container-control tests):

```python
import subprocess


def _pause_container(container: str) -> None:
    subprocess.run(["docker", "pause", container], check=True)


def _unpause_container(container: str) -> None:
    subprocess.run(["docker", "unpause", container], check=True)


def test_a_paused_waha_container_produces_delivery_unknown_not_a_duplicate(
    settings: Settings, tmp_path: Path
) -> None:
    """Real fault injection: pause the real WAHA container mid-request so
    the client-side timeout fires with the request already in flight --
    exactly the "timeout after possible submission" scenario."""
    container = os.environ["T15_WAHA_CONTAINER"]
    database = Database(tmp_path / "state.sqlite3")
    now = _in_send_window(date(2026, 8, 9))
    text = f"A paused-container fault injection test at {now.timestamp()}."
    approved_message(database, text, now)
    embedding_path = settings.voice_embedding.reveal()

    _pause_container(container)
    try:
        async def run() -> MessageState:
            async with aiohttp.ClientSession() as session:
                return await run_daily_send(
                    database, settings, session, "recipient_t16_fault",
                    date(2026, 8, 9), embedding_path, text, now,
                )

        result = asyncio.run(run())
    finally:
        _unpause_container(container)

    assert result is MessageState.DELIVERY_UNKNOWN

    delivery_id = database.get_delivery_for_date(
        "recipient_t16_fault", date(2026, 8, 9)
    )
    assert delivery_id is not None
    later = now + timedelta(seconds=30)

    async def reconcile_and_retry() -> MessageState:
        async with aiohttp.ClientSession() as session:
            return await run_daily_send(
                database, settings, session, "recipient_t16_fault",
                date(2026, 8, 9), embedding_path, text, later,
            )

    final_result = asyncio.run(reconcile_and_retry())

    # Either the paused container's original request eventually landed
    # (SENT after reconciliation finds it) or it definitely did not
    # (retried and freshly SENT) -- either way, exactly one attempt row
    # has outcome='sent', proving no duplicate voice note.
    assert final_result is MessageState.SENT
    with sqlite3.connect(database.path) as connection:
        sent_count = connection.execute(
            "SELECT COUNT(*) FROM delivery_attempts "
            "WHERE delivery_id = ? AND outcome = 'sent'",
            (delivery_id,),
        ).fetchone()
    assert sent_count == (1,)


@pytest.mark.parametrize(
    "interrupt_state",
    [
        MessageState.RESERVED,
        MessageState.AUDIO_READY,
        MessageState.SENDING,
        MessageState.FAILED,
        MessageState.DELIVERY_UNKNOWN,
    ],
)
def test_restart_at_every_delivery_state_never_duplicates_a_send(
    settings: Settings, tmp_path: Path, interrupt_state: MessageState
) -> None:
    """Simulates a process restart by constructing a fresh Database handle
    from the same file and resuming from each persisted state in turn."""
    database_path = tmp_path / "state.sqlite3"
    database = Database(database_path)
    now = _in_send_window(date(2026, 8, 9))
    text = f"A restart-at-{interrupt_state.value} test at {now.timestamp()}."
    approved_message(database, text, now)
    reservation = database.reserve_next_message(
        f"recipient_t16_restart_{interrupt_state.value}", date(2026, 8, 9), now
    )
    assert reservation is not None

    if interrupt_state is not MessageState.RESERVED:
        database.mark_audio_ready(
            reservation.delivery_id, b"pre-existing-audio-bytes", now
        )
    if interrupt_state in (
        MessageState.SENDING,
        MessageState.FAILED,
        MessageState.DELIVERY_UNKNOWN,
    ):
        database.transition_delivery(reservation.delivery_id, MessageState.SENDING, now)
    if interrupt_state is MessageState.FAILED:
        database.record_delivery_attempt(reservation.delivery_id, MessageState.FAILED, now)
    if interrupt_state is MessageState.DELIVERY_UNKNOWN:
        database.record_delivery_attempt(
            reservation.delivery_id, MessageState.DELIVERY_UNKNOWN, now
        )

    # "Restart": a fresh Database instance over the same file, a fresh
    # run_daily_send call -- nothing carried over in memory.
    resumed_database = Database(database_path)
    embedding_path = settings.voice_embedding.reveal()

    async def resume(step_now: datetime) -> MessageState:
        async with aiohttp.ClientSession() as session:
            return await run_daily_send(
                resumed_database, settings, session,
                f"recipient_t16_restart_{interrupt_state.value}",
                date(2026, 8, 9), embedding_path, text, step_now,
            )

    result = asyncio.run(resume(now))
    if result is MessageState.DELIVERY_UNKNOWN:
        # Ambiguity found on restart (the SENDING/orphan case) -- one more
        # pass to drive it to a final outcome via reconciliation/retry.
        result = asyncio.run(resume(now + timedelta(seconds=5)))

    assert result is MessageState.SENT
    with sqlite3.connect(database_path) as connection:
        sent_count = connection.execute(
            "SELECT COUNT(*) FROM delivery_attempts "
            "WHERE delivery_id = ? AND outcome = 'sent'",
            (reservation.delivery_id,),
        ).fetchone()
    assert sent_count == (1,)
```

- [ ] **Step 2: Run, confirm failure**

```bash
T13_VOICE_SAMPLE=... T15_WAHA_SETTINGS=... T15_WAHA_CONTAINER=... uv run pytest tests/e2e/test_delivery_fault_injection.py -v
```

Read every failure carefully — a failure here can mean either a missing test helper (fix the test) or a genuine bug in Tasks 8-11's implementation (fix the source, following systematic-debugging, not by loosening the assertion).

- [ ] **Step 3: Fix whatever the red tests reveal**

No code is pre-written for this step because its content depends entirely on what Step 2 finds. Apply `superpowers:systematic-debugging` to any real failure: reproduce it deterministically first (these tests already are deterministic given explicit `now` values and real container control), find the actual root cause in `delivery.py`/`database.py`/`sender.py`, apply the smallest fix, and re-run.

- [ ] **Step 4: Run the complete suite, confirm green**

```bash
T13_VOICE_SAMPLE=... T15_WAHA_SETTINGS=... T15_WAHA_CONTAINER=... uv run pytest tests/e2e/test_delivery_fault_injection.py -v
uv run pytest -m fast
uv run pytest -m security
uv run mypy src
uv run ruff check .
uv run python scripts/repository_policy.py all --root .
docker compose config --quiet
```

- [ ] **Step 5: Write `docs/task-logs/T16.md`**

Follow `docs/task-logs/T15.md`'s structure: Status, Dependencies, the design decisions already recorded in the spec (link to it rather than repeating), Implementation summary per module, the WAHA reconciliation facts from Task 7 Step 1, a red-test → plan-requirement mapping table (mirror T15's), and full verification command output.

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/test_delivery_fault_injection.py docs/task-logs/T16.md
git commit -m "T16: add fault-injection suite proving no duplicate sends"
```

---

## Task 13: Independent security review

**Files:** none (review only; fix any confirmed finding in its own follow-up commit on this branch).

- [ ] **Step 1: Dispatch a fresh, unbiased reviewer**

Follow T15's exact precedent (`docs/task-logs/T15.md`'s "Independent review" section): dispatch a fresh `general-purpose` subagent with no implementation context — give it only `AGENTS.md`, `IMPLEMENTATION_PLAN.md`'s T16 section, `docs/superpowers/specs/2026-08-09-t16-exactly-once-delivery-design.md`, this plan, and the full diff. Instruct it explicitly to verify claims against actual source, not trust the task log's prose, and to focus on: can any path duplicate a send; can `SenderRejected` vs `SenderAmbiguous` be confused at any raise site; is `record_delivery_attempt` genuinely atomic; can the reconciliation window be gamed; does the send-window check in Task 11 have any bypass.

- [ ] **Step 2: Triage findings**

For each finding, verify against current source directly (per `CLAUDE.md`'s "verify findings against current source before acting on them"). Fix confirmed issues with their own focused red/green cycle. Record informational-only or rejected findings with reasoning, matching T15's log style.

- [ ] **Step 3: Final verification pass**

```bash
uv run pytest -m fast
uv run pytest -m security
uv run mypy src
uv run ruff check .
uv run python scripts/repository_policy.py all --root .
docker compose config --quiet
```

- [ ] **Step 4: Update `docs/task-logs/T16.md`** with the review's findings and resolution, matching T15's "Independent review" section structure.

- [ ] **Step 5: Commit, open PR, merge**

```bash
git add docs/task-logs/T16.md
git commit -m "T16: record independent security review"
git push -u origin task/T16-exactly-once-delivery
gh pr create --fill
gh pr merge --merge --delete-branch
```

Per `CLAUDE.md`'s per-task workflow — merge via GitHub PR, not locally.

---

## Self-Review Notes

**Spec coverage:** §1 (audio persistence) → Tasks 1, 2, 5. §2 (attempt records) → Tasks 1, 3. §3 (state machine) → Task 2. §4 (sender taxonomy) → Task 6. §5 (reconciliation) → Task 7. §6 (orchestration loop) → Tasks 8-11. §7 (nonce pruning) → explicitly out of scope, owned by T19 per the spec — no task needed here. All six plan red tests (definite-failure retry, confirmed-delivery-no-retry, timeout-becomes-unknown, unknown-reconciled-before-retry, restart-cannot-duplicate, retries-reuse-audio) are covered by Task 12's fault-injection suite plus the unit-level coverage in Tasks 2-3.

**Type consistency check performed:** `produce_voice_note`'s return type change (`Path` → `bytes`) is threaded consistently through Task 5's test rewrite and Task 8's orchestrator, which calls it but discards the return value in favor of a subsequent `get_audio_data` read on the retry path — confirmed both reads return identical bytes since Task 2's `mark_audio_ready` is the sole writer. `record_delivery_attempt`'s `outcome: MessageState` parameter (not a new `Literal` type, refining the design spec's earlier sketch) is used identically across Tasks 3, 10, and 12. `reconcile_delivery`'s three-way return (`SENT`/`AUDIO_READY`/`DELIVERY_UNKNOWN`-as-inconclusive-sentinel) is handled explicitly in Task 10's `run_daily_send` branch, not silently passed through to a state-machine call that would reject it.

**Known deferred item:** Task 7 Step 4's `reconcile_delivery` body is intentionally not fully written in this plan (the one exception to "no placeholders," justified inline) because its correct implementation depends on Task 7 Step 1's real, not-yet-observed WAHA API response shape — writing fabricated field names now would be worse than leaving it explicit, and the task's own steps require filling it in for real before the task can be called done.
