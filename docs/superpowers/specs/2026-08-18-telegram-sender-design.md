# Telegram sender migration — design spec

**Date:** 2026-08-18
**Status:** Approved by owner, not yet planned or implemented. This spec is the handoff point —
a fresh session should be able to execute from this document alone, without the brainstorming
conversation that produced it. **2026-08-19 update:** a short follow-up brainstorming pass (new
session, after confirming the git tree was fully synced and this spec was actually merged) closed
the three items this spec originally left open for the plan phase — recipient-enrollment shape,
`file_id` reuse, and backlog placement. All three are now resolved inline below, not deferred.
Nothing in this spec is open anymore; `writing-plans` can proceed directly.
**Author context:** produced via `superpowers:brainstorming`, gated on owner approval per that
skill's hard gate. Research phase used `superpowers:dispatching-parallel-agents` (10 parallel
research agents across two rounds — see the two research notes linked below).

## Read this first if you're picking this up fresh

1. `AGENTS.md` §"Immediate next step" — should point here; if it doesn't, something drifted, trust
   this file and `IMPLEMENTATION_PLAN.md` over stale prose.
2. `docs/research/waha-alternatives.md` — why WAHA/self-hosted WhatsApp-Web automation is dead
   (WhatsApp's own server-side device-linking throttle, 5 real attempts, 4 architecturally distinct
   libraries, all refused identically).
3. `docs/research/next-platform-alternatives.md` — why Telegram beat Signal, Discord, SMS/MMS, and
   a different WhatsApp number, evaluated against the owner's stated priority (lowest cost/effort,
   free strongly preferred).
4. `docs/research/whatsapp-bsp-alternatives.md` — why staying on WhatsApp via an official Business
   Solution Provider (Twilio, 360dialog, or otherwise) does **not** solve this project's requirement:
   every BSP inherits Meta's identical template-only/no-audio-header restriction on proactive
   messages; this is a Meta-platform-level wall, not a provider-selection problem.
5. This document — the actual design to build.

**Decision, final: Telegram Bot API, chosen over Discord** specifically because Discord requires
the recipient to join a private server before any DM is possible (heavier, and recurring policy risk
under Discord's own unsolicited-DM rules), and Discord's voice-message-as-bot capability is confirmed
working today but is unofficial/reverse-engineered per Discord's own docs-repo discussion — a real
regression risk for something meant to run indefinitely. Telegram's `sendVoice` is official,
documented, and the recipient's only action is a one-time `/start`, ever.

## What this replaces

`AGENTS.md` §Confirmed stack currently says "WAHA Core behind a narrow internal sender." That line
becomes wrong once this ships — replace it with "Telegram Bot API behind the same-shaped internal
sender boundary." T15 (locked WAHA sender boundary, merged) and the WAHA-specific portions of T16
(exactly-once delivery, merged, 11/13 tasks complete per `AGENTS.md`) get superseded by this design,
not the general delivery-state-machine work T16 also did — that part survives unchanged (see
"What's kept" below).

## Architecture overview

The pipeline shape is unchanged: discovery → generation → safety gates → approved queue → T14's
audio pipeline (OGG/Opus, unchanged) → sender. Only the sender's transport changes.

- `src/personal_voice_msg/sender.py` gets a new Telegram-backed `send_voice_note`, replacing the
  WAHA HTTP calls.
- `delivery.py`'s orchestrator (crash/restart/retry state machine across delivery states) is kept —
  it doesn't care which transport `sender.py` uses, only whether an outcome was `SENT`, a definite
  rejection (retry-safe), or ambiguous. Its handling of the ambiguous case changes — see "Ambiguous
  outcomes" below.
- Docker Compose loses the WAHA service entirely: no browser-automation container, no session
  volume, no QR pairing. The sender becomes a direct outbound HTTPS client to `api.telegram.org` —
  still loopback-only/no-public-ports (`AGENTS.md` §Network and container rules); Telegram is
  reached outbound-only, nothing listens for anything (see "Inbound handling" below for how STOP
  still gets detected without a public port).
- `Settings` (`config.py`) drops `waha_base_url`, `waha_token`, and the WAHA-specific
  `WAHA_SESSION_NAME` constant; gains a `telegram_bot_token` secret and a stored, allowlisted
  `telegram_chat_id` (replaces the phone number's role as the sender's routing target — see
  "Recipient enrollment").

## What's deleted, kept, and rewritten in `sender.py`

Current file: `src/personal_voice_msg/sender.py`, 373 lines.

**Delete outright** (no Telegram equivalent exists to port to — Telegram's Bot API has no
chat-history-read method for bots, so there is nothing to poll):
- `reconcile_delivery`, `_fetch_matching_provider_id`, `_find_matching_provider_id`,
  `_no_match_outcome` (`sender.py:185-373`)
- `RECONCILE_MESSAGE_LIMIT`, `RECONCILE_MAX_RESPONSE_BYTES`, `RECONCILE_GRACE_SECONDS`,
  `RECONCILE_POLL_ATTEMPTS`, `RECONCILE_POLL_DELAY_SECONDS` (`sender.py:27-56`)

**Keep essentially unchanged** (platform-agnostic — protects the sender's own local trigger
endpoint against replay, independent of which downstream platform receives the voice note):
- `sign_request`, `verify_signature`, `is_fresh` (`sender.py:74-96`)
- `database.record_sender_nonce` / `ReplayDetected` (T15's replay-nonce table, `SCHEMA_V6`)
- The call-out to T14's `audio_pipeline.validate_audio` — Telegram's format requirement (OGG
  encoded with Opus, or MP3, or M4A; 50MB cap) already matches T14's existing output with zero
  pipeline changes needed.

**Rewrite**:
- The actual HTTP call: WAHA's `POST /api/sendVoice` with `{chatId, session, file: {mimetype,
  filename, data: base64}}` and an `X-Api-Key` header → Telegram's `POST
  https://api.telegram.org/bot<token>/sendVoice` with `chat_id` and a multipart `voice` upload (or a
  pre-uploaded `file_id` on retry — see "Ambiguous outcomes").
