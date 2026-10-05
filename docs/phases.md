# DevLoop — Build Phases

Status of each phase, what it contains, and what "done" means for it.
Architecture context lives in [`architecture.md`](./architecture.md).

| Phase | Scope | Status |
|---|---|---|
| **0** | Walking skeleton — state machine, contracts, graph, local CLI | ✅ **Done** |
| **1** | Sandbox + environment bootstrap | 🟡 Partial — per-task clone, `devloop.yml`, host-run baseline; Docker ⬜ |
| **2** | Real agents (Reviewer → Planner → Requirement → Developer) | ✅ **Done** |
| **3** | Human loop (clarify Q&A, scoped feedback, escalation) | ⬜ |
| **4** | Knowledge base | ⬜ |
| **5** | Hardening + telemetry | ⬜ |
| **v1** | GitHub adapter | ⬜ |
| **v2** | Improvement engine | ⬜ |

---

# Phase 0 — Walking skeleton ✅

**Goal:** prove the machine before spending a cent on models. Every node,
every contract, every transition, git, persistence and the human gates run
end to end with **stub agents that make no model calls**.

Rationale: if the state machine is wrong, better models won't save it — and
debugging a state machine through a non-deterministic agent is miserable. Stub
agents make the whole loop deterministic and free to run.

## What was built

### Contracts — `src/devloop/contracts/`

| File | Contents |
|---|---|
| `status.py` | `DevLoopStatus` enum (16 states) with `is_terminal` / `needs_human` helpers |
| `artifacts.py` | Pydantic models every agent must produce: `RequirementResult`, `PlanResult`, `ReviewResult`/`Finding`, `EnvRecipe`, `Deviation`, `TestRunResult` |
| `state.py` | `DevLoopState` (LangGraph `TypedDict`), `TaskInput`, `DiffSummary`, and the caps: `BUDGET_USD_DEFAULT`, `MAX_REVIEW_ITERATIONS = 3`, `MAX_REPLANS = 2` |

Two contract details carry weight beyond Phase 0:

- `TestRunResult.phase` (`baseline` | `post_change`) is what makes
  failure attribution possible — see problem 1 in the architecture doc.
- `Finding.source` and `Finding.resolved` let human feedback flow down the
  *same* repair path as reviewer findings, and let scoped feedback re-open a
  single finding instead of everything.

Accumulating fields (`baseline`, `verification`, `feedback`) use
`Annotated[list[...], operator.add]` so LangGraph merges across iterations
rather than overwriting.

### The policy — `src/devloop/graph/routing.py`

`decide(state) -> DevLoopStatus`. Pure, no I/O, ~90 lines. The only code
permitted to choose the next status.

Guards implemented:

- **Budget** — any in-flight status escalates once `cost_usd >= budget_usd`.
  Deliberately does *not* override a human-gated wait state.
- **Review iterations** — `CHANGES_REQUIRED → IMPLEMENTING` only while
  `iteration < 3`, else `ESCALATED`.
- **Replans** — an unresolved `plan_conformance` finding routes to
  `REPLANNING` while `replan_count < 2`, else `ESCALATED`.
- **Regression gate** — `_new_failures()` compares the post-change failing set
  against the baseline failing set. `REVIEWING → READY_FOR_FINALIZE` needs
  `approved` **and** an empty difference. A repo that was already red stays
  shippable; a repo the agent broke does not.

### The graph — `src/devloop/graph/`

`build.py` wires 13 nodes and routes purely on the status a node just wrote.
`STATUS_TO_NODE` is total over the enum, so an unrouted new status fails loudly.

`nodes/core.py` implements the nodes. Every one ends the same way, via
`_advance()`: compute facts → call `decide()` with those facts folded in →
return `{"status": next_status, **facts}`.

Node design follows the LangGraph re-execution constraint: `clarify`,
`finalize` and `escalate` contain nothing but `interrupt()`, and side effects
live in their own nodes. `env_gate` and `changes_required` are pure routing
gates with no side effects at all.

