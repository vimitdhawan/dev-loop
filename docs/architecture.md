# DevLoop — North Star Architecture

This is the system we are building toward. It is not all implemented — see
[`phases.md`](./phases.md) for what exists today and what is still ahead.

Every piece built in an early phase is a real component of this design, never
a throwaway. When v0 does something the simple way (local CLI instead of a
GitHub App, direct git instead of a container), the seam is drawn so the
swap is a one-module change.

---

## What DevLoop is

A task — a markdown file today, a GitHub issue later — becomes a reviewed,
tested change through a fixed pipeline run by an agent **team**, with humans
gating the two decisions that matter: *are the requirements right* and
*should this merge*.

```
task ──► Product Owner ──► Engineer: plan ──► env ──► Engineer: implement ──► checks ──► QA ──► Reviewer ──► PR
              ▲ ⏸ human        │  ▲ answers                 │                    │         │        │
              └── questions ◄──┴──┴────────── questions ◄────┘                    │         │        │
                                    replan ◄──────────────────────────────────────┼─────────┼────────┤
                                    repair ◄──────────────────────────── red ─────┴── bugs ─┴─ changes
                                                                                         ⏸ human merge gate
```

| Role | Does | Session |
|---|---|---|
| Product Owner | requirements; answers the Engineer | kept |
| Engineer | plans, implements, writes unit + integration tests, fixes | **one session** across all |
| QA | tests the running app in a browser | kept across retests |
| Reviewer | code review, final approval | **fresh every time** |

---

## The one rule

> **Agents produce artifacts. The orchestrator owns state and permissions.**

Made mechanical, this means:

- LangGraph is the **runtime**. A pure function, `decide(state) -> status`, is
  the **policy**.
- `decide()` reads only *stored facts* — a validated artifact, a recorded test
  result, an iteration counter. It never reads an agent's prose or its opinion
  about what should happen next.
- `decide()` has no I/O, so it is exhaustively table-testable. If those tests
  are green, the state machine cannot skip a gate no matter what a model says.

