# Project Map Artifact — Design Spec

## Problem

The owner (solo developer) lost track of what this project actually does and
where it stands after roughly task two. The authoritative record
(`AGENTS.md`, `IMPLEMENTATION_PLAN.md`, `docs/task-logs/*.md`) is accurate but
entirely textual and spread across several long files — reconstructing "what
does this system do end-to-end," "which file does what," and "what's done vs.
next" means reading thousands of lines of prose. There is no visual entry
point into the project.

## Goal

One interactive, visual reference — inspired by Obsidian's graph view — that
lets the owner re-orient quickly: what the system does, how the files relate,
and how far the 20-task build has come. Not a replacement for `AGENTS.md`/
`IMPLEMENTATION_PLAN.md` as the detailed source of truth; a companion that
answers "what's happening" in under a minute, with a path into the detailed
docs for anyone who wants more.

## Format decision

No native "Claude Code sidebar" exists for this (confirmed by web research —
third-party plugins like Understand-Anything exist but are generic
auto-graph tools that don't know this project's actual story). Built instead
as a **published Artifact**: a single self-contained HTML page with a
permanent URL the owner reopens/bookmarks anytime, which gets redeployed to
the same URL as the project evolves. This is the persistence a chat-tied
widget (e.g. the `visualize` tool) can't offer.

## Structure: four tabs

A persistent tab bar, one panel visible at a time (not a long scroll — chosen
over the alternative specifically because it reads as one focused tool
rather than a scroll of three unrelated widgets, and scales better as any one
view grows richer later).

1. **Overview** (default landing tab) — the tab that directly answers "what's
   happening right now": current phase/task, a short recap of what just
   shipped, what's next, and jump-links into the other three tabs. This is
   the tab that most directly solves the owner's stated problem; the other
   three are for drilling in.
2. **Pipeline** — the real 7-stage runtime flow (Discovery → InspirationCard
   → Generation → Judging → Approved queue → Voice synthesis → Delivery),
   using real file paths, with the untrusted-web discovery stages visually
   walled off from the trusted application stages by a distinct boundary
   (not a plain arrow — this is a real trust boundary in the architecture,
   enforced by an isolated Docker network per T18). Clicking a stage opens an
   inline inspector drawer below the row (not a modal) with that stage's
   files, role, and any stage-specific rule (e.g. the 11 deterministic gates
   vs. the LLM judge in Judging) — the full pipeline stays visible while one
   stage is inspected.
3. **File graph** — the ~23 source files (excluding empty `__init__.py`
   files) as a force-directed node graph, laid out by actual call topology
   (not folder grouping) so shared modules like `database.py`/`config.py`
   naturally emerge as hub nodes. Colored by subpackage with a legend, sized
   by connection count, curved edges. Hover shows a tooltip (path, one-line
   purpose, connection count); click isolates a node's direct connections,
   dimming the rest; **drag any node and the rest of the graph visibly
   re-settles around it** (a real, live force response on release — this was
   the one explicit hard requirement from the owner, verified working: an
   initial draft only froze after a one-time layout, which was corrected and
   confirmed with a real drag-and-settle test before this spec was written).
4. **Task timeline** — T00–T20 as a collapsible vertical roadmap grouped into
   the plan's own five milestones (Foundation, Secure discovery & selection,
   Voice & real delivery, Cloud hardening & recovery, Production
   qualification), one continuous status-colored spine (done / next-up /
   not-started) with circular status nodes. T09 (the evaluated-and-rejected
   discovery-agent detour) and T15→T16b (the WAHA→Telegram pivot) are
   rendered as distinct story beats — a diamond "detour" node and a shared
   pivot badge with a cross-link between the two cards — rather than plain
   identical checkmarks, since that honesty about real detours/pivots was
   part of what made the mockup useful rather than a sanitized progress bar.

## Non-goals

- **Not live-synced.** This is a static published page with no backend and
  no ability to read the actual repository at request time. The file
  graph's nodes/edges, the pipeline's stage descriptions, and the task
  timeline's status are hand-authored data embedded in the page's own JS —
  the same way `AGENTS.md` is hand-maintained today, just visual.
