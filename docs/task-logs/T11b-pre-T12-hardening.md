# T11b - Pre-T12 review and hardening pass

## Status

Complete on `task/T11-safety-gates-and-judge` (PR #12 for T11 itself was
already merged to `main` at `ef6d57b` before this pass started; this
addendum lands as its own PR from the same branch, per the lead's explicit
direction, rather than reopening the merged PR).

## Background

Before starting T12 ("Approved queue and safe reserve"), the lead requested
a full pre-T12 review: confirm T12's stated dependencies (T03, T11) against
the plan's own dependency list, re-verify T03's schema/state-machine is
genuinely ready to build on, run `/simplify` and `/security-review` against
the T11 diff, and run a light parallel-agent sweep scoped to the
queue/state-machine/message-schema surface T12 will touch.

## Dependency and T03 re-verification

`IMPLEMENTATION_PLAN.md`'s T12 section (`### T12 — Approved queue and safe
reserve`) states `Dependencies: T03, T11`, matching `AGENTS.md`'s status
paragraph.

T03 (`src/personal_voice_msg/database.py`) was re-read in full alongside
`tests/fast/test_delivery_state_machine.py` (14 tests, all passing). The
transition tables (`CONTENT_TRANSITIONS`, `DELIVERY_TRANSITIONS`),
`reserve_next_message()`'s atomic `BEGIN IMMEDIATE` reservation, and the
30-date concurrent-reservation test all hold under direct re-reading, not
just the T03 task log. T03 is genuinely ready for T12 to build on.

## Fixes implemented (`/simplify`, applied directly)

Four parallel review agents (reuse, simplification, efficiency, altitude)
swept the T11 diff (`src/personal_voice_msg/judging/{gates,judge,pipeline}.py`
and tests). Two real, behavior-preserving efficiency fixes were applied
after verifying no test asserted the old behavior:

### 1. `judging/gates.py` - phrase patterns recompiled on every call

`evaluate_gates()` called `_phrase_pattern(phrase)` - which compiles a new
`re.Pattern` - for every one of ~95 phrases across 10 categories, on every
single invocation, even though the phrase lists are static module
constants. Fixed by precomputing `_COMPILED_PHRASE_CATEGORIES` once at
import time; `evaluate_gates()` now only does `pattern.search()` in its
loop. No change to matching behavior - confirmed by the full
`test_judging_gates.py` suite passing unchanged.

### 2. `judging/judge.py` - risk-flag vocabulary re-joined on every call

`build_judge_prompt()` did `", ".join(sorted(RISK_FLAG_VOCABULARY))` on
every call even though `RISK_FLAG_VOCABULARY` is a static frozenset. Hoisted
to a module-level `_RISK_FLAG_LIST` constant computed once. Low absolute
cost (the call is swamped by the network round-trip in `judge_sentence`),
but free to eliminate.

### 3. `tests/fast/test_judging_gates.py` - parallel `ids=[...]` list

`test_rejects_each_prohibited_category`'s `pytest.mark.parametrize` used a
separately-maintained `ids=[...]` list that positionally duplicated most of
the `expected_category` values already in the parameter tuples - an
easy-to-desync pattern. Rewrote each case as `pytest.param(..., id=...)`
inline; test IDs are unchanged (verified by running with `-v` and diffing
the printed IDs against the original list).

## Findings reviewed and rejected (no code change)

### 4. `judging/pipeline.py` - three equal safety-floor constants

The simplification agent flagged `SAFE_TONE_FLOOR`, `SAFE_WARMTH_FLOOR`,
`SAFE_NATURALNESS_FLOOR` (all currently `6.5`) as a duplicated literal that
could collapse to one `SAFE_SCORE_FLOOR` constant.

Verification: these are three **independently calibrated** floors that
happen to share a value after the T11 calibration run recorded in
`docs/task-logs/T11.md` ("final calibrated floors `SAFE_TONE_FLOOR =
SAFE_WARMTH_FLOOR = SAFE_NATURALNESS_FLOOR = 6.5`" - the equality is a
calibration *result*, not a code-structure choice). A future recalibration
could legitimately set them to different values per metric. Collapsing to
one shared constant would remove that degree of freedom silently. This is
the surface-pattern-matching failure mode `CLAUDE.md` warns about - three
equal literals that read as duplication but encode a real, documented
design intent. No action taken.

### 5. `judging/gates.py` - `stranger_name` and `!!`-count as bolted-on special cases

The altitude agent correctly observed that 10 of 11 gate categories share
one mechanism (`_PHRASE_CATEGORIES` iterated by one loop), while
`stranger_name` (token-set membership) and excessive-`!!` (punctuation
counting, with a fabricated `matched_phrase="!!"` that was never actually
matched by regex) are hand-rolled outside that loop.

This is a legitimate, real observation - `matched_phrase`'s meaning is
inconsistent across the three code paths. It was deliberately **not**
fixed in this pass: unifying it would mean restructuring
`evaluate_gates()`'s matcher shape (e.g. a `(category, matcher)` list
supporting multiple matcher kinds) in a safety-critical, freshly-calibrated
gate function, for a purely cosmetic benefit (nothing downstream reads
`matched_phrase` for a decision - only `category` drives
`evaluate_message_safety`). Restructuring risk right before T12 starts
exercising this code under real load outweighs the benefit. Flagged here
for a future pass, not fixed now.

## Security review (`/security-review`, scoped to T11's new LLM trust boundary)

Focused review of `judging/judge.py`'s `build_judge_prompt()` (which embeds
untrusted, LLM-generated candidate sentence text into the judge prompt),
`judging/gates.py`, and `judging/pipeline.py`. Checked prompt-injection
escape, fail-open error paths, whether judge output can ever set approval
state directly, and secret handling.

**No findings survived verification.** Specifically: the `"""`-escaping in
`build_judge_prompt()` is a textual cue, not a parser boundary - the actual
protections are the explicit data-framing instruction, Gemini's
schema-constrained JSON response, and that `decide_from_judge_result()`
only ever does plain comparisons on the judge's own structured output, never
re-interpreting anything from the candidate sentence. Every parsing failure
mode in `_parse_judge_result()` (missing keys, non-numeric/bool scores,
out-of-range scores including NaN/Infinity, unknown risk flags, non-string
reasons) raises `JudgeError`, and `evaluate_message_safety()`'s only
`except JudgeError` clause always sets `approved=False`. No path defaults
to `approved=True` on error. `SensitiveValue`'s `__str__`/`__repr__`
redaction prevents the API key from leaking into any exception message.

## Parallel sweep (scoped to queue/state-machine/message-schema surface)

Three parallel agents swept `database.py`, `history.py`, and
`judging/pipeline.py`'s wiring to `database.py`, specifically for anything
that would undermine T12. All three independently converged on the same
finding:

**There is no `REJECTED` (or equivalent) message state, and no
orchestration code exists yet connecting `history.evaluate_and_record` ->
`judging.evaluate_message_safety` -> `database.transition_message`.**
`MessageState`'s `CONTENT_TRANSITIONS` only defines
`DISCOVERED -> VALIDATED -> APPROVED -> QUEUED`; there is no legal
transition for a message that `evaluate_message_safety` rejects. A
safety-rejected message can only be left sitting in `DISCOVERED`/`VALIDATED`
state forever, indistinguishable from "not yet judged" - a future
queue-refill loop would need either a new state (`+ schema migration`) or
another rejection-tracking mechanism to avoid re-submitting the same text to
the paid judge API on every refill pass.

This is **not a T03 or T11 bug** - `IMPLEMENTATION_PLAN.md`'s original T03
section never planned a rejection branch, and T11's job was scoring, not
state transitions. It is squarely **T12's own design decision** (T12's red
test "rejected candidates never enter the queue" is currently satisfied
trivially, by construction, only because nothing yet calls
`transition_message` on a rejected candidate). Recorded here, not fixed,
per the instruction not to touch T12 scope.

A secondary naming note for whoever designs T12: `MessageState.RESERVED`
already means "atomically picked for today's specific delivery attempt"
(set by `reserve_next_message`). T12's plan language ("safe reserve",
"pre-approved reserve") describes a different concept - a policy-level
buffer of backup approved-but-unsent messages. Reusing the word "reserve"
for both would be confusing; a distinct name is recommended when T12 designs
the reserve-pool representation.

No other gaps, duplication, or security issues were found in the swept
surface.

## Verification

```text
uv run pytest tests/fast/test_judging_gates.py tests/fast/test_judging_judge_parsing.py \
    tests/fast/test_judging_pipeline_decision.py tests/fast/test_judging_pipeline_short_circuit.py -v -m fast
34 passed

uv run pytest -m fast
415 passed, 113 deselected

uv run pytest -m security
61 passed, 31 skipped (isolated Docker-only cases), 436 deselected

uv run mypy src
Success: no issues found in 19 source files

uv run ruff check .
All checks passed!

uv run python scripts/repository_policy.py all --root .
Exit code 0
```

## Next step

Proceed to T12 ("Approved queue and safe reserve") per
`IMPLEMENTATION_PLAN.md`. Before writing T12's red tests, resolve the two
open design questions recorded above: (1) how a safety-rejected message is
represented in the state machine (new `MessageState` + migration, vs. an
alternative rejection-tracking mechanism), and (2) what to call the
"safe reserve" pool so it does not collide with the existing
`MessageState.RESERVED` delivery-reservation meaning.

## Addendum: both open questions resolved (2026-08-06)

A follow-up brainstorming discussion (`superpowers:brainstorming`) resolved
both open questions before T12 implementation started. Full decisions and
their rationale are recorded directly in `IMPLEMENTATION_PLAN.md`'s T12
section (its new "Pre-T12 decisions" block); summarized here for this log's
own record:

- **Rejection representation**: a new additive `message_rejections` table
  (`SCHEMA_V5`), not a new `MessageState` value. The state-machine-purity
  alternative (`MessageState.REJECTED` via a `CHECK`-constraint table
  rebuild) was considered and explicitly rejected: `messages` is
  referenced by three other tables' foreign keys plus an FTS5 shadow table
  and an immutability trigger, making a rebuild meaningfully riskier than a
  side table for a requirement ("don't re-judge a rejected message") a
  `NOT EXISTS` subquery satisfies completely. This also matches
  `CLAUDE.md`'s Karpathy no-speculative-complexity rule more directly than
  the rebuild path would have.
- **Reserve naming**: "reserve buffer", replacing "safe reserve"
  throughout the plan. The actual buffer mechanism (column/flag vs.
  computed threshold) is intentionally left open for T12's own
  red-test-writing step.
- A real bug this discussion surfaced — `EXPECTED_SCHEMA_V1_OBJECTS`
  deriving its expected `messages` CHECK text from the live `MessageState`
  enum instead of a frozen historical literal, which would silently break
  `_validate_schema` for every existing database the moment a future task
  adds a new `MessageState` member — is tracked in the plan's T12 section
  but deliberately left unfixed here, since T12's chosen design no longer
  triggers it. Fix it in whichever future task actually grows
  `MessageState`.
