---
name: graph-first-exploration
description: Using code-review-graph tools efficiently instead of manual grep/read
---

# Graph-First Code Exploration

This project has `code-review-graph` MCP tools available. They're 5x cheaper in tokens and 10x faster than manual file reading.

## When to Use Graph Tools

Replace manual exploration with graph queries:

| Task | Don't Do | Do This |
|------|----------|---------|
| Understand impact of a change | Read 20 files, grep imports | `get_impact_radius` on the changed function |
| Find which tests cover a feature | Grep for test names | `query_graph pattern="tests_for" function_name` |
| Understand architecture | Read every file | `get_architecture_overview` |
| Find all callers of a function | grep -r and trace | `query_graph pattern="callers_of" function_name` |
| Detect what changed in a PR | Read entire diff | `detect_changes` (returns risk-scored analysis) |
| Find uses of an API | Multiple greps | `query_graph pattern="callees_of"` |

## Common Patterns

### Pattern 1: Understanding a Function's Impact

```
I need to refactor queue_refill.py:refill_queue(). What breaks if I change it?

→ Use: get_impact_radius on refill_queue
→ Returns: all callers, their dependencies, test coverage
→ Decision: Can I refactor safely? Are tests comprehensive?
```

### Pattern 2: Code Review (Fastest Path)

```
Review the diff for this PR. Are all edge cases covered?

→ Step 1: detect_changes (returns risk-scored findings)
→ Step 2: For high-risk changes, get_review_context to read snippets
→ Result: Complete review in 1/5 the tokens
```

### Pattern 3: Finding Patterns in Your Codebase

```
How do other modules handle audio validation?

→ Use: semantic_search_nodes "audio validation"
→ Returns: all functions with similar intent
→ Benefit: Copy existing patterns, maintain consistency
```

### Pattern 4: Architecture Questions

```
What's the dependency flow from voice_enrollment.py → database.py?

→ Use: get_architecture_overview
→ Then: query_graph pattern="imports_of" voice_enrollment.py
→ Result: Understand layers, see where refactoring risks
```

## Token Math

### Manual Way (Read + Grep)
- Read voice_enrollment.py: 8k tokens
- Read database.py: 12k tokens  
- Read config.py: 5k tokens
- Grep for dependencies: 3k
- Total: **28k tokens**

### Graph-First Way
- `get_impact_radius` on validate_sample: 2k
- `get_review_context` for snippets: 1k
- Total: **3k tokens** ✅ 9x cheaper!

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Grepping before checking graph | Ask "can the graph answer this?" first |
| Using graph for small, obvious searches | Fine — but worth checking graph first anyway |
| Forgetting graph exists | Pin this skill to your mental model |
| Using graph for things it can't do | Graph doesn't show *why* code exists, only structure |

## When Graph Can't Help

- "Why was this decision made?" → check git log, read AGENTS.md
- "What does this comment mean?" → you have to read it
- "Is this pattern idiomatic for our team?" → read CLAUDE.md + examples

Graph shines on: structure, relationships, test coverage, impact, dependencies.