`nodes/stubs.py` holds the fake agents. Each returns exactly the artifact a
real agent will later write to `/workspace/.devloop/out/<role>.json`, so
Phase 2 swaps the call site and nothing else.

### Git isolation — `src/devloop/sandbox/local_git.py`

Direct git against the target repo on a `devloop/<task-id>` branch. An
explicit placeholder for Phase 1's Docker sandbox, isolated in one module so
the swap is contained.

- `ensure_clean_repo()` refuses to run against a dirty working tree.
- `_default_branch()` resolves the base branch and **refuses to fork from a
  `devloop/*` branch**.
- `write_and_commit()` fails loudly when there is nothing to commit rather
  than reporting a phantom success.

### CLI — `src/devloop/cli/main.py`

```
devloop run --repo <path> --task <file.md>     # start a task
devloop resume <task-id> --answer <text>       # answer a human gate
```

Checkpointing: **SQLite** at `~/.devloop/checkpoints.sqlite` by default,
Postgres when `DEVLOOP_DATABASE_URL` is set.

### Tests — `tests/graph/test_routing.py`

30 table-driven cases over `decide()`, one per transition and per guard
rejection, plus a purity/idempotence test. Runs in ~20ms with no fixtures,
no database and no network.

## Two bugs this phase caught

Both were found by *running* the thing, not by reading it — which is the
entire argument for building a walking skeleton first.

**1. Task branches forked off the previous task's branch.**
`create_task_branch()` branched from whatever was checked out, which after one
run was the *previous* task's branch. Task 2 would silently inherit task 1's
changes — problem 3 from the architecture doc, appearing in the first hour.
Fixed by resolving an explicit base branch and refusing to fork from
`devloop/*`. Verified: two independent tasks both sit exactly one commit
ahead of `main`.

**2. `InMemorySaver` cannot survive `devloop resume`.**
`run` and `resume` are separate OS processes, so an in-memory checkpointer is
empty by the time `resume` starts — the graph restarted from `ingest` with no
state and crashed on `KeyError: 'task'`. Fixed by defaulting to a SQLite file,
which is still server-free but actually persistent.

A third, subtler one: `SqliteSaver.from_conn_string()` is a generator-based
context manager whose `finally` closes the connection when the generator is
garbage-collected. Calling `__enter__()` and dropping the manager produced
`ProgrammingError: Cannot operate on a closed database`. The manager is now
pinned to the saver for the process lifetime.

## Verified

```
$ devloop run --repo <scratch> --task task.md
-> RECEIVED -> PLANNING -> PLAN_READY -> ENV_BOOTSTRAP -> BASELINE
-> IMPLEMENTING -> TESTING -> REVIEWING -> READY_FOR_FINALIZE
   paused — waiting on a human

$ devloop resume 3e647a97 --answer approve      # separate process
-> READY_FOR_FINALIZE -> FINALIZED
```

Producing a real branch with a real commit, diffable against `main`. Plus:
30/30 tests, `ruff` clean, `mypy --strict` clean.

## Explicitly not in Phase 0

No model calls, no Docker, no real test execution, no GitHub, no knowledge
base, no domain tables (only LangGraph checkpoints), no telemetry.

---

# Phase 1 — Sandbox + environment 🟡

Where problem 1 ("the cloned repo can't run its tests") actually gets solved.

**Built ahead of Phase 2** (the thin slice real agents needed):

- Per-task clone at `~/.devloop/work/<task-id>` on `devloop/<task-id>`
  (`sandbox/workspace.py`) — agents never touch the user's checkout. Same
  `/workspace` layout the container will bind-mount.
- Environment discovery: `devloop.yml` → lockfile heuristics (`go.mod`,
  `uv.lock`, `package.json` + lockfile) in `env/discovery.py`.
- **Baseline gate** wired to real command execution (`env/runner.py`),
  run on the host. Setup failing at the base commit escalates.

