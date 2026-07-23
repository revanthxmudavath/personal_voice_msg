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

T01 through T06 are implemented and audited. T07 is the next backlog task.

Confirmed state as of 2026-07-23:

- Canonical implementation checkout: `F:\personal_voice_msg`.
- T01 through T06 are implemented with task logs and pushed branches; T06
  implementation commit `2e86b91` was merged by PR #4.
- The workspace can edit the repository and `git status` works.
- The repository has a credential-free HTTPS `origin` remote.
- GitHub CLI authentication for `revanthxmudavath` succeeds.
- Docker Desktop is running and `docker info` succeeds.
- Python 3.12.4, `uv`, Git, and Node 22 are installed.
- FFmpeg and ffprobe are not installed; they are required before T14.

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
- deterministic discovery baseline before any agent framework
- conditional restricted Hermes Agent benchmark, then LangChain `create_agent`
- smolagents `ToolCallingAgent` only if the preferred harnesses are incompatible
- small quantized open-weight model through `llama.cpp`
- Pocket TTS for authorized voice embedding and synthesis
- FFmpeg for OGG/Opus conversion and validation
- WAHA Core behind a narrow internal sender
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
- bypasses for SQLite, HTTP, `llama.cpp`, Pocket TTS, FFmpeg, or WAHA in integration tests

Use real implementations instead:

- temporary file-backed SQLite databases
- real ephemeral HTTP services and container networks
- real SearXNG and Trafilatura
- the real selected model through `llama.cpp`
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

Evaluate a restricted Hermes Agent harness first, then LangChain `create_agent`,
then smolagents `ToolCallingAgent`. A candidate may stay only if a 20-run
comparison shows at least 95% correct tool-call completion and materially
better valid unique candidate yield than deterministic searches. Hermes must
run without its built-in toolsets, memory, skills, plugins, gateways,
delegation, or code execution and expose exactly the four allowed discovery
tools. If no candidate passes, remove the framework. Do not use a
code-executing agent.

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
- Pin dependency versions, container digests, and model checksums.
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

## Immediate next step

Begin T07 from the audited T01-T06 foundation. Revalidate GitHub CLI
authentication and Docker access at the T07 boundary because those external
services can change independently of repository state.
