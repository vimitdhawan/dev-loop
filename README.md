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

> **Status: Phase 3.** An agent team — Product Owner, Engineer, QA and
> Reviewer — takes a task from requirements to a PR, with QA testing the
> running app in a real browser. The Docker sandbox is still ahead: until
> then agents, check commands and the app run **on your machine**, so only
> point DevLoop at repos you trust. See [`docs/phases.md`](docs/phases.md).

---

## The team

| Role | Does | Session |
|---|---|---|
| **Product Owner** | turns the task into testable requirements; answers the Engineer's questions (or asks you) | kept for the task |
| **Engineer** | plans, implements, writes unit + integration tests, fixes QA bugs and review comments | **one session** for plan → build → fixes |
| **QA** | tests the running app in headless Chromium (Playwright MCP), with screenshots | kept across retests |
| **Reviewer** | reviews the diff against the plan; **final approver** | **fresh every time** |

```
PO → Engineer plans → plan check → Engineer builds → checks → QA → Reviewer → push + PR → you merge
      ↑ questions ↓        red checks / QA bugs / review comments go back to the Engineer
```

Agents never talk to each other directly — every question, answer, bug and
comment is a validated document the orchestrator routes and records.

## Requirements

- Python ≥ 3.12, [`uv`](https://docs.astral.sh/uv/), git
- An agent CLI: [`claude`](https://docs.claude.com/en/docs/claude-code)
  (default) or [`opencode`](https://opencode.ai) — or `--runtime stub` to
  run the whole loop with no model and no cost
- For browser QA: Node (`npx`) — Playwright MCP is fetched on first use
- For PRs: the [GitHub CLI](https://cli.github.com) logged in (`gh auth login`)

## Setup

```bash
uv sync
cp devloop.config.example.yaml devloop.config.yaml   # then edit
uv run devloop config                                # show the effective config
```

## Configure

### Your team and source — `devloop.config.yaml`

Looked up at `DEVLOOP_CONFIG` → `./devloop.config.yaml` →
`~/.devloop/config.yaml`. Precedence: CLI flag > env var > file > default.

```yaml
agents:
  product_owner: {runtime: claude, model: sonnet}
  engineer:      {runtime: claude, model: opus, timeout_s: 2400}
  qa:            {runtime: claude, model: sonnet}
  reviewer:      {runtime: claude, model: opus}
repo:
  url: https://github.com/your-org/your-repo.git   # or a local path
  base_branch: main                                 # env: DEVLOOP_REPO_URL / DEVLOOP_BASE_BRANCH
pull_request: {enabled: true, draft: false}
budget_usd: 20
```

Every task works in a **fresh clone** of `base_branch` at
`~/.devloop/work/<task-id>`; your checkout is never touched.

### How the target repo builds, tests and runs — its `devloop.yml`

Go, `uv` Python and npm/pnpm/yarn repos are auto-detected without one, but
browser QA needs an `app:` section:

```yaml
setup: [npm ci]
commands:                         # all orchestrator-run; the only results that count
  lint: npm run lint
  unit: npm test
app:                              # optional — enables browser QA
  start: npm run dev -- -p 3100
  url: http://localhost:3100
  ready_timeout_s: 120
  env: {NEXT_PUBLIC_SUPABASE_URL: http://127.0.0.1:54421}
  setup: [supabase start]         # before start
  teardown: [supabase stop]       # after QA
```

## Run

```bash
uv run devloop run --task tasks/reusable_time_picker.md            # repo from config/env
uv run devloop run --task t.md --repo ../playrotation --base-branch develop
uv run devloop run --task t.md --role-model engineer=opus --no-pr  # one-off overrides
```

```
-> RECEIVED → PLANNING → PLAN_READY → ENV_BOOTSTRAP → BASELINE → IMPLEMENTING
   engineer/engineer_implement (claude, attempt 1, resumed session)
-> TESTING → QA_TESTING
   app ready at http://127.0.0.1:4173/
-> REVIEWING → PUBLISHING → READY_FOR_FINALIZE   $0.4764
╭──────────────── waiting on a human ─────────────────╮
│ gate: finalize                                      │
│ pull_request: https://github.com/org/repo/pull/42   │
│ summary: ~/.devloop/tasks/72d7693f/summary.md       │
╰─────────────────────────────────────────────────────╯
```

Then review and merge the PR on GitHub, and close the task:

```bash
uv run devloop show 72d7693f                     # status, cost, every agent run (↻ = resumed session)
uv run devloop resume 72d7693f --answer approve  # or --answer cancel
```

A task can also pause to ask you something — the Product Owner's questions
about the task, or an Engineer question the Product Owner couldn't answer.
Reply with `devloop resume <id> --answer '...'` and it continues where it
stopped. It escalates on budget, iteration/replan/question caps, an agent
failure, QA being blocked, or a disputed finding; `devloop show` says why.

### Watch it in LangGraph Studio

The same graph the CLI runs is exported for LangGraph's dev server
(`langgraph.json` → `src/devloop/graph/studio.py`):

```bash
uv run langgraph dev            # opens Studio: https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024
```

In Studio, pick the `devloop` graph and start a run with:

```json
{"task": {"title": "default rating", "description": "<paste the task markdown>"}}
```

Repo, base branch, team, PR and budget come from `devloop.config.yaml`; put
`repo` / `base_branch` inside `task`, or a `team` object, to override them.
You see every node as it runs, the full state after each one (requirements,
plan, QA report, review, agent runs and costs), and the human gates appear
as interrupts you answer in the UI (`{"answer": "approve"}`).

Things to know:

- Studio runs live in the dev server's own store, **not** in
  `~/.devloop/checkpoints.sqlite` — a task started in Studio is driven from
  Studio, and one started with `devloop run` is driven from the CLI.
- The Studio UI is served from smith.langchain.com and connects to your
  local server; your graph, state and code stay on your machine. LangSmith
  *tracing* (`LANGSMITH_TRACING=true` + an API key in `.env`) is off by
  default — turning it on uploads task descriptions, plans and diffs to
  LangSmith, so check that's acceptable for the repo first (GDPR / internal code).
- The server logs "Deserializing unregistered type devloop.contracts…"
  warnings: `langgraph-api` 0.15 has no way to allow-list msgpack types. They
  are harmless today; the CLI path registers them explicitly.

### Choosing models

Each role's `model` is passed to its runtime as is:

- **claude**: an alias (`sonnet`, `opus`, `haiku`) or a full model id.
- **opencode**: always `provider/model` — exactly as `opencode models` prints
  it, e.g. `nvidia/meta/muse-glimmer-30b`, **not** `meta/muse-glimmer-30b`
  (opencode would read `meta` as the provider and fail with an opaque
  "Unexpected server error"). `devloop run` now checks opencode ids before
  starting and suggests the right one.

Before trusting a model with a task, probe it:

```bash
uv run devloop probe                                     # every runtime/model on your team
uv run devloop probe --runtime opencode --model nvidia/z-ai/glm-5.3 --model nvidia/moonshotai/kimi-k3
```

A probe is one tiny real run through the same file contract every step uses
(read `.devloop/in/context.json`, write a JSON document with a file tool). A
model that fails it — no tool calling, ignores instructions, wrong id, too
slow — would fail every real step too.

Rules of thumb: the **Engineer** and **Reviewer** need the strongest model
you have; the **Product Owner** and **QA** can run on a cheaper/faster one.
QA's browser tools are text-based (accessibility snapshots), so it doesn't
need a vision model. Free-tier models are often slow — raise `timeout_s`.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `product_owner agent failed: {"name": "UnknownError" … "Unexpected server error"}` with opencode | model id missing its provider — use the full id from `opencode models` (`devloop run` now catches this up front) |
| `… timed out after 600s` | slow model — `timeout_s` per role in the config, or `--agent-timeout` |
| `…:3100 is already in use` | your own dev server is on the app's port — stop it; QA must test the task's build |
| `setup fails on the untouched base commit` | the repo's `devloop.yml` `setup` doesn't work on a fresh clone — fix it on the base branch first |
| `no devloop.yml and no recognised build manifest` | add a `devloop.yml` with `commands:` |
| `gh pr create failed` | `gh auth status`; the token needs `repo` scope |
| an agent seems stuck | the `tail -f` path in the heartbeat log line shows its live output |

### Commands

| Command | Purpose |
|---|---|
| `devloop run --task <file.md>` | Start a task |
| `  --repo <path\|url>` / `--base-branch <b>` | Source (else env / config) |
| `  --runtime` / `--model` | Override every role |
| `  --role-runtime qa=opencode` / `--role-model engineer=opus` | Override one role, repeatable |
| `  --agent-timeout <s>` / `--budget-usd <n>` / `--pr/--no-pr` | Limits and PR |
| `devloop resume <task-id> --answer <text>` | Answer a gate and continue |
| `devloop show <task-id>` | Status, PR, cost, escalation reason, agent runs |
| `devloop config` | Effective configuration and its source |
| `devloop probe [--runtime r --model m …]` | Check each runtime/model can do a DevLoop step |
| `langgraph dev` | Run the graph in LangGraph Studio |

### Where things live

| Path | What |
|---|---|
| `~/.devloop/checkpoints.sqlite` | graph state (`DEVLOOP_DATABASE_URL` for Postgres) |
| `~/.devloop/work/<task-id>/` | the task's clone; `.devloop/in`, `.devloop/out`, `.devloop/qa` (screenshots) |
| `~/.devloop/tasks/<task-id>/` | `summary.md` (PR body), `agent_runs.jsonl`, agent/check/app logs |
| `prompts/<step>/v<N>.md` | versioned prompts (`DEVLOOP_PROMPT_<STEP>=v<N>` pins one) |

`DEVLOOP_HOME` moves `~/.devloop`.

## Development

```bash
uv run pytest            # no model calls, no network
uv run ruff check . && uv run mypy src
```