**Still to do:**

- Base sandbox image: git + `claude` + `opencode` + language toolchains
- Bare mirror at `~/.devloop/repos/<owner>/<repo>.git`, then
  `git clone --reference <mirror> --dissociate` per task
  *(preferred over `git worktree`: a worktree's `.git` is a file pointing at
  an absolute host path, which breaks inside a container unless the parent is
  bind-mounted at an identical path)*
- Container lifecycle with `--memory`, `--cpus`, `--pids-limit`, read-only
  root + writable `/workspace`, egress limited to registry + model API
- Service containers via a per-task compose project on an isolated network
- `.devcontainer/` and `.github/workflows/*.yml` discovery
- Bootstrap agent producing a validated `EnvRecipe`
- Image + recipe caching keyed by repo + lockfile hash

**Done when:** pointed at a repo with a deliberately broken test setup, it
either produces a working recipe or escalates cleanly — and never proceeds on
an unattributable baseline.

> ⚠️ Until the container lands, agent CLIs and the repo's own
> `devloop.yml` commands run **on the host**. Point DevLoop only at repos
> you trust.

---

# Phase 2 — Real agents ✅

**Goal:** replace every stub with a real agent CLI behind one interface,
without changing the graph's shape or letting a model choose a transition.

## Why agents came before Docker

The docs originally ordered Phase 1 (sandbox) before Phase 2 (agents). We
swapped them: the hard, uncertain problems — contract validation, prompt
quality, repair behaviour, cost — only show up with real models, while
Docker isolation is well-understood plumbing. A per-task clone plus
host-run checks gave the agents a real repo and real verification in a
fraction of the time. The cost is that, until Phase 1 finishes, agent code
runs on the operator's machine (trusted repos only).

## What was built

### `CodingAgent` seam — `src/devloop/runtimes/`

| File | Contents |
|---|---|
| `base.py` | `CodingAgent` protocol, `AgentInvocation`, `ToolPolicy`, `AgentOutcome` |
| `claude_cli.py` | `claude -p --output-format json`; cost/tokens from the result JSON |
| `opencode_cli.py` | `opencode run --format json`; cost summed from `step_finish` events |
| `codex_cli.py` | registered, raises "not implemented" — the future cross-check reviewer |
| `stub.py` | no-model runtime writing the same contract files; scriptable per role |
| `registry.py` | name → runtime; `register_runtime()` for tests |

Claude isolation, all deliberate: `--permission-mode dontAsk` with an
explicit allowlist; read-only roles get `Edit(./.devloop/out/**)` only;
`--setting-sources ""` + `--strict-mcp-config` so neither the target repo's
`.claude/settings.json` hooks nor the operator's plugins load (runs are
reproducible); `--max-budget-usd` = what's left of the task budget. The
prompt goes via stdin.

### The harness — `src/devloop/agents/harness.py`

`run_role(spec, context, ...)`, identical for every runtime:

1. writes `DevelopmentContext` to `.devloop/in/context.json`
2. deletes any stale `.devloop/out/<role>.json`
3. runs the CLI with the role's `ToolPolicy`
4. validates the output with Pydantic, then the role's deterministic check
5. **one** repair retry with the problems fed back, then escalate

Crashes, timeouts and a read-only role dirtying the workspace are *not*
retried (not format problems). Every attempt becomes an `AgentRunRecord`
in state and in `~/.devloop/tasks/<id>/agent_runs.jsonl`; raw CLI output is
kept under `logs/`.

### Roles — `src/devloop/agents/roles.py`, prompts in `prompts/`

| Role | Contract | Tools |
|---|---|---|
| Requirement | `RequirementResult` | read-only + write own output |
| Planner | `PlanResult` (+ `FileToChange.action`) | read-only + write own output |
| Developer | `ImplementationResult` (new) | edit + the repo's own commands |
| Reviewer | `ReviewResult` | read-only; diff handed over as `.devloop/in/diff.patch` |

