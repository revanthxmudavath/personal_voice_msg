# T08b - Pre-T09 hardening pass

## Status

Complete on `task/T08b-pre-t09-hardening`, branched from `origin/main` at
`3e812f9` (T09 complete and not retained; T01/T04/T06 audit remediation
merged).

## Background

Before starting T09's successor work, the lead requested a full-repository
understanding pass plus a parallel-agent sweep for inefficiencies in the
audited T01-T08 foundation. Three Explore agents mapped the codebase; five
parallel investigation agents then swept for code duplication, database
performance, security validation gaps, test-coverage gaps, and dead code.
The sweep proposed six fixes. Before implementing any of them, each was
re-verified directly against current source, because the exploration agents
had examined the repository before an intervening `git pull` landed 11
commits (including T09 completion and T01/T04/T06 remediation) that touched
several of the targeted files.

Verification found that **four of the six proposed fixes were false
positives** - either already resolved by existing code the agents had
misread, or would introduce a real regression if implemented. Only two
fixes were confirmed real and implemented here.

## Fixes implemented

### 1. `config.py` - RecursionError on deeply nested recipient JSON

`_recipient()` called `json.loads()` on the recipient secret file without
catching `RecursionError`. A deeply nested JSON structure
(`"[" * 100_000 + "]" * 100_000`) crashed the process with an uncaught
`RecursionError` instead of failing closed with `ConfigurationError`,
inconsistent with the existing two-layer defense already present in
`discovery/baseline.py` (`_json_structure_is_bounded()` pre-check plus an
`except (ValueError, RecursionError)` catch) for the same underlying
CPython JSON-decoder behavior.

Fix: added `RecursionError` to the caught exception tuple in `_recipient()`.
A dedicated bounded-depth pre-scan (as used in `baseline.py` for SearXNG's
more complex, deeply-nestable response shape) was not added, because the
recipient schema is a flat two-key object with no legitimate nesting at
all; catching the exception at the one call site is the minimal correct
fix for this shape.

Red evidence: `test_deeply_nested_recipient_json_fails_closed` failed with
`RecursionError: maximum recursion depth exceeded while decoding a JSON
array from a unicode string` propagating out of `load_settings()`.

Green evidence: same test passes; `ConfigurationError` is raised instead.

### 2. `config.py` - unbounded WAHA token file read

`_token()` read the entire token file into memory with `read_text()` before
any size check, only validating non-emptiness afterward. An oversized file
(tested at 10,000,000 bytes) could not be rejected until it was already
fully loaded, a denial-of-service vector via file size with no legitimate
justification (real WAHA tokens are short strings).

Fix: added `MAX_WAHA_TOKEN_CHARACTERS = 4_096` (consistent in scale with
existing `MAX_*_CHARACTERS` bounds elsewhere in the codebase, e.g.
`MAX_URL_CHARACTERS = 2_048` in `discovery/web.py`) and check
`path.stat().st_size` before calling `read_text()`, so an oversized file is
rejected without being read into memory.

Red evidence: `test_oversized_waha_token_file_fails_closed` failed with
`Failed: DID NOT RAISE ConfigurationError`.

Green evidence: same test passes; `ConfigurationError` is raised before the
file is read.

## Findings reviewed and rejected (no code change)

Each of these was part of the original six-item sweep. All four are
recorded here so they are not re-investigated by a future audit without
first reading this reasoning.

### 3. `database.py:645-726` message/delivery reservation "desync"

Two independent agents (a security-validation sweep and a test-coverage
sweep) both flagged `reserve_next_message()` for a claimed risk: if the
delivery `INSERT` fails on its `UNIQUE(recipient_key, pacific_date)`
constraint, the preceding message `UPDATE` to `RESERVED` would supposedly
persist, orphaning the message.

Verification: `reserve_next_message()` runs entirely inside
`self._transaction()`, which issues `BEGIN IMMEDIATE`, and rolls back on
**any** exception (`except BaseException: connection.rollback(); raise`,
`database.py:351-361`). A failure on the delivery insert propagates through
the `with` block and is rolled back along with the message update. No
desync is possible. Additionally, `reserve_next_message()` already checks
for an existing delivery for the same `(recipient_key, pacific_date)`
*inside the same transaction* before attempting the insert, and `BEGIN
IMMEDIATE` holds the write lock from the start of the transaction, so the
insert cannot actually violate that constraint via a concurrent writer in
this code path. Both agents reasoned about the helper method in isolation
without tracing how the enclosing transaction context manager handles
exceptions.

No action needed.

### 4. Bounded-string-validation "duplication" across `inspiration.py`,
`baseline.py`, `web.py`

The duplication-scan agent reported `_is_bounded_single_line()`
(`discovery/inspiration.py`) as re-implemented in `discovery/baseline.py`
and `discovery/web.py`, recommending it be exported and shared.

Verification: the three functions solve different problems and are not
duplicates:

- `inspiration.py._is_bounded_single_line()` is a strict boolean validator
  for single-line `InspirationCard` fields (theme/emotion/source URL):
  reject on any control character (Unicode category `C*`), no
  transformation.
