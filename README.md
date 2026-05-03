# donace

Plan / execute / review pipeline that orchestrates Claude Code and Codex on a per-stage basis.

## What it does

- `/donace:chat` — brainstorm with the main LLM, write `spec.md`.
- `/donace:plan <run-id>` — planner subagent reads `spec.md`, emits `plan.md` with stages tagged `implementer: claude|codex`.
- `/donace:execute <run-id>` — for each stage: dispatch implementer (background), run listed `tests:`, dispatch reviewer (the opposite model), commit on PASS. P0 findings or test failures retry the stage up to 2 times; rate-limit hits drive the stage to `interrupted` and resume cleanly.

The main LLM in your Claude Code session orchestrates everything — there is no team-lead subagent. Workers run in the background so you can chat / clarify / interrupt at any time.

## Install

This is a Claude Code plugin. To run from the local checkout, pass `--plugin-dir` to `claude`:

```bash
claude --plugin-dir /path/to/donace
```

Skills `/donace:chat`, `/donace:plan`, and `/donace:execute` become available, plus the `planner` / `implementer` / `reviewer` agents.

To verify the manifest is valid before launching:

```bash
claude plugin validate /path/to/donace
```

If you previously had a `~/.claude/agents` symlink pointing at this repo's `agents/` (the old install method), remove it before loading the plugin — otherwise agents get registered twice.

Codex stages additionally require the `openai-codex` plugin to be installed (donace shells out to its `codex-companion.mjs`).

## Quick tour

```bash
# Create a fresh run
/donace:chat
# ... brainstorm with the main LLM, it writes spec.md ...

# Plan it
/donace:plan run-a1b2c3d4

# Edit plan.md if needed (especially `implementer:` tags)

# Run it
/donace:execute run-a1b2c3d4

# At any time, glance at the pipeline
python3 sdk/cli.py stage_status run-a1b2c3d4
```

## Layout

- `skills/{chat,plan,execute}/SKILL.md` — main LLM instructions per skill
- `agents/{planner,implementer,reviewer}.md` — subagent definitions
- `references/review-checklist-{python,typescript,ios,general}.md` — stack-specific reviewer hints
- `prompts/codex-{implementer,reviewer}.md` — codex prompt prefixes
- `sdk/codex_call.py` — wrapper around codex-companion.mjs (`task --background --json` + status + result)
- `sdk/cli.py` — `donace run_start | list_runs | parse_plan | stage_status | mark_completed`
- `.ai/runs/<run-id>/` — run state (gitignored)

## Design

See [docs/specs/2026-05-02-donace-simplify-design.md](docs/specs/2026-05-02-donace-simplify-design.md) for the full spec, including the reviewer severity contract, stop-state semantics, and bootstrap order.

## License

MIT (see LICENSE).
