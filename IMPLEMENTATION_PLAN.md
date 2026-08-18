# Personal Voice Message Assistant — Implementation Plan

Status: T01-T11 implemented and audited; T09 selected the deterministic
discovery fallback; T10 qualified the owner-confirmed Gemini API Tier 1
Postpay project (`gemini-3.6-flash`, `temperature=0.2`) with a real
100-trial run (100% structural/prohibited-field compliance, 99%
valid-original yield -- see `docs/task-logs/T10.md`); T11 added
deterministic safety gates plus a structured Gemini judge, calibrated with
a real 42-row Promptfoo run to 100% pass (11/11 red-corpus rejected, 8/8
adversarial rejected, 19/19 normal+boundary-approved accepted) at final
floors `SAFE_TONE_FLOOR = SAFE_WARMTH_FLOOR = SAFE_NATURALNESS_FLOOR = 6.5`
-- see `docs/task-logs/T11.md`; T12 is next
Primary repository target: `F:\personal_voice_msg`
Delivery schedule: every day at 07:00 `America/Los_Angeles`
Development method: agentic, test-driven, no mocked dependencies

## 1. Objective

Build a fully cloud-hosted service that autonomously:

1. Discovers romantic inspiration from permitted public web sources.
2. Produces an original, sweet English sentence rather than copying lyrics or quotes.
3. Rejects duplicate, unsafe, manipulative, overly intimate, or low-quality messages.
4. Synthesizes the accepted sentence using the owner's authorized voice clone.
5. Converts the audio to a WhatsApp-compatible OGG/Opus voice note.
6. Sends exactly one voice note to one allowlisted recipient at 07:00 Pacific time.
7. Preserves a minimal, privacy-conscious history for deduplication and auditing.
8. Runs entirely in the cloud after one-time voice enrollment and WhatsApp QR pairing.

No service must remain running on the owner's laptop.

## 2. Non-negotiable constraints

- Application-controlled code and components must be open source or openly
  licensed. The owner has approved Gemini as one additional proprietary
  external dependency for the T09 benchmark and confirmed its API project as
  a candidate for T10/T11 qualification. This is not production authorization.
- WhatsApp, GitHub, and the cloud provider remain unavoidable proprietary external dependencies.
- The voice clone may only use the owner's voice with explicit authorization.
- A dedicated WhatsApp sending number is strongly recommended.
- The girlfriend must know that the messages are automated and use synthesized audio.
- The recipient can permanently disable delivery by sending an exact `STOP` command.
- The system sends to one hard-coded recipient only.
- The system sends no more than one romantic voice note per Pacific calendar date.
- Unknown safety, rights, delivery, or audio state fails closed.
- Runtime discovery may be agentic; judging, queueing, TTS, scheduling, and sending remain deterministic.
- GitHub is not the operational database.
- No public administration dashboard is required.
- No Kubernetes, vector database, multi-agent runtime hierarchy, or persistent agent memory.

## 3. Confirmed technical decisions

| Area | Decision |
|---|---|
| Main language | Python 3.12 |
| Package manager | `uv` with a committed lockfile |
| Operational database | SQLite with transactions and FTS |
| Web discovery | SearXNG plus Trafilatura |
| Runtime agent | None retained after the T09 candidate failed protocol and hostile-input gates |
| Current discovery | Deterministic T07 predefined search workflow |
| Future agent path | Separate API-based candidate behind the exact restricted tool boundary and a fresh frozen benchmark |
| T10/T11 inference | T10 qualified `gemini-3.6-flash` at `temperature=0.2` via a hand-rolled `aiohttp` client against the owner-confirmed Gemini API Tier 1 Postpay project: real 100-trial run, 100% structural/prohibited-field compliance, 99% valid-original yield (fresh-per-trial metric); a separate accumulating-history measurement found 91/100 unique outputs, recorded as a T11 harness-design input, not a T10 gate failure (`docs/task-logs/T10.md`) |
| Voice cloning | Pocket TTS voice embedding and synthesis |
| Audio conversion | FFmpeg to OGG/Opus |
| WhatsApp bridge | WAHA Core behind a narrow internal sender wrapper |
| Scheduling timezone | `America/Los_Angeles` using timezone-aware dates |
| Container orchestration | Docker Compose |
| Administration | WireGuard VPN plus key-only SSH |
| History | SQLite primary; optional redacted private GitHub audit mirror |
| Backups | Encrypted backups with a recovery key not stored on the VPS |

No agent harness is assumed to be necessary. A framework stays only if a fixed benchmark proves that adaptive discovery materially outperforms deterministic searches while meeting reliability and security thresholds.

## 4. High-level architecture

```mermaid
flowchart LR
    S[Weekly scheduler] --> A[Bounded discovery worker]
    A --> Q[Controlled SearXNG search]
    Q --> F[Secure page fetch and Trafilatura extraction]
    F --> C[Untrusted InspirationCard]
    C --> O[Original sentence generator]
    O --> D[Deterministic rights, safety, and duplicate gates]
    D --> J[Structured quality judge]
    J --> DB[(Approved SQLite queue)]
    DB --> T[Pocket TTS]
    T --> FF[FFmpeg OGG/Opus validation]
    FF --> W[Locked sender wrapper]
    W --> WAHA[Internal WAHA Core]
    WAHA --> R[One allowlisted WhatsApp recipient]
```

Trust boundaries:

