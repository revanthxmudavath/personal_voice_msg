---
name: investigate-codebase
description: Structured codebase investigation that preserves main session context
---

# Investigate Codebase

When you need to understand a codebase section without filling your main session context, use this workflow:

## Workflow

1. **Scope narrowly**
   ```
   Investigate [module/file/function]: [specific question]
   
   Constraints:
   - Answer the question, don't explore tangents
   - Use graph tools first (detect_changes, query_graph, semantic_search)
   - Only read files needed to answer the question
   - Summarize findings as: [what], [why it matters], [1-2 next steps]
   ```

2. **Use subagents for fan-out exploration**
   ```
   Use subagents to investigate:
   - Flow A: [question about component X]
   - Flow B: [question about component Y]
   
   Report each subagent's findings separately.
   ```

3. **Graph-first pattern**
   - Instead of: grep imports, read files, trace by hand
   - Do this:
     ```bash
     # For impact analysis
     get_impact_radius on function_name
     get_affected_flows for this_change
     
     # For structure  
     get_architecture_overview
     query_graph pattern="tests_for"
     
     # For semantic search
     semantic_search_nodes "keyword or description"
     ```

## Example

```
Investigate voice_enrollment.py: How does validate_sample 
reject unsupported formats?

Use graph tools to find:
- What audio formats are supported (check constants/config)
- What validation happens before enrollment
- What tests cover edge cases (format, duration, silence)

Summarize: [what formats are supported], [which ones fail], 
[which tests validate this]
```

## Why This Works

- Graph tools = 5x cheaper than manual Grep
- Subagents = your main session stays clean
- Narrow scope = fast, focused answers
- Evidence-based = reviewer can verify the findings