- **Not a generic dependency-graph tool.** Deliberately scoped to this
  project's real ~23 files and 20 tasks, not an auto-scan of arbitrary
  repos.
- **Not a replacement for `AGENTS.md` / `IMPLEMENTATION_PLAN.md` /
  `docs/task-logs/*.md`.** Those remain the authoritative detailed record.
  This page is a faster orientation layer on top of them, and can link out
  to them (as text references, e.g. "see `docs/task-logs/T18.md`") rather
  than duplicate their prose.

## Maintenance model

Whenever a task or phase finishes, redeploy this same Artifact URL with
updated data — folded into the existing end-of-phase documentation habit
(the global CLAUDE.md "update-info" instruction), not a separate new chore.
Concretely: after finishing a task, update the Overview tab's current-phase
summary, add/adjust the Task timeline's status for that task, and add any
new files to the File graph if the task introduced them (T19 will add
audit/alerting/backup modules, for example).

## Architecture

- Single self-contained `.html` file, per Claude Artifact constraints: no
  external CDN scripts (D3/vis.js/etc. are out — the file graph's force
  layout is hand-rolled vanilla JS, already built and verified); Google
  Fonts stylesheet is the only external resource.
- CSS custom properties for the full palette, defined on bare `:root` for
  light, redefined under `prefers-color-scheme: dark` and
  `[data-theme="dark"]`, so it follows the viewer's theme correctly in both
  the "system" and explicit-choice cases.
- Tab switching is plain vanilla JS show/hide (one panel mounted/visible at
  a time) — no router, no framework.
- Each tab's interactive logic (the graph's physics/drag, the pipeline's
  inspector drawer, the timeline's collapse/expand and cross-links) is
  scoped under its own root class/data-attribute, carried over from the
  three validated mockups with minimal integration changes (mainly: wiring
  each into a tab-panel container instead of a standalone page section).
- No browser-storage persistence needed for v1 (no per-viewer state worth
  remembering across visits — theme already follows system prefs).

## Source material

Three research-and-mockup passes (each: real Dribbble/Behance screenshots
viewed live via browser automation, not just titles, then one drafted
mockup using this project's real files/tasks) produced the validated
starting point for each tab:

- File graph: `graph-view-mockup.html` / `graph-view-notes.md`
- Pipeline: `pipeline-mockup.html` / `pipeline-notes.md`
- Timeline: `timeline-mockup.html` / `timeline-notes.md`

(currently in the session scratchpad — the implementation plan will specify
where these get consolidated into the final single artifact file). All
three were spot-verified for self-containment (no forbidden external
scripts), theme-awareness, and no console errors before being shown to the
owner; the file graph's drag interaction was additionally hardened (a stuck
-drag edge case was found and fixed) and verified with a real simulated
drag-and-release that confirmed neighbor nodes actually reposition.

## Testing / verification approach

This is a static artifact with no application logic to unit-test in the
traditional sense. Verification means, per tab, before each publish:

- No console errors on load, in both a fresh tab and after a theme switch.
- File graph: drag at least one node and confirm neighbors visibly
  re-settle; hover and click-isolate both work; reset control restores full
  view.
- Pipeline: click each stage and confirm its inspector drawer shows correct,
  current content; confirm the trust-boundary treatment renders.
- Timeline: expand/collapse a phase; confirm the T09 detour and T15→T16b
  pivot cross-link both work; confirm status coloring matches the real
  current task state at publish time (this is the one part of the page most
  likely to drift if forgotten during a future update — the implementation
  plan should make this an explicit, easy single-place edit, not scattered
  across the file).
- Both light and dark themes checked visually (or via computed-style
  assertions) for every tab.

## Open items for the implementation plan

- Exact process for consolidating the three mockups plus a new Overview tab
  into one final artifact file (shared CSS token names need reconciling
  across the three, since each mockup defined its own `--bg`/`--text`/etc.
  independently).
- Where the "current phase" data for the Overview tab is sourced from at
  authoring time (read from `AGENTS.md`'s "Immediate next step" section by
  whoever redeploys, not computed automatically).
- Final favicon/title choice for the published Artifact (needed at publish
  time per Artifact requirements).