- Web content is untrusted data and never treated as instructions.
- Inspiration cards are untrusted until deterministic validation succeeds.
- The LLM judge provides a structured score but cannot write approval state.
- Only deterministic application code moves a record into the approved queue.
- Only the locked sender can access WAHA.
- The discovery worker cannot access voice data, WhatsApp, secrets, arbitrary HTTP, shell, or the filesystem.

## 5. Agentic development workflow

Development is agentic; the production application is only narrowly agentic.

### Roles

| Role | Responsibility |
|---|---|
| Lead integrator | Owns the plan, task order, repository state, final reviews, commits, and releases |
| TDD implementer | Writes the failing test first and implements the smallest passing change |
| Security reviewer | Reviews trust boundaries, secrets, SSRF, container permissions, and delivery controls |
| Integration verifier | Runs real dependency tests and independently verifies acceptance criteria |
| Research specialist | Verifies upstream APIs, licenses, model compatibility, and breaking changes |

### Operating rules

- Only one backlog task may be `in_progress` at a time.
- Subagents receive bounded tasks with explicit file ownership and acceptance criteria.
- Two agents must not edit the same file concurrently.
- The lead reviews every subagent diff before accepting it.
- Security-sensitive tasks T06, T15, T16, T17, and T18 require an independent review.
- Subagents never receive production secrets or the real recipient number.
- Each task uses a branch named `task/TXX-short-name` after the remote repository exists.
- Each final task commit must be green; evidence of the initial red test is recorded in the task log or CI output.
- Unrelated refactoring is prohibited.
- A failed dependency or unclear requirement stops only the affected task, not unrelated verified work.

## 6. TDD policy — no mock tests

Every task follows:

1. Write an acceptance test.
2. Run it and prove it fails for the expected reason.
3. Implement the minimum behavior required.
4. Run the focused test until it passes.
5. Refactor only the new code when justified.
6. Run the complete relevant regression suite.
7. Record verification evidence and merge.

Prohibited test mechanisms:

- `unittest.mock`
- `pytest-mock`
- monkeypatching service clients or environment behavior
- fake LLM responses
- in-memory database substitutes
- fake WhatsApp APIs
- bypassing Pocket TTS, FFmpeg, SQLite, HTTP, or WAHA in integration tests

Permitted test mechanisms:

- Direct tests of pure functions with real inputs
- Temporary real SQLite files
- Real ephemeral HTTP services and container networks
- Real SearXNG and Trafilatura
- Real selected provider/model inference at each model task boundary
- Real Pocket TTS inference using a consented test voice
- Real FFmpeg encoding and probing
- Real WAHA session sending only to the owner's test chat
- Real fault injection by stopping containers, severing routes, exhausting configured limits, or corrupting input files
- Explicit clocks and dates passed as data instead of mocked time

Test categories:

- `fast`: pure functions and small file-backed SQLite tests
- `integration`: real local/container dependencies
- `live`: changing external web sources
- `security`: adversarial URLs, content, permissions, and replay tests
- `e2e`: real model, TTS, WAHA, and WhatsApp delivery

### Evaluation and guardrail policy

- Deterministic pytest and security tests remain the authoritative release
  gates. An evaluation framework may orchestrate trials and report metrics but
  cannot authorize a candidate, queue transition, audio artifact, or send.
