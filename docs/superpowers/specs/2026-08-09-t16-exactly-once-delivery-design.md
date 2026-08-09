# T16 — Exactly-once delivery and ambiguity recovery: design spec

Status: approved by owner (design conversation, 2026-08-09). Not yet implemented — no branch, no red tests, no schema changes exist for T16 as of this writing.

## Scope

T16 per `IMPLEMENTATION_PLAN.md`'s "### T16 — Exactly-once delivery and ambiguity recovery" section. Dependencies: T03, T05, T15 — all complete and re-verified against current source during this design session (see "Facts verified before designing" below). Independent security review required before merge (`AGENTS.md`'s list: T06, T15, T16, T17, T18).

## Facts verified before designing (not assumed from the plan or AGENTS.md prose)

- **No orchestrator exists.** `grep` across `src/` for `reserve_next_message`, `produce_voice_note`, and `send_voice_note` found zero callers anywhere in production code. T03/T12's reservation, T14's synthesis, and T15's sender have never been wired together, and there is no CLI module. T16 is not extending an orchestrator — it is writing the first one.
- **Pocket TTS synthesis is not deterministic.** Direct inspection of the installed `pocket_tts` package (`.venv/Lib/site-packages/pocket_tts/models/{flow_lm,tts_model}.py`) confirms noise-sampling parameters (`noise_clamp`, "Maximum value for noise sampling") in the generation path, and no `torch.manual_seed` call anywhere in the pipeline (including `audio_pipeline.py`). Re-synthesizing the same text on restart would not reproduce identical audio bytes. This resolves the plan brief's audio-persistence question in one direction only: the actual produced audio must be durably persisted, not regenerated.
- **`produce_voice_note`** (`src/personal_voice_msg/audio_pipeline.py`) takes a caller-supplied `destination: Path`, synthesizes/converts/validates real files via Pocket TTS and FFmpeg subprocesses, and its own last action is `database.transition_delivery(delivery_id, MessageState.AUDIO_READY, now)` — the produced file's path is never persisted anywhere.
- **`deliveries.provider_message_id`** exists in `SCHEMA_V1` (`database.py`) but `transition_delivery()` never sets it and nothing reads it.
- **No attempt/retry-shaped table or column exists anywhere** in `database.py`.
- **`DELIVERY_TRANSITIONS`** (`database.py`) maps `SENT`, `FAILED`, and `DELIVERY_UNKNOWN` all to `set()` — no outgoing edges exist from any of them today.
- **`transition_delivery`** already enforces `messages.state == deliveries.state` lockstep (raises `DatabaseInvariantError` otherwise) and already updates both tables' `state` columns together inside one transaction — any new delivery-state-changing method must preserve this.
- **`scheduling.classify_trigger` + `planned_triggers_for_date`** (`scheduling.py`) already answer "is `now` inside today's DAILY_SEND window (07:00–07:05 Pacific, start inclusive, cutoff exclusive)" with no gaps found. No new scheduling function is needed.
- **`sender_auth_nonces.expires_at`** (T15's `SCHEMA_V6`) is written by `record_sender_nonce` and read nowhere — confirmed unpruned.
- **T15's `sender.py`** raises one flat `SenderError` for every failure mode: signature/freshness/replay/audio-invalidity (all pre-flight, before any WAHA call), a WAHA `status >= 400` response, and `aiohttp.ClientError`/`TimeoutError`/`json.JSONDecodeError` around the actual POST — collapsing "definitely never reached WAHA," "WAHA explicitly rejected it," and "no idea if it reached WAHA" into one exception type.
- **WAHA has no client-supplied idempotent message ID.** Confirmed against WAHA's own send-messages documentation (https://waha.devlike.pro/docs/how-to/send-messages/) — a retried `sendVoice` call cannot be deduplicated WhatsApp-side by construction. Reconciliation of an ambiguous submission must query WAHA's own record of what happened (e.g. its chat-messages listing), not rely on a dedup key.

## Owner decisions (asked directly — not inferable from the repo)

1. **Durable audio storage: SQLite BLOB, not a new file directory.** `deliveries.audio_data BLOB`, written atomically with the `AUDIO_READY` transition. Rejected alternative: a new persistent `audio_dir` config field + `deliveries.audio_path` column (mirrors T02's existing file-secret pattern, but adds a new directory needing its own permissions/cleanup story that isn't automatically covered by T19's encrypted-backup story the way DB data already is).
2. **Sender error taxonomy: split `SenderError` into typed subclasses inside T15's `sender.py`.** Rejected alternative: leave `sender.py` untouched and have T16 make its own duplicate WAHA HTTP call to observe the raw network exception — rejected because it would create a second code path calling WAHA and duplicate the auth/validation logic T15 already locked down, weakening the "one narrow sender" boundary.
3. **`sender_auth_nonces` pruning is T19's scope, not T16's.** Recorded explicitly here so it is not silently unowned again (this is the second time the gap has been flagged without an owner assigned).

## Design

### 1. Audio persistence

New additive `SCHEMA_V7` statement: `ALTER TABLE deliveries ADD COLUMN audio_data BLOB` (nullable). This is the first migration in this codebase that alters an already-pinned table's SQL rather than adding a brand-new table/index/trigger — `EXPECTED_SCHEMA_V7_OBJECTS`'s `("table", "deliveries")` entry must be overridden with the post-`ALTER` `CREATE TABLE` text SQLite actually records in `sqlite_master`, captured empirically (via a real migration run) rather than hand-typed, matching how every other `EXPECTED_SCHEMA_V*_OBJECTS` entry in this file is already derived from real executed statements.

`produce_voice_note` (`audio_pipeline.py`) keeps its existing signature and still synthesizes/converts/validates to a real temp file — FFmpeg and Pocket TTS both require actual files, this does not change. Its final internal call changes from today's

```python
database.transition_delivery(delivery_id, MessageState.AUDIO_READY, now)
```

to a new

```python
database.mark_audio_ready(delivery_id, destination.read_bytes(), now)
```

which writes the validated OGG/Opus bytes into `deliveries.audio_data` in the same transaction as the `RESERVED → AUDIO_READY` state flip (both `deliveries.state` and `messages.state`, preserving lockstep). The on-disk temp file is deleted immediately after (mirrors the existing `temp_wav.unlink` cleanup already in this function) — nothing on disk needs to survive past this call.

Consequence: **synthesis runs exactly once per delivery.** Every send attempt — the first one and every retry — reads `deliveries.audio_data` and passes those same bytes to `send_voice_note`. "Retries reuse the same sentence and audio" (a plan red test) holds by construction.

The blob is cleared (`audio_data = NULL`) once a delivery reaches `SENT`, or after the plan's bounded failure retention window — mirrors the existing privacy rule ("delete generated audio after confirmed delivery or bounded failure retention") without needing a filesystem cleanup step, since there is no persistent on-disk file in this design.

### 2. Attempt records

New table, same `SCHEMA_V7` migration:

```sql
CREATE TABLE delivery_attempts (
    id INTEGER PRIMARY KEY,
    delivery_id INTEGER NOT NULL REFERENCES deliveries(id) ON DELETE RESTRICT,
    attempted_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('sent','failed','delivery_unknown')),
    provider_message_id TEXT
)
```

One row per concluded attempt — an audit trail of how many times a delivery was tried, what happened, and when. New method:

```python
Database.record_delivery_attempt(
    delivery_id: int,
    outcome: Literal["sent", "failed", "delivery_unknown"],
    now: datetime,
    provider_message_id: str | None = None,
) -> None
```

In one transaction: inserts the `delivery_attempts` row, performs the matching `deliveries`/`messages` state transition (validated against the extended `DELIVERY_TRANSITIONS` below), and — only when `outcome == "sent"` — writes `deliveries.provider_message_id`. This is the literal implementation of the plan's "persist WAHA message identifiers and attempt records transactionally": the id and the attempt landing together, atomically, is the guarantee, not two separate writes.

### 3. State machine: two new edges, zero new `MessageState` values

```python
DELIVERY_TRANSITIONS = {
    ...  # RESERVED, AUDIO_READY, SENDING unchanged
    MessageState.SENT: set(),                                    # unchanged — terminal
    MessageState.FAILED: {MessageState.AUDIO_READY},              # new
    MessageState.DELIVERY_UNKNOWN: {
        MessageState.AUDIO_READY,
        MessageState.SENT,
    },                                                             # new
}
```

No new `MessageState` member is added. This deliberately avoids the landmine `docs/task-logs/T11b-pre-T12-hardening.md` flagged: `EXPECTED_SCHEMA_V1_OBJECTS` derives its `messages` table `CHECK` text from the *live* `MessageState` enum (`ALL_STATES_SQL`), so any future task that grows `MessageState` must pin that historical literal first or it will corrupt `_validate_schema` for every already-migrated database. T16 does not trigger that landmine; it stays live for whichever future task actually adds a new state.

Mapping to the plan's red tests:

- **"Definite pre-submission failure may retry"** → `FAILED → AUDIO_READY` (existing `AUDIO_READY → SENDING` edge carries the retry the rest of the way, reusing the stored blob — no re-synthesis).
- **"Confirmed delivery cannot retry"** → `SENT` has no outgoing edges (unchanged from T03).
- **"Timeout after possible submission becomes `delivery_unknown`"** → existing `SENDING → DELIVERY_UNKNOWN` edge (unchanged, already present since T03).
- **"Restart at every delivery state cannot duplicate a voice note"**, specifically restart while `SENDING`: on startup, the orchestrator treats any delivery already sitting in `SENDING` — one it did not just put there itself in this run — as ambiguous by definition, since a crashed prior process's WAHA call might have landed. It calls `record_delivery_attempt(delivery_id, "delivery_unknown", now)`, reusing the existing `SENDING → DELIVERY_UNKNOWN` edge. No new state is needed for "orphaned in-flight" — it collapses onto the same reconciliation path as a genuine network-level ambiguity.
- **"Unknown delivery is reconciled before any retry"** → reconciliation (§5) resolves `DELIVERY_UNKNOWN` to either `AUDIO_READY` (confirmed not delivered — safe to retry) or `SENT` (it did land after all — `provider_message_id` recovered from the reconciliation query and written via `record_delivery_attempt`). If reconciliation is inconclusive, no transition happens; the delivery stays `DELIVERY_UNKNOWN` and the next loop iteration retries reconciliation, bounded by the send window.

### 4. Sender error taxonomy (`sender.py`, T15's file)

Two new subclasses of the existing `SenderError`:

```python
class SenderRejected(SenderError):
    """The request definitely never reached WAHA, or WAHA definitely
    rejected it. Safe to retry."""

class SenderAmbiguous(SenderError):
    """WAHA may or may not have processed the request. Must be
    reconciled before any retry."""
```

Raised at existing failure points in `send_voice_note`, with no change to its call signature:

- `SenderRejected`: invalid signature, stale timestamp, replay detected, invalid audio (all today's pre-flight checks — already structurally guaranteed to run before any WAHA call), and a WAHA response with `status >= 400` (WAHA received the request and gave a definite answer).
- `SenderAmbiguous`: `aiohttp.ClientError` / `TimeoutError` around the POST, or a malformed/unparseable response body following a non-error status (WAHA may have processed the send; the response just didn't confirm it).

This re-touches previously-reviewed code, which is exactly why it rides on T16's own mandatory independent security review rather than requiring a separate one.

### 5. Reconciliation mechanism

WAHA exposes no client-supplied idempotent message ID (verified above), so reconciliation cannot be "look up by an ID we chose." Mechanism: query WAHA's chat-messages listing for the recipient and look for an outgoing (`fromMe`) voice message whose timestamp falls inside the ambiguous attempt's window (`delivery_attempts.attempted_at` for that attempt through the reconciliation check's own `now`). If found: reconcile to `SENT` with the recovered `provider_message_id`. If the window has clearly passed with no matching message and no further doubt: reconcile to `AUDIO_READY` (safe retry). Otherwise: no transition, stays `DELIVERY_UNKNOWN`.

Left open, deliberately, matching how T12 left its buffer's storage representation open for its own red-test-writing step: the exact WAHA endpoint and whether NOWEB's message-history "Store" needs an explicit deployment flag is not settled by WAHA's public docs and will be verified against the real paired WAHA instance during implementation — the same practice T15's "WAHA facts verified before designing" section followed.

### 6. Orchestration loop

New module (e.g. `src/personal_voice_msg/delivery.py`). Every step takes an explicit `now: datetime` parameter — no wall-clock reads inside orchestration logic, matching this repo's existing explicit-clock testing convention (T05).

1. Confirm `classify_trigger(daily_send_trigger, now) is TriggerStatus.DUE` via T05's existing `scheduling.classify_trigger` / `planned_triggers_for_date`, unchanged. Never starts a new attempt at or after 07:05; never carries a missed send into the next Pacific date (T05's already-established no-catch-up rule extends unchanged to T16).
2. Resolve the day's delivery (`reserve_next_message` if not already reserved for `recipient_key + pacific_date`).
3. Branch on the delivery's current state:
   - `RESERVED` → synthesize once via `produce_voice_note` → `mark_audio_ready` (§1).
   - `AUDIO_READY` → sign a fresh request (`idempotency_key = f"delivery-{delivery_id}"`, constant per delivery; `timestamp = now`, fresh per attempt — the same `idempotency_key` with a different `timestamp` does not collide with T15's `(idempotency_key, timestamp)` replay-protection uniqueness, and gives useful audit correlation across retries of the same delivery) → transition to `SENDING` (durable, before the network call) → call `send_voice_note` with the stored `audio_data` blob → route the outcome through `record_delivery_attempt`.
   - `SENDING` found at orchestrator startup (not set by this run) → reclassify to `DELIVERY_UNKNOWN` immediately (§3).
   - `FAILED` / `DELIVERY_UNKNOWN` → retry or reconcile per §3/§5.
   - `SENT` → done.
4. Loop until `SENT` or the window closes.

### 7. Nonce pruning

Explicitly out of scope for T16. Owned by T19 ("Audit, alerts, backups, and recovery") — recorded here so the gap `sender_auth_nonces.expires_at` (written, never read) is not silently rediscovered a third time.

## Open items to resolve during implementation (not blocking this spec)

- Exact WAHA reconciliation endpoint/config (§5) — verify against the real paired WAHA instance, as T15 did for its own WAHA facts.
- `EXPECTED_SCHEMA_V7_OBJECTS`'s post-`ALTER` `deliveries` text (§1) — capture from a real migration run, not hand-typed.
- Bounded failure-retention window for clearing `deliveries.audio_data` after a terminal non-`SENT` state — a concrete duration, to be set alongside T16's own red tests.
- Orchestrator loop's poll cadence within the DUE window — bounded by the window's own cutoff regardless of the exact interval chosen.

## Non-goals

- No new `MessageState` member (§3).
- No new scheduling function — T05's `classify_trigger`/`planned_triggers_for_date` are already sufficient (verified above).
- No `audio_artifacts` table wiring — superseded by the `deliveries.audio_data` BLOB decision; `audio_artifacts` remains deliberately unwired exactly as T14 and T15 left it.
- No client-side WAHA message-ID-based deduplication — confirmed unavailable in WAHA's API.
- `sender_auth_nonces` pruning — T19's scope, not this task's.

## Next step

Invoke `superpowers:writing-plans` to produce the red-test-first implementation plan from this spec.
