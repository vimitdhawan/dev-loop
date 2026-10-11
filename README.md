# DevLoop

An autonomous development loop: a task — a markdown file or a labelled
GitHub issue — becomes a reviewed, tested pull request through a team of
specialist agents, with humans gating the decisions that matter.

```
issue / task ─► workflow ─► agent ─► artifact ─► agent ─► artifact ─► … ─► PR
                (bug, feature, ui_feature, refactor — picks which agents run)
```

**Design rule:** agents produce artifacts; the orchestrator owns state and
permissions. Each agent's validated output becomes a state field that the
next agent is handed — never a shared conversation — and a pure `decide()`
function is the only code that chooses the next step, reading only those
stored facts.

> **Status: Phase 4.** Workflows per task type, a separate Planner, a UX
> stage (Stitch-capable), every hand-off visible in the CLI and on disk, and
> `devloop watch` for GitHub issues. The Docker sandbox is still ahead:
> until then agents, check commands and the app run **on your machine**, so
> only point DevLoop at repos you trust. See [`docs/phases.md`](docs/phases.md).

---

## The team

| Role | Hands on | Session |
|---|---|---|
| **Product Owner** | `requirements` — testable acceptance criteria; answers the team's questions (or asks you) | kept for the task |
| **UX** | `design` — screens, states, interactions, guidelines; uses Stitch when configured | kept for the task |
| **Planner** | `plan` — files, steps, tests; checked against the repo before any code changes | kept across replans |
| **Engineer** | `implementation` — builds the plan with unit + integration tests; fixes QA bugs and review comments | kept across fixes |
| **QA** | `qa` — tests the running app in headless Chromium (Playwright MCP), with screenshots | kept across retests |
| **Reviewer** | `review` — reviews the diff against the plan and design; **final approver** | **fresh every time** |

```
PO → UX → Planner → plan check → Engineer → checks → QA → Reviewer → push + PR → you merge
   ↑ questions from UX / Planner / Engineer   red checks / QA bugs / review comments → Engineer
```

Agents never talk to each other directly and never share a conversation: a
session only carries a role's memory of its *own* work. Every requirement,
design, plan, question, answer, bug and comment is a validated document the
orchestrator stores and hands to the next agent.

## Workflows

Not every task needs every agent. Each task gets a **workflow** — an ordered
subset of the team — when it starts:

| Workflow | Stages | Picked by label |
|---|---|---|
| `bug` | planner → engineer → qa → reviewer | `bug` |
| `ui_feature` | product_owner → ux → planner → engineer → qa → reviewer | `ui`, `ux`, `design`, `frontend` |
| `feature` (default) | product_owner → planner → engineer → qa → reviewer | `feature`, `enhancement` |
| `refactor` | planner → engineer → qa → reviewer | `refactor`, `tech-debt` |

Selection is deterministic: `--workflow <name>` → the first workflow (config
order, then the built-ins above) with a label the task carries → `default_workflow`.
Redefine any of them or add your own under `workflows:` in the config —
Planner, Engineer and Reviewer are required, the rest optional. Without a
Product Owner the task description *is* the requirement, and questions go
straight to you.

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
  ux:            {runtime: claude, model: sonnet}   # unset → runs as product_owner
  planner:       {runtime: claude, model: opus}     # unset → runs as engineer
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
`~/.devloop/work/<task-id>`; your checkout is never touched. The full
reference, including `workflows:` and `github:`, is
[`devloop.config.example.yaml`](devloop.config.example.yaml).

### Design tools for UX — Stitch or any MCP server

Any role can get extra MCP servers; the UX role is where design tools go:

```yaml
agents:
  ux:
    runtime: claude
    model: sonnet
    mcp_servers:
      stitch:
        url: https://stitch.googleapis.com/mcp      # or command: [npx, -y, some-mcp]
        headers: {X-Goog-Api-Key: "${STITCH_API_KEY}"}
```

