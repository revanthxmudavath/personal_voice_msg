# AGENTS.md

This file is the operational entry point for every development agent working on the Personal Voice Message Assistant.

## Read first

Before changing code, read the complete implementation plan:

- `IMPLEMENTATION_PLAN.md`
- Absolute path: `F:\personal_voice_msg\IMPLEMENTATION_PLAN.md`

The plan defines tasks T00 through T20, their dependencies, red tests, implementation boundaries, security reviews, and completion gates. Do not silently change those decisions. If implementation evidence contradicts the plan, record the evidence and ask the lead to update the plan.

## Project objective

Build a fully cloud-hosted service that:

1. Discovers permitted romantic inspiration from the public web.
2. Generates one original, sweet English sentence without copying lyrics or quotes.
3. Rejects duplicates, unsafe content, manipulation, fabricated memories, and low-quality messages.
4. Synthesizes the accepted sentence with the owner's authorized voice clone.
5. Converts the audio to WhatsApp-compatible OGG/Opus.
6. Sends exactly one voice note to one allowlisted recipient at 07:00 `America/Los_Angeles` every day.
7. Runs without any service remaining on the owner's computer after enrollment.

## Current status and blockers

T01 through T12 are implemented and audited. T09 selected the deterministic
T07 discovery fallback after the restricted LangChain/Gemini candidate failed
the protocol and hostile-input security gates. T10 qualified the
owner-confirmed Gemini API Tier 1 Postpay project (`gemini-3.6-flash`,
`temperature=0.2`, hand-rolled `aiohttp` client) with a real, unmodified
100-trial Promptfoo run on 2026-08-05: 100% structural/prohibited-field
compliance and 99% valid-original yield, measured by the harness's own
fresh-per-trial-database metric, clearing both of the plan's done-when
thresholds (`docs/task-logs/T10.md`). A separate post-hoc measurement
replaying the same 99 recorded outputs against an accumulating shared
history (this repo's own `fuzz.token_sort_ratio`/`NEAR_DUPLICATE_THRESHOLD`
dedup metric, rather than the harness's per-trial-isolated databases) found
91/100 unique outputs; this does not fail T10's gate (the harness's
isolation was the documented design, and a real near-duplicate is handled
by production's existing dedup-and-regenerate path, not a task failure) but
is recorded as a design input for T11's own qualification harness.

T11 (deterministic safety gates and structured judge) is complete. Two
layers gate a T10-generated sentence: eleven local deterministic
prohibition categories (`judging/gates.py`, zero API cost, checked first)
followed by a structured Gemini judge (`judging/judge.py`, same
`gemini-3.6-flash` pin, `temperature=0.0`) that scores tone/warmth/
naturalness and lists risk flags but never itself sets approval state --
`judging/pipeline.py`'s `evaluate_message_safety` is the one deterministic
function that reads the judge's returned fields with plain comparisons.
The judge was calibrated with a real, paid 42-row Promptfoo run (11
red-corpus fixtures, 15 normal + 8 boundary rows, 8 adversarial rows) over
5 total real runs across 2 fix rounds -- the final run cleared all three
conditions at once: 11/11 red-corpus rejected, 8/8 adversarial rejected,
19/19 normal+boundary-approved accepted (100%), with final calibrated
floors `SAFE_TONE_FLOOR = SAFE_WARMTH_FLOOR = SAFE_NATURALNESS_FLOOR =
6.5`. A separate end-to-end test chained real generation and safety
evaluation over 16 trials against one shared, accumulating database, per
the design note above: 1 trial was dedup-rejected, 15 reached the safety
gate, 9 were approved. Full record, including the calibration iteration
history and a known LLM-judge non-determinism caveat, in
`docs/task-logs/T11.md`. No new dependency or provider was introduced.

A pre-T12 review pass (T11b, `docs/task-logs/T11b-pre-T12-hardening.md`)
re-verified T03/T11 as genuinely ready, applied two behavior-preserving
efficiency fixes to `judging/gates.py` and `judging/judge.py`, and resolved
two design questions T12 needed: a new additive `message_rejections` table
(`SCHEMA_V5`) records safety rejections instead of a new `MessageState`
value, and the plan's "safe reserve" was renamed "reserve buffer" to avoid
colliding with the existing `MessageState.RESERVED` meaning.

T12 (approved queue and safe reserve) is complete. Before T12 there was no
production code driving `DISCOVERED -> VALIDATED -> APPROVED -> QUEUED` or
calling T11's `evaluate_message_safety` at all -- T12 is that orchestration
layer. `database.py` gained `SCHEMA_V5` (`message_rejections`, additive)
and three new atomic methods -- `reject_message` (walks
`DISCOVERED -> VALIDATED` if needed, then records the rejection in one
transaction), `approve_message` (walks to `QUEUED` via the existing
`CONTENT_TRANSITIONS` table), and `next_unjudged_message`/
`count_queued_messages` for candidate selection and queue health. The new
`queue_refill.py` module's `refill_queue()` loops these together against
T11's safety pipeline until `MIN_QUEUE_SIZE = 30` `QUEUED` messages exist or
candidates run out, returning a computed `QueueHealth` signal for a future
T19 alert (T12 does not itself send owner notifications -- that is T19's
scope). The reserve buffer is a computed threshold over `QUEUED` row count,
not a new column or separate pool; T03's existing `reserve_next_message`
already only draws from `QUEUED`, so "exhaustion selects only the
pre-approved buffer" and "no buffer means no send" are enforced by that
existing method once refill is wired up. All of T12's fast tests use the
same real-gate-short-circuit technique T11 established (no live judge call,
no mock) -- full mapping of plan red tests to tests, and fresh
`pytest -m fast`/`-m security`/`mypy`/`ruff`/`repository_policy.py`
verification evidence, in `docs/task-logs/T12.md`.

T13 (secure voice enrollment) is complete. `voice_enrollment.py` adds
`validate_sample` (rejects unsupported format/too-short/silent/clipped
input) and `enroll_voice` (validate -> real Pocket TTS
`get_state_for_audio_prompt` -> `export_model_state` -> read the export back
to verify it -> delete the raw sample only after that verification). Pocket
TTS (`pocket-tts`, `soundfile`) is now actually installed and pinned in
`pyproject.toml`/`uv.lock` -- confirmed working end-to-end against a real
consented test recording, including the gated `kyutai/pocket-tts`
HuggingFace access the owner granted via their own `hf auth login`.
"Discovery and sender identities cannot read the embedding" is enforced by a
real AST-based test over `discovery/`, `generation/`, and `judging/`
(`tests/security/test_voice_enrollment_boundaries.py`) -- scoped to
currently-existing code only, since the sender doesn't exist until
T15/T16, which must extend this same check when built. FFmpeg-based input
transcoding was deliberately kept out of T13's scope (unsupported formats
like `.m4a` are rejected, not converted) since AGENTS.md ties FFmpeg's
introduction to T14. Full record, including the three pre-implementation
blockers resolved (missing test sample, missing Pocket TTS install, gated
HF access) and fresh verification evidence, in `docs/task-logs/T13.md`.