- Use [Promptfoo](https://github.com/promptfoo/promptfoo) as a test-only
  orchestrator for the real T10/T11 application boundaries. Use committed
  trusted configuration, a scoped evaluation credential, and `--no-cache`.
  Do not enable hosted sharing or treat Promptfoo as a sandbox.
- Upgrade Node from the current 22.13 release to at least 22.22 before
  installing the current Promptfoo release.
- Eval runners are development and protected-staging dependencies, never
  production runtime services or trust boundaries.
- Version and pin each corpus, prompt, provider, stable model ID, API contract,
  client, tool schema, and scoring rule. A change to any of them requires the
  affected eval suite to be rerun before acceptance.
- Use real provider calls and real application state. Provider errors, quota
  failures, timeouts, malformed outputs, and uncertain outcomes count as
  failures rather than skipped or replayed successes.
- Grade actual outcomes and state transitions, not the agent's conversational
  claim. Preserve only redacted traces, aggregate counters, and non-reversible
  fingerprints; never persist secrets, private history, recipient data, raw
  scraped text, voice data, or generated audio in eval reports.
- Seed every critical corpus with human-labelled normal, edge, and adversarial
  examples. Calibrate any model-based quality grader against those labels;
  deterministic security invariants always take precedence.

## 7. Cloud test and deployment environments

### CI environment

Runs on every pull request without production secrets:

- formatting, linting, and type checks
- fast tests
- SQLite integration tests
- state-machine and timezone tests
- controlled HTTP security tests
- secret scanning
- dependency and container image scanning
- Docker Compose configuration validation when Docker is available
- validation of pinned Promptfoo configurations and corpus schemas;
  real-provider eval execution remains opt-in in protected staging

### Temporary staging environment

Runs heavy and live tests in the cloud:

- real selected provider/model APIs with task-specific data boundaries
- deterministic-versus-agent discovery benchmark
- live SearXNG and extraction
- Pocket TTS and FFmpeg
- WAHA paired to the dedicated sending number
- WhatsApp delivery to the owner's test chat only
- restart, timeout, and network-failure testing

### Production environment

- One hardened US-West Linux VPS initially sized around 8 vCPU and 16 GB RAM.
- Services run sequentially so the LLM and voice model do not compete for memory.
- All application ports are private.
- Only WireGuard is reachable publicly for administration.
- The instance may be resized after measured staging results.

## 8. Repository layout target

```text
personal_voice_msg/
├── .github/workflows/
├── docs/
│   ├── adr/
│   ├── task-logs/
│   ├── threat-model.md
│   └── operations.md
├── infra/
│   ├── compose/
│   ├── firewall/
│   └── systemd/
├── src/personal_voice_msg/
│   ├── config.py
│   ├── database.py
│   ├── scheduling.py
│   ├── discovery/
│   ├── generation/
│   ├── judging/
│   ├── queueing/
│   ├── voice/
│   ├── sender/
│   └── cli.py
├── tests/
│   ├── fast/
│   ├── integration/
│   ├── live/
│   ├── security/
│   └── e2e/
├── .gitignore
├── docker-compose.yml
├── pyproject.toml
├── uv.lock
└── README.md
```

Directories are created only when their first task needs them.

## 9. Executable task backlog

### T00 — Unblock and establish the canonical repository

Dependencies: none
Current status: complete

Historical blockers, now cleared:

- `F:\personal_voice_msg` was opened as the writable canonical repository.
- The GitHub repository and credential-free HTTPS remote were created.
- The Codex workspace can edit the repository and run Git normally.
- GitHub CLI authentication for `revanthxmudavath` succeeds.
- Docker and Compose can reach the Docker daemon.
- Python 3.12.4 and `uv` are installed.
- FFmpeg and ffprobe remain a deferred host prerequisite for T14, not T00.

Required actions:

1. Reopen or add `F:\personal_voice_msg` as a writable Codex workspace root.
2. Re-authenticate with `gh auth login -h github.com`.
3. Start Docker Desktop for container-dependent tests.
4. Create a GitHub repository named `personal_voice_msg` unless another name is chosen.
5. Add the remote without embedding credentials.

Verification:

- `apply_patch` can create a file in the repository.
- `git status` runs without unsafe ownership workarounds in the active workspace.
- `gh auth status` succeeds.
- `docker info` succeeds before container-dependent tasks.
- The GitHub repository exists; public visibility is explicitly approved by the lead..

### T01 — Repository and no-mock test foundation

Dependencies: T00

Red tests:

- Policy scan fails when a prohibited mock import is planted.
- Configuration check fails when the lockfile is absent or stale.
- Secret scanner detects a planted dummy credential.
- CI configuration validation fails for an intentionally invalid workflow fixture.

Implementation:

- Initialize the minimal Python package with `uv`.
- Add pytest categories, Ruff, and a type checker.
- Add a repository policy test prohibiting mock libraries and client monkeypatching.
- Add `.gitignore`, a concise README, and the first CI workflow.
- Commit `uv.lock`.

Done when:

- A clean checkout can run the fast suite with one documented command.
- `uv sync --locked` succeeds.
- Tests, lint, and type checks pass.
- No production functionality is introduced.

### T02 — Typed configuration and secret boundaries

Dependencies: T01

Red tests:

- Missing required settings prevent startup.
- Unknown settings fail validation.
- staging cannot load the production recipient configuration.
- logs never reveal tokens, phone numbers, voice paths, or session data.

Implementation:

- Add the smallest typed configuration schema.
- Load secrets from profile-bound files outside the project for deployed
  profiles rather than from command-line arguments. T18 enforces deployed
  ownership and restrictive Unix modes against the actual service identity.
- Define development, staging, and production profiles.
- Add centralized value redaction.

Done when configuration fails closed and no secret is stored in Git, images, or logs.

### T03 — SQLite schema and delivery state machine

Dependencies: T02

Red tests:

- Invalid state transitions fail.
- two workers cannot reserve the same message/date.
- restart at each state resumes safely.
- migrations succeed on a real empty SQLite file.

Implementation:

- Add migrations for sources, inspiration cards, messages, runs, audio artifacts, and deliveries.
- Implement explicit transitions:

```text
discovered -> validated -> approved -> queued
queued -> reserved -> audio_ready -> sending
sending -> sent | failed | delivery_unknown
```

Done when 30 simulated dates produce one atomic reservation per date under concurrency.

### T04 — Normalization, history search, and deduplication

Dependencies: T03

Red tests:

- exact, case, whitespace, and punctuation variants are rejected.
- near-duplicate paraphrases over the selected threshold are rejected.
- distinct messages remain accepted.
- copying six consecutive source words is rejected.

Implementation:

- Add normalized hashes, SQLite FTS, RapidFuzz scoring, and source-span comparison.
- Store only the minimum text required for history and auditing.

Done when the curated duplicate corpus has no known false negatives and documented acceptable false positives.

### T05 — Pacific-time scheduler and idempotent daily run

Dependencies: T03

Red tests:

- spring and autumn DST dates still resolve to 07:00 Pacific.
- the server timezone cannot change delivery time.
- restarts before, during, and after a run cannot create a second daily run.
- weekly discovery is due from Monday 00:00 inclusive until Tuesday 00:00
  exclusive, including exact boundary tests across DST-offset changes.
- daily preparation is due from 06:50 inclusive until 07:00 exclusive.
- daily sending is due from 07:00 inclusive until 07:05 exclusive.
- each exact cutoff is missed, and missed work never catches up on the next
  Pacific calendar day.

Implementation:

- Add timezone-aware schedules with a 24-hour weekly discovery grace, a
  10-minute daily preparation window, and a 5-minute daily send window.
- Use `recipient + Pacific local date` as the idempotency boundary.
- Prepare at 06:50, target sending at 07:00, and reject new work at the exact
  window cutoffs.

Done when a simulated calendar year produces exactly one correct trigger per
eligible date and the start, last eligible instant, exact cutoff, DST, and
restart boundaries all follow the documented policy.

### T06 — Secure result-ID-based web fetcher

Dependencies: T02

Independent security review required.

Red tests using real HTTP endpoints and networks:

- reject localhost, private, link-local, and cloud metadata destinations.
- reject non-HTTP(S) schemes.
- reject public-to-private redirects.
- reject DNS destination changes, oversized bodies, slow responses, excessive redirects, and unsupported content types.

Implementation:

- Provide run-scoped registration of sanitized trusted search hits and return
  opaque result IDs; T07 supplies those hits through its real SearXNG
  `search_web(query)` integration.
- `fetch_public_page(result_id)` accepts only a result from the same discovery run.
- Validate DNS before connection and after every redirect.
- Apply byte, time, redirect, and content-type limits.

Done when the hostile URL suite cannot reach any internal service or arbitrary URL.

### T07 — Deterministic discovery baseline

Dependencies: T04, T06

Red tests:

- real SearXNG results are parsed into bounded records.
- permitted public pages are extracted with Trafilatura.
- extraction failures create no candidate.
- every record maps to a result actually returned in that run.
- raw scraped creative text is removed after transient analysis.

Implementation:

- Add a small curated source list and predefined romantic-theme queries.
- Implement `search_web(query)` through real SearXNG, register only results
  returned in that run through the T06 boundary, and add Trafilatura extraction.
- Do not add an arbitrary-URL fetch path.
- Record source URL, retrieval time, and rights evidence.

Done when repeated live runs produce a reliable baseline of valid source records.

### T08 — InspirationCard and rights transformation

Dependencies: T07

Red tests:

- lyrics and long quote passages cannot pass through as generated messages.
- unknown licensing stays unknown.
- missing provenance and oversized cards fail.
- page instructions cannot modify the schema or task.
- a card cannot approve itself.

Implementation:

- Define a strict InspirationCard containing theme, emotion, imagery, tone, source, rights category, evidence, and discovery timestamp.
- Remove copied creative language before later generation.
- Treat every card as untrusted.

Done when every card is traceable and contains no prohibited source passage.

### T09 — Conditional discovery-agent harness benchmark

Dependencies: T07, T08
Current status: complete. The corrected benchmark rejected the
LangChain/Gemini candidate and selected deterministic T07 discovery.

Red tests:

- the worker exposes exactly four tools.
- arbitrary URLs and unregistered tools fail.
- final conversational text cannot submit a candidate.
- malformed tool calls and timeouts terminate safely.
- prompt-injected pages cannot change policies or access forbidden capabilities.

Allowed tools only:

```text
search_web(query)
fetch_public_page(result_id)
search_message_history(candidate)
submit_inspiration_card(card)
```

Evaluated implementation:

- Use LangChain `create_agent` around the fixed stable model ID
  `gemini-3.6-flash`, one prompt, one schema, and hard limits. The initially
  proposed `gemini-2.5-flash` returned `404 NOT_FOUND` for the new API project
  before benchmarking; Google documents `gemini-3.6-flash` as its current
  stable GA Flash model.
- Compatibility review excluded Hermes Agent because custom tools require its
  plugin machinery and excluded smolagents `ToolCallingAgent` because it adds
  an implicit `final_answer` tool; neither can prove the exact literal
  four-tool surface. Google ADK is excluded from T09.
- The Gemini Developer API is approved for this benchmark only. The owner
  reports Google AI Pro, but Google documents API billing and rate limits as
  properties of the API key's project and linked billing account; verify the
  project's Plan in Google AI Studio rather than inferring it from a consumer
  subscription. The owner subsequently confirmed the API project as Tier 1
  Postpay on 2026-07-30. Send only public-page excerpts bounded to at most
  3,000 characters and synthetic message history.
  Keep the full extracted page text transient in host process memory solely
  for deterministic T08 source-copy validation, then discard it at run end.
  Never send private message history, recipient data, voice data, secrets, or
  full page text to the Gemini API.
- No LangSmith requirement, persistence, checkpointer, memory, skills, plugins,
  gateways, filesystem, shell, code execution, delegation, subagents, TTS,
  WhatsApp, secrets, or generic HTTP.

Decision benchmark:

- Pin the provider, stable model ID, API contract, and client version for the
  benchmark. Any change requires the benchmark to be rerun before acceptance.
- Run deterministic and LangChain `create_agent` 20 times each. Agent runs use
  the real fixed `gemini-3.6-flash` API and the same bounded inputs and resource
  limits.
- Use identical per-run search, fetch, history, submission, token, and time
  budgets. The versioned scenario corpus must contain natural semantic
  variation, duplicates, extraction failures, and hostile page instructions;
  it must not let either arm reach the maximum possible yield trivially.
- Keep the harness only if at least 95% of runs complete without malformed
  calls and it materially improves valid unique candidate yield within
  resource limits.
- API errors, quota exhaustion, timeouts, malformed calls, or uncertain output
  fail closed and create zero candidates.
- If LangChain does not pass every retention gate, remove all agent-framework
  and Gemini-client dependencies introduced by T09 and retain deterministic
  T07.
- T09 does not approve this Gemini configuration for T10 or T11. Those tasks
  require explicit private/production selection and data-handling,
  reliability, cost, API, model, and client requalification. The
  owner-confirmed Tier 1 Postpay status satisfies the billing-plan prerequisite
  only.

Final evidence and decision:

- The historical paired report recorded 20/20 protocol completion and 40 agent
  cards versus 1 deterministic card. That comparison used unequal baseline
  behavior and is retained only as debugging history, not as current retention
  evidence.
- The corrected `t09-semantic-v2` corpus contained 20 natural, semantic-only,
  and hostile scenarios. Both arms received equal 120-second limits and the
  runner independently hashed the bytes observed through each arm. The
  order-sensitive corpus SHA-256 was
  `0b07ed96da644ff8b500dd2a45dc60565f586748a653df0c9727337949e0a636`.
- The deterministic arm completed 5/20 protocol-perfect runs and produced 10
  unique valid cards. It passed the security and resource gates.
- The agent arm completed 13/20 protocol-perfect runs and produced 26 unique
  valid cards. It passed the material-yield and resource gates, but failed the
  required 19/20 protocol threshold and hostile-input security gate. The run
  consumed 191,620 input and 7,262 output tokens.
- The result is `retained: false`. The LangChain harness, Gemini client, live
  fixtures, and T09-only tests were removed. Generic Gemini credential
  detection and log redaction remain as provider-independent protections.
- The external owner-managed Gemini key was not changed or deleted.
- A future API-based discovery agent is a separate proposal. It must use the
  exact restricted tool boundary above, a fresh frozen non-ceiling corpus, and
  the same deterministic authorization. Reusing or prompt-tuning against the
  T09 evaluation corpus is prohibited.

### T10 — Original English sentence generation

Dependencies: T08, T09

Red tests with the real model:

- output is exactly one natural spoken English sentence.
- output contains no URL, citation, scraped instruction, stranger name, or fabricated memory.
- copied and near-copied source wording fails.
- malformed output fails closed.

Implementation:

- Generate only from the sanitized InspirationCard.
- Use low temperature, bounded length, and structured output.
- Re-run deterministic source and history checks after generation.
- Use Promptfoo with a custom Python provider that calls the real generation
  boundary over a versioned fixed InspirationCard corpus. Disable caching and
  use the application's deterministic assertions rather than duplicated
  model-graded rules.

Done when at least 100 fresh real-provider trials have 100% structural and
prohibited-field compliance and at least 95% valid-original yield, with every
failure preserved as a redacted regression fixture.

### T11 — Deterministic safety gates and structured judge

Dependencies: T04, T10

Red corpus must include:

- sexual content
- possessiveness
- manipulation and guilt
- breakup language
- proposals and major commitments
- money requests
- insults
- stranger names
- fabricated memories
- excessive emotional intensity
- prompt injection

Implementation:

- Run deterministic prohibitions first.
- Use a separate structured LLM judge for romantic tone, warmth, spoken naturalness, and remaining risks.
- The judge returns a score and reasons but cannot update approval state.
- Run the full deterministic-gates-plus-judge boundary through Promptfoo with
  the same real custom provider, pinned corpus, and `--no-cache`. Model-graded
  Promptfoo assertions are non-authoritative and must not introduce a hidden
  second judge.
- Calibrate the structured judge against human-labelled normal, boundary, and
  adversarial examples before setting the final safe-corpus acceptance floor.

Done when every prohibited fixture and every malformed or uncertain judge
output is rejected, the human-calibrated safe corpus achieves at least 95%
acceptance, and no model result can bypass deterministic approval code.

### T12 — Approved queue and safe reserve

Dependencies: T03, T11

Pre-T12 decisions (confirmed 2026-08-06, discussion recorded in
`docs/task-logs/T11b-pre-T12-hardening.md`):

- A safety-rejected message is recorded in a new **`message_rejections`**
  table (`message_id REFERENCES messages(id)`, `reason`, `rejected_at`) —
  not a new `MessageState` value. `reason` stores
  `evaluate_message_safety`'s own rejection reason
  (`gate_violation` / `judge_risk_flag` / `judge_score_floor` /
  `judge_error`), which doubles as free audit data for judge-calibration
  debugging later. This is a purely additive `SCHEMA_V5` migration (one
  `CREATE TABLE IF NOT EXISTS`) — it does not touch `messages`'s `state`
  `CHECK` constraint, `CONTENT_TRANSITIONS`'s shape, or any existing FK
  (`deliveries`, `audio_artifacts`, `message_history` all reference
  `messages(id)`, so a `CHECK`-constraint table rebuild was rejected as
  unnecessary migration risk for a requirement a side table satisfies
  fully). A message that fails safety review simply stays at `VALIDATED`
  with a matching `message_rejections` row; a refill query excludes it via
  `NOT EXISTS (SELECT 1 FROM message_rejections WHERE message_id = ...)`.
- Deliberately **not** fixed as part of T12: `database.py`'s
  `EXPECTED_SCHEMA_V1_OBJECTS` derives its `messages` table CHECK text from
  the *live* `MessageState` enum (`ALL_STATES_SQL`) rather than a frozen
  historical literal. This means a *future* task that legitimately needs to
  grow `MessageState` (not T12, since T12 no longer needs to) will corrupt
  `_validate_schema` for every already-migrated database unless that task
  pins `EXPECTED_SCHEMA_V1_OBJECTS`'s historical text first. Tracked here so
  it is not rediscovered from scratch; fix it in whichever task first adds a
  new `MessageState` member.
- The plan's "safe reserve" is renamed **reserve buffer** below, to avoid
  colliding with the existing `MessageState.RESERVED` (which means
  "atomically picked for today's specific delivery attempt" — an unrelated,
  already-taken meaning). The buffer's actual representation (a column/flag
  on `messages`, or a computed threshold over `QUEUED` rows) is an open
  implementation decision for T12's own red-test-writing step, not resolved
  here.