This is the guardrail against the dominant failure mode in multi-agent systems:
the [MAST taxonomy](https://www.augmentcode.com/guides/why-multi-agent-llm-systems-fail-and-how-to-fix-them)
(1,600+ traces, NeurIPS 2025) attributes **79% of production failures to
specification ambiguity and verification gaps** — not to model quality.

---

## Two planes

```
┌─ CONTROL PLANE (trusted — your machine, later your cluster) ─────────┐
│                                                                      │
│  Sources              LangGraph runtime            Stores            │
│  ├ local CLI    ──►   ingest → clarify ⏸  ──►      ├ state (Postgres)│
│  ├ GitHub App         → plan → env → baseline      ├ knowledge base  │
│  └ Jira / Linear      → implement → verify         ├ evidence/artifacts
│                       → review → finalize ⏸        └ traces (LangSmith)
│                              │                                       │
│                        decide() — pure policy, sole state writer     │
└──────────────────────────────┼───────────────────────────────────────┘
                               │  one-shot, isolated, disposable
┌─ EXECUTION PLANE (untrusted) ─────────────────────────────────────────┐
│  Docker sandbox per task: repo clone + toolchain + service containers │
│  the agent CLI runs IN HERE (claude / opencode / codex)               │
│  results land at /workspace/.devloop/out/<role>.json                  │
└───────────────────────────────────────────────────────────────────────┘
```

The control plane never executes agent-authored code. The execution plane
never decides anything.

---

## Adapter seams

The things that must stay swappable, and what fills each slot over time:

| Seam | v0 (today) | Later |
|---|---|---|
| **Source** | local CLI + markdown file; repo = path or clone URL + base branch | GitHub App webhooks, Jira, Linear |
| **Runtime** | `claude` CLI, `opencode` CLI — per role, from `devloop.config.yaml` | `codex exec`, in-process Agent SDK |
| **Sandbox** | per-task fresh clone, commands + app run on host | Docker per task → E2B / Modal / k8s |
| **Sink** | pushed branch + GitHub PR via `gh` (body = plan + evidence) | GitHub App, checks, pinned status comment |
| **State** | SQLite checkpoints | Postgres + domain tables |
| **Knowledge** | — | Postgres facts rendered into the sandbox |

---

## Contracts

Every agent writes exactly one JSON document to
`/workspace/.devloop/out/<role>.json`. The orchestrator reads that file and
validates it with Pydantic. Prose is never parsed.

This is deliberate. Three CLIs speak three dialects — `claude --output-format
stream-json`, `codex exec --json --output-schema`, `opencode run --format
json` — and a file-based contract is identical across all of them, survives a
CLI changing its stdout format, and gives a free repair retry (feed the
validation errors back, exactly once, then fail the node).

Defined in `devloop.contracts`:

| Contract | Produced by | Read by |
|---|---|---|
| `RequirementResult` | Product Owner | `decide()` (`status` field) |
| `POAnswer` (`Clarification`) | Product Owner | Engineer (via context), `decide()` (`needs_human`) |
| `PlanResult` (+ `questions_for_po`) | Engineer | Engineer, QA, Reviewer, `decide()` |
| `QAResult` (`QAScenario`, `Finding`) | QA | `decide()` (`verdict`, `severity`), Engineer, Reviewer |
| `EnvRecipe` | Bootstrap agent | Sandbox, cached per repo |
| `ImplementationResult` (`Deviation`, `PlanInvalidation`, `FindingDispute`, `questions_for_po`) | Engineer | `decide()` (`plan_invalid`, questions), QA, Reviewer |
| `DiffSummary` | **Orchestrator only** (git, after the Developer) | `decide()` (empty diff), Reviewer |
| `TestRunResult` | **Orchestrator only** | `decide()` (baseline vs new failures) |
| `ReviewResult` / `Finding` | Reviewer *and* humans | `decide()` (`verdict`, `severity`) |

`ReviewResult.verdict` and `Finding.severity` are read directly by the routing
policy — treat them as versioned API, not implementation detail.

An agent may run tests for its own feedback. Only **orchestrator-run** results
are admissible as facts.

---

## The five hard problems

These are the failure modes that decide whether an autonomous loop is real or
a demo. Each is an architectural commitment, not a later patch.

### 1. The cloned repo can't run its tests

The most common silent killer, so it gets its own phase of the graph.

- **Discovery in priority order:** `devloop.yml` → `.devcontainer/` →
  `.github/workflows/*.yml` (the ground truth of how the repo really builds)
  → lockfile heuristics.
- **Bootstrap agent** whose only job is making setup + tests pass on the
  **untouched base commit**. It may iterate on setup commands; it may not
  touch source. Output: an `EnvRecipe`.
- **Baseline gate:** the orchestrator runs lint/typecheck/tests at the base
  commit *before* the Developer sees the repo. Prefer green; if red, snapshot
  the failing set and gate on **"no new failures"** instead. Real repos have
  flaky and pre-broken tests — without that snapshot you cannot attribute a
  failure to the agent.
- **Caching:** image tagged by repo + lockfile hash, recipe stored in the
  knowledge base. The second task on a repo skips bootstrap entirely.
- **Services:** Postgres/Redis/Kafka as a per-task compose project on an
  isolated network, torn down with the sandbox.
- **Unsatisfiable deps** (private registries, third-party keys) → `ESCALATED`
  with a named capability gap. Once a human supplies it, it becomes knowledge
  and is never asked again.

### 2. The plan is fine until implementation proves it isn't

- **Plans are hypotheses.** An explicit `REPLANNING` transition: the Developer
  may declare the plan invalid with evidence, and the orchestrator returns to
  planning carrying the failure. Bounded at 2 replans, then `ESCALATED`.
- **Deterministic plan check** before implementing — do referenced files
  exist, do named tests exist, is the scope sane. No LLM, no cost.
- **Thin plans.** Files to touch, strategy, test strategy, risks. Line-level
  plans are more specific, more often wrong, and more expensive to invalidate.
- **Declared deviation.** The Developer must emit `deviations: list[Deviation]`.
  The Reviewer checks plan-vs-diff conformance; *undeclared drift is itself a
  finding*.

### 3. A change lands that nobody wanted

The fix is to make **the plan the review surface, not the diff**.

- Requirements, plan, deviations and test evidence are published *with* the
  change — printed and written to `.devloop/` in v0; the PR body plus a pinned
  status comment in v1.
- Human feedback maps into the **same `Finding` contract** as the Reviewer's.
  One repair path, not two.
- Feedback is **scoped**: a comment on one finding re-opens only that finding,
  so an otherwise-good 11-file change isn't regenerated to fix one of them.
- The task resumes on its existing thread, so plan, evidence and conversation
  are all still in state — nothing is re-derived, nothing is re-asked.

### 4. Everything stays local

v0 runs entirely on your machine: SQLite (or Dockerised Postgres) for state,
sandboxes in Docker, agent CLIs inside those sandboxes, task in as a markdown
file, result out as a local branch you inspect with `git log -p`. LangSmith is
the single optional outbound dependency and sits behind a flag. No GitHub App,
no tunnel, no public endpoint.

### 5. A project knowledge base that compounds

The highest-leverage component, and the reason traces are stored from day one.

**Shape** — atomic, per-repo facts with provenance, not a wiki blob:

| kind | example |
|---|---|
| `env_recipe` | how to build and test this repo |
| `repo_map` | module boundaries, entry points, where things live |
| `convention` | "errors are wrapped with `fmt.Errorf(\"ctx: %w\", err)`" |
| `failure_pattern` | "integration tests need `TESTCONTAINERS_RYUK_DISABLED=true`" |
| `glossary` | domain terms — what a "weightable product" is |
| `task_history` | task → plan → outcome → what the human changed |

Each fact carries `confidence`, `provenance: list[task_id]`,
`last_confirmed_at`, and `status: proposed | active | retired`.

- **Population:** a `harvest_knowledge` step after every task asks one
  question of the trace — *what would have made this task easier if we'd known
  it at the start?* Proposals are human-approved at first; high-confidence
  kinds auto-promote later.
- **Injection:** rendered into the sandbox as
  `/workspace/.devloop/knowledge/*.md` plus a compact index in the prompt.
  Since `claude` and `opencode` both read `AGENTS.md`/`CLAUDE.md` natively,
  the knowledge base can also be **emitted into the target repo as its own
  PR** — reviewed through the same loop, and useful to engineers who never
  touch DevLoop.
- **Decay:** a fact contradicted by a later task is retired, not edited.
  Provenance survives, so the improvement engine can learn which kinds of
  knowledge actually pay off.
- **Retrieval:** load everything while it's kilobytes; hybrid BM25 + embedding
  retrieval scoped to touched files only when size demands it.

---

## The graph

16 nodes; each status maps to exactly one (`STATUS_TO_NODE` in
`graph/build.py`, total over the enum).

```
ingest (PO: requirements) ─► clarify ⏸ ◄──────────────┐ needs_human
        │                        │                      │
        ▼                        ▼                      │
      plan (Engineer) ◄──── answers ──── consult_po (PO) ◄── questions_for_po
        │                                               ▲   (from plan, replanning, implement)
        ▼                                               │
   env_gate ─► env_bootstrap ─► baseline ─► implement (Engineer, same session)
                                               │  ▲
                                               ▼  │ changes_required ◄── red checks / QA bugs / review
                                            verify ─► qa_test (QA + app) ─► review (fresh) ─► publish (push + PR)
                                                                                                 │
                                         replanning ◄── plan_conformance / plan_invalid          ▼
                                         escalate ⏸ ◄── budget, caps, agent failure          finalize ⏸ → done
```

**Node granularity is dictated by a LangGraph constraint, not by taste.**
`interrupt()` re-runs its node from the top on resume — checkpoints exist only
at node boundaries, so any side effect placed before `interrupt()` fires
twice. Therefore:

- `clarify`, `finalize` and `escalate` do **nothing but** interrupt.
- Every side effect — `docker run`, `git clone`, `git push`, an agent
  invocation — lives in its own node, keyed by `(task_id, node, iteration)`
  so a crash-resume can never double-create a container or double-push a
  branch.

### Guards in `decide()`

| Guard | Rule |
|---|---|
| Budget | any in-flight status → `ESCALATED` once `cost_usd >= budget_usd` |
| Failed side effect | any in-flight status → `ESCALATED` once a node records `escalation_reason` |
| Review iterations | `CHANGES_REQUIRED → IMPLEMENTING` only while `iteration < 3` (attempts, not questions) |
| Replans | plan-conformance findings or `plan_invalid` → `REPLANNING` only while `replan_count < 2` |
| PO consultations | Engineer questions → `CONSULTING_PO` only while `po_consultations < 3` |
| Red before QA | `TESTING` with a failure absent from baseline → `CHANGES_REQUIRED` (no QA/review spend) |
| QA | `failed`, or any unresolved P0/P1 → `CHANGES_REQUIRED`; `blocked` → `ESCALATED` |
| Publish | `REVIEWING → PUBLISHING` requires `approved`, no unresolved P0/P1, no new failures **and** QA `passed`/`skipped` |

`ESCALATED` means *needs a human*, not dead. `CANCELLED` and `FINALIZED` are
terminal.

---

## Data model

LangGraph's checkpointer owns graph state for resume. It does **not** replace
the domain tables, which exist for querying, auditing and evals:

| Table | Purpose |
|---|---|
| `dev_tasks` | one row per task; `thread_id`, `iteration`, `cost_usd`, `env_recipe_id`; `UNIQUE (source, external_id)` |
| `agent_runs` | every agent execution: `role`, `runtime`, `prompt_version`, `iteration`, `cost_usd`, `trace_id` |
| `test_runs` | `phase` (`baseline` \| `post_change`) so failures can be attributed |
| `reviews` | reviewer verdicts and findings |
| `human_feedback` | `finding_id` links scoped feedback to what it answers |
| `task_events` | append-only transition log; replaying it must reproduce current status |
| `repo_knowledge` | the knowledge base |
| `env_recipes` | cached, keyed by repo + lockfile hash |

`agent_runs` is the raw material for self-improvement. It is not deferrable —
without it, the improvement engine has nothing to learn from.

---

## Why this stack

**LangGraph + LangSmith (Python).** The graph gives durable checkpoints,
human-in-the-loop interrupts and resume for free, and LangGraph Studio
(`langgraph dev`) gives a live view of the graph, its state and its
interrupts without building a UI — the same compiled graph the CLI runs is
exported in `graph/studio.py`. LangSmith tracing is opt-in (it sends task
content off the machine). The cost is a framework whose value is
mostly realised at the human gates rather than in agent orchestration —
because the agents are subprocesses, the orchestrator itself makes no model
calls.

**Agents as CLI subprocesses.** `claude`, `opencode` and later `codex` behind
one `CodingAgent` interface. This buys genuine runtime independence: the
Claude-implements/Codex-reviews swap becomes a config edit, and each CLI's own
harness (its file tools, its context management) is reused rather than
reimplemented. The cost is losing in-process hooks and permission callbacks;
the file-based contract is what makes that cost acceptable.

**Workflow over autonomy.** [Agentless (arXiv 2407.01489)](https://arxiv.org/abs/2407.01489)
showed a fixed localise → repair → validate pipeline beating most autonomous
SWE agents at roughly a tenth of the cost. A constrained pipeline with real
verification gates is the design, not a limitation of it.

---

## Where self-improvement fits

Deliberately last, and only after a real change has merged. The ordering
matters: an improvement engine optimising a loop that doesn't work yet just
converges faster on the wrong thing.

```
production runs → traces → failure clustering → improvement agent
                                                      ↓
                          eval set ◄─── proposed prompt/tool/workflow change
                             ↓
                    better? → PR to this repo → human review
                    worse?  → reject
```

[GEPA](https://arxiv.org/pdf/2507.19457) (ICLR 2026 oral) is the technique
that matches this design: reflective prompt evolution reads full execution
traces and beats GRPO with ~35× fewer rollouts on 20–100 examples — precisely
the volume `agent_runs` and `human_feedback` will hold. `prompts/` is a
versioned top-level directory specifically so it can be the mutation target.

---

## Further reading

- [Agentless: Demystifying LLM-based Software Engineering Agents](https://arxiv.org/abs/2407.01489)
- [Why multi-agent LLM systems fail (MAST taxonomy)](https://www.augmentcode.com/guides/why-multi-agent-llm-systems-fail-and-how-to-fix-them)
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) · [persistence](https://docs.langchain.com/oss/python/langgraph/persistence) · [the double-execution problem](https://blog.raed.dev/posts/langgraph-hitl/)
- [GEPA: reflective prompt evolution](https://arxiv.org/pdf/2507.19457)
- [AI agent sandboxing compared](https://amux.io/guides/ai-agent-sandboxing/)
