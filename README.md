# DevLoop

An autonomous development loop: a task becomes a reviewed, tested change
through a fixed pipeline of specialist agents, with humans gating the two
decisions that matter — *are the requirements right* and *should this ship*.

```
task ──► requirements ──► plan ──► environment ──► implement ──► verify ──► review ──► branch
            ▲  ⏸ human                                 │            │
            └──────────────── replan ◄─────────────────┘            │
                              repair ◄──────────────────────────────┘
                                                          ⏸ human merge gate
```

**Design rule:** agents produce artifacts; the orchestrator owns state and
permissions. A pure `decide()` function is the only code that chooses the next
step, and it reads only stored facts — never a model's opinion about what
should happen next.

> **Status: Phase 0.** The state machine, contracts, graph, human gates and
> CLI are built and verified end to end — with **stub agents that make no
> model calls**. Real agents and the Docker sandbox are Phase 1–2. See
> [`docs/phases.md`](docs/phases.md).

---

## Requirements

- Python ≥ 3.12
- [`uv`](https://docs.astral.sh/uv/)
- git

Nothing else for Phase 0 — no database server, no Docker, no API keys.

## Setup

```bash
uv sync
```

## Usage

DevLoop works against a **target repository** — the repo it writes code in,
which is not this repo. Point it at a scratch repo first.

```bash
# 1. a target repo with a clean working tree
mkdir /tmp/demo && cd /tmp/demo && git init
echo "# demo" > README.md && git add -A && git commit -m "initial"

# 2. describe the task in markdown
echo "Add a changelog note describing today's work." > /tmp/task.md

# 3. run it
cd /path/to/dev-loop
uv run devloop run --repo /tmp/demo --task /tmp/task.md
```

```
╭──────────────────────────────────────────────╮
│ task 3e647a97: task                          │
│ repo: /tmp/demo                              │
╰──────────────────────────────────────────────╯
-> RECEIVED
-> PLANNING
-> PLAN_READY
-> ENV_BOOTSTRAP
-> BASELINE
-> IMPLEMENTING
-> TESTING
-> REVIEWING
-> READY_FOR_FINALIZE
╭──────────── waiting on a human ──────────────╮
│ paused — resume with:                        │
│   devloop resume 3e647a97 --answer '...'     │
╰──────────────────────────────────────────────╯
```

The run pauses at a human gate. Approve it — from a *separate* process, state
is persisted:

```bash
uv run devloop resume 3e647a97 --answer approve
# -> READY_FOR_FINALIZE -> FINALIZED
```

Inspect what it did:

```bash
cd /tmp/demo
git log --oneline devloop/3e647a97
git diff main devloop/3e647a97
```

### Commands

| Command | Purpose |
|---|---|
| `devloop run --repo <path> --task <file.md>` | Start a task |
| `devloop resume <task-id> --answer <text>` | Answer a human gate and continue |

`devloop run` also accepts `--budget-usd` (default `20.0`) — the task
escalates to a human rather than continuing once estimated cost reaches it.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DEVLOOP_DATABASE_URL` | unset | Postgres DSN for checkpoints. Unset → SQLite at `~/.devloop/checkpoints.sqlite` |

State persists across CLI invocations by design: `run` and `resume` are
separate processes, so an in-memory store would lose everything between them.

### Safety notes

- DevLoop **refuses to run against a repo with uncommitted changes**.
- Each task gets its own `devloop/<task-id>` branch, always forked from the
  repo's base branch — never from a previous task's branch.
- Nothing is pushed. Phase 0 produces local branches only.

---

## Development

```bash
uv run pytest          # 30 table-driven policy tests, ~20ms, no fixtures
uv run ruff check src tests
uv run mypy src
```

The routing tests are the highest-value tests in the repo: `decide()` is pure,
so every transition and guard rejection is a table row. If they're green, the
state machine cannot skip a gate regardless of what a model produces.

## Layout

```
src/devloop/
  contracts/     # Pydantic artifact contracts + status enum — the API surface
  graph/
    routing.py   # decide(): the pure orchestrator policy
    build.py     # LangGraph wiring, status -> node routing
    nodes/       # node implementations + Phase 0 stub agents
  sandbox/       # git isolation now; Docker sandbox in Phase 1
  cli/           # typer CLI: run, resume
  runtimes/      # CodingAgent adapters (claude, opencode, codex) — Phase 2
  env/           # environment discovery + bootstrap — Phase 1
  knowledge/     # per-repo knowledge base — Phase 4
  sources/       # local CLI now; GitHub App in v1
  store/         # domain tables — Phase 5
  telemetry/     # OTel -> LangSmith — Phase 5
prompts/         # versioned per role; mutation target of the v2 engine
docs/
tests/
```

Empty packages are intentional — they mark the adapter seams described in the
architecture doc, so each phase fills a slot rather than reshaping the tree.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — the North Star: two-plane
  design, contracts, the five hard problems (broken test environments, plans
  that die on contact, unwanted changes, staying local, the knowledge base)
- [`docs/phases.md`](docs/phases.md) — what each phase contains, what Phase 0
  actually built, and what "done" means for the phases ahead

## Stack

Python · LangGraph (durable checkpoints + human-in-the-loop interrupts) ·
Pydantic (contracts) · Typer · SQLite/Postgres · LangSmith (traces, Phase 5).

Agents run as **CLI subprocesses** (`claude`, `opencode`, later `codex`)
inside a sandbox, behind one `CodingAgent` interface — so swapping which model
implements and which reviews is a config change.
