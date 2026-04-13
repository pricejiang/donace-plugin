# Hybrid Orchestration Design

## Background

donace 的 orchestration 经历了两个阶段：

### Phase A: Pure MD (team-lead 全权控制)

```
user → team-lead (LLM) → spawn agents → judge results → next step
```

- **优点**: 灵活。team-lead 能根据任务性质决定跑哪些 agent、怎么组合、何时停止
- **缺点**: 不可靠。LLM 经常跳过 test-engineer、runtime-evaluator、codex review，因为它"觉得不需要"。prompt-based flow control 是 advisory 而非 mandatory

### Phase B: Pure Python (orchestrator 全权控制)

```
user → team-lead (传话) → orchestrator.py (固定 pipeline) → agents
```

- **优点**: 可靠。每个 stage 必经 implement → test → codex review → runtime verify，不会遗漏
- **缺点**: 死板。一行 typo 和一个新功能走同一套 full pipeline。team-lead 沦为传话角色，浪费 Opus context window。无法根据任务性质变通

### 结构性问题 (来自外部 review)

1. **Token 消耗是乘法增长** — 多 agent 重复 ingest 相似上下文，evaluator loop 放大成本
2. **自学习能力有限** — 有记忆沉淀但无策略级进化，orchestrator 决策逻辑是硬编码的
3. **每次都走 full pipeline** — 没有根据任务复杂度调整流程的机制

## Solution: Hybrid Mode

**核心思想: 把"做什么"还给 LLM，把"怎么做完整"留在 Python。**

```
user → team-lead (战略决策) → orchestrator (工具) → agents
            ↑                        ↓
            ←── 每次调用结果回到 team-lead ──
```

### 三层架构

| 层 | 负责什么 | 实现 | 举例 |
|---|---|---|---|
| **team-lead** | 拆任务、选命令、并行决策、结果判断、用户沟通 | LLM (Opus) | "这个任务需要先 plan，然后 stage 1 和 2 并行 run_job" |
| **orchestrator** | team-lead 的唯一执行工具。每个命令保证内部流程完整 | Python CLI | `plan`, `run_job`, `verify`, `review`, `document` |
| **agent dispatch** | 单个 agent 的 prompt、timeout、安全 hooks | Python (内部) | 命令黑名单、路径边界、EventBus |

team-lead **只通过 orchestrator 执行工作**，没有"ad-hoc 直接 spawn agent"的旁路。原因：

- **统一可见性**: 所有 agent 调用都经过 EventBus → dashboard 可追踪
- **统一安全**: 命令黑名单、路径边界、timeout 对所有 agent 生效
- **统一历史**: run_validator 能覆盖所有执行，不存在盲区

即使某个命令内部只调一个 agent（如 `review` 只调 reviewer，`document` 只调 documenter），也走 orchestrator。复杂度在 orchestrator 内部处理，team-lead 不需要关心。

## Orchestrator 命令集

### 命令清单

| 命令 | 做什么 | 内部流程 |
|---|---|---|
| `run_start` | 开始一次 team-lead 会话 | emit `run.started`，创建 run 目录 |
| `run_complete` | 结束一次 team-lead 会话 | 聚合 job 结果 → run_validator → emit `run.completed` |
| `plan` | 规划任务，生成分阶段 plan | planner → architect → codex plan review → 返回 plan JSON |
| `run_job` | 执行一个 stage 的完整开发流程 | contract → implement → test → codex → fix loop → 返回 job result |
| `verify` | 对已有代码做独立验证 (read-only) | test run + codex review + runtime-verifier (并行) → 返回 report |
| `review` | 代码审查 | typescript-reviewer 或 ios-reviewer → 返回 findings |
| `document` | 更新文档 | documenter → 更新 README/CHANGELOG 等 |

所有命令共享同一套基础设施：EventBus、dashboard 推送、安全 hooks、timeout 管理。

### CLI 接口