Confirmed state as of 2026-07-30:

- Canonical implementation checkout: `F:\personal_voice_msg`.
- T01 through T09 are implemented with task logs; T09 completion commit
  `45f492c` is on `origin/main`.
- The corrected, equal-budget T09 benchmark used the frozen
  `t09-semantic-v2` corpus and independently observed corpus SHA-256
  `0b07ed96da644ff8b500dd2a45dc60565f586748a653df0c9727337949e0a636`.
- The deterministic arm completed 5/20 protocol-perfect runs and produced 10
  unique valid cards. The agent arm completed 13/20 protocol-perfect runs and
  produced 26 unique valid cards, but failed the required 19/20 protocol floor
  and the hostile-input security gate. Resource limits passed.
- T09 therefore retained no runtime discovery agent. Its LangChain/Gemini
  runtime code and dependencies were removed; T07 is the deployable discovery
  path. The external owner-managed Gemini key was not modified.
- Generic Gemini-key repository scanning and log redaction remain because they
  strengthen provider-independent secret handling.
- A future API-based discovery agent requires a plan amendment and a fresh,
  frozen, non-ceiling benchmark. It may not be prompt-tuned against the T09
  evaluation corpus.
- The owner confirmed the Gemini API project as Tier 1 Postpay on 2026-07-30.
  This satisfies the billing-plan prerequisite but does not approve private or
  production inference or change the T09 no-retention decision.
- The workspace can edit the repository and `git status` works.
- The repository has a credential-free HTTPS `origin` remote.
- GitHub CLI authentication for `revanthxmudavath` succeeds outside the
  workspace sandbox; a sandboxed `gh auth status` may falsely report the
  keyring token as invalid.
- Docker Desktop is running and `docker info` succeeds.
- Python 3.12.4, `uv`, Git, and Node 22.13 are installed.
- FFmpeg and ffprobe are installed and working (verified during T14;
  corrects the earlier statement here that they were missing --
  `docs/task-logs/T14.md`).

### 2026-08-04 pre-T10 audit addendum (T08b)

A repository-understanding pass plus a parallel-agent inefficiency sweep
across T01-T08 proposed six hardening fixes. Direct verification against
current source found four were false positives (a reservation-transaction
"desync" that the existing `BEGIN IMMEDIATE`/rollback-on-exception design
already prevents; a bounded-string-validator "duplication" that is really
three functions with distinct, intentional semantics; a `web.py`
scheme-port/fragment "gap" already closed by existing validation; and an
FTS5 fuzzy-match "optimization" that would have reopened a
typo-obfuscation duplicate-detection bypass). Two were real and fixed:
`config.py` was missing a `RecursionError` guard on recipient JSON parsing
and had no size limit on the WAHA token file read. Full findings and
reasoning are in `docs/task-logs/T08b-pre-T09-hardening.md`. T09 remains
complete and not retained (see the entry above); this addendum does not
change that decision.