Red tests:

- discovery failure preserves the existing queue.
- rejected candidates never enter the queue.
- a rejected message gets exactly one `message_rejections` row and is never
  re-submitted to the judge on a later refill pass.
- queue refill cannot modify sent records.
- exhaustion selects only a pre-approved reserve buffer.
- no reserve buffer means no send.

Implementation:

- Add the `message_rejections` table via an additive `SCHEMA_V5` migration.
- Maintain at least 30 approved messages and a small reserve buffer.
- Add atomic reservation and queue-health alerts.

Done when repeated refill, failure, and restart tests preserve queue invariants.

### T13 — Secure voice enrollment

Dependencies: T02

Red tests using a real consented test voice:

- invalid, silent, clipped, and too-short samples fail.
- valid audio produces a usable Pocket TTS embedding.
- the raw sample is deleted after verified export.
- discovery and sender identities cannot read the embedding.

Implementation:

- Upload only through the administrative VPN.
- Validate and normalize the sample.
- Export the Pocket TTS voice embedding.
- Store it on a restricted volume and remove the raw sample.

Done when a test voice passes before the owner's real voice is enrolled.

### T14 — Pocket TTS and OGG/Opus pipeline

Dependencies: T12, T13

Host prerequisite: install FFmpeg and ffprobe.