- `baseline.py`'s check (inline in `analyze_fetched_page`) rejects control
  characters (`Cc` only) in *multi-line extracted page text*, explicitly
  allowing `\n` and `\t` - a different character-class policy for a
  different (multi-line) shape.
- `web.py._sanitize_metadata()` is a *sanitizing transformer* (strips HTML,
  NFKC-normalizes, replaces whitespace/control/format characters, then
  truncates) for raw search-result titles/snippets - it never raises, it
  always returns a cleaned value.

Unifying these into one shared utility would either lose the deliberate
per-context behavior differences (reject-vs-sanitize, single-line-vs-multi-
line, `Cc`-vs-`C*`-vs-`Cc+Cf`) or require enough parameterization to
reproduce three distinct behaviors, which is the speculative abstraction
the project's Karpathy rules direct against.

No action needed.

### 5. `history.py` FTS5 pre-filter for fuzzy-match performance

The database-performance sweep found `_evaluate_with_connection()` still
fetches every row of `messages` and runs `normalize_text()` +
`fuzz.token_sort_ratio()` against each one (confirmed still true; the
intervening pull only renamed two method calls, not this logic). The
originally proposed fix was an FTS5 `MATCH` query against the existing
`message_history_fts` index to shortlist candidates sharing tokens with the
normalized candidate, before running RapidFuzz only over the shortlist.

Verification against the existing test suite found this fix unsafe before
it was written: `tests/fast/test_message_history.py::
test_curated_near_duplicate_paraphrases_are_rejected[typo-obfuscated]`
pairs `"Your kindness makes every morning brighter."` with
`"Y0ur kindnes makez evry mornng brightr."` - a pair that shares **zero**
literal tokens by design, specifically to prove that character-level
fuzzy matching catches obfuscated near-duplicates that word-based matching
would miss. Any FTS token-overlap shortlist, however tuned, would never
include this row in its candidate set, because token-based indexing and
character-level fuzzy matching are answering different questions. Gating
the shortlist behind a corpus-size threshold would not fix this: it would
make the duplicate-detection guarantee silently weaker exactly once the
message history is large, which is exactly when weaker detection matters
most.

A behavior-preserving alternative exists (cache each message's
pre-normalized text in a new column, populated once at write time, so
`normalize_text()` is not recomputed on every row on every call - this
removes real repeated Unicode-category-scan cost without touching matching
semantics at all), but it requires a schema migration
(`SCHEMA_V5` + backfill, following the `V1`-`V4` pattern in `database.py`).

Weighed against actual project scale - this system sends at most one
message per Pacific date to one recipient (`AGENTS.md`, "Maximum one voice
note per recipient and Pacific calendar date"), so realistic message-table
growth is on the order of hundreds to low thousands of rows over multi-year
operation, not the 100k-1M scale the performance sweep's severity rating
assumed. At realistic scale the current full scan costs low tens of
milliseconds per `evaluate()` call, not seconds, for a once-daily batch
generation step with no interactive latency requirement.

Deferred, not fixed: implementing the unsafe FTS shortlist would reopen a
duplicate-detection bypass; implementing the safe schema-migration
alternative now would be speculative, future-facing work for a scale this
project is not currently near, which the project's own development rules
direct against. Revisit if real message-history growth approaches a scale
where the full scan is measurably slow in practice, and implement the
normalized-text-caching approach at that time, not the FTS shortlist.

### 6. `web.py` scheme-port mismatch and query/fragment hardening

The security sweep reported that `canonical_public_url()` accepts
mismatched scheme-port combinations (e.g. `http://host:443`) and silently
strips URL fragments without an audit trail.

Verification: `canonical_public_url()` (`discovery/web.py:209-249`) already
computes `expected_port = 80 if scheme == "http" else 443` and rejects any
URL whose explicit port does not match (`web.py:239-241`); this logic
predates the intervening pull and was unaffected by it. Separately,
dropping the URL fragment before constructing the canonical form
(`web.py:245`, fragment position set to `""`) is standard, correct HTTP
client behavior - fragments are a client-side-only construct and are never
sent to an origin server in a real HTTP request, so there is nothing to
audit-log; treating this as a security gap would be incorrect.

No action needed.

## Verification

```text
uv run pytest tests/fast/test_configuration.py -v
32 passed (30 pre-existing + 2 new: RecursionError guard, token size limit)

uv run pytest -m fast
356 passed, 107 deselected

uv run pytest -m security
61 passed, 31 skipped (isolated Docker-only cases), 371 deselected

uv run ruff check .
All checks passed!

uv run mypy src
Success: no issues found in 11 source files

uv run python scripts/repository_policy.py all --root .
Exit code 0

git diff --check --cached
No output (clean)
```

## Next step

Proceed to T10 (original English sentence generation) per
`IMPLEMENTATION_PLAN.md`. T09 is already complete and not retained
(`docs/task-logs/T09.md`); the deterministic T07 discovery baseline remains
the production discovery path feeding T10.