```bash
# 所有命令共享 --run-id 和 --dashboard-url

# 开始一次 run
python3 -m sdk.orchestrator run_start \
  --run-id run-abc123 \
  --cwd /project \
  --dashboard-url ws://localhost:8741

# 规划 — 返回 plan JSON (含 stage id、files、dependencies)
python3 -m sdk.orchestrator plan \
  --task "Add dark mode to ForkBar" \
  --cwd /project \
  --run-id run-abc123 \
  --dashboard-url ws://localhost:8741

# 执行单个 job — 用 stage id 引用 plan 中的 stage
python3 -m sdk.orchestrator run_job \
  --stage-id stage-1 \
  --plan .ai/plans/current-plan.json \
  --cwd /project \
  --skip-agents "runtime-verifier" \
  --max-fix-attempts 2 \
  --run-id run-abc123 \
  --dashboard-url ws://localhost:8741

# 独立验证 — read-only，并行跑 test + codex + runtime
python3 -m sdk.orchestrator verify \
  --cwd /project \
  --scope "src/theme/**" \
  --agents "test,codex" \
  --run-id run-abc123 \
  --dashboard-url ws://localhost:8741

# 代码审查
python3 -m sdk.orchestrator review \
  --cwd /project \
  --reviewer typescript \
  --run-id run-abc123 \
  --dashboard-url ws://localhost:8741

# 更新文档
python3 -m sdk.orchestrator document \
  --cwd /project \
  --run-id run-abc123 \
  --dashboard-url ws://localhost:8741

# 结束 run — 聚合所有 job 结果并跑 run_validator
python3 -m sdk.orchestrator run_complete \
  --run-id run-abc123 \
  --cwd /project \
  --dashboard-url ws://localhost:8741
```

### run_job 内部流程 (Python 保证)

```
run_job(stage):
  1. contract    — runtime-evaluator 生成验收标准 (可 skip)
  2. implement   — implementer 写代码
     如果 implement 失败 → 直接返回 BLOCKED，不跑 verify
  3. verify      — 并行执行 (均可通过 --skip-agents 跳过):
     - test-engineer (默认必跑)
     - codex review (默认跳过)
     - runtime-verifier (默认跳过)
  4. fix loop    — 如果 verify 有 failure:
     - 最多 N 轮 (--max-fix-attempts)
     - 每轮只重跑 test (不重跑 codex/runtime)
     - 全部修完 → PASS
     - 修不完 → 返回 BLOCKED + failure details
  5. 返回 job result JSON
```

team-lead 拿到 result 后自行决定: 继续下一个 job、retry、调整 task、或跟用户商量。

### plan 内部流程

```
plan(task):
  1. planner     — 展开需求为产品 spec (可 skip，简单任务不需要)
  2. architect   — 分析代码库，生成分阶段 plan (Markdown)
  3. parse       — 解析 Markdown 为 JSON sidecar (.ai/plans/current-plan.json)
  4. codex review — 审核 plan 质量 (可 skip)
  5. 返回 plan JSON: stages, files, dependencies, estimates
```

## team-lead 的角色

### 核心原则

team-lead 是**决策者**，orchestrator 是它的**工具**。team-lead 决定做什么、怎么组合、按什么顺序；orchestrator 保证每次调用内部的流程完整性。

### 典型工作流: 复杂任务

```
1. 用户: "给 ForkBar 加 dark mode"

2. team-lead 创建 run，然后规划:
   → Bash: python3 -m sdk.orchestrator run_start --run-id run-abc123 --cwd /project
   → Bash: python3 -m sdk.orchestrator plan --task "..." --cwd /project --run-id run-abc123
   → 拿回 current-plan.json: 3 个 stage

3. team-lead 审 plan
   → stage-1 (主题系统) 和 stage-2 (组件适配) 无依赖，files 无重叠
   → stage-3 (持久化) 依赖 1 和 2
   → 跟用户确认: "计划分 3 步，1 和 2 文件不重叠可以并行。要开始吗?"

4. 用户 ok

5. team-lead 并行启动两个 job (background):
   → Bash (bg): python3 -m sdk.orchestrator run_job --stage-id stage-1 --plan .ai/plans/current-plan.json ...
   → Bash (bg): python3 -m sdk.orchestrator run_job --stage-id stage-2 --plan .ai/plans/current-plan.json ...

6. 两个 job 返回
   → stage-1: PASS
   → stage-2: BLOCKED (test failure in Button.test.tsx)
   → team-lead 分析 failure，决定: 简单问题，retry
   → Bash: python3 -m sdk.orchestrator run_job --stage-id stage-2 --max-fix-attempts 3

7. stage-2 PASS

8. team-lead 启动 stage-3:
   → Bash: python3 -m sdk.orchestrator run_job --stage-id stage-3 --plan .ai/plans/current-plan.json

9. 全部 stage 完成，跑完整验证:
   → Bash: python3 -m sdk.orchestrator verify --cwd /project --agents "test,codex"

10. team-lead 决定跑 final review + 文档:
    → Bash: python3 -m sdk.orchestrator review --cwd /project
    → Bash: python3 -m sdk.orchestrator document --cwd /project

11. 结束 run，汇报给用户:
    → Bash: python3 -m sdk.orchestrator run_complete --run-id run-abc123 --cwd /project
```

### 典型工作流: 轻量任务

