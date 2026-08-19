# T17 — Telegram consent, STOP, and kill switch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Telegram sender a durable global sending-control flag driven by three
signals — an exact `STOP` from the enrolled recipient (via low-frequency `getUpdates`
polling), a `403 bot was blocked by the user` response at send time, and a plain
owner-run admin kill switch — plus a structural tie between `run_daily_send`'s
`recipient_key` and the enrolled `telegram_chat_id`.

**Architecture:** A new `SCHEMA_V8` adds a single-row current-state table, an
append-only audit-event table, and a single-row inbound-offset-cursor table to
`database.py`, with five new `Database` methods. A new `consent.py` module polls
`getUpdates` once per call and durably disables sending on an exact STOP from the
enrolled chat. `sender.py` gains a narrow `SenderBlocked` exception for the
blocked-by-user 403. `delivery.py`'s `run_daily_send` gains a disabled-sending gate
(checked before any production/network work) and a fail-closed assertion tying
`recipient_key` to the enrolled chat id.

**Tech Stack:** Python 3.12, SQLite (stdlib `sqlite3`), `aiohttp`, `pytest` — no new
dependencies.

## Global Constraints

- No mocks: real file-backed SQLite, real local sockets for HTTP fault injection, real
  Telegram API calls for integration/e2e tests (env-gated, skip cleanly without
  credentials) — `AGENTS.md` §Strict no-mock TDD policy.
- Fail closed: unknown/malformed state is a rejection, not a pass-through.
- One recipient, one send per Pacific calendar date; exact `STOP` disables sending
  durably; admin kill switch always wins — `AGENTS.md` §WhatsApp and delivery rules.
- Secrets (bot token, chat id) never in Git, logs, or task prompts.
- T17 is on `AGENTS.md`'s mandatory independent-security-review list — Task 6 requests
  that review; do not self-approve.
- Design reference: `docs/superpowers/specs/2026-08-19-t17-telegram-consent-stop-killswitch-design.md`.

---

### Task 1: Schema V8 — sending control, audit trail, inbound offset cursor

**Files:**
- Modify: `src/personal_voice_msg/database.py`
- Test: `tests/fast/test_database_migrations.py`
- Test: `tests/fast/test_sending_control.py` (new)

**Interfaces:**
- Produces: `DisableReason(StrEnum)` with members `STOP_COMMAND`, `BLOCKED_BY_USER`,
  `ADMIN_KILL_SWITCH`; `recipient_key_for_chat_id(chat_id: int) -> str`;
  `Database.is_sending_enabled(self) -> bool`;
  `Database.disable_sending(self, reason: DisableReason, now: datetime) -> None`;
  `Database.enable_sending(self, note: str, now: datetime) -> None`;
  `Database.get_telegram_inbound_offset(self) -> int | None`;
  `Database.set_telegram_inbound_offset(self, offset: int, now: datetime) -> None`.
  All consumed by Tasks 2–4.

- [ ] **Step 1: Write the failing tests**

Create `tests/fast/test_sending_control.py`:

```python
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
```

Add to `tests/fast/test_database_migrations.py`:

1. Extend `EXPECTED_TABLES` (line 15-27) to include the three new tables:

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
    "sending_control",
    "sending_control_events",
    "telegram_inbound_offset",
}
```

2. Add a downgrade helper and upgrade test, mirroring the existing
   `downgrade_current_database_to_v6`/`test_version_six_database_upgrades_to_delivery_attempts_without_data_loss`
   pair — append after `downgrade_current_database_to_v6` (after line 124):

```python
def downgrade_current_database_to_v7(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE IF EXISTS sending_control")
        connection.execute("DROP TABLE IF EXISTS sending_control_events")
        connection.execute("DROP TABLE IF EXISTS telegram_inbound_offset")
        connection.execute("DELETE FROM schema_migrations WHERE version = 8")
```

3. Add the upgrade test, mirroring the V6-upgrade test's shape, appended after
   `test_version_six_database_upgrades_to_delivery_attempts_without_data_loss` (after
   line 176):

```python
@pytest.mark.fast
def test_version_seven_database_upgrades_to_sending_control_without_data_loss(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "version-seven.sqlite3"
    database = Database(database_path)
    database.migrate()
    downgrade_current_database_to_v7(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO runs (run_kind, pacific_date, state, started_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "migration_probe",
                "2026-08-19",
                "preserve-me",
                "2026-08-19T13:50:00+00:00",
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
        new_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN "
            "('sending_control', 'sending_control_events', "
            "'telegram_inbound_offset') ORDER BY name"
        ).fetchall()
    assert versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)]
    assert preserved == [
        (
            "migration_probe",
            "2026-08-19",
            "preserve-me",
            "2026-08-19T13:50:00+00:00",
        )
    ]
    assert new_tables == [
        ("sending_control",),
        ("sending_control_events",),
        ("telegram_inbound_offset",),
    ]
```

4. Update every existing `assert versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,)]`
   in this file to append `, (8,)`. This appears in:
   `test_rerunning_migration_is_idempotent` (line 200),
   `test_version_three_database_upgrades_to_daily_runs_without_data_loss` (line 528),
   `test_version_five_database_upgrades_to_sender_auth_nonces_without_data_loss`
   (line 580), `test_version_four_database_upgrades_to_message_rejections_without_data_loss`
   (line 632), `test_version_two_database_upgrades_to_unique_normalized_hashes`
   (line 662). Change each occurrence from:
   ```python
   assert versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,)]
   ```
   to:
   ```python
   assert versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)]
   ```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/fast/test_sending_control.py tests/fast/test_database_migrations.py -v`
