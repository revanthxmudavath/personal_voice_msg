# T17 — Recipient consent, STOP, and kill switch (Telegram) — design

Status: approved, ready for `writing-plans`.

## Context

T15/T16/T16b are complete and merged to `main` (`bbc1611`). T16b migrated the sender to the
Telegram Bot API and deleted the WAHA chat-history-reconciliation subsystem outright, since
Telegram's Bot API has no chat-history-read method for bots. It left two things for T17,
recorded in `docs/task-logs/T16b.md`'s "Next step" section:

1. Inbound STOP / admin kill switch is unimplemented — WAHA's chat-history-polling approach
   (the original, pre-2026-08-19 shape of this task) is fully superseded; nothing carries
   forward from it except the underlying requirement.
2. A latent structural gap: `run_daily_send`'s idempotency boundary (`recipient_key`, an
   opaque caller-supplied string) and the real delivery destination
   (`settings.telegram_chat_id`) are not tied together anywhere. Not a live bug today — no
   production caller of `run_daily_send` exists yet — but flagged as a T17 precondition.

This design covers both. Starting context: `AGENTS.md` §Confirmed stack, §WhatsApp and
delivery rules (Telegram-shaped), §Immediate next step; `IMPLEMENTATION_PLAN.md`'s
"T17 — Recipient consent, STOP, and kill switch (Telegram)" section (rewritten 2026-08-19);
the design spec's "Inbound handling" section
(`docs/superpowers/specs/2026-08-18-telegram-sender-design.md`).

T17 is on `AGENTS.md`'s mandatory independent-security-review list — it will not be
self-approved.

## Scope (from `IMPLEMENTATION_PLAN.md`'s T17 section)

Red tests, using real inbound Telegram messages against a real test bot/chat, no mocks:

- Exact `STOP` from the enrolled `telegram_chat_id` disables sending durably.
- `STOP` from any other chat id has no effect (never enrolled, never checked).
- Other replies never invoke the discovery agent.
- Disabled state survives restart.
- The administrator kill switch stops a reserved send.
- A `403 Forbidden: bot was blocked by the user` response from `send_voice_note` also
  durably disables sending — Telegram's only proactive block signal, necessarily reactive
  (learned only by attempting a send), not queryable in advance.

Plus, per T16b's carried-forward note: structurally tie `run_daily_send`'s `recipient_key`
to the enrolled `telegram_chat_id`.

## 1. Data model

New `SCHEMA_V8` in `database.py`, following the existing additive-migration pattern
(`SCHEMA_V1_STATEMENTS` … `SCHEMA_V7_STATEMENTS`, `_validate_schema` against an
`EXPECTED_SCHEMA_V8_OBJECTS` dict, `CURRENT_SCHEMA_VERSION = 8`).

Three small tables, mirroring the existing `deliveries` (current state) +
`delivery_attempts` (append-only audit, added in `SCHEMA_V7`) split:

- **`sending_control`** — single row, current state:
  `enabled INTEGER NOT NULL DEFAULT 1`, `reason TEXT`, `changed_at TEXT`.
- **`sending_control_events`** — append-only audit log of every disable/enable transition:
  `id INTEGER PRIMARY KEY AUTOINCREMENT`, `enabled INTEGER NOT NULL`,
  `reason TEXT NOT NULL`, `note TEXT`, `changed_at TEXT NOT NULL`. This is what makes the
  re-enable procedure "audited" — a full history, not just the latest value.
- **`telegram_inbound_offset`** — single row: the durable `getUpdates` cursor,
  `next_offset INTEGER`, `updated_at TEXT`.

New `DisableReason(StrEnum)` in `database.py`, next to `MessageState`/`DailyRunState`:
`STOP_COMMAND`, `BLOCKED_BY_USER`, `ADMIN_KILL_SWITCH`.

New `Database` methods:

- `is_sending_enabled() -> bool`
- `disable_sending(reason: DisableReason, now: datetime) -> None` — **idempotent**: a
  no-op if sending is already disabled (first trigger wins the audit record; repeated
  triggers — e.g. a second STOP, or hitting a blocked-by-user 403 on multiple days — don't
  spam the log with redundant transitions).
- `enable_sending(note: str, now: datetime) -> None` — the audited re-enable procedure.
  **Requires a non-empty, non-whitespace-only `note`** (fail-closed on blank input, same
  validation posture as `config.py`'s required-text settings) — every re-enable durably
  records why. No-op if already enabled.

The admin kill switch **is** `database.disable_sending(DisableReason.ADMIN_KILL_SWITCH, now)`
— a plain function the owner runs directly against the production database, matching this
project's existing one-off-owner-action pattern (`voice_enrollment.enroll_voice`,
`recipient_enrollment.enroll_recipient`). No new authentication surface, no new inbound
channel: whoever can already run code against the production database has full control
regardless.

