# Pre-T18 reverification audit

## Status

Complete. This is an audit, not an implementation task — no T18 work was
started. Scope: confirm T17/T17b are genuinely complete and merged with
evidence, sweep T01-T16b for the same class of documentation drift the
user found at T17 (`AGENTS.md`/`docs/task-logs/T17.md` both saying "not
yet merged" after the real merge), run the full verification suite for
real, and give a go/no-go recommendation on starting T18.

## T17 / T17b: confirmed genuinely complete and merged

- `git log --oneline main | grep -i "T17\b"` and a merge-commit sweep both
  confirm PR #31 (T17, merge commit `f577caa`) and PR #32 (T17b, merge
  commit `18b2b03`) are in `main`'s real history, in the right order, with
  no unmerged commits ahead of them on this branch (`git status`/`git log`
  show this branch's HEAD is exactly `main`'s HEAD before this audit's own
  two doc commits).
- Read `docs/task-logs/T17.md` and `T17b.md` in full and checked their
  specific claims against current source, not just their prose:
  - `SCHEMA_V8`, the `DisableReason` enum, `recipient_key_for_chat_id`, and
    all five new `Database` methods (`is_sending_enabled`,
    `disable_sending`, `enable_sending`, `get_telegram_inbound_offset`,
    `set_telegram_inbound_offset`) exist in `database.py` as described.
  - `consent.py` has `TelegramPollError`, `_process_updates`, and
    `poll_inbound_stop` as described.
  - `sender.py` has `SenderBlocked`/`_is_blocked_by_user` as described.
  - `delivery.py` gates `run_daily_send` on `is_sending_enabled()`, handles
    `SenderBlocked`, and asserts `recipient_key_for_chat_id` as described.
  - `daily_send_entrypoint.py` and `scripts/run_daily_entrypoint.py` exist
    with `run_daily_entrypoint` as described; the AST trust-boundary test
    forbids `consent.py` and the three new `Database` control-method names
    as attribute names, as described.
  - All new/modified test files T17.md and T17b.md list as "Files changed"
    exist on disk.
- `IMPLEMENTATION_PLAN.md`'s `### T18` section reads `Dependencies: T06,
  T15, T17, T17b` — correct, matches the plan text quoted above.
- Both explicitly-acknowledged open items are still genuinely open, not
  silently closed or forgotten (confirmed by reading both task logs' own
  "Live verification" sections, which state this outcome plainly):
  1. `tests/integration/test_consent_integration.py::test_a_real_exact_stop_from_the_enrolled_chat_disables_sending_durably`
     has never been run against real Telegram — deliberately deferred,
     since it requires a real, already-sent STOP and would durably disable
     production sending if run for real. Independent backstops
     (blocked-by-user 403, admin kill switch) are both proven live in
     T17b's own verification.
  2. A real `scripts/run_daily_entrypoint.py` invocation during a genuine,
     unmodified 07:00-07:05 Pacific `DAILY_SEND` window has never
     happened. Three scheduled attempts missed the window on scheduler
     dispatch latency (not application error). A separate schedule-patched
     sandbox test proved the send-path mechanics work end-to-end but was
     explicitly and deliberately not accepted as satisfying this specific
     requirement (human-adjudicated, recorded in `docs/task-logs/T17b.md`).

## Real drift found and fixed (documentation-only)

Two stale-status items were found, both far older and larger than the
single line the user described at T17 — this branch's own doc corrections
were the first time either had been touched since T16 merged, three tasks
ago. Both are fixed directly in this commit; neither implicated code
behavior.

1. **`CLAUDE.md` §"Current architecture snapshot"** described T16 as "in
   progress ... not yet merged," with mid-implementation task-by-task
   detail (11/13 tasks, Task 12 in progress, WAHA logged out), and pointed
   at a `.superpowers/sdd/2026-08-09-t16-exactly-once-delivery/progress.md`
   ledger file that no longer exists (deleted at merge, per that skill's
   own convention — confirmed via `find`/`ls`). This was stale across
   **four** subsequent merges (T16 itself, T16b, T17, T17b) — last touched
   for status content at commit `cac16f6`, written mid-T16, and never
   updated again despite `CLAUDE.md`'s own stated design principle
   ("derived from `AGENTS.md`... never duplicates their content long
   enough to drift out of sync"). Fixed: replaced with a short paragraph
   that states current completion (T00-T17b) and points to `AGENTS.md`
   for detail, rather than re-embedding task-specific state that will
   drift the same way again.
2. **`docs/task-logs/T15.md`**'s own `## Status` line read "Design
   approved by owner on 2026-08-08. Implementation starting." and its
   `## Next step` line read "Branch/commit/PR/merge per CLAUDE.md
   §Per-task workflow." — both leftover from the pre-implementation draft,
   never updated even though the rest of the same document (350+ lines)
   fully documents completed implementation, a real double-verified WAHA
   send confirmed by the owner in chat, and a clean independent security
   review. Every other file that references T15 (`AGENTS.md`, `T16.md`,
   `T16b.md`, `T17.md`, `T17b.md`) already correctly described it as
   complete and merged — only T15's own status/next-step lines were wrong.
   Fixed: both lines now state the actual completion (commit `0fcb611`,
   PR #18, merge commit `d755e17`) with a correction note.

Both fixes are sourced directly from `git log`/`git show` on this branch,
are additive (nothing that was accurate was removed), and change no code.

## Investigated, not drift (confirmed accurate or deliberate)

- **`IMPLEMENTATION_PLAN.md` §10's dependency diagram/milestones** omit
  T16b and T17b (the ASCII chain still reads `T15 -> T16 -> T17 -> T18`;
  the milestone list still reads "Voice and real delivery: T13–T17").
  Confirmed this is exactly what the user described: a harmless
  simplification, not misleading. The diagram doesn't misstate the order
  or dependencies of any node it names — it's incomplete (missing two
  nodes), not wrong. The load-bearing dependency line is §9's per-task
  `Dependencies:` text, which is correct (`T18` correctly lists `T17b`).
  Left as-is; not worth a fix that only touches a supplementary diagram.