- Response parsing: WAHA's `payload["key"]["id"]` → Telegram's `payload["result"]["message_id"]`,
  gated on `payload["ok"] is True`.
- The `SenderRejected`/`SenderAmbiguous` exception split stays as a taxonomy but the proportion
  shifts hard toward `Rejected`: Telegram's 400/401/403/404/429 responses are all synchronous and
  definite (bad chat, blocked-by-user, chat-not-found, revoked token, rate-limited-with-
  `retry_after` respectively) — map each to `SenderRejected` with the specific Telegram
  `error_code`/`description` preserved for diagnostics. Only a genuine network failure with **no**
  HTTP response received at all (`aiohttp.ClientError`/`TimeoutError` before any status line) stays
  `SenderAmbiguous`.

## Recipient enrollment (one-time, manual — same trust shape as T13's voice enrollment)

1. Owner creates the bot via `@BotFather` (out-of-band, human step; the resulting bot token is a
   secret, handled exactly like `sender_auth_key`/`waha_token` were — never in git, logs, or task
   prompts, per `AGENTS.md` §Voice and privacy rules' secret-handling posture, which applies
   platform-agnostically).
2. Owner privately shares the bot's `t.me/<name>` link with the recipient. The link itself is the
   invite — same trust model as a private WhatsApp number; the bot should not be publicly searchable.
3. Recipient sends the bot any message (conventionally `/start`).
4. A one-time enrollment step (new script, shape modeled on T13's `enroll_voice` one-time-trust
   pattern) polls `getUpdates` once, captures the `chat_id` of whoever sent that first inbound
   message, and stores it as the fixed, allowlisted recipient — directly satisfying `AGENTS.md`'s
   existing "one recipient is fixed server-side and allowlisted" rule, just with `chat_id` instead
   of a phone number as the stored identity.
5. This step must run once, deliberately, by the owner, and must not keep accepting new senders
   afterward — the captured `chat_id` becomes immutable once enrolled, same as the current phone
   number field's trust model.

**Resolved (2026-08-19, follow-up brainstorming pass):** a plain function, no CLI framework —
matches T13's actual precedent exactly (`voice_enrollment.py`'s `enroll_voice` has no CLI wrapper
of its own today; it's called directly). New module `src/personal_voice_msg/recipient_enrollment.py`
with one function, `enroll_recipient(bot_token, database) -> int`, invoked the same way
`enroll_voice` already is — a short one-off invocation the owner runs once, not a permanent
`argparse`-based CLI tool. Raises if a recipient is already enrolled (immutable once set, same
trust model as voice enrollment).

## Inbound handling: STOP, without a public port

Telegram doesn't require a webhook — long-polling `getUpdates` is an **outbound** HTTPS call, so it
satisfies `AGENTS.md` §Network and container rules unchanged (no inbound port opens anywhere, ever).

Design: a low-frequency poll (once during the daily send window is sufficient at this volume — no
need for continuous polling) with a durably-stored `offset` cursor (new DB column/table, avoids
reprocessing the same update twice), inspecting only messages from the enrolled `telegram_chat_id`.
Anything from any other chat is ignored outright — matches the existing "other inbound messages are
ignored and never invoke the discovery agent" rule.

Exact-`STOP` matching reuses the existing deterministic exact-match logic unchanged (same semantics:
"exact STOP from the allowlisted recipient disables sending durably").

Secondary signal: a `403 Forbidden: bot was blocked by the user` at send time gets logged and should
also durably disable sending (same durability requirement as STOP) — this is the closest Telegram
gets to a proactive block signal, but it's necessarily reactive (learned only by attempting a send),
not queryable in advance.

## Ambiguous outcomes: the one genuinely new design decision

With no reconciliation target available (nothing to poll), the rule has to be: **on a true ambiguous
outcome — a network failure with no HTTP response received at all — do not retry that day.**

Rationale: retrying blind risks a duplicate voice note, which violates the existing "maximum one
voice note per recipient and Pacific calendar date" rule (`AGENTS.md` §WhatsApp and delivery rules —
applies platform-agnostically despite the section heading). Not retrying and marking the delivery
`DELIVERY_UNKNOWN` for that date, left for the owner to check, is consistent with the existing
"retry only when non-delivery is certain" and "never carry a missed preparation or send into the
next Pacific calendar day" rules.

