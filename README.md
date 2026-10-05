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

> **Status: Phase 2.** Real agents (`claude`, `opencode`) run every role
> behind file-based contracts, with a per-task clone and real test runs.
> The Docker sandbox (rest of Phase 1) is next — until then agents and the
> repo's check commands run **on your machine**, so only point DevLoop at
> repos you trust. See [`docs/phases.md`](docs/phases.md).

---

## Requirements

- Python ≥ 3.12, [`uv`](https://docs.astral.sh/uv/), git
- An agent CLI: [`claude`](https://docs.claude.com/en/docs/claude-code)
  (default) or [`opencode`](https://opencode.ai) — or `--runtime stub` to
  run the whole loop with no model and no cost

## Setup

```bash
uv sync
```

## Usage

DevLoop works against a **target repository** — the repo it writes code in,
which is not this repo. It clones it to `~/.devloop/work/<task-id>`, so your
checkout is never touched; the result appears as a `devloop/<task-id>`
branch in your repo.

### 1. Tell DevLoop how to check the repo

Add a `devloop.yml` to the target repo (Go, `uv` Python and npm/pnpm/yarn
repos are auto-detected without one):

```yaml
setup:                       # optional, runs before every check
  - uv sync
commands:                    # name -> command, all must pass (or fail as before)
  lint: uv run ruff check .
  unit: uv run pytest -q
```

### 2. Describe the task and run it

```bash
echo "Add a slugify(text) helper to textutil/words.py, with tests." > /tmp/add-slugify.md
uv run devloop run --repo ~/code/textutil --task /tmp/add-slugify.md --model sonnet --budget-usd 3
```

```
-> RECEIVED  $0.0000
-> PLANNING  $0.0540
-> PLAN_READY  $0.1045
-> ENV_BOOTSTRAP  $0.1045
-> BASELINE  $0.1045
-> IMPLEMENTING  $0.1045
-> TESTING  $0.1748
-> REVIEWING  $0.1748
-> READY_FOR_FINALIZE  $0.2262
╭──────────────── waiting on a human ─────────────────╮
│ gate: finalize                                      │
│ branch: devloop/c81908dc                            │
│ inspect: git -C ~/code/textutil diff 792f810b41..devloop/c81908dc │
│ review: { "verdict": "approved", ... }              │
╰─────────────────────────────────────────────────────╯
```

### 3. Inspect, then approve or cancel

```bash
git -C ~/code/textutil diff main..devloop/c81908dc
uv run devloop show c81908dc                      # cost + every agent run
uv run devloop resume c81908dc --answer approve   # or --answer cancel
```

A task can also pause to ask a clarifying question (`--answer` with your
reply) or escalate (budget, iteration/replan caps, an agent failure, a
disputed finding) — `devloop show` prints the reason.

### Commands

| Command | Purpose |
|---|---|
| `devloop run --repo <path> --task <file.md>` | Start a task |
| `  --runtime claude\|opencode\|stub` | Agent CLI for every role (default `claude`) |
| `  --model <name>` | Model passed to the CLI (`sonnet`, `opus`, …) |
| `  --role-runtime reviewer=opencode` | Per-role override, repeatable |
| `  --budget-usd <n>` | Escalate once spend reaches this (default 20) |
| `devloop resume <task-id> --answer <text>` | Answer a human gate and continue |
| `devloop show <task-id>` | Status, cost, escalation reason, agent runs |

### Where things live

| Path | What |
|---|---|
| `~/.devloop/checkpoints.sqlite` | graph state (`DEVLOOP_DATABASE_URL` for Postgres) |
| `~/.devloop/work/<task-id>/` | the task's clone; `.devloop/in` / `.devloop/out` hold agent I/O |
| `~/.devloop/tasks/<task-id>/` | `agent_runs.jsonl`, raw CLI logs, check logs |
| `prompts/<role>/v<N>.md` | versioned prompts (`DEVLOOP_PROMPT_<ROLE>=v<N>` pins one) |

`DEVLOOP_HOME` moves `~/.devloop`.

## Development

```bash
uv run pytest            # no model calls, no network
uv run ruff check . && uv run mypy src
```