Do not bypass the workspace sandbox or write repository files through shell
redirection. Revalidate external toolchain gates before a dependent task uses
them.

T00 is complete only when:

- `apply_patch` can edit the canonical repository.
- `git status` works normally in the active workspace.
- `gh auth status` succeeds.
- a GitHub repository exists with a credential-free remote URL.
- `docker info` succeeds before container-dependent tasks begin.

## Confirmed stack

- Python 3.12
- `uv` with committed `uv.lock`
- pytest, Ruff, and a Python type checker
- SQLite with transactions and FTS
- SearXNG and Trafilatura
- deterministic T07 discovery selected for the current runtime
- no retained runtime agent framework
- Gemini API Tier 1 Postpay is eligible as the T10/T11 provider candidate;
  private/production model, API, client, data-handling, reliability, and cost
  qualification remains required
- Pocket TTS for authorized voice embedding and synthesis
- FFmpeg for OGG/Opus conversion and validation
- Telegram Bot API behind the same-shaped internal sender boundary
  (migrated from WAHA in T16b -- see docs/task-logs/T16b.md; WAHA/self-hosted
  WhatsApp-Web automation confirmed dead, see docs/research/waha-alternatives.md)
- Recipient consent, `STOP`, and an admin kill switch via a durable
  `sending_control` flag plus `getUpdates` offset-cursor polling
  (T17, merged; see docs/task-logs/T17.md), driven by a minimal daily-send
  entrypoint an external timer invokes every 1-2 minutes (T17b, merged; see
  docs/task-logs/T17b.md)
- Docker Compose on one hardened US-West cloud VPS
- WireGuard and key-only SSH for administration

Do not introduce Ollama, Kubernetes, a vector database, a public dashboard, persistent runtime-agent memory, runtime subagents, or LangSmith as a required service.

## Karpathy development rules

- State assumptions and tradeoffs before coding.
- Choose the minimum code that satisfies the current task.
- Do not add speculative abstractions or future-facing configurability.
- Make surgical changes; do not refactor unrelated code.
- Every changed line must trace to the active task.
- Define measurable success before implementation.
- If a simpler deterministic solution works, prefer it over an agent framework.
- Remove only dead code created by the current change.

## Task execution protocol

Only one backlog task may be in progress at a time.

For each task:

1. Confirm all dependencies are complete.
2. Restate the task assumptions and acceptance criteria.
3. Assign bounded subagent work with explicit file ownership.
4. Write the failing test first.
5. Run it and verify that it fails for the intended reason.
6. Implement the smallest passing change.
7. Run the focused test until green.
8. Run the relevant regression suites.
9. Request independent review for security-sensitive tasks.
10. Review all subagent diffs before accepting them.
11. Record verification evidence in `docs/task-logs/TXX.md` when that directory exists.
12. Commit only green code.

Branch names after the GitHub remote exists:

```text
task/TXX-short-name
```

Suggested commit form:

```text
TXX: concise verified outcome
```

Never combine unrelated backlog tasks in one implementation change.

## Agent collaboration rules

- The lead integrator owns task order, repository state, merges, and releases.
- TDD implementers own only the files named in their assignment.
- Security reviewers should review without editing unless explicitly assigned a fix.
- Integration verifiers must run real dependencies and independently report results.
- Research agents verify upstream APIs, licenses, compatibility, and breaking changes; they do not change architecture silently.
- Two agents must not edit the same file concurrently.
- Subagents never receive production secrets, the real recipient number, WhatsApp session data, or the production voice embedding.
- Security-sensitive tasks T06, T15, T16, T17, and T18 require independent review.
- If blocked, report the exact failing command, dependency, or permission and stop only the affected task.

## Strict no-mock TDD policy

Tests must not use:

- `unittest.mock`
- `pytest-mock`
- monkeypatching service clients or environment behavior
- fake LLM responses
- in-memory database substitutes
- fake WhatsApp APIs
- bypasses for SQLite, HTTP, the selected real model API, Pocket TTS, FFmpeg, or WAHA in integration tests

Use real implementations instead:

- temporary file-backed SQLite databases
- real ephemeral HTTP services and container networks
- real SearXNG and Trafilatura
- the real selected provider/model at the applicable model task boundary
- a real, consented non-production voice for initial TTS integration
- real FFmpeg/ffprobe processing
- real WAHA paired to a dedicated test session
- the owner's test WhatsApp chat as the only staging recipient
- real fault injection by stopping containers, severing routes, exhausting configured limits, or supplying corrupt files
- explicit clock/date inputs instead of mocked time

