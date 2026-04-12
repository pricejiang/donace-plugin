"""Micro-benchmarks for prompt-context assembly.

Does not call Claude. Used to verify that token optimizations do not add
meaningful Python-side overhead.
"""
from __future__ import annotations

import argparse
import statistics
import time

from sdk.context_budget import build_stage_context, build_wrap_context
from sdk.events import Stage, StageResult
from sdk.orchestrator import SharedContext
from sdk.token_audit import estimate_tokens


def _make_shared_context(stage_count: int) -> SharedContext:
    shared_ctx = SharedContext(run_id="perf", cwd="/tmp/project", task="Build a staged user-facing feature.")
    shared_ctx.add("Project", "Stack: typescript\nCwd: /tmp/project")
    plan_lines = []
    for idx in range(1, stage_count + 1):
        plan_lines.append(f"- Stage {idx}: Slice {idx} (user-facing: True, depends: none)")
    shared_ctx.add("Plan", "Stages:\n" + "\n".join(plan_lines) + "\n\nFull plan:\n" + ("x" * 2400))
    for idx in range(1, stage_count + 1):
        shared_ctx.add(
            f"Stage Result: Slice {idx}",
            StageResult(
                name=f"Slice {idx}",
                status="PASS",
                contract="contract",
                test_result={"passed": 10, "failed": 0},
                codex_result={"status": "completed", "has_issues": False, "output": ""},
                runtime_result={"status": "PASS", "score": "3/3"},
                fix_attempts=0,
            ).name
            + "\nStatus: PASS\nTests: 10 passed, 0 failed\nFix attempts: 0\n"
            + ("detail " * 30),
        )
    shared_ctx.add("Final Review", "warning " * 200)
    return shared_ctx


def _measure(fn, iterations: int) -> dict[str, float]:
    samples: list[float] = []
    last_value = ""
    for _ in range(iterations):
        t0 = time.perf_counter()
        last_value = fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return {
        "mean_ms": statistics.mean(samples),
        "p95_ms": statistics.quantiles(samples, n=20)[18] if len(samples) >= 20 else max(samples),
        "max_ms": max(samples),
        "chars": float(len(last_value)),
        "est_tokens": float(estimate_tokens(last_value)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark prompt context assembly cost")
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--stages", type=int, default=8)
    args = parser.parse_args()

    shared_ctx = _make_shared_context(args.stages)
    full = _measure(shared_ctx.to_prompt_prefix, args.iterations)
    compact_stage = _measure(lambda: build_stage_context(shared_ctx.sections), args.iterations)
    compact_wrap = _measure(lambda: build_wrap_context(shared_ctx.sections), args.iterations)

    print("Prompt Assembly Benchmark")
    print("")
    print(
        f"- full-context: mean={full['mean_ms']:.3f}ms p95={full['p95_ms']:.3f}ms chars={int(full['chars'])} est_tokens={int(full['est_tokens'])}"
    )
    print(
        f"- compact-stage: mean={compact_stage['mean_ms']:.3f}ms p95={compact_stage['p95_ms']:.3f}ms chars={int(compact_stage['chars'])} est_tokens={int(compact_stage['est_tokens'])}"
    )
    print(
        f"- compact-wrap: mean={compact_wrap['mean_ms']:.3f}ms p95={compact_wrap['p95_ms']:.3f}ms chars={int(compact_wrap['chars'])} est_tokens={int(compact_wrap['est_tokens'])}"
    )


if __name__ == "__main__":
    main()
