# T17b — Daily-send entrypoint, live STOP wiring, and live verification — design

Status: approved, ready for `writing-plans`.

## Context

T17 (recipient consent, STOP, and admin kill switch) is complete, independently
reviewed, and about to merge. Two things it deliberately left out of scope surfaced
during finish-up:

1. `poll_inbound_stop` (`consent.py`) and `run_daily_send` (`delivery.py`) both have
   **no caller anywhere in the codebase**. Nothing in this project has ever built the
   process that actually runs the app on a schedule — `scheduling.py` (T05) only
   computes *when* things are due; nothing loops and calls the pieces. STOP and the
   kill switch are correctly-built primitives, not yet live controls.
2. T17's live verification (a real STOP message, a real blocked-by-user 403, a real
   send) was never run against genuine Telegram infrastructure — the sandboxed
   session that built it cannot make outbound HTTPS calls to `api.telegram.org` at
   all (confirmed: every attempt gets a self-signed certificate injected by the
   sandbox's network path, not a Telegram-specific issue).

This task closes both gaps, narrowly: a minimal daily-send entrypoint that gives
`poll_inbound_stop` and `run_daily_send` their first real caller, plus the exact
steps for the owner to run live verification themselves, outside the sandbox.

Named **T17b**, following the T16b precedent (a follow-on task inserted between two
numbered tasks, same shape as T16b sitting between T16 and T17). Inserted between
T17 and T18 in `IMPLEMENTATION_PLAN.md` — T18 ("cloud and container hardening")
gains a new dependency on it, since hardening the deployed container needs to know
what process actually runs inside it.

## Explicitly out of scope

Deliberately narrow, matching only what triggered this task — not a general
"finish the production system" mandate:

- **Weekly discovery invocation and `queue_refill.refill_queue()` wiring.** Both
  have no caller either, but that gap predates T17 and isn't something either
  bullet that triggered this task mentioned. Left for a future task (likely folded
  into T20's "run the complete cloud system" prerequisite, or split out if T20
  doesn't want to absorb it).
- **Any persistent daemon, process supervision, or internal scheduling loop.** See
  "Entrypoint shape" below for why.
- **Structured logging, alerting, log rotation.** T19's scope, not built here. The
  entrypoint's error handling is deliberately minimal (see below).
- **Docker/systemd/cron unit files themselves.** T18's scope (it already owns
  "cloud and container hardening" and pinning how things run in production). This
  task produces the Python entrypoint T18 will wire into a container; it does not
  write the container's cron/systemd configuration.

## Entrypoint shape: short-lived script, not a daemon

A single Python entrypoint that runs once, does whatever's due, and exits. An
external timer (cron inside the container, or a systemd timer — T18's concern, not
this task's) invokes it every 1-2 minutes. This is not a new design choice — it's
the shape T05 already committed to: every idempotency mechanism in this codebase
(`reserve_next_message`'s `ON CONFLICT DO NOTHING`, `claim_daily_run`, the whole
`DELIVERY_TRANSITIONS` state machine, `poll_inbound_stop`'s offset-cursor
idempotency) is designed around "safe to call repeatedly, cheaply, within a
window," not around a long-running process holding state in memory. A daemon with
an internal sleep loop would need new supervision concerns (crash recovery, graceful
shutdown) this project has never needed and didn't ask for here.

## Exact interfaces consumed (re-verified against current source, not memory)

This section exists specifically to answer "does this align with what's already
built" — every signature below was read directly from the actual current file
content on this branch immediately before writing this spec, not recalled from
earlier in this session.

**`src/personal_voice_msg/delivery.py`:**
```python
async def run_daily_send(
    database: Database,
    settings: Settings,
    session: aiohttp.ClientSession,
    recipient_key: str,
    pacific_date: date,
    embedding_path: Path,
    now: datetime,
    *,
    api_base: str = TELEGRAM_API_BASE,  # imported from sender.py
) -> MessageState
```
Raises `ValueError` if called outside the `DAILY_SEND` window. Returns
`MessageState.QUEUED` if nothing is reserved and nothing was already queued for
that date — this is a valid, non-error outcome the entrypoint must treat as
"nothing to do," not a failure.

**`src/personal_voice_msg/consent.py`:**
```python
async def poll_inbound_stop(
    session: aiohttp.ClientSession,
    database: Database,
    settings: Settings,
    now: datetime,
    *,
    api_base: str = TELEGRAM_API_BASE,  # consent.py's own separate constant,
                                          # same value, independent copy
) -> bool
```
Raises `TelegramPollError` on network failure or a malformed/non-ok response.

**`src/personal_voice_msg/scheduling.py`** (unchanged since T05, already used
identically by `run_daily_send` itself):
```python
class ScheduleKind(StrEnum):
    WEEKLY_DISCOVERY = "weekly_discovery"
    DAILY_PREPARE = "daily_prepare"
    DAILY_SEND = "daily_send"

class TriggerStatus(StrEnum):
    NOT_DUE = "not_due"
    DUE = "due"
    MISSED = "missed"

def planned_triggers_for_date(pacific_date: date) -> tuple[ScheduledTrigger, ...]
def classify_trigger(trigger: ScheduledTrigger, now: datetime) -> TriggerStatus
```

**`src/personal_voice_msg/config.py`:**
```python
def load_settings(config_path: Path) -> Settings
```
`Settings` has no database-path field — every existing call site (tests, this
entrypoint) constructs `Database(path)` with an explicitly supplied path,
independent of `Settings`.

**`src/personal_voice_msg/database.py`:**
```python
def recipient_key_for_chat_id(chat_id: int) -> str  # "recipient_telegram_<id>"
class Database:
    def __init__(self, path: Path) -> None
    def migrate(self) -> None
```

**Two independent `TELEGRAM_API_BASE` constants**, both `"https://api.telegram.org"`
in production — `sender.py`'s (which `delivery.py` re-exports as its own default)
and `consent.py`'s own. Deliberately not unified (an earlier T17 task decision,
matching each module's own no-unnecessary-coupling posture) — the entrypoint
accepts one `api_base` test-override parameter and forwards the same value to
both calls, since they're the same real URL in production and a single local
fake server can serve both routes in tests.

## The entrypoint function

New module `src/personal_voice_msg/daily_send_entrypoint.py`, one function:

```python
async def run_daily_entrypoint(
    database: Database,
    settings: Settings,
    session: aiohttp.ClientSession,
    recipient_key: str,
    pacific_date: date,
    embedding_path: Path,
    now: datetime,
    *,
    api_base: str = TELEGRAM_API_BASE,  # this module's own constant, same value
) -> MessageState | None
```

- Computes the `DAILY_SEND` trigger for `pacific_date` (same
  `planned_triggers_for_date`/`classify_trigger` calls `run_daily_send` already
  makes internally). If `classify_trigger(...) is not TriggerStatus.DUE`, returns
  `None` immediately — a pure no-op, no DB or network touched. Safe to invoke on
  every cron tick all day; it only does anything during the 5-minute window.
- Inside the window: calls `poll_inbound_stop(session, database, settings, now,
  api_base=api_base)`. A `TelegramPollError` here is caught and discarded — poll
  fragility must never block a legitimate send attempt (the same posture the T17
  design spec already stated for this exact failure mode). No structured logging
  exists yet (T19), so this is a bare `except TelegramPollError: pass`, commented
  to say why, not dressed up as more than it is.
- Then calls and returns `await run_daily_send(database, settings, session,
  recipient_key, pacific_date, embedding_path, now, api_base=api_base)`.

Calling `poll_inbound_stop` on every cron tick within the window (up to ~5 times
across a 5-minute window at 1-minute cadence) is intentionally harmless: the
durable offset cursor makes a repeat poll within the same window return zero new
updates. This avoids inventing new "have I already polled this window" state —
Karpathy rule: no abstraction the requirement doesn't need.

## The runnable script

New `scripts/run_daily_entrypoint.py`, following this repo's existing
`scripts/repository_policy.py` convention (`argparse`, a `if __name__ ==
"__main__":` block) rather than an env-var-driven design, since that's the one
precedent already in this codebase for a real, directly-invoked script:

```
usage: run_daily_entrypoint.py --config CONFIG --database DATABASE
```

- `--config`: path to the `load_settings`-compatible TOML file.
- `--database`: path to the SQLite state file (matches `Database`'s existing
  explicit-path-always convention — `Settings` carries no DB path).

Internally: `load_settings(config_path)`, `recipient_key_for_chat_id(settings.
telegram_chat_id.reveal())`, `settings.voice_embedding.reveal()` for
`embedding_path`, `Database(database_path)` + `.migrate()`, a real
`aiohttp.ClientSession`, `now = datetime.now(UTC)`,
`pacific_date = now.astimezone(PACIFIC).date()`, then one call to
`run_daily_entrypoint(...)`. Prints the resulting `MessageState` (or "not due,
skipped" for `None`) to stdout and exits 0. An unhandled exception is allowed to
propagate and exit non-zero — cron's own job-failure signal is sufficient for this
task's scope; anything richer is T19's alerting, not built here.

## Testing plan (no-mock, matching every prior T17 task)

- **Fast tests**: window-gating logic — outside the window returns `None` with
  `session=None`/`settings=None` (the same "`None` proves no network was touched"
  pattern already used throughout `tests/fast/test_delivery_window.py`).
- **Security/integration tests**: reuse the existing local fake-server pattern
  (`_FixedStatusServer`/`_HangingServer`, already established across
  `tests/security/test_sender_error_taxonomy.py` and
  `tests/e2e/test_delivery_fault_injection.py`) to prove: (a) a real STOP-shaped
  `getUpdates` response followed by a real send both happen in one call and in the
  right order; (b) a `TelegramPollError` from a broken/hanging fake `getUpdates`
  endpoint does not prevent `run_daily_send` from still being attempted.
- Nothing here needs real Telegram credentials — identical no-mock discipline to
  every task so far, just against local fake servers instead of the real API.

## Live verification — runs outside this sandbox

The sandboxed session building this cannot reach `api.telegram.org` over genuine
TLS (confirmed: a self-signed certificate is injected on every outbound HTTPS
attempt, to any host, not just Telegram — almost certainly the sandbox's own
network egress path doing TLS inspection). This means the actual live proof — a
real STOP detected, a real send delivered, a real blocked-by-user 403 handled —
cannot be executed from inside this session, regardless of whether credentials are
in place.

This mirrors T15's and T16b's own precedent exactly ("not available in the
environment these tasks ran in... closed by the owner directly"). The task
produces:

1. The one-time recipient enrollment command (`recipient_enrollment.
   enroll_recipient`), to be run by the owner in their own terminal, outside the
   sandbox.
2. The exact `pytest` invocations with the required env vars
   (`T13_VOICE_SAMPLE`, `T16B_TELEGRAM_SETTINGS`, `T16B_TEST_BOT_TOKEN`) for the
   env-gated integration/e2e suites T17 already wrote (including T17's own
   final-review-added blocked-by-user e2e test) plus this task's own entrypoint
   integration test, all to be run by the owner outside the sandbox.
3. A real invocation of the new `scripts/run_daily_entrypoint.py` itself, once,
   during a real 07:00-07:05 Pacific window, by the owner.

The owner reports back the actual results; the task log records them honestly —
including if something fails, the same policy this project has followed since T10.

## Independent review

Not on `AGENTS.md`'s original fixed review list (`T06, T15, T16, T17, T18`), but
following T16b's own precedent (also not on that original list, reviewed anyway
because it touched the sender/secrets boundary): this task loads real secrets,
derives real production identifiers, and drives a real send through a new
production-facing entry surface. Mandatory independent whole-branch review before
merge, same discipline as every T17 task.

## What does NOT change

- T01-T17b's own prerequisite work (`consent.py`, `delivery.py`, `sender.py`,
  `database.py`) — this task only adds a new caller, zero changes to the functions
  it calls.
- The no-mock, real-dependency testing policy throughout.
- The one-recipient, one-send-per-Pacific-day, fail-closed philosophy.