New pure function, next to the existing `OPAQUE_RECIPIENT_KEY` regex it must satisfy:

```python
def recipient_key_for_chat_id(chat_id: int) -> str:
    return f"recipient_telegram_{chat_id}"
```

(See §4.)

## 2. Inbound STOP polling

New module `src/personal_voice_msg/consent.py`:

```python
async def poll_inbound_stop(
    session: aiohttp.ClientSession,
    database: Database,
    settings: Settings,
    now: datetime,
    *,
    api_base: str = TELEGRAM_API_BASE,
) -> bool
```

Design:

- Reads the durable offset. `None`/absent (first-ever poll) omits Telegram's `offset`
  parameter entirely.
- One `GET {api_base}/bot<token>/getUpdates` call, Telegram's default batch size (up to
  100 pending updates) — no pagination loop. At single-recipient, once-a-day polling
  volume, exceeding 100 unprocessed updates between polls is implausible; this is a
  documented assumption, not built for scale this project doesn't need (Karpathy rules,
  `AGENTS.md` §Karpathy development rules).
- For each returned update, in order: track the maximum `update_id` seen. If
  `message.chat.id == settings.telegram_chat_id.reveal()` **and**
  `message.text == "STOP"` — an exact, case-sensitive, untrimmed match, matching
  `AGENTS.md`'s literal "Exact STOP" wording precisely — call
  `database.disable_sending(DisableReason.STOP_COMMAND, now)`. A message from any other
  chat id is inspected only far enough to be discarded; it is never enrolled, never
  compared to `STOP`, and this function never touches `discovery/` in any way, so "other
  replies never invoke the discovery agent" holds by construction, not by a runtime check.
- After the whole batch is handled, write the new offset (`max_update_id + 1`) **once**,
  in a single DB write. If the process crashes mid-batch, the offset was never advanced,
  so the next poll re-fetches and re-processes the same batch — safe, because
  `disable_sending` is idempotent and non-enrolled-chat messages are side-effect-free
  either way.
- A network failure or a malformed/non-`ok` response raises a new `TelegramPollError`
  (mirrors `sender.py`'s existing `SenderRejected`/`SenderAmbiguous` style — raise, don't
  swallow) and leaves the offset untouched, so the next poll retries the same batch rather
  than silently losing it.
- Returns `True` if sending is disabled as a result of this call (either just now, or
  already disabled beforehand).