Pure functions may be tested directly with real values.

Test markers:

- `fast`: pure logic and small file-backed SQLite tests
- `integration`: real local or container dependencies
- `live`: changing public web sources
- `security`: hostile input, trust-boundary, permission, replay, and SSRF tests
- `e2e`: real model, voice, audio, WAHA, and WhatsApp delivery

Model tests should validate schemas, invariants, pass rates, and failure behavior rather than exact generated sentences.

## Runtime agent boundary

The production discovery worker may expose exactly these tools:

```text
search_web(query)
fetch_public_page(result_id)
search_message_history(candidate)
submit_inspiration_card(card)
```

Rules:

- `fetch_public_page` accepts only an opaque result ID returned during the same run, never an arbitrary URL.
- The agent has no shell, filesystem, generic HTTP, private-network, secret, voice, TTS, WhatsApp, or unrestricted database access.
- The agent has fixed limits for searches, fetches, submissions, model tokens, steps, CPU, memory, and wall-clock time.
- The agent's conversational final response is not trusted data.
- Only validated `submit_inspiration_card` calls create untrusted candidate records.
- The agent cannot approve, queue, synthesize, or send a message.
- Security checks live in deterministic tool implementations, not only in prompts.
- A failed discovery run creates zero candidates and cannot interfere with the daily sender.

T09 evaluated LangChain `create_agent` with `gemini-3.6-flash` and did not
retain it. The deterministic T07 workflow is the current production discovery
implementation. Do not reintroduce a runtime agent through incidental work.

Any future discovery-agent proposal must use a fresh versioned corpus and
repeat the full 20-run comparison. A candidate may stay only if at least 95%
of runs have correct tool trajectories, every hostile-input and resource gate
passes, and it materially improves valid unique yield. It must still expose
exactly the four tools above and may never approve, synthesize, or send.

## Content and rights rules

- Lyrics, quotes, and other creative text may be used only as inspiration when copying rights are unknown.
- The generator receives a sanitized InspirationCard, not the full source passage.
- Reject a candidate that copies six consecutive source words or exceeds the configured similarity threshold.
- Unknown rights remain `unknown`; the model cannot certify licensing.
- Raw scraped creative text is transient and removed after comparison.
- Reject sexual, possessive, manipulative, guilt-inducing, insulting, breakup-oriented, commitment-pressuring, or money-related content.
- Reject stranger names, fabricated shared memories, URLs, scraped instructions, and excessive emotional intensity.
- Malformed or uncertain judge output is a rejection.

## WhatsApp and delivery rules

- Use a dedicated sending number where possible.
- One recipient is fixed server-side and allowlisted.
- Staging can send only to the owner's test chat.
- Maximum one voice note per recipient and Pacific calendar date.
- Use `recipient + Pacific local date` as the idempotency boundary.
- Weekly discovery is due Monday at 00:00 `America/Los_Angeles` and remains
  eligible until Tuesday at 00:00; after that 24-hour grace it is missed.
- Daily preparation is eligible from 06:50 inclusive until 07:00 exclusive.
  Do not create a new daily run at or after 07:00.
- Daily sending targets 07:00 and remains eligible until 07:05 exclusive for
  bounded recovery. After 07:05, do not start a new send for that date.
- Never carry a missed preparation or send into the next Pacific calendar day.
- Retry only when non-delivery is certain.
- A timeout after possible submission becomes `delivery_unknown` and must be reconciled before any retry.
- T16 must reconcile ambiguous submissions within the same 07:00-07:05 send
  window; ambiguity never permits a blind retry or next-day catch-up.
- Retries reuse the same sentence and audio.
- Exact `STOP` from the allowlisted recipient disables sending durably.
- Other inbound messages are ignored and never invoke the discovery agent.
- A durable administrator kill switch must stop reserved sends.

WAHA is unofficial WhatsApp Web automation and can be logged out or restricted. Do not claim otherwise. Production release requires a real seven-day staging soak.

## Voice and privacy rules

- Clone only the owner's explicitly authorized voice.
- Validate enrollment audio before processing.
- Export a Pocket TTS voice embedding.
- Delete the raw enrollment recording after the embedding is verified.
- Restrict the embedding to the voice service volume and identity.
- Delete generated audio after confirmed delivery or bounded failure retention.
- Never place voice samples, embeddings, phone numbers, WhatsApp sessions, tokens, or secrets in Git, logs, container images, task prompts, or GitHub artifacts.
- Use a non-production consented voice for routine integration tests.
- The real voice is used only in the protected staging/production environment.

## Network and container rules