Expected: FAIL — `ImportError: cannot import name 'DisableReason'` (and similar) for
`test_sending_control.py`; the updated `assert versions == [..., (8,)]` lines FAIL with
an actual list ending in `(7,)`.

- [ ] **Step 3: Add `DisableReason` and `recipient_key_for_chat_id`**

In `src/personal_voice_msg/database.py`, right after the `DailyRunState` class
(currently lines 37-38, right before `CONTENT_TRANSITIONS`):

```python
class DisableReason(StrEnum):
    STOP_COMMAND = "stop_command"
    BLOCKED_BY_USER = "blocked_by_user"
    ADMIN_KILL_SWITCH = "admin_kill_switch"
```

Right after the `OPAQUE_RECIPIENT_KEY` regex definition (currently line 71):

```python
def recipient_key_for_chat_id(chat_id: int) -> str:
    """Canonical recipient_key for a given enrolled telegram_chat_id --
    ties run_daily_send's idempotency boundary to the real delivery
    destination. See
    docs/superpowers/specs/2026-08-19-t17-telegram-consent-stop-killswitch-design.md.
    """

    return f"recipient_telegram_{chat_id}"
```

- [ ] **Step 4: Add `SCHEMA_V8_STATEMENTS` and `EXPECTED_SCHEMA_V8_OBJECTS`**

Right after this existing line (currently line 357):

```python
EXPECTED_SCHEMA_V7_OBJECTS[("table", "deliveries")] = _v7_deliveries_sql
```

insert:

```python
# T17's durable sending-control state, audit trail, and inbound-poll
# offset cursor (docs/superpowers/specs/2026-08-19-t17-telegram-consent-stop-killswitch-design.md)
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
```

- [ ] **Step 5: Bump `CURRENT_SCHEMA_VERSION`**

Change (currently line 70):
```python
CURRENT_SCHEMA_VERSION = 7
```
to:
```python
CURRENT_SCHEMA_VERSION = 8
```

- [ ] **Step 6: Update `migrate()`'s version guards and final V7→V8 step**

Two identical guard blocks exist in `migrate()` (near what are currently lines
469-476 and 484-491). Using `replace_all: true` (both occurrences are byte-identical),
change:

```python
            if versions not in (
                set(),
                {1},
                {1, 2},
                {1, 2, 3},
                {1, 2, 3, 4},
                {1, 2, 3, 4, 5},
                {1, 2, 3, 4, 5, 6},
                {1, 2, 3, 4, 5, 6, CURRENT_SCHEMA_VERSION},
            ):
                raise MigrationError("database has an unknown migration version")
```

to:

```python
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
```

Then, the tail of `migrate()` (currently the block starting at
`if versions == {1, 2, 3, 4, 5, 6}:` through the final `connection.close()`) is unique
text — replace it (not `replace_all`) from:

```python
            if versions == {1, 2, 3, 4, 5, 6}:
                for statement in SCHEMA_V7_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (CURRENT_SCHEMA_VERSION,),
                )

            _validate_schema(connection, EXPECTED_SCHEMA_V7_OBJECTS)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
```

to:

```python
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
```

Do **not** change `v7_reached = 7 in versions_at_entry` — it still correctly governs
whether the `deliveries` table's post-ALTER (V7) shape should be substituted into the
V1-V6 staged validation dicts, which is unrelated to V8 (V8 adds no ALTER).

- [ ] **Step 7: Add the five new `Database` methods**

Append after `record_sender_nonce` (the last method in the class, currently ending at
line 1373):

```python

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
            if row is not None and bool(row[0]):
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
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/fast/test_sending_control.py tests/fast/test_database_migrations.py -v`
Expected: PASS, all tests green.

- [ ] **Step 9: Run the full fast suite and type/lint checks**

Run: `uv run pytest -m fast -q`
Expected: PASS, no regressions (previous baseline: 518 passed).

Run: `uv run mypy src` and `uv run ruff check .`
Expected: no new errors.

- [ ] **Step 10: Commit**

```bash
git add src/personal_voice_msg/database.py tests/fast/test_sending_control.py tests/fast/test_database_migrations.py
git commit -m "T17: add SCHEMA_V8 sending-control, audit trail, and inbound offset cursor"
```

---

### Task 2: `consent.py` — inbound STOP polling

**Files:**
- Create: `src/personal_voice_msg/consent.py`
- Test: `tests/fast/test_consent.py` (new)
- Test: `tests/integration/test_consent_integration.py` (new)

**Interfaces:**
- Consumes: `Database.is_sending_enabled`, `Database.disable_sending`,
  `Database.get_telegram_inbound_offset`, `Database.set_telegram_inbound_offset`,
  `DisableReason.STOP_COMMAND` (Task 1); `Settings.telegram_chat_id`,
  `Settings.telegram_bot_token` (existing `config.py`).
- Produces: `TelegramPollError(RuntimeError)`;
  `poll_inbound_stop(session, database, settings, now, *, api_base=TELEGRAM_API_BASE) -> bool`
  (async). Not consumed by any other task in this plan — a standalone primitive for a
  future scheduler to call.

- [ ] **Step 1: Write the failing tests**