`${VAR}` is read from the environment when the agent starts (`devloop run`
refuses to start if it's unset), so keys stay out of the config file; the
per-run MCP config holding the resolved key is owner-only and deleted when
the agent exits. With no design tool, UX designs in words. Either way its
`design` document — screens, states, interactions, guidelines and links to
the Stitch screens — is what the Planner, Engineer, QA and Reviewer get.

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
uv run devloop run --task t.md --workflow bug                      # or --label bug
uv run devloop run --task t.md --repo ../playrotation --base-branch develop
uv run devloop run --task t.md --role-model engineer=opus --no-pr  # one-off overrides
```

Each agent's hand-off is printed as it lands (`-q` for status lines only). Abridged, illustrative:

```
╭ task 72d7693f: reusable time picker ─────────────────────────────────────╮
│ workflow: ui_feature (label: ui): product_owner → ux → planner → …       │
ingest → DESIGNING $0.0712
╭─ Product Owner → requirements ───────────────────────────────────────────╮
│ acceptance criteria                                                      │
│   • Selecting 10:30 shows `10:30` in the field and saves `10:30:00`      │
design → PLANNING $0.1830
╭─ UX → design ────────────────────────────────────────────────────────────╮
│ time picker — choose a slot without typing                               │
│   state: invalid: field outlined red, "Use HH:MM"                        │
plan → PLAN_READY $0.2954
╭─ Planner → plan (handed to the Engineer) ────────────────────────────────╮
│ files   modify  src/components/TimeField.tsx   reuse the field's styles  │
│ steps   1. …                                                             │
… Engineer → implementation · checks · QA → passed · Reviewer → approved
publish → READY_FOR_FINALIZE $0.6120
╭──────────────── waiting on a human ─────────────────╮
│ pull_request: https://github.com/org/repo/pull/42   │
```

Then review and merge the PR on GitHub, and close the task:

```bash
uv run devloop resume 72d7693f --answer approve  # or --answer cancel
```

### Inspect a task later

```bash
uv run devloop show 72d7693f              # every artifact, then every agent run (↻ = resumed)
uv run devloop show 72d7693f --run 4      # run #4's exact input (context.json) and output
uv run devloop show 72d7693f --json plan  # one state field as JSON (requirements, design, qa, …)
uv run devloop show 72d7693f --events     # node-by-node timeline
```

The graph itself writes each task's record — `state.json` after every
node, `events.jsonl`, and per agent run `runs/NNN-<step>-a<n>/` with the
exact `context.json`, `prompt.md` and `output.json` — so this works for
tasks run from `devloop run`, `devloop watch` **and** LangGraph Studio, and
it's what a TUI, web UI or eval would read.

A task can also pause to ask you something — the Product Owner's questions
about the task, or a UX / Planner / Engineer question the Product Owner
couldn't answer (or any question, in a workflow without a Product Owner).
Reply with `devloop resume <id> --answer '...'` and it continues where it
stopped. It escalates on budget, iteration/replan/question caps, an agent
failure, QA being blocked, or a disputed finding; `devloop show` says why.

### Run from GitHub issues — `devloop watch`

```yaml
github:
  repo: your-org/your-repo      # default: from repo.url
  labels: [devloop]             # issues need all of these
  exclude_labels: [wip]
  priority_labels: [p0, p1]
  poll_interval_s: 300
  max_concurrent: 1
```

```bash
uv run devloop watch --dry-run   # eligible issues in pick order, with the workflow each gets
uv run devloop watch --once      # one poll: start what fits, wait, exit (for cron)
uv run devloop watch             # poll every poll_interval_s until Ctrl-C
```

Each poll lists open issues carrying `labels`, drops any with an
`exclude_labels` or DevLoop status label, orders them (earliest
`priority_labels` match, then oldest, then lowest number) and starts as
many as `max_concurrent` allows. The issue's labels pick its workflow.

| Issue label | Means | To retry |
|---|---|---|
| `devloop:in-progress` | a run is going | — |
| `devloop:pr-open` | approved by review, PR opened (`Closes #n` in the body) | — |
| `devloop:needs-human` | waiting on an answer or escalated — the comment says why and gives the `devloop resume` command | answer it with `devloop resume`, or remove the label |
| `devloop:failed` | crashed or cancelled | remove the label |

An issue is never run twice at once: a claim in
`~/.devloop/automation.sqlite` (atomic across processes on this machine)
plus the in-progress label (visible to other machines — best effort, this is
not a distributed lock). A watcher killed mid-run flags its issues
`needs-human` on the next start. `devloop resume` on a watched task updates
the issue the same way.

### Watch it in LangGraph Studio

The same graph the CLI runs is exported for LangGraph's dev server
(`langgraph.json` → `src/devloop/graph/studio.py`):

```bash
uv run langgraph dev            # opens Studio: https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024
```

In Studio, pick the `devloop` graph and start a run with:

```json
{"task": {"title": "default rating", "description": "<paste the task markdown>", "labels": ["ui"]}}
```

Repo, base branch, team, PR and budget come from `devloop.config.yaml`; put
`repo` / `base_branch` / `workflow` / `labels` inside `task`, or a `team`
object, to override them.
You see every node as it runs, the full state after each one (requirements,
plan, QA report, review, agent runs and costs), and the human gates appear
as interrupts you answer in the UI (`{"answer": "approve"}`).

Things to know:

- Studio runs live in the dev server's own store, **not** in
  `~/.devloop/checkpoints.sqlite` — a task started in Studio is *driven*
  from Studio, and one started with `devloop run` from the CLI. Both write
  the same task record, so `devloop show <task-id>` (the id is in the
  state's `task.external_id`) inspects either.
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
| `MCP server 'stitch' needs STITCH_API_KEY` | export the variable the config's `${…}` refers to |
| `unknown workflow 'x'` | `--workflow` / `task.workflow` must name one in `devloop config` |
| `devloop watch` picks nothing | `--dry-run`; the issue needs every `github.labels` and no `devloop:*` label |

### Commands

| Command | Purpose |
|---|---|
| `devloop run --task <file.md>` | Start a task |
| `  --repo <path\|url>` / `--base-branch <b>` | Source (else env / config) |
| `  --runtime` / `--model` | Override every role |
| `  --role-runtime qa=opencode` / `--role-model engineer=opus` | Override one role, repeatable |
| `  --agent-timeout <s>` / `--budget-usd <n>` / `--pr/--no-pr` | Limits and PR |
| `  --workflow <name>` / `--label <l>` / `-q` | Pick the workflow; quiet output |
| `devloop resume <task-id> --answer <text>` | Answer a gate and continue |
| `devloop show <task-id> [--run N] [--json F] [--events]` | Artifacts, agent runs, exact hand-offs, timeline |
| `devloop watch [--once] [--dry-run]` | Run on labelled GitHub issues |
| `devloop config` | Effective configuration and its source |
| `devloop probe [--runtime r --model m …]` | Check each runtime/model can do a DevLoop step |
| `langgraph dev` | Run the graph in LangGraph Studio |

### Where things live

| Path | What |
|---|---|
| `~/.devloop/checkpoints.sqlite` | graph state (`DEVLOOP_DATABASE_URL` for Postgres) |
| `~/.devloop/work/<task-id>/` | the task's clone; `.devloop/in`, `.devloop/out`, `.devloop/qa` (screenshots) |
| `~/.devloop/tasks/<task-id>/` | `state.json`, `events.jsonl`, `runs/NNN-*/` (context, prompt, output), `summary.md` (PR body), `agent_runs.jsonl`, logs |
| `~/.devloop/automation.sqlite` | `devloop watch` issue claims |
| `prompts/<step>/v<N>.md` | versioned prompts (`DEVLOOP_PROMPT_<STEP>=v<N>` pins one) |

`DEVLOOP_HOME` moves `~/.devloop`.

## Development

```bash
uv run pytest            # no model calls, no network
uv run ruff check . && uv run mypy src
```