- Deny all inbound traffic except the WireGuard administration endpoint.
- SSH is key-only, available through the VPN, with password and direct root login disabled.
- WAHA, SQLite, model, TTS, and application APIs have no public ports.
- Do not mount the Docker socket into containers.
- Run non-root where supported.
- Drop unnecessary capabilities and enable no-new-privileges.
- Use read-only root filesystems where practical.
- Apply CPU, memory, process, response-size, redirect, and timeout limits.
- Separate discovery, model, voice, and sender networks and volumes.
- Pin dependency versions and container digests. For managed model APIs, pin
  the provider, stable model ID, API contract, and client version, and require
  requalification before changing any of them.
- Validate DNS before connection and after every redirect.
- Block loopback, private, link-local, multicast, and cloud metadata destinations.
- T18 must disable NAT64 on discovery networks or supply and test the deployed
  Pref64, backed by container egress controls that block private services.

## Data and audit rules

- SQLite is the operational source of truth.
- GitHub is never the runtime database.
- A  GitHub audit mirror is optional and must be redacted.
- Store only data required for provenance, deduplication, idempotency, and incident investigation.
- Logs must exclude secrets, phone numbers, voice paths, session data, generated audio, and raw scraped text.
- Encrypt backups using a recovery key not stored on the VPS.
- A restore drill must prove that message history and idempotency survive recovery.

## Backlog order

Execute in dependency order; consult the full plan for red tests and completion gates:

```text
T00  Unblock canonical repository and cloud toolchain
T01  Repository and no-mock test foundation
T02  Typed configuration and secret boundaries
T03  SQLite schema and delivery state machine
T04  Normalization, history search, and deduplication
T05  Pacific-time scheduler and idempotent daily run
T06  Secure result-ID-based web fetcher
T07  Deterministic discovery baseline
T08  InspirationCard and rights transformation
T09  Conditional discovery-agent harness benchmark
T10  Original English sentence generation
T11  Deterministic safety gates and structured judge
T12  Approved queue and safe reserve
T13  Secure voice enrollment
T14  Pocket TTS and OGG/Opus pipeline
T15  Locked WAHA sender boundary
T16  Exactly-once delivery and ambiguity recovery
T17  Recipient consent, STOP, and kill switch
T18  Cloud and container hardening
T19  Audit, alerts, backups, and recovery
T20  Seven-day staging soak and production cutover
```

## Expected commands after T01

These are targets, not permission to generate configuration before its task:

```powershell
uv sync --locked
uv run pytest -m fast
uv run pytest -m integration
uv run pytest -m security
uv run ruff check .
uv run mypy src
docker compose config --quiet
```

Live and end-to-end suites must be opt-in and protected from production recipients.

## Project definition of done

The project is complete only when:

- no owner computer is required for daily operation;
- exactly one original English romantic voice note is sent at 07:00 Pacific per eligible date;
- the verified owner-authorized voice embedding is used;
- the recipient is informed, allowlisted, and able to stop delivery;
- date simulations, restarts, and live fault tests show no duplicate sends;
- hostile web and prompt-injection cases cannot cross trust boundaries;
- prohibited content is rejected and safe content meets the documented quality threshold;
- invalid audio is never sent;
- seven consecutive cloud staging days pass;
- kill switch, alerts, backup restoration, and session-loss handling are demonstrated;
- all tests use real implementations or protocol endpoints without mocks;
- repository history contains no secrets or private voice/session artifacts.

T14 (Pocket TTS and OGG/Opus pipeline) is complete, including the owner's
listening acceptance, on `task/T14-pocket-tts-opus-pipeline`. A pre-T14
audit had stated FFmpeg and ffprobe were not installed; direct
re-verification at the start of this task found they actually are
(`ffmpeg`/`ffprobe` 9.0-full_build via winget, `--enable-libopus`) -- a
factual correction, recorded in `docs/task-logs/T14.md`. New module
`audio_pipeline.py` adds `synthesize_to_wav` (real Pocket TTS, reusing
T13's exported `.safetensors` embedding directly, calibrated
`eos_threshold=0.0`/`frames_after_eos=10`), `convert_to_opus` (real
FFmpeg, WhatsApp-standard mono/48kHz/24kbps Opus), `validate_audio` (real
ffprobe format/duration probe plus a `soundfile`-decoded silence/clipping
check), `produce_voice_note` (orchestrates all three and marks the
delivery `AUDIO_READY` via the existing `transition_delivery`, leaving no
partial file or state change on any failure), and
`remove_audio_after_delivery` (deletes the temp file only once the
delivery is confirmed `SENT`). No `database.py` changes were needed --
T03/T12's existing state-machine methods already covered everything T14's
red tests require. The long-dormant `audio_artifacts` table was
deliberately left unwired; how T15's sender will locate the produced
audio file is an explicit open note for T15.