This is a **simplification** of T16, not new complexity: `MessageState.DELIVERY_UNKNOWN` already
exists in `database.py`'s state machine (`DELIVERY_TRANSITIONS[MessageState.DELIVERY_UNKNOWN]` only
permits `AUDIO_READY`/`SENT`, per `sender.py:330-334`'s existing docstring). Under Telegram, it just
stops being an intermediate state `reconcile_delivery` auto-resolves via polling, and becomes
terminal-for-the-day instead — surfaced for the owner, tying into T19's not-yet-built alerting scope
the same way it already would have. Given Telegram's failure surface is overwhelmingly
synchronous/definite (per `docs/research/next-platform-alternatives.md`'s Telegram research), this
case should be rare in production.

**Resolved (2026-08-19, follow-up brainstorming pass): defer `file_id` reuse entirely.** Traced
through the actual retry path and found the literal premise doesn't hold: Telegram only returns a
`file_id` on a *successful* send, and every `SenderRejected` outcome (bad signature, stale
timestamp, validation failure, or a definite 4xx from Telegram) means the upload never completed —
there is no `file_id` to have cached from a rejected attempt. A successful send is terminal (no
retry follows it). A real `file_id`-caching optimization would need a different shape entirely
(upload once to a private cache chat, reuse that `file_id` for the real send) — a second upload
path with its own failure modes, not a small tweak. Given voice notes here are small (seconds of
speech, well under the 50MB cap) and this is once-a-day cadence, not high-volume, the simplest
correct version — upload the audio bytes directly on every real send attempt, no caching layer —
is the implementation. Revisit only if real production retry volume ever justifies the added
complexity.

## Testing plan (no-mock TDD policy unchanged, `AGENTS.md` §Strict no-mock TDD policy)

- A second, non-production bot/token plus the owner's own personal Telegram chat becomes the
  staging target — same shape as the existing "staging can send only to the owner's test chat" rule,
  just against a second bot instead of a second WAHA session.
- **Fast tests**: pure logic — signature verification (unchanged), response-parsing logic for each
  Telegram status code, the `SenderRejected`/`SenderAmbiguous` mapping table.
- **Integration tests**: real `sendVoice` calls against the test bot/chat, asserting on real HTTP
  outcomes (a real 400 from a bad `chat_id`, a real successful send returning a real `message_id`).
- **Security tests**: the existing AST-based sender-boundary test
  (`tests/security/test_voice_enrollment_boundaries.py`) gets extended to cover the new Telegram
  sender module in place of the WAHA-specific checks it currently has — `discovery/`, `generation/`,
  `judging/` still must never reach the sender, the bot token, or the enrolled `chat_id`.
- The T20-equivalent staging soak (real days of delivery to the owner's own test chat) still applies
  before flipping the enrolled `chat_id` to the real recipient.

## Backlog placement (resolved 2026-08-19, follow-up brainstorming pass)

This migration replaces the WAHA-specific parts of T15/T16 without touching T16's general
delivery-state-machine work. `IMPLEMENTATION_PLAN.md` needs an amendment.

**Decision: insert a new task, T16b, between T16 and T17**, scoped to just the transport
migration — delete the reconciliation subsystem, add Telegram `sendVoice`, recipient enrollment,
`Settings` changes. T17 ("Recipient consent, STOP, and kill switch") keeps its number and its own
independent security review, but its red tests and implementation section get rewritten for
Telegram's actual mechanics (the "Inbound handling" section above — `getUpdates` polling, not a
WAHA-received-message assumption) rather than superseded or merged into T16b. Rationale: T16b's
review stays focused on "does the sender correctly talk to Telegram," T17's stays focused on "does
STOP/kill-switch actually work and survive restart" — neither task balloons, and T18-T20's numbers
never move, so no cascading edits through the rest of the plan or any doc that references them by
number. This split, and T17's content rewrite, are `writing-plans`' job to actually carry out
against `IMPLEMENTATION_PLAN.md` — this spec only fixes the decision.

## What does NOT change

- T01-T14 (discovery through audio pipeline) — zero changes, this design only touches the sender.
- The safety/rights/content rules in `AGENTS.md` §Content and rights rules.
- The one-recipient, one-send-per-Pacific-day, fail-closed philosophy throughout.
- The "no mocks, real dependencies" testing policy.
- The admin kill-switch requirement (durable, always wins) — implementation detail changes (no
  longer gated through WAHA-specific plumbing) but the requirement itself is unchanged.

## Explicitly out of scope for this spec

- Discord, Signal, SMS/MMS, WhatsApp-via-BSP, and a different WhatsApp number were all evaluated and
  rejected — see the three linked research notes. Do not re-litigate these without new evidence.
- The daily WhatsApp button-tap mechanism (`docs/research/whatsapp-bsp-alternatives.md`) was
  evaluated as real but a worse trade (daily recipient action, template-approval risk, doubles T16's
  exactly-once complexity) — not pursued.