Every role may run read-only shell (`git status|diff|log|show`, `ls`,
`cat`, `head`, `tail`, `wc`, `grep`). The Developer may also run the
recipe's setup/check commands. Nobody may commit, push or fetch — the
orchestrator commits after the Developer and publishes the branch to the
target repo.

`ImplementationResult` carries what a diff can't: `deviations`,
`plan_invalid` (→ REPLANNING with evidence), `disputed_findings` (a
finding the Developer declined, e.g. it contradicts the requirements) and
`notes_for_reviewer`.

Prompts are `prompts/<role>/v<N>.md` + a shared `prompts/_contract.md`.
The recorded version is `v<N>+<sha8>` of the exact text, so an unbumped
edit still shows as a different version in `agent_runs`.

### Deterministic checks — `src/devloop/agents/checks.py`

No model, no cost, fed into the repair retry like schema errors:

- **plan**: paths relative and inside the repo, not under `.git`/`.devloop`;
  `modify`/`delete` targets exist, `create` targets don't; ≤ 25 files; no
  duplicates; at least one step
- **requirements**: `needs_clarification` has questions; `ready` has criteria
- **review**: unique finding ids; `changes_requested` has findings
- **implementation**: disputed finding ids exist

### Policy changes — `decide()`

| Rule | Why |
|---|---|
| `escalation_reason` set → `ESCALATED` (in-flight only) | failed side effects route through the policy, not around it |
| `RECEIVED` → `PLANNING` only when requirements are `ready` | previously any requirements skipped clarification |
| `ENV_BOOTSTRAP` with no commands → `ESCALATED` | nothing could verify the change |
| `BASELINE` with failing setup → `ESCALATED` | nothing after it is attributable |
| `PLAN_READY` with a baseline → `IMPLEMENTING` | a replan doesn't re-run baseline |
| `IMPLEMENTING` + `plan_invalid` → `REPLANNING` (capped) | Developer-declared invalid plans |
| `IMPLEMENTING` with an empty diff → `ESCALATED` | no infinite loop on a no-op Developer |
| `TESTING` with new failures → `CHANGES_REQUIRED` | don't pay a Reviewer to read red code |
| `REVIEWING`: `approved` + unresolved P0/P1 → `CHANGES_REQUIRED` | severity beats a contradictory verdict |
| Regression gate compares only the **latest** attempt | a failure fixed in iteration 2 stops counting |
| Unparsed failures key on the command | a compile error is a failure even with no test names |

`iteration` now increments in `implement` (attempts), so the cap still
holds when red checks skip review.

### CLI

```
devloop run --repo <path> --task <file.md> [--runtime claude|opencode|stub]
            [--model sonnet] [--role-runtime reviewer=opencode] [--budget-usd 20]
devloop resume <task-id> --answer <text|approve|cancel>
devloop show <task-id>        # status, cost, every agent run
```

Interrupt payloads (questions, review, escalation reason, how to inspect the
branch) are printed at every gate.

## Bugs fixed on the way

Found in Phase-0 code while wiring real agents:

1. **Repair wiped the previous attempt.** `implement` re-created the task
   branch with `branch -D` every iteration.
2. **Reviewer `plan_conformance` findings could never trigger a replan** —
   `decide()` only looked at human `feedback`.
3. **Regression gate missed build breaks** (failures with no parsed test
   names) and **counted already-fixed failures forever** (it unioned every
   iteration's verification).
4. **`finalize` could never cancel** — it compared the resume payload
   `{"answer": "cancel"}` to the string `"cancel"`.
5. `cost_usd` was never incremented, so the budget guard was dead.

Found by running real models:

6. A bare `Edit` allow rule does **not** cover Claude's `Write` tool (a
   path-scoped `Edit(...)` rule does). The Developer couldn't create its
   output file; it now gets both.
