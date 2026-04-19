---
name: chat
description: Ad-hoc donace mode for small tasks. Use when the user invokes `/donace:chat` or asks for quick fixes, single reviewer passes, or focused work without the plan→execute ceremony. Handle tasks via direct Edit and subagent dispatch (Agent tool) — NEVER invoke the donace orchestrator. Escalate to `/donace:plan` when complexity crosses the thresholds listed below.
---

# Donace Chat

Ad-hoc mode. Main conversation handles everything directly. **Do NOT invoke any orchestrator command** — no runs, no dashboard, no `.ai/runs/` artifacts, no auto-commits.

This skill stays loaded for the session once invoked. You do not need to re-invoke it per turn.

## What you can do

### 1. Direct edits (fastest)

Use `Edit` / `Write` for:
- Typos, formatting, comments, docstrings
- Single-line config or value swaps
- Simple renames within one file
- Small refactors contained to one file

### 2. Subagent dispatch via `Agent` tool

Dispatch subagents directly — **NOT through `orchestrator.py`**. No run context, no dashboard URL. Subagents do one focused task and return.

| Need | `subagent_type` |
|---|---|
| TypeScript / React review | `typescript-reviewer` |
| iOS / Swift review | `ios-reviewer` |
| Run or write tests | `test-engineer` |
| QA / user-flow testing | `qa` |
| Update docs, CHANGELOG, knowledge cards | `documenter` |
| Focused coding task on known files | `implementer` |
| UI / UX design specs | `ui-designer` |

Each subagent receives a **self-contained prompt** — they see zero context from this conversation. Include file paths, line numbers, the exact change or question. Terse prompts produce shallow work.

**Parallel dispatch is fine** when tasks are independent (e.g., "review this AND run tests" → dispatch `typescript-reviewer` and `test-engineer` in one message with two `Agent` tool calls).

## What you must NOT do

| ❌ Forbidden | Why |
|---|---|
| `run_start`, `run_complete` | No runs in chat mode |
| `run_job`, `verify`, `review`, `document`, `mark` | Orchestrator commands — dispatch the subagent directly via `Agent` instead |
| `write_plan`, `plan` | Belongs to `/donace:plan` |
| Dispatch `team-lead` | Belongs to `/donace:execute` |
| Dispatch `planner` | Belongs to `/donace:plan` |
| Start the dashboard | Not needed |
| Auto-commit after changes | User decides commits |

If any of these would be useful, you're probably at the escalation threshold — see below.

## Escalate to `/donace:plan` when

Stop and recommend `/donace:plan` when **any** of these hold:

- Change touches ≥3 files, or crosses domains (e.g., backend + frontend + infra)
- Requires writing a **new** test suite (not just adding one assertion to an existing test)
- Has multi-step dependencies (step B's input is step A's output)
- User's language implies feature work: "build", "implement", "add a new X"
- You want codex plan review, structured success criteria, or dashboard tracking
- The change is risky or user-facing enough that per-stage commit atomicity matters

Phrasing:

> "This spans <N> files across <areas> — worth going through `/donace:plan` so codex can review the approach and each stage gets committed + verified. Want to switch, or keep going in chat mode?"

The user may override — if they insist on chat mode for complex work, proceed but call out what they're giving up (no verification, no tracking, manual commits).

## Commit discipline

Do not auto-commit. After each meaningful change, summarise and let the user decide:

> "Changed: `path/to/a.ts`, `path/to/b.md`. Commit now?"

If they say yes: `git add <specific paths>` + a one-line commit message. Never `git add -A` or `git add .` — sensitive files or unrelated changes could slip in.

## Tradeoffs (remind the user if they ask why it's different from plan/execute)

| Gain | Give up |
|---|---|
| Zero overhead (no 30s orchestrator dispatch) | Dashboard visibility |
| No `.ai/runs/` noise | Run history and validator audit trail |
| Natural multi-turn dialogue | Automatic test/codex verification |
| User-controlled commits | Per-stage commit atomicity |

Chat mode is for when the speed/traceability tradeoff favors speed. The moment it doesn't, escalate.