Red tests:

- output decodes as OGG/Opus.
- silent, clipped, corrupt, incorrectly sampled, or excessive-duration audio fails.
- failed synthesis leaves no sendable artifact.
- successful delivery removes temporary audio.

Implementation:

- Synthesize with the real Pocket TTS embedding.
- Convert with FFmpeg using WhatsApp-compatible parameters.
- Probe format, signal, and duration before marking `audio_ready`.

Done when ten representative sentences pass automated checks and the owner's one-time listening acceptance.

### T15 — Locked WAHA sender boundary

Dependencies: T02, T14

Independent security review required.

Red tests using a real WAHA session:

- non-allowlisted recipients fail.
- text and invalid audio requests fail.
- replayed or unauthenticated requests fail.
- discovery, model, and TTS services cannot call WAHA directly.
- WAHA has no public dashboard or API exposure.

Implementation:

- Deploy pinned WAHA Core internally.
- Add a narrow sender accepting validated audio bytes and an idempotency key.
- Fix the recipient server-side.
- Authenticate internal requests with timestamp and replay protection.

Done when a real voice note reaches only the owner's staging chat.

### T16 — Exactly-once delivery and ambiguity recovery

Dependencies: T03, T05, T15

Independent security review required.

Red tests with real fault injection:

- definite pre-submission failure may retry.
- confirmed delivery cannot retry.
- timeout after possible submission becomes `delivery_unknown`.
- unknown delivery is reconciled before another attempt.
- restart at every delivery state cannot duplicate a voice note.
- retries reuse the same sentence and audio.