Create `tests/fast/test_consent.py` (pure logic, no network — mirrors
`tests/fast/test_recipient_enrollment.py`'s `_extract_chat_id` unit-test style):

```python
from __future__ import annotations

import pytest

from personal_voice_msg.consent import TelegramPollError, _process_updates


@pytest.mark.fast
def test_process_updates_finds_an_exact_stop_from_the_enrolled_chat() -> None:
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 100,
                "message": {"chat": {"id": 555}, "text": "STOP"},
            }
        ],
    }

    new_offset, stop_found = _process_updates(payload, enrolled_chat_id=555)

    assert new_offset == 101
    assert stop_found is True


@pytest.mark.fast
def test_process_updates_ignores_stop_from_a_different_chat() -> None:
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 200,
                "message": {"chat": {"id": 999}, "text": "STOP"},
            }
        ],
    }

    new_offset, stop_found = _process_updates(payload, enrolled_chat_id=555)

    assert new_offset == 201
    assert stop_found is False


@pytest.mark.fast
def test_process_updates_ignores_non_stop_text_from_the_enrolled_chat() -> None:
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 300,
                "message": {"chat": {"id": 555}, "text": "hello"},
            }
        ],
    }

    new_offset, stop_found = _process_updates(payload, enrolled_chat_id=555)

    assert new_offset == 301
    assert stop_found is False


@pytest.mark.fast
@pytest.mark.parametrize("text", ["stop", "Stop", " STOP", "STOP ", "STOP!"])
def test_process_updates_requires_an_exact_case_sensitive_untrimmed_match(
    text: str,
) -> None:
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 400,
                "message": {"chat": {"id": 555}, "text": text},
            }
        ],
    }

    _, stop_found = _process_updates(payload, enrolled_chat_id=555)

    assert stop_found is False


@pytest.mark.fast
def test_process_updates_ignores_non_message_updates() -> None:
    payload = {
        "ok": True,
        "result": [
            {"update_id": 500, "edited_message": {"chat": {"id": 555}, "text": "STOP"}},
        ],
    }

    new_offset, stop_found = _process_updates(payload, enrolled_chat_id=555)

    assert new_offset == 501
    assert stop_found is False


@pytest.mark.fast
def test_process_updates_returns_none_offset_for_an_empty_batch() -> None:
    payload = {"ok": True, "result": []}

    new_offset, stop_found = _process_updates(payload, enrolled_chat_id=555)

    assert new_offset is None
    assert stop_found is False


@pytest.mark.fast
def test_process_updates_advances_offset_past_the_highest_update_id_seen() -> None:
    payload = {
        "ok": True,
        "result": [
            {"update_id": 10, "message": {"chat": {"id": 999}, "text": "hi"}},
            {"update_id": 12, "message": {"chat": {"id": 555}, "text": "STOP"}},
            {"update_id": 11, "message": {"chat": {"id": 999}, "text": "hi"}},
        ],
    }

    new_offset, stop_found = _process_updates(payload, enrolled_chat_id=555)

    assert new_offset == 13
    assert stop_found is True


@pytest.mark.fast
def test_process_updates_raises_when_telegram_reports_failure() -> None:
    payload = {"ok": False, "error_code": 401, "description": "Unauthorized"}

    with pytest.raises(TelegramPollError, match="getUpdates failed"):
        _process_updates(payload, enrolled_chat_id=555)


@pytest.mark.fast
def test_process_updates_raises_when_result_is_not_a_list() -> None:
    payload = {"ok": True, "result": "not-a-list"}

    with pytest.raises(TelegramPollError, match="malformed"):
        _process_updates(payload, enrolled_chat_id=555)


@pytest.mark.fast
def test_process_updates_raises_when_an_update_id_is_missing_or_invalid() -> None:
    payload = {
        "ok": True,
        "result": [{"message": {"chat": {"id": 555}, "text": "STOP"}}],
    }

    with pytest.raises(TelegramPollError, match="malformed"):
        _process_updates(payload, enrolled_chat_id=555)
```

Create `tests/integration/test_consent_integration.py`:

```python
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.config import load_settings
from personal_voice_msg.consent import poll_inbound_stop
from personal_voice_msg.database import Database

pytestmark = pytest.mark.integration

TELEGRAM_SETTINGS_ENV = "T16B_TELEGRAM_SETTINGS"
_MISSING = [n for n in (TELEGRAM_SETTINGS_ENV,) if n not in os.environ]
if _MISSING:
    pytestmark = [
        pytest.mark.integration,
        pytest.mark.skip(
            reason=(
                "requires a real Telegram bot token/chat id, and a real "
                "'STOP' message already sent by hand from the enrolled "
                f"chat to that bot before running; set {', '.join(_MISSING)}"
            )
        ),
    ]


def test_a_real_exact_stop_from_the_enrolled_chat_disables_sending_durably(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    settings = load_settings(Path(os.environ[TELEGRAM_SETTINGS_ENV]))
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    now = datetime.now(UTC)

    async def poll() -> bool:
        async with aiohttp.ClientSession() as session:
            return await poll_inbound_stop(session, database, settings, now)

    disabled = asyncio.run(poll())

    assert disabled is True
    assert database.is_sending_enabled() is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/fast/test_consent.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'personal_voice_msg.consent'`.

- [ ] **Step 3: Implement `consent.py`**

```python
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import aiohttp

from personal_voice_msg.config import Settings
from personal_voice_msg.database import Database, DisableReason

TELEGRAM_API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 65_536
RESPONSE_CHUNK_BYTES = 8_192
STOP_TEXT = "STOP"


class TelegramPollError(RuntimeError):
    """Raised on a network failure or a malformed/non-ok getUpdates
    response. The inbound offset cursor is left untouched, so the next
    poll retries the same batch instead of silently losing it -- see
    docs/superpowers/specs/2026-08-19-t17-telegram-consent-stop-killswitch-design.md.
    """


def _process_updates(
    payload: Any, enrolled_chat_id: int
) -> tuple[int | None, bool]:
    """Pure and synchronous on purpose -- no I/O -- so the matching logic
    can be unit-tested directly with real Telegram-shaped payloads,
    matching this project's real-data-over-mocks testing policy (compare
    recipient_enrollment.py's ``_extract_chat_id``).

    Returns ``(new_offset, stop_found)``. ``new_offset`` is ``None`` when
    the batch was empty (nothing to acknowledge); otherwise it is one past
    the highest ``update_id`` seen, Telegram's own documented
    acknowledgment convention. A message from any chat other than
    ``enrolled_chat_id`` is inspected only far enough to be ignored --
    never compared against STOP_TEXT, never causing any other effect.
    """

    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise TelegramPollError(f"Telegram getUpdates failed: {payload!r}")
    updates = payload.get("result")
    if not isinstance(updates, list):
        raise TelegramPollError("Telegram getUpdates response was malformed")

    max_update_id: int | None = None
    stop_found = False
    for update in updates:
        if not isinstance(update, dict):
            raise TelegramPollError("Telegram getUpdates response was malformed")
        update_id = update.get("update_id")
        if type(update_id) is not int:
            raise TelegramPollError("Telegram getUpdates response was malformed")
        if max_update_id is None or update_id > max_update_id:
            max_update_id = update_id

        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        if chat_id == enrolled_chat_id and message.get("text") == STOP_TEXT:
            stop_found = True

    new_offset = None if max_update_id is None else max_update_id + 1
    return new_offset, stop_found


async def poll_inbound_stop(
    session: aiohttp.ClientSession,
    database: Database,
    settings: Settings,
    now: datetime,
    *,
    api_base: str = TELEGRAM_API_BASE,
) -> bool:
    """Poll Telegram's getUpdates once, using the durably-stored offset
    cursor, and durably disable sending on an exact STOP from the
    enrolled telegram_chat_id. Returns whether sending is disabled after
    this call (either just now, or already disabled beforehand).

    Not wired to any scheduler -- no scheduler task exists yet. Callers
    (a future daily-window orchestrator) call this once per window, per
    IMPLEMENTATION_PLAN.md's T17 section.
    """

    offset = database.get_telegram_inbound_offset()
    params: dict[str, str] = {"timeout": "0"}
    if offset is not None:
        params["offset"] = str(offset)
    bot_token = settings.telegram_bot_token.reveal()

    try:
        async with session.get(
            f"{api_base}/bot{bot_token}/getUpdates",
            params=params,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            allow_redirects=False,
        ) as response:
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(RESPONSE_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    break
                chunks.append(chunk)
            body = b"".join(chunks)
    except (aiohttp.ClientError, TimeoutError):
        raise TelegramPollError("no response received from Telegram") from None

    if total > MAX_RESPONSE_BYTES:
        raise TelegramPollError("Telegram response exceeded the size limit")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise TelegramPollError("Telegram response was not valid JSON") from None

    new_offset, stop_found = _process_updates(
        payload, settings.telegram_chat_id.reveal()
    )

    if stop_found:
        database.disable_sending(DisableReason.STOP_COMMAND, now)
    if new_offset is not None:
        database.set_telegram_inbound_offset(new_offset, now)

    return not database.is_sending_enabled()
```

- [ ] **Step 4: Run the fast tests to verify they pass**

Run: `uv run pytest tests/fast/test_consent.py -v`
Expected: PASS, all tests green.

- [ ] **Step 5: Run integration test if credentials are available, otherwise confirm clean skip**

Run: `uv run pytest tests/integration/test_consent_integration.py -v`
Expected: skip cleanly (no `T16B_TELEGRAM_SETTINGS`) unless the owner has set it up with
a real pre-sent "STOP" message, in which case it should PASS.

- [ ] **Step 6: Run full fast suite and type/lint checks**

Run: `uv run pytest -m fast -q`
Expected: PASS, no regressions.

Run: `uv run mypy src` and `uv run ruff check .`
Expected: no new errors.

- [ ] **Step 7: Commit**

```bash
git add src/personal_voice_msg/consent.py tests/fast/test_consent.py tests/integration/test_consent_integration.py
git commit -m "T17: add consent.py inbound STOP polling"
```

---

### Task 3: `sender.py` — `SenderBlocked` for the blocked-by-user 403

**Files:**
- Modify: `src/personal_voice_msg/sender.py`
- Test: `tests/security/test_sender_error_taxonomy.py`

**Interfaces:**
- Produces: `SenderBlocked(SenderRejected)`. Consumed by Task 4 (`delivery.py`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/security/test_sender_error_taxonomy.py` (reuses the file's existing
`_FixedStatusServer`, `_settings`, `valid_audio_bytes`, `_send` fixtures/helpers as-is):

```python
def test_a_blocked_by_user_403_raises_sender_blocked_not_generic_rejected(
    valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    """T17: Telegram's specific blocked-by-user 403 description must be
    distinguishable from any other definite rejection, so the caller can
    treat it as a durable stop signal alongside STOP."""
    from personal_voice_msg.sender import SenderBlocked

    server = _FixedStatusServer(
        "HTTP/1.1 403 Forbidden",
        body=(
            b'{"ok":false,"error_code":403,'
            b'"description":"Forbidden: bot was blocked by the user"}'
        ),
    )
    try:
        settings = _settings(tmp_path)
        database = Database(tmp_path / "state.sqlite3")
        database.migrate()

        with pytest.raises(SenderBlocked):
            asyncio.run(
                _send(
                    settings,
                    valid_audio_bytes,
                    database,
                    f"http://127.0.0.1:{server.port}",
                )
            )
    finally:
        server.stop()


def test_a_different_403_reason_raises_plain_sender_rejected_not_blocked(
    valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    """A 403 for a different reason (e.g. a revoked token) must not be
    misclassified as blocked-by-user -- exact type check, not isinstance,
    since SenderBlocked is itself a SenderRejected subclass."""
    from personal_voice_msg.sender import SenderBlocked

    server = _FixedStatusServer(
        "HTTP/1.1 403 Forbidden",
        body=b'{"ok":false,"error_code":403,"description":"Forbidden: unauthorized"}',
    )
    try:
        settings = _settings(tmp_path)
        database = Database(tmp_path / "state.sqlite3")
        database.migrate()

        with pytest.raises(SenderRejected) as exc_info:
            asyncio.run(
                _send(
                    settings,
                    valid_audio_bytes,
                    database,
                    f"http://127.0.0.1:{server.port}",
                )
            )
        assert type(exc_info.value) is SenderRejected
        assert not isinstance(exc_info.value, SenderBlocked)
    finally:
        server.stop()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/security/test_sender_error_taxonomy.py -k blocked -v`
Expected: FAIL — `ImportError: cannot import name 'SenderBlocked'` (skips cleanly
instead if `T13_VOICE_SAMPLE` is unset — set it locally to actually exercise this, or
verify the failure reason is the import error, not the skip, before proceeding).

- [ ] **Step 3: Implement `SenderBlocked` and detection in `sender.py`**

Add the new exception class right after `SenderRejected` (currently lines 40-42):

```python
class SenderRejected(SenderError):
    """The request definitely never reached Telegram, or Telegram gave a
    definite rejection. Safe to retry immediately."""


class SenderBlocked(SenderRejected):
    """Telegram's specific 403 'Forbidden: bot was blocked by the user'
    response -- the closest thing to a proactive block signal Telegram
    offers, though still necessarily reactive (learned only by attempting
    a send). Callers must treat this as a durable stop signal alongside
    STOP -- see delivery.py and
    docs/superpowers/specs/2026-08-19-t17-telegram-consent-stop-killswitch-design.md.
    """
```

Add a narrow detection helper right after `_describe_rejection` (currently ending at
line 102):

```python
def _is_blocked_by_user(body: bytes) -> bool:
    """True only for Telegram's specific blocked-by-user 403 description
    -- a narrow, exact substring check, not a general 403 assumption
    (other 403 reasons exist in principle, even for a private one-to-one
    chat)."""

    try:
        payload = json.loads(body)
        description = payload.get("description")
    except (json.JSONDecodeError, AttributeError):
        return False
    return isinstance(description, str) and "blocked by the user" in description
```

Change the status-code check inside `send_voice_note` (currently lines 190-193) from:

```python
    if status in _DEFINITE_REJECTION_STATUS_CODES:
        raise SenderRejected(
            f"Telegram rejected the request: {_describe_rejection(body)}"
        )
```

to:

```python
    if status == 403 and _is_blocked_by_user(body):
        raise SenderBlocked(
            f"Telegram rejected the request: {_describe_rejection(body)}"
        )
    if status in _DEFINITE_REJECTION_STATUS_CODES:
        raise SenderRejected(
            f"Telegram rejected the request: {_describe_rejection(body)}"
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/security/test_sender_error_taxonomy.py -v`
Expected: PASS if `T13_VOICE_SAMPLE` is set; clean skip otherwise (confirm the skip
reason, not a failure).

- [ ] **Step 5: Run full fast/security suites and type/lint checks**

Run: `uv run pytest -m fast -q` and `uv run pytest -m security -q`
Expected: PASS, no regressions.

Run: `uv run mypy src` and `uv run ruff check .`
Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add src/personal_voice_msg/sender.py tests/security/test_sender_error_taxonomy.py
git commit -m "T17: add SenderBlocked for Telegram's blocked-by-user 403"
```

---

### Task 4: `delivery.py` — kill-switch gate, `SenderBlocked` handling, `recipient_key`/`chat_id` tie

**Files:**
- Modify: `src/personal_voice_msg/delivery.py`
- Modify: `tests/fast/test_delivery_window.py`
- Modify: `tests/e2e/test_delivery.py`
- Modify: `tests/e2e/test_delivery_fault_injection.py`

**Interfaces:**
- Consumes: `Database.is_sending_enabled`, `Database.disable_sending`,
  `DisableReason.BLOCKED_BY_USER`, `recipient_key_for_chat_id` (Task 1);
  `SenderBlocked` (Task 3).

- [ ] **Step 1: Write the failing tests**

Append to `tests/fast/test_delivery_window.py` (reuses the file's existing
`_send_trigger_bounds` helper):

```python
from personal_voice_msg.config import RuntimeProfile, Settings
from personal_voice_msg.database import DisableReason
from personal_voice_msg.redaction import SensitiveValue


def _settings(tmp_path: Path, chat_id: int) -> Settings:
    return Settings(
        profile=RuntimeProfile.DEVELOPMENT,
        telegram_chat_id=SensitiveValue(chat_id),
        telegram_bot_token=SensitiveValue("test-token"),
        voice_embedding=SensitiveValue(tmp_path / "embedding.safetensors"),
        sender_auth_key=SensitiveValue("test-sender-auth-key"),
    )


@pytest.mark.fast
def test_run_daily_send_does_not_progress_a_reserved_delivery_while_disabled(
    tmp_path: Path,
) -> None:
    """T17: the admin kill switch (or a prior STOP/blocked-by-user event)
    must stop a RESERVED delivery before any audio production or network
    call -- session=None and settings=None prove the disabled gate is
    checked before either is ever touched."""
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    pacific_date = date(2026, 8, 9)
    start, _ = _send_trigger_bounds(pacific_date)

    decision = MessageHistory(database).evaluate_and_record(
        "A kill-switch reserved-send test.", start
    )
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, start)
    recipient_key = "recipient_t17_kill_switch"
    reservation = database.reserve_next_message(recipient_key, pacific_date, start)
    assert reservation is not None

    database.disable_sending(DisableReason.ADMIN_KILL_SWITCH, start)

    async def call() -> MessageState:
        return await run_daily_send(
            database, None, None, recipient_key,  # type: ignore[arg-type]
            pacific_date, Path("unused"), start,
        )

    result = asyncio.run(call())

    assert result is MessageState.RESERVED
    assert (
        database.get_delivery_state(reservation.delivery_id)
        is MessageState.RESERVED
    )


@pytest.mark.fast
def test_run_daily_send_rejects_a_recipient_key_that_does_not_match_the_enrolled_chat_id(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    pacific_date = date(2026, 8, 9)
    start, _ = _send_trigger_bounds(pacific_date)

    decision = MessageHistory(database).evaluate_and_record(
        "A recipient key mismatch test.", start
    )
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, start)
    mismatched_key = "recipient_some_other_key"
    reservation = database.reserve_next_message(mismatched_key, pacific_date, start)
    assert reservation is not None
    database.mark_audio_ready(reservation.delivery_id, b"stale-audio-bytes", start)
    settings = _settings(tmp_path, chat_id=555)

    async def call() -> MessageState:
        return await run_daily_send(
            database, settings, None, mismatched_key,  # type: ignore[arg-type]
            pacific_date, Path("unused"), start,
        )

    with pytest.raises(ValueError, match="does not match the enrolled chat id"):
        asyncio.run(call())

    assert (
        database.get_delivery_state(reservation.delivery_id)
        is MessageState.AUDIO_READY
    )
```

Update `tests/e2e/test_delivery.py`:

1. Add the import:
```python
from personal_voice_msg.database import Database, MessageState, recipient_key_for_chat_id
```
(replaces the current `from personal_voice_msg.database import Database, MessageState`).

2. In `test_run_daily_send_reaches_sent_from_a_queued_message`, change:
```python
                database, settings, session, "recipient_t16_e2e",
```
to:
```python
                database, settings, session,
                recipient_key_for_chat_id(settings.telegram_chat_id.reveal()),
```

3. In `test_run_daily_send_retries_a_failed_delivery_reusing_stored_audio`, both
   occurrences (the `reserve_next_message` call and the `run_daily_send` call) change
   from the literal `"recipient_t16_retry"` to a shared local:
```python
    recipient_key = recipient_key_for_chat_id(settings.telegram_chat_id.reveal())
    reservation = database.reserve_next_message(
        recipient_key, PACIFIC_DATE, now
    )
```
   and:
```python
            return await run_daily_send(
                database, settings, session, recipient_key,
                PACIFIC_DATE, embedding_path, now,
            )
```

4. `test_run_daily_send_reclassifies_orphaned_sending_as_delivery_unknown` is left
   **unchanged** — it never reaches the `AUDIO_READY` branch (crash-recovery from
   `SENDING` returns `DELIVERY_UNKNOWN` directly without touching `settings`), so the
   new assertion never fires for this test; changing its literal key would be
   pointless churn.

Update `tests/e2e/test_delivery_fault_injection.py`:

1. Add `recipient_key_for_chat_id` to the existing `database` import:
```python
from personal_voice_msg.database import Database, MessageState, recipient_key_for_chat_id
```

2. In `test_restart_at_every_delivery_state_never_duplicates_a_send`, change:
```python
    recipient_key = f"recipient_t16b_restart_{interrupt_state.value}"
```
to:
```python
    recipient_key = recipient_key_for_chat_id(settings.telegram_chat_id.reveal())
```

3. In `test_a_real_timeout_during_send_becomes_delivery_unknown_and_never_retries`,
   change:
```python
    recipient_key = "recipient_t16b_hang"
```
to:
```python
    recipient_key = recipient_key_for_chat_id(settings.telegram_chat_id.reveal())
```

- [ ] **Step 2: Run the new fast tests to verify they fail for the intended reason**

Run: `uv run pytest tests/fast/test_delivery_window.py -k "kill_switch or recipient_key" -v`
Expected: FAIL — the kill-switch test currently reaches `RESERVED` -> attempts
production (`produce_voice_note`) with `Path("unused")` and blows up on a filesystem
error, not a controlled `RESERVED` return; the mismatch test currently proceeds past
where the new `ValueError` should fire and instead fails trying to call
`send_voice_note` with `session=None`.

- [ ] **Step 3: Implement the changes in `delivery.py`**

Update the `sender` import (currently lines 18-24) from:

```python
from personal_voice_msg.sender import (
    TELEGRAM_API_BASE,
    SenderAmbiguous,
    SenderRejected,
    send_voice_note,
    sign_request,
)
```

to:

```python
from personal_voice_msg.sender import (
    TELEGRAM_API_BASE,
    SenderAmbiguous,
    SenderBlocked,
    SenderRejected,
    send_voice_note,
    sign_request,
)
```

Update the `database` import (currently line 11) from:

```python
from personal_voice_msg.database import Database, MessageState
```

to:

```python
from personal_voice_msg.database import (
    Database,
    DisableReason,
    MessageState,
    recipient_key_for_chat_id,
)
```

Insert the disabled-sending gate right after the `SENDING` branch and before the
`FAILED` branch (currently the code between line 87's `return MessageState.DELIVERY_UNKNOWN`
and line 89's `if state is MessageState.FAILED:`):

```python
        sending_started_at = database.get_delivery_updated_at(delivery_id)
        database.record_delivery_attempt(
            delivery_id, MessageState.DELIVERY_UNKNOWN, sending_started_at
        )
        return MessageState.DELIVERY_UNKNOWN

    if not database.is_sending_enabled():
        # A STOP, a blocked-by-user 403, or the admin kill switch already
        # disabled sending -- stop before any production or network work,
        # for every state this could still progress from (FAILED retry,
        # RESERVED production, AUDIO_READY send). The delivery is left
        # exactly where it was; nothing here mutates it.
        return state

    if state is MessageState.FAILED:
```

Insert the `recipient_key`/`chat_id` assertion as the first line inside the
`AUDIO_READY` branch (currently line 112's `if state is MessageState.AUDIO_READY:`),
changing from:

```python
    if state is MessageState.AUDIO_READY:
        audio_bytes = database.get_audio_data(delivery_id)
```

to:

```python
    if state is MessageState.AUDIO_READY:
        if recipient_key != recipient_key_for_chat_id(
            settings.telegram_chat_id.reveal()
        ):
            raise ValueError(
                "recipient_key does not match the enrolled chat id"
            )
        audio_bytes = database.get_audio_data(delivery_id)
```

Add the `SenderBlocked` catch before the existing `SenderRejected` catch (currently
lines 126-128), changing from:

```python
        try:
            provider_message_id = await send_voice_note(
                session, database, settings, audio_bytes,
                idempotency_key, timestamp, signature, now,
                api_base=api_base,
            )
        except SenderRejected:
            database.record_delivery_attempt(delivery_id, MessageState.FAILED, now)
            return MessageState.FAILED
```

to:

```python
        try:
            provider_message_id = await send_voice_note(
                session, database, settings, audio_bytes,
                idempotency_key, timestamp, signature, now,
                api_base=api_base,
            )
        except SenderBlocked:
            database.disable_sending(DisableReason.BLOCKED_BY_USER, now)
            database.record_delivery_attempt(delivery_id, MessageState.FAILED, now)
            return MessageState.FAILED
        except SenderRejected:
            database.record_delivery_attempt(delivery_id, MessageState.FAILED, now)
            return MessageState.FAILED
```

(`except SenderBlocked` must appear textually before `except SenderRejected` --
`SenderBlocked` is a subclass, and Python matches the first applicable `except` in
order.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/fast/test_delivery_window.py -v`
Expected: PASS, all tests in the file green (existing `session=None`/`settings=None`
tests for the `SENDING`/`DELIVERY_UNKNOWN` branches are unaffected — the new gate sits
after those branches' early returns).

- [ ] **Step 5: Run full fast suite; run e2e suite if credentials are available**

Run: `uv run pytest -m fast -q`
Expected: PASS, no regressions.

Run: `uv run pytest -m e2e -q`
Expected: skip cleanly without `T13_VOICE_SAMPLE`/`T16B_TELEGRAM_SETTINGS`; PASS if the
owner has both configured against the real test bot/chat.

Run: `uv run mypy src` and `uv run ruff check .`
Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add src/personal_voice_msg/delivery.py tests/fast/test_delivery_window.py tests/e2e/test_delivery.py tests/e2e/test_delivery_fault_injection.py
git commit -m "T17: gate run_daily_send on the sending-control flag, tie recipient_key to chat_id"
```

---

### Task 5: Security boundary extension

**Files:**
- Modify: `tests/security/test_voice_enrollment_boundaries.py`

**Interfaces:**
- Consumes: nothing new; extends the existing AST-based check to cover `consent.py`.

- [ ] **Step 1: Write the failing test setup**

Change `FORBIDDEN_MODULES` (currently lines 12-17) from:

```python
FORBIDDEN_MODULES = {
    "pocket_tts",
    "personal_voice_msg.voice_enrollment",
    "personal_voice_msg.sender",
    "personal_voice_msg.delivery",
}
```

to:

```python
FORBIDDEN_MODULES = {
    "pocket_tts",
    "personal_voice_msg.voice_enrollment",
    "personal_voice_msg.sender",
    "personal_voice_msg.delivery",
    "personal_voice_msg.consent",
}
```

No new `FORBIDDEN_ATTRIBUTE_NAMES` are needed: `consent.py` introduces no new secret
attribute beyond `telegram_bot_token`/`telegram_chat_id`, already forbidden.

- [ ] **Step 2: Verify the existing test still passes as-is (this is an additive-only change, nothing should currently violate it)**

Run: `uv run pytest tests/security/test_voice_enrollment_boundaries.py -v`
Expected: PASS — `discovery/`, `generation/`, `judging/` do not import `consent.py`
today, so this extension has nothing to catch yet; it's a regression guard for the
future.

- [ ] **Step 3: Run full security suite**

Run: `uv run pytest -m security -q`
Expected: PASS, no regressions.

- [ ] **Step 4: Commit**

```bash
git add tests/security/test_voice_enrollment_boundaries.py
git commit -m "T17: extend AST trust-boundary test to consent.py"
```

---

### Task 6: Independent security review, task log, `AGENTS.md` update, final verification

**Files:**
- Create: `docs/task-logs/T17.md`
- Modify: `AGENTS.md`

**Interfaces:** None — documentation and verification only.

- [ ] **Step 1: Run the complete verification suite fresh, in order**

```bash
uv run pytest -m fast -q
uv run pytest -m integration -q
uv run pytest -m security -q
uv run ruff check .
uv run mypy src
uv run python scripts/repository_policy.py all --root .
```

Record the exact pass/fail counts and any env-gated skips (matching T16b's own
verification section style in `docs/task-logs/T16b.md`).

- [ ] **Step 2: Request the mandatory independent security review**

Per `AGENTS.md` §Agent collaboration rules ("Security-sensitive tasks T06, T15, T16,
T17, and T18 require independent review") and this project's CLAUDE.md ("Independent
review for security-sensitive tasks ... do not self-approve"), dispatch a fresh,
unbiased reviewer (no prior context from this implementation) to trace the actual
source of `consent.py`, the `sending_control` gate in `delivery.py`, and the
`SenderBlocked` detection in `sender.py` against:
- the design spec (`docs/superpowers/specs/2026-08-19-t17-telegram-consent-stop-killswitch-design.md`)
- the plan's red tests (`IMPLEMENTATION_PLAN.md`'s T17 section)

Fix any findings the reviewer confirms against current source (not the task log's
prose) before proceeding. Record the review outcome in `docs/task-logs/T17.md`.

- [ ] **Step 3: Write `docs/task-logs/T17.md`**

Follow the existing task-log format (see `docs/task-logs/T16b.md` for the most recent
example): Status, Design summary (link the design spec), Implementation (task-by-task
commit list), Verification (the Step 1 command output), independent review findings and
fix round if any, Next step.

- [ ] **Step 4: Update `AGENTS.md`**

In §Confirmed stack and the "Immediate next step" section, replace the "T17 has not yet
been planned" language with a completion entry mirroring T16b's own entry shape —
summarize the sending-control flag, the three disable triggers (STOP, blocked-by-user,
admin kill switch), the `recipient_key`/`chat_id` tie, and point to
`docs/task-logs/T17.md`. Update the "Actual next step" line to point at T18.

- [ ] **Step 5: Commit**

```bash
git add docs/task-logs/T17.md AGENTS.md
git commit -m "T17: record independent security review and update confirmed stack"
```

- [ ] **Step 6: Open the PR**

```bash
gh pr create --title "T17: recipient consent, STOP, and kill switch (Telegram)" --body "$(cat <<'EOF'
Implements the sending-control flag (STOP, blocked-by-user 403, admin kill switch),
inbound getUpdates polling, and the recipient_key/chat_id structural tie, per
docs/superpowers/specs/2026-08-19-t17-telegram-consent-stop-killswitch-design.md and
docs/superpowers/plans/2026-08-19-t17-telegram-consent-stop-killswitch.md.

Independent security review: see docs/task-logs/T17.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Then, once green and reviewed: `gh pr merge --merge --delete-branch`.

---

## Self-Review

**Spec coverage:**
- Exact STOP from enrolled chat id disables durably → Task 2 (`poll_inbound_stop` +
  `_process_updates`), tested in `test_consent.py` and `test_consent_integration.py`.
- STOP from any other chat id has no effect → `_process_updates` fast tests (Task 2).
- Other replies never invoke discovery → by construction (`consent.py` has zero
  coupling to `discovery/`), enforced by Task 5's AST boundary extension.
- Disabled state survives restart → Task 1's
  `test_disabled_state_survives_reopening_the_database`.
- Admin kill switch stops a reserved send → Task 4's
  `test_run_daily_send_does_not_progress_a_reserved_delivery_while_disabled`.
- 403 blocked-by-user durably disables sending → Task 3 (`SenderBlocked` detection) +
  Task 4 (the `except SenderBlocked` handler calling `disable_sending`).
- `recipient_key`/`chat_id` structural tie → Task 4's fail-closed assertion and its
  test, plus updated e2e call sites.
- Independent security review → Task 6.

**Placeholder scan:** No TBD/TODO; every step has complete, runnable code or an exact
shell command.

**Type consistency:** `DisableReason`, `recipient_key_for_chat_id`, `is_sending_enabled`,
`disable_sending`, `enable_sending`, `get_telegram_inbound_offset`,
`set_telegram_inbound_offset` (Task 1) are used with identical names and signatures in
Tasks 2 and 4. `SenderBlocked` (Task 3) is imported and caught with the same name in
Task 4. `poll_inbound_stop`/`TelegramPollError` (Task 2) are self-contained and not
consumed elsewhere in this plan.