7. The Developer piped test output through `tail` and was denied; the
   read-only utility allowlist and an explicit "your tools" prompt section
   fixed it.
8. Given a review finding that contradicted the requirements, the Developer
   correctly refused — but had no way to say so except prose, so the
   escalation read "made no changes". Hence `disputed_findings`.
9. `devloop.yml` with `unit: true` failed validation (YAML bool).

## Verified

- 118 tests: routing table (50 cases), harness (repair, give-up, crash,
  read-only violation, stale output), plan checks, adapters' argv/parsing,
  workspace, env, prompts, and end-to-end graph runs with the stub runtime
  (happy path, repair cycle, iteration cap, red checks skip review, replan,
  clarification, dispute, missing recipe, invalid output, resume in a fresh
  process). `ruff` + `mypy --strict` clean.
- **Real run** (`claude`, Sonnet) on a small Python repo, "add `slugify`
  with tests": correct 2-file diff, 9/9 tests green, **$0.23**, ~70s.
- **Real repair cycle**: real Requirement/Planner/Developer, scripted
  Reviewer rejecting once with a P1 testing finding → iteration 2 was a
  3-line commit adding exactly that test → approved. **$0.25** total.

## Known gaps → later phases

- Crash-resume of an agent node re-runs the agent (double spend) — Phase 5.
- Human feedback findings are never marked resolved — Phase 3 (`devloop feedback`).
- `escalate` can only `cancel`; retry-from-here is Phase 3.
- The Reviewer occasionally cites line numbers beyond the file's length;
  a cheap deterministic check is a candidate addition.

---

# Phase 3 — Human loop ⬜

- `clarify` interrupt with real terminal Q&A, answers folded into
  `acceptance_criteria`
- `devloop feedback` mapping free text to `Finding` objects
- Scoped feedback — re-open one finding, not the whole change
- `ESCALATED` surfaced with the reason and the capability gap
- Requirements + plan + deviations + evidence written to `.devloop/` alongside
  the branch, so the plan is reviewable

---

# Phase 4 — Knowledge base ⬜

- `repo_knowledge` store with confidence, provenance, decay
- `harvest_knowledge` node after every task
- Rendering into `/workspace/.devloop/knowledge/*.md`
- CLI approval flow for proposed facts
- Optional `AGENTS.md` emission back into the target repo

---

# Phase 5 — Hardening + telemetry ⬜

- Budget and iteration enforcement under real cost accounting
- Retry/backoff on CLI failures
- Secret redaction in logs and artifacts
- Guaranteed sandbox teardown on crash
- Domain tables (`dev_tasks`, `agent_runs`, `test_runs`, `task_events`, …)
  via alembic
- OTel GenAI spans → LangSmith

**Resume-safety test (the LangGraph-specific risk):** interrupt a task
mid-`implement`, kill the process, resume — assert no duplicate container, no
duplicate branch, no duplicate agent run. Repeat for `clarify` and `finalize`.

---

# v1 — GitHub adapter ⬜

The core graph does not change. Only `sources/` and the sink.

- GitHub App registration, scoped installation tokens
- Webhook receiver: verify `X-Hub-Signature-256` synchronously, enqueue,
  return within GitHub's 10s timeout
- `X-GitHub-Delivery` idempotency so a redelivery cannot fork a second task
- Events: `issues`, `issue_comment`, `pull_request`,
  `pull_request_review`, `pull_request_review_comment`, `workflow_run`
- Issue-comment requirement Q&A
- PR creation with **plan as the PR body**, pinned status comment updated on
  every transition
- PR review comments → scoped `Finding` → same repair path

---

# v2 — Improvement engine ⬜

Only after a real change has merged.

- Eval datasets assembled from `agent_runs` + `human_feedback`
- Failure clustering over traces
- GEPA-style reflective prompt evolution against a held-out set
- Proposed harness changes raised as PRs against this repo, reviewed by a human
- Benchmark to prove a change is actually better before it ships
