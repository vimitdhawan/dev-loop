# DevLoop — Build Phases

Status of each phase, what it contains, and what "done" means for it.
Architecture context lives in [`architecture.md`](./architecture.md).

| Phase | Scope | Status |
|---|---|---|
| **0** | Walking skeleton — state machine, contracts, graph, local CLI | ✅ **Done** |
| **1** | Sandbox + environment bootstrap | ⬜ Next |
| **2** | Real agents (Reviewer → Planner → Requirement → Developer) | ⬜ |
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

# Phase 1 — Sandbox + environment ⬜

Where problem 1 ("the cloned repo can't run its tests") actually gets solved.

- Base sandbox image: git + `claude` + `opencode` + language toolchains
- Bare mirror at `~/.devloop/repos/<owner>/<repo>.git`, then
  `git clone --reference <mirror> --dissociate` per task
  *(preferred over `git worktree`: a worktree's `.git` is a file pointing at
  an absolute host path, which breaks inside a container unless the parent is
  bind-mounted at an identical path)*
- Container lifecycle with `--memory`, `--cpus`, `--pids-limit`, read-only
  root + writable `/workspace`, egress limited to registry + model API
- Streamed log capture to per-task artifacts
- Service containers via a per-task compose project on an isolated network
- Environment discovery: `devloop.yml` → `.devcontainer/` →
  `.github/workflows/*.yml` → lockfile heuristics
- Bootstrap agent producing a validated `EnvRecipe`
- **Baseline gate** wired to real command execution
- Image + recipe caching keyed by repo + lockfile hash

**Done when:** pointed at a repo with a deliberately broken test setup, it
either produces a working recipe or escalates cleanly — and never proceeds on
an unattributable baseline.

---

# Phase 2 — Real agents ⬜

Order is deliberate: **Reviewer → Planner → Requirement → Developer.**

Reviewer first because it is pure input → JSON, so it builds the
contract-validation and repair-retry harness with no repo mutation. Developer
last because it is the only role that writes code.

- `CodingAgent` interface + `claude` and `opencode` adapters
  (`codex` stubbed — not installed locally, highest value later as the
  cross-check reviewer)
- File-based contract: agent writes `/workspace/.devloop/out/<role>.json`,
  orchestrator validates with Pydantic, one repair retry on validation error
- `DevelopmentContext` assembled by the orchestrator and passed as
  `/workspace/.devloop/in/context.json`, never as a giant prompt string
- Versioned prompts in `prompts/`, recorded per run
- Deterministic plan check before implementing
- Cost and token capture from each CLI's output into `agent_runs`

**Done when:** a small, well-specified task on a real repo produces a correct
diff, and a second run where the Reviewer must request changes exercises the
repair cycle and the iteration cap.

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