- **`docs/task-logs/T16.md`**'s own "Verification (Task 12, whole-branch,
  final)" section shows three green runs of
  `tests/e2e/test_delivery_fault_injection.py` against a real, live WAHA
  container — apparently contradicting the file's own `## Status` line
  ("has not yet been run green against a real, live session"). Read the
  full file (1257 lines) to resolve this: the three green runs predate
  Task 13's independent review, which found the passing assertions were
  tautological/false-positive (F1/F2/F6 in that section) and required a
  fix that was never re-verified against WAHA before the session went
  down for good. The file's own "Final verification (post-fix)" section,
  written after the fix, honestly states the suite "remains not yet run
  green against live WAHA" — matching the `## Status` line. Not a
  documentation bug: a correctly-sequenced narrative of a real bug the
  project's own review process caught, not left silently passing. Moot
  for current code regardless — `reconcile_delivery` and the WAHA-specific
  fault-injection tests it describes no longer exist; T16b deleted that
  subsystem outright (confirmed: `grep -rn "reconcile_delivery"
  src/personal_voice_msg/` finds nothing).
- **T00-T05 task logs** use an older format with no `## Status` header
  (`## Scope` / `## Red evidence` / `## Green evidence` instead). Not
  drift — this predates the `## Status` convention adopted from T06
  onward; none of them make a stale claim.
- A broader grep for "not yet merged"/"in progress"/"stale" across
  `AGENTS.md`, `IMPLEMENTATION_PLAN.md`, `CLAUDE.md`, and every task log
  found no other hits beyond the two fixed above and the two already
  self-corrected at T17/T17b's own merge time. The other "stale" hits
  (T02, T04, T06, T07) are unrelated technical usage — stale filters,
  stale hashes, stale DNS, a historical "stale task log" review finding
  from 2026-07 already resolved in the same file.
- `AGENTS.md` §Current status and blockers and §Immediate next step read
  as accurate and internally consistent throughout T01-T17b, including
  the T16→T16b WAHA-death narrative and the T17 self-correction already
  present before this audit started.

## Verification commands actually run

Run fresh in this session, on this branch (== `main` HEAD `18b2b03` plus
this audit's own two doc commits):

```text
$ uv sync --locked
(clean)

$ uv run pytest -m fast -q
554 passed, 1 failed, 181 deselected in 30.94s
  FAILED tests/fast/test_run_daily_entrypoint_script.py::
    test_settings_redactor_scrubs_a_real_audio_pipeline_error

$ uv run pytest -m integration -q
6 passed, 58 skipped, 672 deselected in 6.36s

$ uv run pytest -m security -q
66 passed, 52 skipped, 618 deselected in 5.35s

$ uv run ruff check .
All checks passed!

$ uv run mypy src
Success: no issues found in 27 source files

$ uv run python scripts/repository_policy.py all --root .
(clean, exit 0)
```

**The one `fast` failure is this sandbox's own network policy, not a code
defect.** `test_settings_redactor_scrubs_a_real_audio_pipeline_error`
calls the real Pocket TTS pipeline, which needs to fetch model metadata
from `huggingface.co` on a cold cache. This session's outbound proxy
explicitly denies that host (`curl "$HTTPS_PROXY/__agentproxy/status"`
shows `"connect_rejected"`/`"gateway answered 403 to CONNECT"` for
`huggingface.co:443`, and `~/.cache/huggingface/hub` does not exist in
this fresh container). Confirmed this is not a real regression by pulling
the actual GitHub Actions run for PR #32's `quality` job on this exact
commit, today: **555 passed, 181 deselected in 29.77s**, including that
exact test, with real network access. Every other number above (554 of
555 fast, integration, security, ruff, mypy, repository_policy) matches
what `docs/task-logs/T17b.md` and today's real CI run report exactly; the
`integration`/`security` skips are the same documented env-gating pattern
(no real Telegram bot token / voice sample in this sandbox) as T16b/T17/
T17b, not fabricated passes.

## Go/no-go on T18

**Go.** T17 and T17b are genuinely complete, merged, and independently
reviewed; the plan's own dependency line for T18 is correct
(`T06, T15, T17, T17b`); the full verification suite is green (modulo the
one explained-and-cross-verified sandbox network artifact above); and the
two documentation-drift items found were fixed directly in this commit,
both non-behavioral. The two explicitly-open live-verification items
(real STOP test, real in-window script run) remain open exactly as
`docs/task-logs/T17b.md` describes — neither is a new finding, neither
blocks T18 per the plan's own dependency line, and per `AGENTS.md`'s own
"Immediate next step," closing them is recommended alongside T18's start
("ideally together, live, in one sitting"), not a precondition for it.
