# Donace

Generator-Evaluator agent harness for Claude Code. Sprint-based development workflow with cross-model review (Claude + Codex) and knowledge persistence.

## Agents

| Agent | Model | Role |
|---|---|---|
| **team-lead** | opus | Orchestrator — coordinates the full workflow |
| **planner** | opus | Expands brief into product spec |
| **architect** | opus | Designs staged implementation plan |
| **implementer** | sonnet | Writes production code following the plan |
| **runtime-evaluator** | opus | Runtime verification via Playwright / Xcode Simulator / curl |
| **test-engineer** | sonnet | Writes and runs unit tests |
| **ios-reviewer** | opus | Deep iOS/Swift code review |
| **typescript-reviewer** | opus | Deep TypeScript/React code review |
| **ui-designer** | opus | On-demand UI/UX design specs |

## Usage

Two user-invokable skills drive the workflow:

```
/donace:plan <task>     →  interactive Q&A, writes .ai/runs/<id>/plan.md,
                           runs codex plan review, hands off
(user reviews plan.md)
/donace:execute <id>    →  dispatches team-lead to run run_job → verify →
                           review → document → run_complete
```

### 1. Plan

```
/donace:plan add rate limiting to the notification API
```

The plan skill:
- Scans for resumable runs (resume if mid-plan)
- Clarifies vague briefs with up to 3 targeted questions
- Calls `run_start` + `write_plan` + `plan` to produce a codex-reviewed plan
- Hands off with the run-id — does **not** execute

If codex flags the plan (status=REVIEW), the skill loops back with a revision brief.

### 2. Review

Open `.ai/runs/<run-id>/plan.md` in your editor. Sanity-check the stages, dependencies, success criteria.

### 3. Execute

```
/donace:execute <run-id>
```

Or omit the run-id — the execute skill auto-picks the newest PASS run. It dispatches `team-lead`, which runs the full pipeline and returns a 3-line summary.

### Revising a plan

Re-invoke `/donace:plan` on the same run-id (the skill detects the existing run and offers resume). `write_plan` treats the new task brief as a revision directive against the existing `plan.md`.

## Install as Plugin

```bash
/plugin marketplace add pricejiang/donace-plugin
/plugin install donace@pricejiang-donace-plugin
```

Once installed, use the skills with the `donace:` namespace: `/donace:plan`, `/donace:execute`. Team-lead and the other agents are invoked internally by the skills — you do not normally launch them directly.

## Install via Symlink (for contributors)

```bash
git clone https://github.com/pricejiang/donace-plugin.git
ln -sf $(pwd)/donace-plugin/agents/*.md ~/.claude/agents/
ln -sf $(pwd)/donace-plugin/skills/plan ~/.claude/skills/donace-plan
ln -sf $(pwd)/donace-plugin/skills/execute ~/.claude/skills/donace-execute
```

## Key Design Decisions

- **Generator-Evaluator pattern** — inspired by [Anthropic's harness design blog](https://www.anthropic.com/engineering/harness-design-long-running-apps)
- **Cross-model review** — Codex reviews Claude's code every sprint via `/codex:review`
- **Claude reviewer at ship time** — deep stack-specific review runs once at the end, not every sprint (saves tokens)
- **Knowledge persistence** — `.ai/cards/` for reusable insights, `.ai/sessions/` for session logs
- **Ralph Loop compatible** — resumption check skips re-planning on restart

## License

MIT