```
1. 用户: "把 README 里的 typo 修一下"

2. team-lead 判断: 极小任务，不需要 plan
   → Bash: python3 -m sdk.orchestrator run_job \
       --stage "Fix README typo" \
       --cwd /project \
       --skip-agents "contract,codex,runtime"

3. 完成，汇报
```

### 典型工作流: 纯审查任务

```
1. 用户: "帮我 review 一下 auth 模块的安全性"

2. team-lead 判断: 审查任务
   → Bash: python3 -m sdk.orchestrator review \
       --cwd /project \
       --reviewer typescript \
       --scope "src/auth/**"

3. 拿回 findings，汇报
```

### team-lead 的决策能力

| 决策 | 依据 | 举例 |
|---|---|---|
| 要不要做 plan | 任务复杂度 | typo fix 不需要，新功能需要 |
| 哪些 stage 并行 | plan 中的依赖关系 | 无依赖的 stage 并行跑 |
| run_job 跳过哪些 agent | 任务性质 | 纯 refactor 跳过 runtime-verifier |
| fix 几轮 | 失败原因 | flaky test 多给几轮，架构错误直接 BLOCK |
| 失败后怎么办 | failure details | 简单 → retry，复杂 → 跟用户商量 |
| 要不要 final review | 改动规模 | 1 个文件不需要，10 个文件需要 |
| 要不要更新文档 | 是否有 user-facing changes | 内部 refactor 不需要 |
| 调哪种 reviewer | tech stack | TypeScript 项目 → typescript-reviewer |
| 要不要中断正在跑的 job | 用户反馈 + job 状态 | 用户说"方向错了" → 中断 + 调整 |

### 中断机制

后台 job 运行期间，team-lead 不会被 block，用户可以随时对话。

**正常情况不需要中断。** job runner 内部已经处理了所有异常：agent 超时、test 失败、fix loop 耗尽 — 这些都会作为 job result 返回给 team-lead。team-lead 等结果就行。

**中断只用于用户主动想停：** 改需求了、方向不对、不想等了。用户通过 dashboard 观察进度，发现问题后告诉 team-lead。

```
用户在 dashboard 看到进度 → 告诉 team-lead "停掉 stage 2"
                                    ↓
                            team-lead 发中断信号
                                    ↓
                            orchestrator 优雅停止
```

**team-lead 的中断操作:**

```bash
# 查询正在跑的 job
curl -s localhost:8741/api/jobs/active

# 发送中断信号
curl -s -X POST localhost:8741/api/interrupt \
  --data '{"job_id": "xxx", "reason": "user requested"}'
```

**orchestrator 收到中断后:**

1. 等当前 agent 调用完成（不硬杀正在跑的 agent）
2. 不启动下一步（比如 implement 完了不进 verify）
3. emit `job.interrupted` 事件
4. 返回 partial result:
   ```json
   {
     "status": "INTERRUPTED",
     "completed_steps": ["contract", "implement"],
     "interrupted_at": "verify",
     "reason": "user requested",
     "changed_files": ["src/theme.ts", "src/colors.ts"]
   }
   ```

**team-lead 拿到 partial result 后可以:**

- 跟用户讨论下一步
- 用新参数重新 `run_job`（已写的代码还在磁盘上）
- 跑 `verify` 单独验证已有改动
- 放弃这个 stage，继续其他工作

**典型中断场景:**

```
1. 用户: "stage 2 不用跑了，需求变了"
   → team-lead: curl interrupt
   → team-lead: "Stage 2 已中断，implement 阶段改了 2 个文件。
                  要我回滚这些改动，还是在此基础上改？"

2. 用户在 dashboard 上看到 fix loop 跑了 3 轮还没修好
   → 用户: "别修了，这个方向不对"
   → team-lead: curl interrupt
   → team-lead: "已中断。核心问题是 X，建议重新设计这个 stage。"
```

## 并发模型

并行 run_job 是 hybrid mode 的核心能力，但也是最容易出问题的地方。

**核心不变量: 同一个 checkout 内，并行 job 的文件写入范围不能重叠。**

### 问题 1: cwd 级锁会杀掉并行 job

现有 `_acquire_lock()` (orchestrator.py:548) 检测到同 cwd 下已有 orchestrator 进程就直接 `os.kill(old_pid, 9)`。并行启动第二个 run_job 会杀掉第一个。

**方案: job-level registration**

```
之前: .ai/runs/.lock  (一个 cwd 一个锁，互斥，会 kill 旧进程)

现在: .ai/runs/{run_id}/jobs/{job_id}.lock  (每个 job 独立注册)
```