Implementation:

- Persist WAHA message identifiers and attempt records transactionally.
- Retry within the same date's 07:00-07:05 Pacific send window only when
  non-delivery is certain; never begin a new attempt at or after 07:05.
- Never retry blindly after an ambiguous result.
- Reconcile an ambiguous submission before any retry. If certainty is not
  restored before 07:05, retain `delivery_unknown` for operator review and do
  not catch it up on the next day.

Done when fault tests produce no duplicate sends. This blocks production release.

**Status note (2026-08-19):** WAHA/self-hosted WhatsApp-Web automation is confirmed dead (see
`docs/research/waha-alternatives.md`) — the account is blocked at WhatsApp's own server-side
device-linking layer, not by any tool bug. The owner approved migrating the sender to the
Telegram Bot API (`docs/superpowers/specs/2026-08-18-telegram-sender-design.md`). T16's
delivery-state-machine work above (schema, transactional attempt records, the crash/restart
orchestration in `delivery.py`) is transport-agnostic and is kept as-is. Only the WAHA-specific
transport (reconciliation-by-chat-history-scraping, the WAHA HTTP calls) is superseded — by T16b,
below.

### T16b — Telegram sender migration

Dependencies: T15, T16 (kept: the delivery-state-machine; superseded: the WAHA-specific transport
and reconciliation subsystem)

Independent security review required (same mandatory-review posture as T15/T16 — this task
touches secrets and the locked sender boundary).

Full design: `docs/superpowers/specs/2026-08-18-telegram-sender-design.md`. Detailed task-by-task
plan: `docs/superpowers/plans/2026-08-19-t16b-telegram-sender-migration.md`.

Red tests:

- a valid signed local-trigger request produces a real Telegram voice-note send.
- `send_voice_note` has no recipient-shaped parameter (structural, unchanged from T15).
- pre-network checks (signature, replay, audio validation) reject before any Telegram call is made.
- a Telegram 4xx response (400/401/403/404/429) maps to `SenderRejected`.
- a network failure with no HTTP response received at all maps to `SenderAmbiguous`.
- an ambiguous outcome never retries the same Pacific day (`delivery.py`'s `DELIVERY_UNKNOWN`
  branch is terminal-for-the-day, not auto-resolved).
- recipient enrollment captures the first inbound message's `chat_id` and refuses to overwrite an
  already-enrolled recipient.
- `discovery`/`generation`/`judging` still cannot reach the sender, the bot token, or the enrolled
  `chat_id` (security AST boundary test, extended).

Implementation:

- Rewrite `sender.py`'s `send_voice_note` to call Telegram's `sendVoice`; delete the
  reconciliation subsystem (`reconcile_delivery`, `_fetch_matching_provider_id`,
  `_find_matching_provider_id`, `_no_match_outcome`, and their WAHA-chat-history constants)
  outright — Telegram's Bot API has no chat-history-read method for bots, so there is nothing to
  reconcile against.
- Add `recipient_enrollment.py` (`enroll_recipient`), modeled on T13's `enroll_voice` one-time,
  file-based, immutable-once-written trust pattern.
- Replace `Settings`' WAHA fields (`waha_base_url`, `waha_token`, `waha_session`, `recipient`)
  with `telegram_bot_token` and `telegram_chat_id`.
- Simplify `delivery.py`'s `DELIVERY_UNKNOWN` branch: remove the reconciliation call, make the
  state terminal for the Pacific day (surfaced for the owner, per the "never carry a missed send
  into the next Pacific day" rule).
- Remove the WAHA service from `docker-compose.yml` entirely — no browser-automation container,
  no session volume, no QR pairing.

Done when a real signed local-trigger request produces a real Telegram voice note in the owner's
own test chat, no duplicate sends are possible (traced statically through every delivery state,
plus a real fault-injection test proving `DELIVERY_UNKNOWN` never auto-retries), and the
independent review is clean.

### T17 — Recipient consent, STOP, and kill switch (Telegram)

Dependencies: T15, T16, T16b

Independent security review required.

**Rewritten 2026-08-19** for Telegram's actual mechanics — see
`docs/superpowers/specs/2026-08-18-telegram-sender-design.md`'s "Inbound handling" section. The
original WAHA-shaped version of this task (inbound WhatsApp messages) is superseded; nothing from
it carries forward except the underlying requirement (exact STOP, kill switch, restart-durable).

Red tests using real inbound Telegram messages:

- exact `STOP` from the enrolled `telegram_chat_id` disables sending durably.
- `STOP` from any other chat id has no effect (never enrolled, never checked — matches the
  existing "other inbound messages are ignored" rule).
- other replies never invoke the discovery agent.
- disabled state survives restart.
- the administrator kill switch stops a reserved send.
- a `403 Forbidden: bot was blocked by the user` at send time also durably disables sending —
  Telegram's only proactive block signal, necessarily reactive (learned only by attempting a
  send), not queryable in advance.

Implementation:

- Poll Telegram's `getUpdates` at low frequency (once during the daily send window is sufficient
  at this volume) with a durably-stored `offset` cursor, inspecting only messages from the
  enrolled `telegram_chat_id`. This is an outbound HTTPS call — no inbound port opens anywhere,
  `AGENTS.md` §Network and container rules is satisfied unchanged.
- Process only the exact opt-out command from the enrolled chat id; ignore everything else.
- Add a durable global sending flag and audited re-enable procedure.
- Treat a `403 bot was blocked` response from `send_voice_note` as an additional durable stop
  signal, alongside exact STOP.

Done when opt-out, the blocked-by-user signal, and kill-switch behavior all survive restart and
cannot be bypassed. Plan this task in its own `writing-plans` session once T16b ships — do not plan
it now, per this project's one-task-at-a-time discipline.

### T18 — Cloud and container hardening

Dependencies: T06, T15, T17

Independent security review required.

Red tests:

- external port scan must find no application ports.
- containers cannot access the Docker socket.
- discovery cannot access WAHA, TTS, secrets, or private networks.
- discovery cannot reach private services through a deployment-specific NAT64
  Pref64.
- WAHA cannot access the crawler network.
- resource exhaustion terminates the bounded worker.
- reboot preserves only intended state.
- staging and production reject secret roots inside the repository, and the
  deployed secret files fail closed unless owned by the service administrator
  with the documented restrictive Unix modes.
- a full Git-history and built-image scan finds no recipient data, credentials,
  voice samples or embeddings, WAHA sessions, or private keys.

Implementation:

- Deny inbound traffic except WireGuard.
- Allow key-only SSH through the VPN and disable password/root login.
- Run containers non-root where supported.
- Drop capabilities, enable no-new-privileges, use read-only roots where possible, and set CPU, memory, and process limits.
- Separate discovery, model, voice, and sender networks and volumes.
- Disable NAT64 on discovery networks or supply and test the deployment Pref64,
  with egress controls that block discovery access to private services.
- Pin container digests. For managed model APIs, pin the provider, stable model
  ID, API contract, and client version, and requalify before changing them.
