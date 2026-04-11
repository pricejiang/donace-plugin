# Donace Plugin

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

## Workflow

```
Phase 0: Boot (restore context from .ai/sessions/ and .ai/cards/)
Phase 1: Planning (planner → architect)
Phase 2: Sprint Loop
  ├── Sprint contract (runtime-evaluator)
  ├── Implement (implementer)
  ├── Verify (test-engineer + Codex review + runtime-evaluator)
  └── Fix loop (max 3 cycles)
Phase 3: Completion (final Claude review + session log + knowledge cards)
```

## Install as Plugin

```bash
/plugin marketplace add pricejiang/donace-plugin
/plugin install donace@pricejiang-donace-plugin
```

Then use agents with the `donace:` prefix:

```bash
claude --agent donace:team-lead
```

## Install via Symlink (for contributors)

```bash
git clone https://github.com/pricejiang/donace-plugin.git
ln -sf $(pwd)/donace-plugin/agents/*.md ~/.claude/agents/
```

Then use agents directly:

```bash
claude --agent team-lead
```

## Key Design Decisions

- **Generator-Evaluator pattern** — inspired by [Anthropic's harness design blog](https://www.anthropic.com/engineering/harness-design-long-running-apps)
- **Cross-model review** — Codex reviews Claude's code every sprint via `/codex:review`
- **Claude reviewer at ship time** — deep stack-specific review runs once at the end, not every sprint (saves tokens)
- **Knowledge persistence** — `.ai/cards/` for reusable insights, `.ai/sessions/` for session logs
- **Ralph Loop compatible** — resumption check skips re-planning on restart

## License

MIT