- 去掉 kill 型 cwd 级互斥锁
- 每个 orchestrator 进程创建自己的 job lock，记录 pid + job_id
- 进程退出时清理 lock（atexit hook + finally block）
- 不主动杀其他进程

### 问题 2: 多个 implementer 写同一 checkout

两个 implementer 同时修改同一个文件 → 互相覆盖。两个 test-engineer 同时跑测试 → 端口冲突、测试污染。

**方案: 文件分区 + file-scope 护栏 + 延迟验证**

architect 的 plan 已经为每个 stage 定义了 Files 列表。这就是天然的分区边界：

```
stage-1: src/theme.ts, src/colors.ts
stage-2: src/components/Button.tsx, src/components/Card.tsx
```

**规则:**
1. team-lead 只并行启动**文件无重叠**的 stage。plan JSON 里有 files 列表，team-lead 能判断。**有重叠 → 串行，不并行**
2. `run_job` 从 plan JSON 读取 stage 的 files 作为 `--file-scope`。PreToolUse hook 限制 Write/Edit 只能操作 scope 内的文件
3. 并行 run_job 的 verify 阶段**只跑单元测试**（针对改动文件），不跑集成测试
4. 所有并行 job 完成后，team-lead 用 `verify` 命令跑一次完整验证（包括集成测试）

```
team-lead 的并行流程:
  run_job(stage-1) → implement src/theme.ts, src/colors.ts         ─┐
  run_job(stage-2) → implement src/components/Button.tsx, Card.tsx ─┤ 并行，文件不重叠
                                                                     ↓
  verify --cwd /project --agents "test,codex"                      ← 完整验证
```

### 问题 3: SharedContext 并发写

多个并行 job 同时追加 `.ai/runs/{run_id}.context.md` → 竞态。

**方案: 每个 job 写独立文件，读时合并**

```
.ai/runs/{run_id}/context/
  plan.md                  ← plan 命令写入
  job-stage1.md            ← run_job stage 1 写入
  job-stage2.md            ← run_job stage 2 写入 (并行，无冲突)
```

读取时按 job 完成时间排序合并。每个 job 只写自己的文件，没有并发写同一文件的问题。

### 问题 4: 中断路由到具体进程

当前 control channel 只处理 `"action": "resolve"` 决策。需要扩展为通用 control protocol。

**方案: Job registry + control message schema**

每个 orchestrator 进程启动时向 dashboard 注册：

```json
// orchestrator → dashboard (via ingest WebSocket)
{
  "type": "job.register",
  "job_id": "job-xyz",
  "run_id": "run-abc123",
  "pid": 12345,
  "command": "run_job",
  "stage": "Stage 1: Theme system"
}
```

Dashboard 维护 active job registry。每个 orchestrator 进程连接 control WebSocket 时带上 job_id：

```
ws://localhost:8741/api/control?run_id=run-abc123&job_id=job-xyz
```

Dashboard 维护 `job_id → control_ws` 映射。中断请求精确路由到对应 job：

```json
// team-lead → dashboard (via REST)
POST /api/interrupt  {"job_id": "job-xyz", "reason": "user requested"}

// dashboard → 对应 orchestrator 进程 (via control WebSocket)
{"action": "interrupt", "job_id": "job-xyz", "reason": "user requested"}
```

如果找不到对应 job → 404；job 已完成 → 409。不广播给所有 orchestrator 进程。

**Orchestrator 端: cancellation flag**

```python
class EventBus:
    def __init__(self):
        self._cancelled = False
        self._cancel_reason = ""

    def cancel(self, reason: str):
        self._cancelled = True
        self._cancel_reason = reason

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled
```

Job runner 在每个步骤之间检查 `bus.is_cancelled`：

```python
async def run_job(stage, bus, ...):
    await run_contract(stage, bus, ...)
    if bus.is_cancelled: return partial_result("contract")

    await run_implement(stage, bus, ...)
    if bus.is_cancelled: return partial_result("implement")

    await run_verify(stage, bus, ...)
    # ...
```

Control message schema:

```json
// 现有: checkpoint 决策 (保留)
{"action": "resolve", "checkpoint": "post-verify", "decision": "CONTINUE"}

// 新增: 中断
{"action": "interrupt", "job_id": "job-xyz", "reason": "user requested"}
```

Emitter 的 `_listen_controls()` 扩展为处理多种 action type。

## Plan Schema

### 问题

architect 写 Markdown plan (`.ai/plans/current-plan.md`)，orchestrator 用 `_parse_plan_stages()` 解析为 Stage 对象。但 Markdown 解析脆弱——格式稍有变化就可能解析失败。