The owner's listening review surfaced and fixed two real Pocket TTS bugs
beyond the plan's original red tests: a premature-end-of-speech default
that truncated cloned-voice audio to well under a second (fixed via a
calibrated `eos_threshold`), and an untruncated >30s voice-conditioning
prompt in T13's `enroll_voice` that destabilized generation for longer
sentences specifically (fixed with `truncate=True`, plus a new
`_trim_leading_silence` helper after a ~2.5s dead-air lead-in in the test
recording was found eating into that 30s budget) -- this second fix
touched already-merged T13 code, re-verified against T13's own test
suite. A real trial of Chatterbox (MIT-licensed, the strongest fully-open
voice-cloning alternative found by web research; XTTS v2 and F5-TTS's
original weights both fail the open-license constraint) was run via an
ephemeral `uv run --with` install -- never added as a project dependency
-- and was decisively rejected by the owner's own listening comparison,
confirming Pocket TTS as the right choice rather than leaving it
unexamined. Ten representative sentences passed both automated checks and
the owner's final listening acceptance. Full record, including all
intermediate findings and the four listening rounds, in
`docs/task-logs/T14.md`.

T15 (locked WAHA sender boundary) is complete on
`task/T15-locked-waha-sender`. The open design question T14 left --
how the sender locates a delivery's audio file -- resolved to neither of
the two options the plan brief offered: the plan's own implementation
line says the sender "accepts validated audio bytes," so it never needs a
DB-driven lookup at all; the caller (T16's future orchestrator) calls
T14's `produce_voice_note` first and passes the resulting bytes straight
through. `audio_artifacts` remains deliberately unwired.

New module `src/personal_voice_msg/sender.py`: `send_voice_note(session,
database, settings, audio_bytes, idempotency_key, timestamp, signature,
now) -> str` has no recipient-shaped parameter anywhere in its signature
-- the destination is always `settings.recipient`, read server-side.
Checks run in order, all fail-closed, all before any WAHA call: HMAC-SHA256
signature (`hmac.compare_digest`, constant-time), timestamp freshness
(300s window), replay protection (new `SCHEMA_V6` `sender_auth_nonces`
table, atomic `BEGIN IMMEDIATE` insert -- authentication-layer only,
distinct from T16's future exactly-once delivery bookkeeping), then real
audio validation (reuses T14's `audio_pipeline.validate_audio`). Only
then a real `POST /api/sendVoice` to WAHA. `config.py` gained two new
`Settings` fields (`waha_base_url`, loopback-only validated;
`sender_auth_key`, a new secret). T13's AST-based boundary test
(`test_voice_enrollment_boundaries.py`) was extended, exactly as that
task's own entry above said T15 would need to, so `discovery/`,
`generation/`, and `judging/` still cannot reach the sender or WAHA
secrets. New `docker-compose.yml` runs WAHA Core (MIT-licensed NOWEB
engine, pinned to a real dated tag *and* its resolved digest, not
`latest`) bound to `127.0.0.1:3000` only -- never a public port -- with
its own API key, dashboard, and Swagger auth. The done-when gate ("a real
voice note reaches only the owner's staging chat") was proven twice
against a real, owner-paired WAHA session, independently confirmed
received by the owner in chat both times. A fresh, unbiased subagent
performed the mandatory independent security review (T15 is on the
review-required list) and reported zero high-confidence findings after
tracing the actual signature/replay/SSRF-validation logic against source,
not the task log's prose. Full record, including a real gap this task's
own tooling caught in its own local setup (dev secrets briefly sitting in
a git-ignored-but-still-in-repo directory, correctly flagged by
`repository_policy.py`'s scanner, which deliberately does not trust
`.gitignore`), in `docs/task-logs/T15.md`.

## Immediate next step

**Correction to this section's prior text (2026-08-18):** it previously said T16 was "not yet
merged." That was stale -- T16 (`task/T16-exactly-once-delivery`) merged as PR #24
(commit `37d7621`) before the events below unfolded. Its Task 12 open item (the done-when-gate
fault-injection suite's live-WAHA verification, blocked because the real WAHA session was logged
out and refusing re-pairing) was left outstanding at merge time -- see
`.superpowers/sdd/2026-08-09-t16-exactly-once-delivery/progress.md` in that branch's worktree for
the full ledger. That open item is now **superseded, not resolved**: see below for why.

**WAHA/WhatsApp-Web self-hosted automation is confirmed dead**, not just stalled. The WAHA session's
"logged out, won't re-pair" symptom turned out to be WhatsApp's own server-side device-linking
throttle: 5 real linking attempts across 4 architecturally distinct client libraries (Baileys/NOWEB,
whatsmeow/GOWS, whatsapp-web.js via OpenWA and via WPPConnect Server), all refused identically with
"can't link new device." Full evidence and sourcing: `docs/research/waha-alternatives.md`. This
exhausts the practical self-hosted design space -- do not re-litigate it.

**Staying on WhatsApp via an official Business Solution Provider was also evaluated and rejected.**
Every BSP (Twilio, 360dialog, plus a comparative sweep of Gupshup/Bird/Vonage/Infobip) sits on the
identical Meta WhatsApp Business Platform and inherits the same two rules: proactive messages
outside an open 24-hour session require a pre-approved template, and no template header format
supports audio (confirmed live against current Meta docs, 2026-08-17 -- still true, and Meta added a
new wrinkle since mid-2026: Marketing-category templates, the one category that doesn't need a
session, are now blocked outright for US recipients). The one real bridge mechanism (a daily
button-tap reopening the session) is genuine but was rejected: it's a recipient action every day
forever (not one-time), the trigger template likely can't get Utility approval, and it roughly
doubles T16's exactly-once-delivery complexity with a new inbound-webhook dedup/auth surface this
project's design doesn't otherwise need. Full evidence: `docs/research/whatsapp-bsp-alternatives.md`.

**Decision (owner-approved, 2026-08-18): migrate the sender to the Telegram Bot API**, chosen over
Discord, Signal, and SMS/MMS -- see `docs/research/next-platform-alternatives.md` for the full
comparison and `docs/superpowers/specs/2026-08-18-telegram-sender-design.md` for the approved
design. Telegram is free, its `sendVoice` format requirement already matches T14's OGG/Opus output
with zero pipeline changes, the recipient's only action is a one-time `/start` (not recurring), and
-- the most consequential fact for T16 -- Telegram's Bot API has no chat-history-read method for
bots at all, so T16's chat-history-scraping reconciliation subsystem (`sender.py`'s
`reconcile_delivery`/`_find_matching_provider_id`/etc., over half that file) is not something to
port, it's something to delete outright. The general delivery-state-machine work T16 also did
(crash/restart/retry orchestration in `delivery.py`) is kept, not redone.

**Update (2026-08-19): T16b is complete and merged.** The plan produced from
`docs/superpowers/specs/2026-08-18-telegram-sender-design.md` (8 tasks,
`docs/superpowers/plans/2026-08-19-t16b-telegram-sender-migration.md`) migrated the sender to the
Telegram Bot API, deleted the WAHA reconciliation subsystem outright, and passed this project's
mandatory independent whole-branch security review (3 Important findings found and fixed, 6 Minor
findings triaged and either fixed or deferred with recorded reasoning) before merge. Full detail:
`docs/task-logs/T16b.md`.

**Update (2026-08-19): T17 is functionally complete, reviewed clean at the per-task level, and
merged (PR #31, commit `f577caa`).** The plan produced from
`docs/superpowers/specs/2026-08-19-t17-telegram-consent-stop-killswitch-design.md` (5 code/test
tasks plus this documentation task,
`docs/superpowers/plans/2026-08-19-t17-telegram-consent-stop-killswitch.md`) added `SCHEMA_V8`'s
`sending_control`/`sending_control_events`/`telegram_inbound_offset` tables and a `DisableReason`
enum; `consent.py`'s `getUpdates`-polling `poll_inbound_stop` (offset-cursor based, since Telegram's
Bot API has no chat-history-read method for bots); `sender.py`'s `SenderBlocked` exception for the
specific blocked-by-user 403 (with a fix round widening its exception handling to malformed
bodies); and `delivery.py`'s disabled-sending gate in `run_daily_send` (checked before any
production/network work, so the kill switch stops even an already-`RESERVED` delivery), its
`SenderBlocked` handler, and a fail-closed `recipient_key`/`chat_id` structural tie via
`recipient_key_for_chat_id` -- resolving the T16b-flagged decoupling gap. Three disable triggers
now durably turn off sending: an exact `STOP` from the enrolled `chat_id`, Telegram's blocked-by-user
403, and the admin kill switch (an operator calling `disable_sending(DisableReason.ADMIN_KILL_SWITCH)`
directly) -- all recorded to the `sending_control_events` audit trail and all surviving restart via
SQLite. Each task was reviewed clean (one fix round on Task 3, described above). Full detail,
including per-task review notes and the fresh verification suite run: `docs/task-logs/T17.md`.

**Update (2026-08-24): T17b (daily-send entrypoint and live STOP wiring) is complete and merged.**
`poll_inbound_stop` (T17) and `run_daily_send` (T16b) had no caller anywhere in the codebase before
this task -- it gives them their first real caller: a minimal `run_daily_entrypoint` function
(`daily_send_entrypoint.py`) plus the runnable `scripts/run_daily_entrypoint.py` an external timer
invokes every 1-2 minutes. Not on this project's original mandatory-review list, reviewed anyway per
the T16b precedent (loads real secrets, drives a real send through a new production-facing entry
surface): 4 Important findings and 3 actionable Minor findings, all fixed and re-reviewed clean.
Recipient enrollment, the branch's own fault-injection tests, and the full `integration`/`e2e` test
suites all ran for real against genuine Telegram infrastructure -- this also closed T16b's and T17's
own long-deferred live-verification debt, not just T17b's. Full detail, including the honestly-
flagged one still-open live-verification item (a real script invocation during an actual,
unmodified 07:00-07:05 Pacific window -- three scheduling attempts missed the window on scheduler
dispatch latency, not application error; a separate schedule-patched sandbox test proved the
send-path mechanics work end-to-end but was explicitly not accepted as satisfying this specific
requirement) and T17's own still-open real-STOP live test: `docs/task-logs/T17b.md`.

**Actual next step: T18 (cloud and container hardening)**, next per the backlog order above --
`IMPLEMENTATION_PLAN.md`'s T18 section now depends on T17b as well. Whoever picks up T18 should also
close the two still-open live-verification items above (T17b's real in-window script run, T17's
real-STOP test), ideally together, live, in one sitting -- not blindly scheduled.

**Update (2026-08-24): T18's plan section reconciled against current architecture, before any T18
implementation.** The plan's original wording predates T16b and was WAHA-era: separate
discovery/model/voice/sender isolation, "WAHA cannot access the crawler network." T16b deleted the
WAHA container and `docker-compose.yml` outright when it migrated the sender to the Telegram Bot
API (an outbound-only HTTPS client, no local service) -- there is currently no `docker-compose.yml`
and nothing to isolate WAHA-style. Reconciled, human-confirmed topology: one app container
(generation/judging/voice/sender/delivery/scheduler, in-process, matching T15's own precedent) plus
one separate discovery container/network, since discovery is the actual trust boundary. T17/T17b's
two open live-verification items are folded into T18's own scope as an explicit task
(human-confirmed) rather than tracked separately. Full reconciled text: `IMPLEMENTATION_PLAN.md`'s
`### T18` section.

**Update (2026-08-25): T18's 9 build/red-test tasks are complete, each reviewed
clean at the per-task level** (one fix round on Tasks 1, 2, 3, 4, 5, and 7; Tasks
6, 8, 9 approved clean on first review) -- new `Dockerfile`/`.dockerignore` (one
shared, pinned, non-root image), `docker-compose.yml` (none existed since T16b
deleted the WAHA-era one -- two services, `app` and `discovery`, on separate
networks/volumes, capabilities dropped, `no-new-privileges`, read-only roots,
resource limits), `scripts/crontab` wiring supercronic to
`scripts/run_daily_entrypoint.py` (T17b) inside the `app` container,
`discovery_worker_entrypoint.py`/`scripts/run_discovery_worker.py` (a bounded
verification harness reusing T07's discovery code, not new production
candidate-generation wiring), a full Git-history/built-image secret scanner
added to `scripts/repository_policy.py`, and infra-as-code for firewall/
WireGuard/SSH hardening (`infra/`) with a real local Docker-network-namespace
substitute for "the host" proving the nftables ruleset is genuinely selective
(a real UDP echo round-trip through the loaded ruleset, not just a
negative-only control). Full per-task ledger:
`.superpowers/sdd/2026-08-24-t18-cloud-container-hardening/progress.md`.

**Task 10 (this documentation/verification task) ran the full regression suite
for real, found and fixed one real failure.** The failure was root-caused to a
Windows-sandbox-only git checkout artifact (`core.autocrlf=true` converting
the committed LF-only `infra/firewall/rules.nft` to CRLF on disk, which breaks
`nft -f`'s parser) -- confirmed by manually reproducing both the failure
(CRLF) and a clean pass (LF, byte-identical content otherwise) against the
same built verification image. Judged not to be a defer-able sandbox-only
quirk: `core.autocrlf=true` is a common Windows Git default, so the same
corruption could hit a real Windows-based contributor or CI runner preparing
files for the real VPS deployment, and it meant T18's own "security suite
passes" done-when gate was not actually met. Fixed within T18: a new root
`.gitattributes` (`* text=auto eol=lf`, verified safe -- zero committed blobs
anywhere in this repository's history contain CRLF, and both this project's CI
and its real deployment target are Linux-only) plus a forced renormalization
of `infra/firewall/rules.nft` and `docker-compose.yml` (the two files the
stale checkout actually broke; zero Git diff on either file's tracked
content). `uv run pytest -m security -q` now passes clean: **92 passed, 0
failed, 52 skipped**. **T18's two folded-in live-verification items (a real
STOP from the enrolled Telegram chat; a real, unmodified 07:00-07:05 Pacific
`DAILY_SEND` cron firing) remain open** -- documented with exact commands in
`docs/task-logs/T18.md`, not executed, for the same reason every prior live
item in this project required the owner (no safe/verified path to real
Telegram infrastructure or a live wall-clock window from an unattended
sandbox). **T18's mandatory independent whole-branch security review and PR
are still pending**, handled separately from this task. Full detail, including
every command's real output: `docs/task-logs/T18.md`.