- Mount deployed secrets outside the application checkout, validate their
  ownership and modes at startup, and expose each only to its required service.
- Scan the complete Git history and built images for sensitive artifacts before
  staging and production release.

Done when the security suite and external exposure scan pass.

### T19 — Audit, alerts, backups, and recovery

Dependencies: T03, T12, T18

Red tests:

- logs contain no protected data or raw scraped text.
- forced permanent failure alerts the owner.
- encrypted backup restores into a fresh real SQLite database.
- restored history preserves idempotency.
- optional GitHub export excludes phone, voice, session, and secret fields.
- disk and queue exhaustion alert safely.

Implementation:

- Add structured redacted logs with rotation.
- Add owner alerts for queue exhaustion, session loss, missed delivery, disk pressure, and service failure.
- Encrypt daily backups with an external recovery key.
- Perform a documented restore drill.

Done when a fresh environment can restore safely and continue without duplicate delivery.

### T20 — Seven-day staging soak and production cutover

Dependencies: all previous tasks

Execution:

1. Run the complete cloud system against the owner's test chat.
2. Operate for seven consecutive Pacific calendar days.
3. Exercise a restart, discovery failure, TTS failure, and controlled WhatsApp/network failure.
4. Verify messages, voice quality, uniqueness, audit records, alerts, and backups.
5. Obtain recipient consent and confirm STOP behavior.
6. Replace the staging allowlist with the girlfriend's number.
7. Lock production configuration and enable the 07:00 schedule.
8. Rerun the frozen T10/T11 Promptfoo suites with caching disabled against the
   exact release pins.

Production release gate:

- seven consecutive days without a duplicate or unexplained missed send
- correct 07:00 Pacific execution across tested DST boundaries
- successful kill switch and STOP tests
- successful restore drill
- no exposed application ports
- no leaked secrets
- no unresolved high-severity dependency or image findings
- all safety, originality, SSRF, prompt-injection, audio, and delivery suites green
- no failed or skipped required Promptfoo release gate

## 10. Task dependency summary

```text
T00 -> T01 -> T02 -> T03 -> T04 -> T07 -> T08 -> T09 -> T10 -> T11 -> T12
T02 -----------------------> T06 -> T07
T03 -----------------------> T05 -------------------------------> T16
T02 -> T13 -> T14 -> T15 -> T16 -> T17 -> T18 -> T19 -> T20
T12 -----------------------> T14
T03 -----------------------> T16
```

Milestones:

1. Foundation: T00–T05
2. Secure discovery and selection: T06–T12
3. Voice and real delivery: T13–T17
4. Cloud hardening and recovery: T18–T19
5. Production qualification: T20

## 11. Security guardrail checklist

- [ ] Dedicated sending number
- [ ] One fixed recipient allowlist
- [ ] Maximum one send per Pacific date
- [ ] Recipient consent and exact STOP command
- [ ] Administrator kill switch
- [ ] No arbitrary URL fetches
- [ ] Redirect and DNS revalidation
- [ ] No crawler access to private networks or cloud metadata
- [ ] No agent access to filesystem, shell, secrets, voice, TTS, or WhatsApp
- [ ] Deterministic validation remains authoritative
- [ ] No copying of uncertain lyrics or quotes
- [ ] Raw scraped creative text discarded after comparison
- [ ] Raw enrollment recording deleted after embedding export
- [ ] Temporary generated audio deleted after delivery or bounded failure retention
- [ ] No public WAHA, model, database, or administration port
- [ ] Secrets absent from Git, images, logs, and command arguments
- [ ] Encrypted backups and tested recovery
- [ ] Pinned dependencies and images; pinned provider/model/API/client with
      requalification for managed inference
- [ ] Secret, dependency, and image scans in CI
- [ ] Ambiguous delivery never retried blindly

## 12. Project-wide definition of done

The project is complete only when:

1. The production system runs without the owner's computer.
2. Exactly one original English romantic voice note is sent at 07:00 Pacific daily.
3. The voice note uses the verified owner-authorized voice embedding.
4. The recipient is allowlisted, informed, and able to stop delivery.
5. Thirty date simulations and live fault tests show no duplicate sends.
6. One hundred hostile web and prompt-injection cases cannot cross trust boundaries.
7. The prohibited-content corpus is rejected and safe content meets the documented quality threshold.
8. Audio validation rejects silence, clipping, corruption, and invalid encoding.
9. Seven cloud-hosted staging days pass before production cutover.
10. Backup recovery, alerts, kill switch, and session-loss handling are demonstrated.
11. All tests use real implementations or real protocol endpoints; no mocks are introduced.
12. Repository history contains no secrets or private voice/session artifacts.

## 13. Immediate next action

Begin T12 (approved queue and safe reserve) from the audited T01-T11
foundation. T11 leaves `judging/pipeline.py`'s `evaluate_message_safety`
producing a `SafetyDecision` per candidate sentence; T12 turns approved
decisions into a durably persisted queue against the existing SQLite
`Database` boundary, maintaining at least 30 approved messages plus a small
safe reserve, with atomic reservation and queue-health alerts. Red tests:
discovery failure preserves the existing queue, rejected candidates never
enter the queue, queue refill cannot modify already-sent records,
exhaustion selects only from the pre-approved reserve, and no reserve means
no send -- keep the same fail-closed posture T11 established (an empty or
exhausted reserve is a skipped send, never a fallback to an unapproved
sentence).