### 方案: Markdown + JSON sidecar

保留 Markdown 给人读（architect 写、team-lead 审、用户看）。orchestrator 解析后生成 JSON sidecar 给机器读。

```
.ai/plans/
  current-plan.md          ← architect 写的 Markdown (人读)
  current-plan.json        ← orchestrator 解析后生成 (机器读)
```

**JSON schema:**

```json
{
  "task": "Add dark mode to ForkBar",
  "stages": [
    {
      "id": "stage-1",
      "name": "Theme system",
      "files": ["src/theme.ts", "src/colors.ts"],
      "dependencies": [],
      "has_user_facing_changes": true,
      "estimated_turns": 25,
      "description": "Create theme provider with light/dark mode support",
      "success_criteria": ["Theme provider exposes light/dark tokens"],
      "tests": ["Unit test theme token selection"]
    },
    {
      "id": "stage-2",
      "name": "Component adaptation",
      "files": ["src/components/Button.tsx", "src/components/Card.tsx"],
      "dependencies": [],
      "has_user_facing_changes": true,
      "estimated_turns": 20,
      "description": "Update components to use theme tokens",
      "success_criteria": ["Button and Card render correctly in both themes"],
      "tests": ["Component tests for light and dark variants"]
    },
    {
      "id": "stage-3",
      "name": "Persistence",
      "files": ["src/hooks/useTheme.ts", "src/utils/storage.ts"],
      "dependencies": ["stage-1", "stage-2"],
      "has_user_facing_changes": false,
      "estimated_turns": 15,
      "description": "Persist theme preference to localStorage",
      "success_criteria": ["Theme preference survives reload"],
      "tests": ["Hook/storage unit test for persistence"]
    }
  ]
}
```

`plan` 命令的输出就是这个 JSON。team-lead 用 `--plan` 把它传给 `run_job`。

### run_job 怎么用 plan JSON

```bash
# team-lead 先拿到 plan
python3 -m sdk.orchestrator plan --task "..." --cwd /project --run-id run-abc123

# plan 输出 JSON，team-lead 解析后按 stage 调用
python3 -m sdk.orchestrator run_job \
  --stage-id stage-1 \
  --plan .ai/plans/current-plan.json \
  --cwd /project \
  --run-id run-abc123
```

`--stage-id` 替代 `--stage "Stage 1: Theme system"` 字符串，更可靠。

## Run 生命周期

### 问题

Dashboard 无法推断 team-lead 是否还会启动更多 job。"首个/末个 job 自动生成 run.started/run.completed" 在多次独立 CLI 调用下不可判定。

### 方案: 显式 lifecycle 命令

```bash
# team-lead 会话开始
python3 -m sdk.orchestrator run_start --run-id run-abc123 --dashboard-url ws://localhost:8741

# ... 多次 plan / run_job / verify / review / document ...

# team-lead 会话结束
python3 -m sdk.orchestrator run_complete --run-id run-abc123 --dashboard-url ws://localhost:8741
```

- `run_start` → emit `run.started` 事件，创建 `.ai/runs/{run_id}/` 目录
- `run_complete` → 聚合所有 job 结果 → run_validator → emit `run.completed` → 生成最终报告

这两个是轻量命令（不启动 agent），只做事件 emit 和 bookkeeping。

`run_complete` 聚合逻辑：

```
.ai/runs/{run_id}/
  jobs/{job_id}.json        ← 每个 command/job 的结构化结果
  context/*.md              ← plan、job context
  result.json               ← run_complete 聚合输出
```

- 所有 `run_job` 状态汇总为 stage summary
- `verify` job 作为 final verification，单独记录
- `review` / `document` job 作为 wrap summary
- 任一 `run_job` 为 BLOCKED 或 final verify 失败 → run summary 不能是 PASS

Dashboard 根据有无 `run.completed` 事件判断 run 状态：
- 有 job 事件但无 `run.completed` → Live (进行中)
- 有 `run.completed` → Completed
- 有 `run.started` 但长时间无新事件 → Stale (team-lead 可能忘了调 run_complete)

## 去掉 sub-implementer

### 问题

当前架构: implementer → sub-implementer (implementer 内部再拆)

- sub-implementer 的作用有限 — implementer 本身就能处理大多数 stage
- 多了一层嵌套，增加了 token 消耗和复杂度
- team-lead 在 Phase A 时就能直接 spawn 多个 implementer

### 方案

去掉 sub-implementer。并行通过 team-lead 同时启动多个 `run_job` 实现:

```
之前:
  team-lead → orchestrator → implementer → sub-implementer A
                                         → sub-implementer B

现在:
  team-lead → run_job(stage 1) → implementer A  ─┐
            → run_job(stage 2) → implementer B  ─┤ 并行
                                                  ↓
            ← 两个 job result 返回 team-lead
```

每个 `run_job` 内部有独立的 implementer 实例，完整走 implement → verify → fix loop。并行由 team-lead 控制（同时启动多个后台命令），不需要 implementer 内部再拆。

### 改动

- 删除 `agents/sub-implementer.md`
- `agents/implementer.md` 去掉 Agent(sub-implementer) 相关内容
- `sdk/agent_dispatch.py` 去掉 sub-implementer 相关 hooks 和 dispatch

## Token 优化策略

### 1. 按需组合 (team-lead 决策)

team-lead 根据任务性质选择不同的命令组合:

| 任务 | team-lead 的命令序列 |
|---|---|
| Fix typo | `run_job --skip-agents "contract,codex,runtime"` |
| Add simple feature | `plan` → `run_job` (test only) |
| Complex feature | `plan` → parallel `run_job` (test + codex) → `review` |
| Critical system change | `plan` → `run_job` (test + codex + runtime) → `review` → `document` |

LLM 判断比硬编码规则灵活。省 token 的关键不是优化单个 agent，而是不跑不需要的 agent。

### 2. SharedContext 分级

旧模型里 SharedContext 在单进程内存中积累。新模型每个 orchestrator 调用是独立进程，需要持久化：

```
.ai/runs/{run_id}.context.md   ← 磁盘文件，每个 job 结束后追加
```

每个 `run_job` 启动时读取现有 context，结束时追加本次结果（改动文件、test 结果、发现的问题）。不同 agent 按需读取不同详细度：

```python
class SharedContext:
    def for_implementer(self) -> str:
        """完整 context: 前序 stage 的改动、测试结果、代码约束"""
        return self.full()

    def for_test_engineer(self) -> str:
        """只给改动文件 + 测试相关 context"""
        return self.changed_files_section()

    def for_documenter(self) -> str:
        """只给摘要"""
        return self.summary_section()
```

### 3. Routing Hints (历史数据驱动)

`~/.claude/plugins/data/donace/routing_hints.json`:

```json
{
  "stack_hints": {
    "typescript-nextjs": {
      "codex_useful_rate": 0.2,
      "runtime_useful_rate": 0.6,
      "avg_fix_loops": 1.3
    }
  },
  "total_runs": 15
}
```

run_validator 每次 run 结束后更新。team-lead 可以读取作为决策参考（不是强制）:

> "这个 TypeScript 项目历史上 codex review 只有 20% 发现问题。这次是个小改动，跳过 codex。"

### 4. Context Budget (兜底)

给 agent 设软限制 + 硬限制:

- **Prompt 级**: "TOKEN BUDGET: ~8,000 output tokens. Focus on listed files."
- **max_turns**: test-engineer 30, runtime-evaluator 15, codex 10
- **timeout**: 保持现有机制 (implementer 900s, 其他 300-600s)

## 文件改动清单

### 重写

| 文件 | 改动 |
|---|---|
| `agents/team-lead.md` | 从 "pipeline 启动器" 变为 "战略决策者 + orchestrator 工具使用者" |
| `sdk/orchestrator.py` | 从单一 `main()` 拆为 `cmd_plan()`, `cmd_run_job()`, `cmd_verify()`, `cmd_review()`, `cmd_document()`, `cmd_run_start()`, `cmd_run_complete()`。CLI 用 subcommand 模式。去掉 cwd 级锁，改为 job-level locking |
| `sdk/sprint_loop.py` | 缩小为 single job runner: implement → verify → fix loop → 返回结果。去掉 wave scheduler、依赖图解析 |

### 修改

| 文件 | 改动 |
|---|---|
| `sdk/agent_dispatch.py` | 去掉 sub-implementer hooks；加 `--file-scope` PreToolUse hook 限制并行 job 的写入范围 |
| `sdk/events.py` | 去掉 wave/phase 相关事件，新增 job-level 事件 (job.register, job.started, job.completed, job.interrupted)。EventBus 加 cancellation flag |
| `sdk/dashboard.py` | 新增 `/api/jobs/active` 和 `/api/interrupt` 端点；维护 job registry；适配新事件格式 |
| `sdk/static/index.html` | 适配新事件格式: job cards 取代 stage cards |
| `sdk/run_validator.py` | 适配 per-job 验证 (小改) |
| `agents/implementer.md` | 去掉 sub-implementer 相关 |
| `sdk/emitter.py` | `_listen_controls()` 扩展：除 "resolve" 外新增 "interrupt" action type |

