# Task Completion Checklist

Use this before committing task work. It's a quick gate ensuring you don't miss verification steps.

## Pre-Commit Gate

- [ ] All red tests from IMPLEMENTATION_PLAN.md pass (with evidence in session)
- [ ] All tests in `-m fast` and `-m security` pass (run: `uv run pytest -m fast -m security`)
- [ ] All lint/type checks pass (run: `uv run ruff check . && uv run mypy src`)
- [ ] Task log created at `docs/task-logs/TXX.md` with evidence
- [ ] No unrelated changes included (scope strictly matches IMPLEMENTATION_PLAN.md)
- [ ] Branch name follows format: `task/TXX-short-name`

### For Safety-Sensitive Tasks (T06, T15, T16, T17, T18)
- [ ] Independent reviewer signed off on findings
- [ ] Safety gates present in code (fail-closed, not pass-through)
- [ ] No secrets in code, logs, or task prompt
- [ ] Trust boundaries documented in task log

### After Implementation
- [ ] Run task-reviewer subagent: `use task-reviewer subagent to verify this task against the spec`
- [ ] Address any reported correctness gaps (ignore style preferences)

## Commit & PR

- [ ] Message format: `TXX: concise verified outcome` (e.g., `T10: 100% generation compliance with Gemini pinned`)
- [ ] Create PR: `gh pr create`
- [ ] Merge: `gh pr merge --merge --delete-branch`
- [ ] Don't merge locally — use GitHub PR flow to match established history

## Common Mistakes (Don't Do)

❌ "Tests pass locally, I'll merge and see if CI catches issues"  
✅ Run `-m fast` and `-m security` before committing

❌ "I tested the happy path, edge cases are probably fine"  
✅ Subagent reviewer will catch edge case gaps

❌ "I'll refactor this unrelated code while I'm here"  
✅ One task = one change = one commit. Refactoring is a separate task.

❌ "Tests pass, but I'm not sure what they're testing"  
✅ Task logs should have evidence (command run, output, interpretation)

❌ "This is a small change, I'll skip the task reviewer"  
✅ Reviewer catches what you miss. Always run it.

## Example (T12: Approved Queue & Safe Reserve)

✅ Red tests from plan: `test_refill_queue_reaches_min_size`, `test_reserve_buffer_blocks_send`  
✅ Full suite: `pytest -m fast && pytest -m security` → all pass  
✅ Task log: `docs/task-logs/T12.md` with full Promptfoo evidence  
✅ Subagent review: "Clear. All requirements implemented, no unrelated changes."  
✅ Commit: `T12: approved queue and safe reserve buffer implementation`  
✅ Merged via `gh pr merge --merge`

Result: Feature shipped, evidence preserved, confidence high.