This function is **not wired into any scheduler**. No scheduler/cron task exists yet in
the plan (`run_daily_send` itself has no production caller today, per T16b's note). T17
builds the polling primitive and its own tests call it directly against a real test
bot/chat — the same shape as `recipient_enrollment.py`'s standalone `enroll_recipient`.
Whoever eventually wires the daily scheduler calls this once per send window, per the
plan's "once during the daily send window is sufficient at this volume" line.

## 3. Blocked-by-user detection + kill-switch gate

**`sender.py`**: `send_voice_note` currently maps the entire
`_DEFINITE_REJECTION_STATUS_CODES` allow-list (`{400, 401, 403, 404, 429}`) to a generic
`SenderRejected`. Add a narrower check: when `status == 403` **and** the parsed body's
`description` contains `"blocked by the user"` (Telegram's actual, specific wording for
this case), raise a new `SenderBlocked(SenderRejected)` — a distinguishable subclass —
instead of the generic exception. Any other 403 (there are no other realistic ones for a
private one-to-one chat, but fail safe rather than assume) still falls through to plain
`SenderRejected`.

**`delivery.py`**: `run_daily_send` gains two changes.

1. **The kill-switch/STOP gate.** Placed after the existing SENDING-crash-recovery branch
   and the DELIVERY_UNKNOWN-terminal branch (both must keep running regardless of the
   flag — they record/preserve history about a prior attempt, they don't initiate a new
   one, and neither touches the network), and *before* the FAILED-retry /
   RESERVED-production / AUDIO_READY-send progression:

   ```python
   if not database.is_sending_enabled():
       return state
   ```

   This needs only `database`, not `settings` — it does not disturb the existing tests
   that deliberately pass `session=None`/`settings=None` to prove certain branches never
   touch the network (default DB state is enabled, so untouched tests are unaffected).
   Placing the gate here means a `RESERVED` delivery never reaches audio production while
   disabled — literally "stops a reserved send" — and avoids wasted TTS/FFmpeg work too.

2. **Catch `SenderBlocked` before `SenderRejected`** in the `AUDIO_READY` branch's
   `try/except` around `send_voice_note`: call
   `database.disable_sending(DisableReason.BLOCKED_BY_USER, now)`, then record the
   delivery attempt as `FAILED` (same bookkeeping as today's generic-rejection path) and
   return. Sending is now durably disabled for every subsequent call via the gate above.

## 4. `recipient_key` ↔ `telegram_chat_id` structural tie

T16b's note offered two shapes: derive `recipient_key` internally (drop it as a parameter)
or assert it against a canonical derivation (keep the parameter). This design chooses
**assert**: `recipient_key` stays an explicit parameter to `run_daily_send` — it's already
the established primary key threaded through `deliveries`, `reserve_next_message`,
`get_delivery_for_date`, and multiple existing tests; dropping it ripples further than
this task needs.

`run_daily_send`'s `AUDIO_READY` branch — right where `settings.sender_auth_key` is
already read, immediately before calling `send_voice_note` — gains:

```python
if recipient_key != recipient_key_for_chat_id(settings.telegram_chat_id.reveal()):
    raise ValueError("recipient_key does not match the enrolled chat id")
```

This only fires on the path that is actually about to contact Telegram, so it does not
affect the early-return branches that pass `settings=None` today. It does require updating
the existing test call sites that reach `AUDIO_READY` (`tests/e2e/test_delivery.py`,
`tests/e2e/test_delivery_fault_injection.py`, the relevant cases in
`tests/fast/test_delivery_window.py`) to use a `recipient_key` derived from their own
settings fixture's `telegram_chat_id`, instead of today's arbitrary strings — bounded,
contained to test files this task already touches.

## 5. Testing plan

No-mock TDD throughout (`AGENTS.md` §Strict no-mock TDD policy). Mapping to the plan's red
tests:

| Plan requirement | Test shape |
|---|---|
| Exact STOP from enrolled chat id disables durably | Integration: real `getUpdates` call against the test bot/chat, a real "STOP" message, asserts `is_sending_enabled() == False` afterward |
| STOP from any other chat id has no effect | Same integration harness, a message from a second, non-enrolled chat id → asserts still enabled |
| Other replies never invoke discovery | Fast test: a non-STOP message batch through `poll_inbound_stop` leaves discovery-adjacent tables untouched and sending stays enabled |
| Disabled state survives restart | Fast test: disable, close and reopen a fresh `Database` handle on the same file, assert still disabled |
| Admin kill switch stops a reserved send | Fast test: reserve a message to `RESERVED`, call `disable_sending(ADMIN_KILL_SWITCH, ...)`, call `run_daily_send` with `session=None` (proves no network path is even reachable), assert state is still `RESERVED` |
| 403 blocked-by-user durably disables sending | Integration: local fake HTTP server returns a real 403 body with Telegram's exact "blocked by the user" description; asserts `SenderBlocked` is raised, then `run_daily_send` converts it into a disabled flag plus `FAILED` |
| `recipient_key`/`chat_id` tie | Fast test: a mismatched key raises before any network attempt is possible; existing e2e/fast call sites updated to matching keys |

Staging target for integration tests: a second, non-production bot/token plus the owner's
own personal Telegram chat, matching T16b's existing staging pattern.

## 6. Security review

T17 is on `AGENTS.md`'s mandatory independent-security-review list — this task will not be
self-approved. Extend the existing AST-based boundary test
(`tests/security/test_voice_enrollment_boundaries.py`) so `discovery/`, `generation/`, and
`judging/` still cannot reach `consent.py`, the bot token, the enrolled chat id, or the new
`Database.disable_sending` / `enable_sending` / `is_sending_enabled` methods — the same
shape as T13/T15/T16b's existing extensions to that file.

## What does NOT change

- T01–T16b (discovery through the Telegram sender migration) — zero changes beyond the
  `sender.py`/`delivery.py`/`database.py` edits described above.
- The one-recipient, one-send-per-Pacific-day, fail-closed philosophy throughout.
- The "no mocks, real dependencies" testing policy.

## Explicitly out of scope

- No scheduler/cron entrypoint that actually calls `poll_inbound_stop` on a schedule —
  that is a future task, not yet planned. T17 builds the callable primitive only.
- No admin-side Telegram command channel for the kill switch — a plain, owner-run
  database function was chosen instead (§1), matching this project's existing
  one-off-owner-action trust model.
- No owner alerting when sending gets disabled — queue-exhaustion/alerting is T19's scope.
- No pagination loop for `getUpdates` beyond one call's default batch size.