### 删除

| 文件 | 原因 |
|---|---|
| `agents/sub-implementer.md` | 不再需要 |

### 不变

| 文件 | 原因 |
|---|---|
| `agents/architect.md` | Plan 格式不变（Markdown），orchestrator 负责生成 JSON sidecar |
| `agents/runtime-evaluator.md` | Contract 生成不变 |
| `agents/runtime-verifier.md` | 黑盒验证不变 |
| `agents/planner.md` | 需求展开不变 |
| `agents/documenter.md` | 文档更新不变 |
| 其他 reviewer agent md | 不受影响 |

## Dashboard 适配

Dashboard 核心不变 (EventStore, WebSocket, REST API)。需要适配的:

### 事件模型变化

```
之前 (一次 run = 一个完整 pipeline):
  run.started → phase.started → stage.changed → agent.started → ... → run.completed

现在 (一次 run = team-lead 的一次会话，包含多次 orchestrator 调用):
  run.started
    → job.started (plan) → agent.started (architect) → ... → job.completed
    → job.started (run_job stage 1) → agent.started (implementer) → ... → job.completed
    → job.started (run_job stage 2) → agent.started (implementer) → ... → job.completed  (并行)
    → job.started (review) → agent.started (reviewer) → ... → job.completed
  → run.completed
```

- `run_id` 由 team-lead 生成，通过 `--run-id` 传给每次 orchestrator 调用
- 每次 orchestrator 调用是一个 `job`，共享 `run_id`
- `run.started` / `run.completed` 由 team-lead 通过显式 `run_start` / `run_complete` 命令触发（见 "Run 生命周期" 章节）
- `phase.started` / `phase.completed` 去掉 (不再有固定 phase)
- 新增 `job.register` 事件 (job 注册到 dashboard registry)
- 新增 `job.interrupted` 事件 (用户中断)

### 新增 API

| 端点 | 方法 | 用途 |
|---|---|---|
| `/api/jobs/active` | GET | team-lead 查询正在跑的 job 状态 |
| `/api/interrupt` | POST | team-lead 发送中断信号，body: `{"job_id": "xxx", "reason": "..."}` |

Dashboard 收到 interrupt 请求后通过 control WebSocket 转发给 orchestrator。orchestrator 优雅中断后 emit `job.interrupted` 事件。

### UI 变化

- 去掉 Phase 0/1/2/3 的固定 header
- Job cards 取代 Stage cards，按时间排列
- 并行 job 水平排列
- 每个 job card 内部展示 agent 活动（和现有 agent card 类似）
- team-lead 的决策点可视化 (plan review, 并行决策, retry)
- Job interrupted 状态显示 (区别于 completed / blocked)

## 迁移策略

### 阶段一: 基础设施

- `orchestrator.py` 支持 subcommand: `run_start`, `run_complete`, `plan`, `run_job`, `verify`, `review`, `document`
- 去掉 cwd 级锁，改为 job-level locking
- EventBus 加 cancellation flag
- emitter.py 扩展 control message schema
- sprint_loop.py 重构为 single job runner
- 保留旧的 `--task` 入口作为兼容
- 验证: 旧流程照跑，新命令可独立测试

### 阶段二: 并发与 plan schema

- Plan JSON sidecar 生成
- `--file-scope` PreToolUse hook
- SharedContext 拆为 per-job 独立文件
- Dashboard: job registry, `/api/jobs/active`, `/api/interrupt`
- 验证: 两个无重叠 stage 并行跑，确认无冲突

### 阶段三: 重写 team-lead

- 新 team-lead.md: 战略决策者，通过 orchestrator 命令集执行所有工作
- 去掉 sub-implementer
- 所有执行走 orchestrator（无 ad-hoc bypass）
- 验证: 用 team-lead 手动跑一个多 stage 任务，确认并行、中断、结果判断正确

### 阶段四: 优化

- SharedContext 分级（per-agent 详细度）
- Routing hints
- Context budget
- Dashboard 前端适配新事件模型
- 验证: 对比新旧模式的 token 消耗

