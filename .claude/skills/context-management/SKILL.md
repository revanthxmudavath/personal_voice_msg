---
name: context-management
description: Strategies for keeping context fresh and preventing degradation
---

# Context Management

Claude's context window is your most precious resource. This skill outlines when and how to reset context.

## When to Clear Context (`/clear`)

Use `/clear` between **unrelated tasks**. Examples:

- ✅ Finished T10 implementation → Moving to T11 security audit = **CLEAR**
- ✅ Finished task → Writing docs unrelated to next task = **CLEAR**
- ❌ Same task, iterating → Don't clear, you need the history

**Rule**: If the next task doesn't reference anything from the previous task, clear.

## When to Compact Context (`/compact`)

When working on **one task** but context gets noisy:

```
/compact "Preserve all code changes, test status, and safety verification results"
```

**Trigger**: Context approaches 50k tokens while still on the same task.

**Result**: Conversation history gets summarized, code state preserved, you keep working.

## When to Use `/goal` for Unattended Runs

For tasks you want Claude to finish without you watching:

```
/goal "All tests in -m fast pass AND all tests in -m security pass"
```

Then: `Implement [task description]. Run the goal check after each iteration until it passes.`

**Result**: Claude loops until both conditions hold. You come back to green code.

## Parallel Sessions (Worktrees)

For **truly independent** work:

```bash
claude --new-worktree task-name
# This clones the repo to a worktree, you get a fresh context
```

**When to use**: T15 (security review) happening while T16 (new feature) is being written.

Each session = independent context, no token contention.

## Status Line

Always know where you stand:

```
/statusline
```

Shows real-time context tokens. Helps you know when to compact.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Context fills, Claude forgets instructions | `/clear` between tasks |
| One task's context goes 100k+ | `/compact` mid-task to reset noise |
| Debugging + implementation mixed together | Start fresh session for implementation |
| Exploring codebase fills context | Use subagents instead (see investigate-codebase) |

## Example: T10 Implementation Session

```
Session start: 5k tokens (project CLAUDE.md + task plan)
├─ Read current code: +12k = 17k
├─ Write failing test: +8k = 25k
├─ Implement + iterate: +15k = 40k
├─ Final verification: +6k = 46k → Run /compact here
├─ After /compact: 12k (summary + final code state)
└─ Final tests run: +5k = 17k (COMPLETE, green)
```

Without compact: would hit 70k → performance degrades → mistakes creep in.
With compact: back to 17k → fresh context → ship with confidence.
