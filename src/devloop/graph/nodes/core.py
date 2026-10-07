"""Graph node implementations — the team at work.

    Product Owner ─► UX ─► Planner ─► (plan check) ─► Engineer
          ▲  questions │       │                         │
          └────────────┴───────┴─────────────────────────┘
    Engineer ─► checks ─► QA (browser) ─► Reviewer (fresh) ─► publish/PR
        ▲          │ red       │ bugs           │ changes
        └──────────┴───────────┴────────────────┘

Which of the optional stages (Product Owner, UX, QA) run is the task's
workflow, a fact `decide()` routes on; nodes don't check it.

Agents never talk to each other directly, and never inherit each other's
conversations. Each one's validated output document becomes a state field
(`requirements`, `design`, `plan`, `implementation`, `qa`, `review`) that
the next one is handed in its context file. A question is a field too: the
orchestrator routes it to the Product Owner's session (or to a human) and
resumes the asker's session with the answer. Every hand-off is a stored
fact.

Node granularity follows LangGraph's re-execution rule: `interrupt()`
re-runs its node from the top on resume, so `clarify`, `finalize` and
`escalate` do *nothing but* interrupt, and every side effect (git, agent
runs, check runs, the app, the PR) lives in its own node and is idempotent
where a crash-resume could repeat it.

Every node ends the same way: compute facts, then call `decide()` with
those facts folded into the state to get the next status. A side effect
that fails in a way only a human can fix (`DevLoopError`) becomes the
`escalation_reason` fact — `decide()` then escalates.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from langgraph.types import interrupt

from devloop.agents import roles
from devloop.agents.checks import (
    check_design,
    check_implementation,
    check_plan,
    check_po_answer,
    check_qa,
    check_requirements,
    check_review,
)
from devloop.agents.harness import StepRun, StepSpec, run_step
from devloop.contracts.artifacts import (
    Clarification,
    Finding,
    FindingCategory,
    QAResult,
    QAVerdict,
    RequirementResult,
    RequirementStatus,
    Severity,
)
from devloop.contracts.context import DevelopmentContext, VerificationSummary
from devloop.contracts.runs import PullRequestConfig, Role, TeamConfig
from devloop.contracts.state import DevLoopState, DiffSummary
from devloop.contracts.status import DevLoopStatus as St
from devloop.env.app import AppEnvironmentError, AppError, running_app
from devloop.env.discovery import discover_recipe
from devloop.env.runner import run_checks
from devloop.errors import DevLoopError
from devloop.graph.inputs import is_raw_input, normalize_input
from devloop.graph.routing import (
    decide,
    failure_keys,
    latest_verification,
    new_failure_keys,
    open_findings,
    workflow_of,
)
from devloop.paths import task_dir
from devloop.runtimes.base import IN_DIR
from devloop.sandbox.workspace import (
    branch_name,
    commit_all,
    diff_patch,
    diff_stat,
    discard_changes,
    github_slug,
    origin_url,
    prepare_workspace,
    publish_branch,
)
from devloop.sink.github import open_or_update_pr
from devloop.sink.summary import render_summary

Node = Callable[[DevLoopState], dict[str, Any]]

# Failing check output handed to an agent is trimmed to this many chars.
_AGENT_OUTPUT_TAIL = 3_000


def _advance(state: DevLoopState, **facts: Any) -> dict[str, Any]:
    local = cast(DevLoopState, {**state, **facts})
    # raw input (LangGraph Studio) may carry the status as a plain string, or none
    local["status"] = St(local.get("status") or St.RECEIVED)
    next_status = decide(local)
    return {**facts, "status": next_status}


def _escalate_on_error[N: Node](node: N) -> N:
    @functools.wraps(node)
    def wrapper(state: DevLoopState) -> dict[str, Any]:
        try:
            return node(state)
        except DevLoopError as exc:
            return _advance(state, escalation_reason=f"{node.__name__}: {exc}")

    return cast(N, wrapper)


# --- helpers ---------------------------------------------------------------


def _workspace(state: DevLoopState) -> Path:
    path = state.get("workspace_path")
    assert path is not None, "workspace is prepared in ingest"
    return Path(path)


def _with(state: DevLoopState, **facts: Any) -> DevLoopState:
    return cast(DevLoopState, {**state, **facts})


def _context(state: DevLoopState, spec: StepSpec[Any], **extra: Any) -> DevelopmentContext:
    return DevelopmentContext(
        role=spec.role,
        step=spec.step,
        task=state["task"],
        iteration=extra.pop("iteration", state.get("iteration", 0)),
        base_commit=state.get("base_commit") or "",
        branch=state.get("branch") or "",
        workflow=workflow_of(state),
        requirements=state.get("requirements"),
        clarifications=state.get("clarifications", []),
        design=state.get("design"),
        plan=state.get("plan"),
        commands=_commands(state),
        **extra,
    )


def _run(
    state: DevLoopState,
    spec: StepSpec[Any],
    context: DevelopmentContext,
    check: Callable[[Any], list[str]] | None = None,
) -> StepRun[Any]:
    team = state.get("team") or TeamConfig()
    budget = state.get("budget_usd")
    session = (
        state.get("sessions", {}).get(spec.role.value) if spec.role in roles.SESSION_ROLES else None
    )
    return run_step(
        spec,
        context,
        workspace=_workspace(state),
        agent_config=team.for_role(spec.role),
        budget_left_usd=None if budget is None else budget - state.get("cost_usd", 0.0),
        session_id=session,
        check=check,
    )


def _run_facts(state: DevLoopState, spec: StepSpec[Any], run: StepRun[Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "agent_runs": run.records,
        "cost_usd": state.get("cost_usd", 0.0) + run.cost_usd,
    }
    if spec.role in roles.SESSION_ROLES and run.session_id:
        facts["sessions"] = {**state.get("sessions", {}), spec.role.value: run.session_id}
    if run.error:
        facts["escalation_reason"] = run.error
    return facts


def _ask_po(status: St, questions: list[str]) -> dict[str, Any]:
    return {"pending_questions": questions, "consult_return": status}


_ANSWERED: dict[str, Any] = {"pending_questions": [], "consult_return": None}

# who is waiting on an answer, by the status they'll return to
_ASKER = {
    St.DESIGNING: Role.UX.value,
    St.PLANNING: Role.PLANNER.value,
    St.REPLANNING: Role.PLANNER.value,
    St.IMPLEMENTING: Role.ENGINEER.value,
}


def _verification_summaries(state: DevLoopState) -> list[VerificationSummary]:
    new = new_failure_keys(state)
    return [
        VerificationSummary(
            type=r.type,
            command=r.command,
            passed=r.passed,
            new_failure=bool(failure_keys(r) & new),
            failures=[f.name for f in r.failures],
            output_tail="" if r.passed else r.output[-_AGENT_OUTPUT_TAIL:],
        )
        for r in latest_verification(state)
    ]


def _commands(state: DevLoopState) -> dict[str, str]:
    env = state.get("env")
    if env is None:
        return {}
    commands = dict(env.commands)
    if env.setup:
        commands = {"setup": " && ".join(env.setup), **commands}
    return commands


def _write_diff(state: DevLoopState) -> str:
    ws = _workspace(state)
    rel = f"{IN_DIR}/diff.patch"
    (ws / rel).parent.mkdir(parents=True, exist_ok=True)
    (ws / rel).write_text(diff_patch(ws, state.get("base_commit") or "HEAD"))
    return rel


def _answer_text(payload: object) -> str:
    """`devloop resume --answer X` sends `{"answer": X}`; a bare resume
    sends `"approve"`."""

    if isinstance(payload, dict):
        return str(payload.get("answer", "")).strip()
    return str(payload).strip()


def _stamp(findings: list[Finding], source: str) -> list[Finding]:
    """The orchestrator, not the agent, records who raised a finding."""

    return [f.model_copy(update={"source": source}) for f in findings]


# --- nodes -----------------------------------------------------------------


@_escalate_on_error
def ingest(state: DevLoopState) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if is_raw_input(state):
        # started from plain JSON (LangGraph Studio): build the typed initial
        # state, and persist it as this node's facts
        state = normalize_input(state)
        facts.update(state)
    task = state["task"]
    ws, base = prepare_workspace(
        task.repo,
        task.external_id,
        branch=state.get("branch") or branch_name(task.external_id, task.title),
        base_branch=task.base_branch,
    )
    facts |= {
        "workspace_path": str(ws.path),
        "base_branch": base,
        "base_commit": ws.base_commit,
        "branch": ws.branch,
    }
    state = _with(state, **facts)
    if not workflow_of(state).has(Role.PRODUCT_OWNER):
        # e.g. a bug: the issue itself is the spec, the Planner starts from it
        return _advance(state, **facts)

    spec = roles.REQUIREMENTS
    run = _run(state, spec, _context(state, spec), check_requirements)
    facts.update(_run_facts(state, spec, run))
    if run.artifact is not None:
        facts["requirements"] = run.artifact
    return _advance(state, **facts)


def clarify(state: DevLoopState) -> dict[str, Any]:
    """Pauses for a human: either the Product Owner's questions about the
    task, or an Engineer's question the Product Owner couldn't answer.
    Everything before `interrupt()` must be idempotent — resuming replays
    this node from the top."""

    pending = state.get("pending_questions", [])
    req = state.get("requirements")
    if not pending and (
        (req is not None and req.status == RequirementStatus.READY)
        or not workflow_of(state).has(Role.PRODUCT_OWNER)
    ):
        return _advance(state)

    questions = pending or (req.questions if req else [])
    asked_by = Role.PRODUCT_OWNER.value
    if pending:
        asked_by = _ASKER.get(state.get("consult_return") or St.PLANNING, "the team")
        if workflow_of(state).has(Role.PRODUCT_OWNER):
            asked_by += " (via product owner)"
    payload = interrupt(
        {
            "gate": "clarify",
            "asked_by": asked_by,
            "task": state["task"].title,
            "questions": questions,
        }
    )
    answer = _answer_text(payload) or "no answer given; use your best judgement"
    asked = "; ".join(questions) or "clarification"
    clarification = Clarification(question=asked, answer=answer, answered_by="human")

    if pending:
        return _advance(state, clarifications=[clarification], pending_questions=[])

    constraints = list(req.constraints) if req else []
    constraints.append(f"Human answer to [{asked}]: {answer}")
    resolved = RequirementResult(
        status=RequirementStatus.READY,
        summary=req.summary if req else state["task"].title,
        acceptance_criteria=req.acceptance_criteria if req else [],
        constraints=constraints,
        questions=[],
    )
    return _advance(state, requirements=resolved, clarifications=[clarification])


@_escalate_on_error
def consult_po(state: DevLoopState) -> dict[str, Any]:
    """The Product Owner answers whoever asked (UX, Planner or Engineer),
    in its own session — it still remembers exploring the task when it
    wrote the requirements."""

    questions = state.get("pending_questions", [])
    spec = roles.PO_ANSWER
    run = _run(
        state,
        spec,
        _context(state, spec, questions=questions),
        lambda a: check_po_answer(a, questions),
    )
    facts: dict[str, Any] = {
        **_run_facts(state, spec, run),
        "po_consultations": state.get("po_consultations", 0) + 1,
    }
    if run.artifact is not None:
        facts["clarifications"] = [
            a.model_copy(update={"answered_by": "product_owner"}) for a in run.artifact.answers
        ]
        facts["pending_questions"] = run.artifact.needs_human
    return _advance(state, **facts)


@_escalate_on_error
def design(state: DevLoopState) -> dict[str, Any]:
    """UX turns the requirements into screens the Planner can plan from —
    with Stitch (or another design tool) when the role has one configured."""

    spec = roles.DESIGN
    run = _run(state, spec, _context(state, spec), check_design)
    facts = _run_facts(state, spec, run)
    if run.artifact is not None and run.artifact.questions_for_po:
        facts.update(_ask_po(St.DESIGNING, run.artifact.questions_for_po))
    elif run.artifact is not None:
        facts.update(_ANSWERED, design=run.artifact)
    return _advance(state, **facts)


def _plan(state: DevLoopState, *, replan_reason: str | None) -> dict[str, Any]:
    ws = _workspace(state)
    spec = roles.PLAN
    run = _run(
        state,
        spec,
        _context(state, spec, replan_reason=replan_reason),
        lambda p: check_plan(p, ws),
    )
    facts = _run_facts(state, spec, run)
    if run.artifact is not None and run.artifact.questions_for_po:
        facts.update(_ask_po(state["status"], run.artifact.questions_for_po))
    elif run.artifact is not None:
        facts.update(_ANSWERED, plan=run.artifact)
        if replan_reason is not None:
            facts["replan_count"] = state.get("replan_count", 0) + 1
    return _advance(state, **facts)


@_escalate_on_error
def plan(state: DevLoopState) -> dict[str, Any]:
    return _plan(state, replan_reason=None)


@_escalate_on_error
def replanning(state: DevLoopState) -> dict[str, Any]:
    impl = state.get("implementation")
    reason = state.get("replan_reason")  # set when resuming after a question
    if not reason and impl is not None and impl.plan_invalid is not None:
        reason = f"{impl.plan_invalid.reason}\nEvidence: {impl.plan_invalid.evidence}"
    if not reason:
        reason = "\n".join(
            f"{f.id}: {f.description} -> {f.recommendation}"
            for f in open_findings(state)
            if f.category == FindingCategory.PLAN_CONFORMANCE
        )
    # plan cleared so `REPLANNING` only advances on the new plan
    result = _plan(_with(state, plan=None), replan_reason=reason)
    result.setdefault("plan", None)
    # the reason is only kept while the replan waits on an answer
    result["replan_reason"] = reason if result.get("pending_questions") else None
    return result


def env_gate(state: DevLoopState) -> dict[str, Any]:
    """Pure routing gate for PLAN_READY: sends the task to `env_bootstrap`
    (no recipe yet), `baseline`, or — after a replan — straight back to
    `implement`."""

    return _advance(state)


@_escalate_on_error
def env_bootstrap(state: DevLoopState) -> dict[str, Any]:
    recipe = discover_recipe(_workspace(state), state.get("base_commit") or "")
    if recipe is None:
        return _advance(
            state,
            env=None,
            escalation_reason=(
                "no devloop.yml and no recognised build manifest (go.mod, uv.lock, "
                "package.json with a test script); add a devloop.yml to the repo"
            ),
        )
    return _advance(state, env=recipe)


@_escalate_on_error
def baseline(state: DevLoopState) -> dict[str, Any]:
    env = state.get("env")
    assert env is not None
    results = run_checks(
        _workspace(state),
        env,
        phase="baseline",
        iteration=0,
        log_dir=task_dir(state["task"].external_id) / "checks",
    )
    facts: dict[str, Any] = {"baseline": results}
    setup = next((r for r in results if r.type == "setup" and not r.passed), None)
    if setup is not None:
        facts["escalation_reason"] = (
            f"setup fails on the untouched base commit: {setup.output[-500:]}"
        )
    return _advance(state, **facts)


@_escalate_on_error
def implement(state: DevLoopState) -> dict[str, Any]:
    task = state["task"]
    ws = _workspace(state)
    attempt = state.get("iteration", 0) + 1
    spec = roles.implement(state.get("env"))
    findings = open_findings(state)
    context = _context(
        state,
        spec,
        iteration=attempt,
        open_findings=findings,
        verification=_verification_summaries(state) if attempt > 1 else [],
    )
    finding_ids = {f.id for f in findings}
    run = _run(state, spec, context, lambda impl: check_implementation(impl, finding_ids))
    facts: dict[str, Any] = _run_facts(state, spec, run)
    if run.artifact is None:
        return _advance(state, **facts)

    if run.artifact.questions_for_po:
        # Nothing is committed and the attempt isn't counted: the same
        # session picks up where it stopped once the answer is in.
        facts.update(_ask_po(St.IMPLEMENTING, run.artifact.questions_for_po))
        return _advance(state, **facts)

    # `qa`/`review: None` — reports on the previous attempt say nothing
    # about this one, and must not be mistaken for it by `decide()`.
    facts.update(
        _ANSWERED,
        iteration=attempt,
        implementation=run.artifact,
        prior_findings=findings,
        qa=None,
        review=None,
        diff=None,
    )
    if run.artifact.plan_invalid is not None:
        discard_changes(ws)
        return _advance(state, **facts)

    commit = commit_all(ws, f"devloop: {task.title} (iteration {attempt})")
    if not commit.files_changed:
        facts["diff"] = DiffSummary()
        disputes = run.artifact.disputed_findings
        facts["escalation_reason"] = (
            "the engineer disputes " + "; ".join(f"{d.finding_id}: {d.reason}" for d in disputes)
            if disputes
            else "the engineer made no changes and did not declare the plan invalid"
        )
        return _advance(state, **facts)

    total = diff_stat(ws, state.get("base_commit") or "HEAD")
    facts["diff"] = DiffSummary(
        files_changed=total.files_changed,
        insertions=total.insertions,
        deletions=total.deletions,
        deviations=run.artifact.deviations,
    )
    return _advance(state, **facts)


@_escalate_on_error
def verify(state: DevLoopState) -> dict[str, Any]:
    env = state.get("env")
    assert env is not None
    results = run_checks(
        _workspace(state),
        env,
        phase="post_change",
        iteration=state.get("iteration", 0),
        log_dir=task_dir(state["task"].external_id) / "checks",
    )
    return _advance(state, verification=results)


@_escalate_on_error
def qa_test(state: DevLoopState) -> dict[str, Any]:
    env = state.get("env")
    app = env.app if env else None
    if app is None:
        skipped = QAResult(verdict=QAVerdict.SKIPPED, notes="devloop.yml has no app: section")
        return _advance(state, qa=skipped)

    iteration = state.get("iteration", 0)
    evidence_dir = f".devloop/qa/iteration-{iteration}"
    ws = _workspace(state)
    (ws / evidence_dir).mkdir(parents=True, exist_ok=True)
    spec = roles.qa(str(ws / evidence_dir))
    impl = state.get("implementation")
    context = _context(
        state,
        spec,
        app_url=app.url,
        evidence_dir=evidence_dir,
        prior_findings=state.get("prior_findings", []),
        developer_notes=impl.summary if impl else "",
    )
    log_dir = task_dir(state["task"].external_id) / "app" / f"iteration-{iteration}"
    try:
        with running_app(ws, app, log_dir):
            run = _run(state, spec, context, check_qa)
    except AppEnvironmentError:
        raise  # not the Engineer's doing — escalate
    except AppError as exc:
        # The change doesn't start: a bug like any other, for the Engineer.
        broken = QAResult(
            verdict=QAVerdict.FAILED,
            findings=[
                Finding(
                    id="QA-APP",
                    severity=Severity.P0,
                    category=FindingCategory.FUNCTIONAL,
                    description=f"The app does not start: {exc}",
                    recommendation="Make the app start and serve its URL again.",
                    source="orchestrator",
                )
            ],
            notes="app failed to start",
        )
        return _advance(state, qa=broken)

    facts = _run_facts(state, spec, run)
    if run.artifact is not None:
        facts["qa"] = run.artifact.model_copy(
            update={"findings": _stamp(run.artifact.findings, "qa")}
        )
        if run.artifact.verdict == QAVerdict.BLOCKED:
            facts["escalation_reason"] = f"QA is blocked: {run.artifact.notes}"
    return _advance(state, **facts)


@_escalate_on_error
def review(state: DevLoopState) -> dict[str, Any]:
    """Always a fresh session: the Reviewer must not inherit anyone's
    assumptions — including its own from the previous round."""

    impl = state.get("implementation")
    spec = roles.REVIEW
    context = _context(
        state,
        spec,
        diff_path=_write_diff(state),
        deviations=impl.deviations if impl else [],
        disputed_findings=impl.disputed_findings if impl else [],
        developer_notes=impl.notes_for_reviewer if impl else "",
        verification=_verification_summaries(state),
        qa=state.get("qa"),
        prior_findings=state.get("prior_findings", []),
    )
    run = _run(state, spec, context, check_review)
    facts = _run_facts(state, spec, run)
    if run.artifact is not None:
        facts["review"] = run.artifact.model_copy(
            update={"findings": _stamp(run.artifact.findings, "reviewer")}
        )
    return _advance(state, **facts)


def changes_required(state: DevLoopState) -> dict[str, Any]:
    """Pure routing gate: re-evaluates the guards in `decide()` (iteration
    cap, replan cap) once a change has been sent back."""

    return _advance(state)


@_escalate_on_error
def publish(state: DevLoopState) -> dict[str, Any]:
    """Push the approved branch and open (or update) the PR. The PR body is
    the plan and the evidence, so the human reviews *what was meant*, not
    just the diff."""

    task = state["task"]
    ws = _workspace(state)
    branch = state.get("branch") or ""
    body_file = task_dir(task.external_id) / "summary.md"
    body_file.write_text(render_summary(state))

    publish_branch(ws, branch)
    facts: dict[str, Any] = {"pull_request_url": None}
    pr = state.get("pull_request") or PullRequestConfig()
    slug = github_slug(origin_url(ws))
    if pr.enabled and slug is not None:
        facts["pull_request_url"] = open_or_update_pr(
            workspace=ws,
            repo=slug,
            base=state.get("base_branch") or "main",
            head=branch,
            title=task.title,
            body_file=body_file,
            draft=pr.draft,
        )
    return _advance(state, **facts)


def finalize(state: DevLoopState) -> dict[str, Any]:
    """Pauses for the human decision. With a PR, merging happens on GitHub;
    `approve` here just closes the task."""

    task = state["task"]
    decision = interrupt(
        {
            "gate": "finalize",
            "task": task.title,
            "pull_request": state.get("pull_request_url"),
            "branch": state.get("branch"),
            "inspect": (
                f"git -C {task.repo} diff {state.get('base_branch')}...{state.get('branch')}"
                if Path(task.repo).exists()
                else None
            ),
            "summary": str(task_dir(task.external_id) / "summary.md"),
            "cost_usd": round(state.get("cost_usd", 0.0), 4),
            "answer": "approve | cancel",
        }
    )
    if _answer_text(decision).lower() == "cancel":
        return {"status": St.CANCELLED}
    return {"status": St.FINALIZED}


def escalate(state: DevLoopState) -> dict[str, Any]:
    """Pauses until a human cancels. Any other answer keeps the task parked
    here; retry-from-here is not built yet."""

    decision = interrupt(
        {
            "gate": "escalate",
            "reason": state.get("escalation_reason") or "a guard tripped (budget or caps)",
            "cost_usd": round(state.get("cost_usd", 0.0), 4),
            "iteration": state.get("iteration"),
            "replan_count": state.get("replan_count"),
        }
    )
    if _answer_text(decision).lower() == "cancel":
        return {"status": St.CANCELLED}
    return {"status": St.ESCALATED}