## 设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| team-lead 能否绕过 orchestrator | 不能 | 统一可见性、安全、历史记录。即使单 agent 任务也走 orchestrator |
| team-lead 用什么调 orchestrator | Bash (CLI subcommand) | team-lead 通过 Bash tool 调用。每个命令是独立进程，进程结束 = 调用完成 |
| run_job 内部哪些必跑 | 默认 test-engineer 必跑，但 team-lead 可以通过 `--skip-agents` 全部跳过 | test 是默认的质量保证，但 team-lead 对任务有完整判断权（如 typo fix 不需要跑 test） |
| 并行怎么实现 | team-lead 同时启动多个后台 Bash 命令 | 利用 Claude Code 的 run_in_background 机制，team-lead 启动后等通知 |
| routing hints 是建议还是强制 | 建议 | team-lead 参考但不被绑定。强制规则在 job runner 内部 (如 test 必跑) |
| 旧 pipeline 是否保留 | 保留 `--task` 入口作为兼容 | 迁移期间可以对比。稳定后删除 |
| run_id 粒度 | 一次 team-lead 会话 = 一个 run | team-lead 启动 dashboard 时生成 run_id，所有 orchestrator 调用共享 |
| 单 agent 命令 (review, document) 为什么走 orchestrator | 统一基础设施 | EventBus、安全 hooks、timeout 对所有 agent 一视同仁。消除盲区 |
| 中断机制由谁触发 | 用户触发，team-lead 执行 | 正常异常（超时、test 失败）由 job runner 内部处理并在 result 中返回。中断只用于用户主动想停 |
| 中断是硬杀还是优雅停止 | 优雅停止 | 等当前 agent 完成，不启动下一步，返回 partial result。通过 EventBus cancellation flag 实现 |
| 并行 job 如何隔离 | 文件分区（不用 git worktree） | team-lead 根据 plan 的 files 列表判断是否有重叠。有重叠 → 串行。implementer 通过 `--file-scope` + PreToolUse hook 限制写入范围 |
| Plan 格式 | Markdown + JSON sidecar | Markdown 给人读（architect 写、team-lead 审），JSON 给机器读（orchestrator 解析后生成） |
| Run 生命周期 | 显式 run_start / run_complete 命令 | Dashboard 无法推断 team-lead 是否还会启动更多 job，必须显式声明 |
| SharedContext 并发 | 每个 job 写独立文件，读时合并 | 避免并发写同一文件的竞态 |

## Future: 高级并发控制

当前并发模型依赖 team-lead 判断文件是否重叠 + file-scope PreToolUse hook 护栏。这对 2-3 个并行 job、明确文件边界的场景足够。以下是当规模或复杂度增长后可能需要的增强，暂不实现：

### Write-set / Resource Lock 体系

当 team-lead 无法准确判断文件重叠时（如 Bash 命令可能写未知文件、codegen 可能改全仓），需要系统级锁：

```
.ai/runs/.locks/
  paths/{sha256(path)}.json    ← 文件级写锁
  resources/{name}.json        ← 共享资源锁 (端口、数据库、dev server、package manager)
  global-writer.json           ← 兜底锁，用于 write-set 不明确的情况
```

- Path locks 按 normalized path 排序获取，避免死锁
- 两个 job 的 path/resource locks 不重叠 → 可以并行写
- Write-set 不完整或 Bash 命令无法判断写入范围 → 获取 global-writer 兜底锁
- `--queue` flag: 拿不到锁时等待而非立即失败
- `job.waiting_for_locks`、`locks.acquired`/`locks.released` 事件给 dashboard 展示实际并发状态

### run_job 分段锁

将 run_job 拆为 read-only 和 mutating 两个阶段，分段持锁：

```
run_job(stage):
  0. register      — 注册 job
  1. preflight     — read-only: 读取 plan/context，准备 prompt (可并行)
  2. contract      — read-only: runtime-evaluator 生成验收标准 (可并行)
  3. wait_locks    — 获取 stage write-set locks
  4. refresh       — 获取 locks 后重读最新 SharedContext
  5. implement     — mutating: 写代码 (持有 locks)
  6. test-write    — mutating: 补测试、跑局部测试 (持有 locks)
  7. fix loop      — mutating: 修复 (持有 locks)
  8. unlock        — 释放 locks
  9. post_verify   — read-only: codex/runtime verification (可并行)
```

好处是 preflight/contract/post_verify 无锁可并行，只有 implement/test/fix 需要锁。但实现复杂度显著增加。

### Bash 命令分类

对 implementer 的 Bash 命令按写入范围分类：

- **read-only**: 允许并行（如 `cat`, `grep`, `npm test`）
- **scoped-write**: 只允许在持有对应 path locks 时运行（如 `npx prettier --write src/theme.ts`）
- **global-write / unknown**: 必须获取 global-writer 锁（如 `npm install`, `prisma generate`）

### 触发条件

当以下情况出现时，考虑实现上述增强：

- 并行 job 数量经常 > 3
- architect 的 files 列表经常不准确
- implementer 通过 Bash 写出 file-scope 范围的文件
- 需要并行 job 共享端口、数据库等资源
