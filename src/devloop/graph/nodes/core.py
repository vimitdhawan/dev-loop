"""Graph node implementations.

Node granularity follows LangGraph's re-execution rule: `interrupt()`
re-runs its node from the top on resume (checkpoints only exist at node
boundaries), so `clarify`, `finalize` and `escalate` do *nothing but*
interrupt, and every side effect (git, agent runs, check runs) lives in
its own node and is idempotent where a crash-resume could repeat it.

Every node ends the same way: compute facts, then call `decide()` with
those facts folded into the state to get the next status, and return
`{"status": next_status, **facts}`.

A side effect that fails in a way only a human can fix (`DevLoopError`)
becomes the `escalation_reason` fact — `decide()` then escalates. Nodes
never pick a status themselves, failure included.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from langgraph.types import interrupt

from devloop.agents import roles
from devloop.agents.checks import (
    check_implementation,
    check_plan,
    check_requirements,
    check_review,
)
from devloop.agents.harness import RoleRun, RoleSpec, run_role
from devloop.contracts.artifacts import (
    FindingCategory,
    RequirementResult,
    RequirementStatus,
)
from devloop.contracts.context import DevelopmentContext, VerificationSummary
from devloop.contracts.runs import RuntimeConfig
from devloop.contracts.state import DevLoopState, DiffSummary
from devloop.contracts.status import DevLoopStatus as St
from devloop.env.discovery import discover_recipe
from devloop.env.runner import run_checks
from devloop.errors import DevLoopError
from devloop.graph.routing import (
    decide,
    failure_keys,
    latest_verification,
    new_failure_keys,
    open_findings,
)
from devloop.paths import task_dir
from devloop.runtimes.base import IN_DIR
from devloop.sandbox.workspace import (
    commit_all,
    diff_patch,
    diff_stat,
    discard_changes,
    prepare_workspace,
    publish_branch,
)

Node = Callable[[DevLoopState], dict[str, Any]]

# Failing check output handed to an agent is trimmed to this many chars.
_AGENT_OUTPUT_TAIL = 3_000


def _advance(state: DevLoopState, **facts: Any) -> dict[str, Any]:
    local = cast(DevLoopState, {**state, **facts})
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


def _context(state: DevLoopState, spec: RoleSpec[Any], **extra: Any) -> DevelopmentContext:
    return DevelopmentContext(
        role=spec.role,
        task=state["task"],
        iteration=extra.pop("iteration", state.get("iteration", 0)),
        base_commit=state.get("base_commit") or "",
        branch=state.get("branch") or "",
        requirements=state.get("requirements"),
        plan=state.get("plan"),
        **extra,
    )


def _run(
    state: DevLoopState,
    spec: RoleSpec[Any],
    context: DevelopmentContext,
    check: Callable[[Any], list[str]] | None = None,
) -> RoleRun[Any]:
    runtime = state.get("runtime") or RuntimeConfig()
    budget = state.get("budget_usd")
    return run_role(
        spec,
        context,
        workspace=_workspace(state),
        runtime=runtime.runtime_for(spec.role),
        model=runtime.model,
        budget_left_usd=None if budget is None else budget - state.get("cost_usd", 0.0),
        check=check,
        timeout_s=runtime.agent_timeout_s,
    )


def _run_facts(state: DevLoopState, run: RoleRun[Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "agent_runs": run.records,
        "cost_usd": state.get("cost_usd", 0.0) + run.cost_usd,
    }
    if run.error:
        facts["escalation_reason"] = run.error
    return facts


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


def _answer_text(payload: object) -> str:
    """`devloop resume --answer X` sends `{"answer": X}`; a bare resume
    sends `"approve"`."""

    if isinstance(payload, dict):
        return str(payload.get("answer", "")).strip()
    return str(payload).strip()


# --- nodes -----------------------------------------------------------------


@_escalate_on_error
def ingest(state: DevLoopState) -> dict[str, Any]:
    task = state["task"]
    ws = prepare_workspace(Path(task.repo_path), task.external_id)
    facts: dict[str, Any] = {
        "workspace_path": str(ws.path),
        "base_commit": ws.base_commit,
        "branch": ws.branch,
    }
    state = cast(DevLoopState, {**state, **facts})

    run = _run(state, roles.REQUIREMENT, _context(state, roles.REQUIREMENT), check_requirements)
    facts.update(_run_facts(state, run))
    if run.artifact is not None:
        facts["requirements"] = run.artifact
    return _advance(state, **facts)


def clarify(state: DevLoopState) -> dict[str, Any]:
    """Pauses for a human answer. Everything before `interrupt()` must be
    idempotent, because resuming replays this node from the top."""

    req = state.get("requirements")
    if req is not None and req.status == RequirementStatus.READY:
        return _advance(state, requirements=req)

    questions = req.questions if req else []
    payload = interrupt({"task": state["task"].title, "questions": questions})
    answer = _answer_text(payload)
    constraints = list(req.constraints) if req else []
    if answer:
        asked = "; ".join(questions) or "clarification"
        constraints.append(f"Human answer to [{asked}]: {answer}")
    resolved = RequirementResult(
        status=RequirementStatus.READY,
        summary=req.summary if req else state["task"].title,
        acceptance_criteria=req.acceptance_criteria if req else [],
        constraints=constraints,
        questions=[],
    )
    return _advance(state, requirements=resolved)


@_escalate_on_error
def plan(state: DevLoopState) -> dict[str, Any]:
    ws = _workspace(state)
    run = _run(
        state,
        roles.PLANNER,
        _context(state, roles.PLANNER, commands=_commands(state)),
        lambda p: check_plan(p, ws),
    )
    facts = _run_facts(state, run)
    if run.artifact is not None:
        facts["plan"] = run.artifact
    return _advance(state, **facts)


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
    iteration = state.get("iteration", 0) + 1
    spec = roles.developer(state.get("env"))
    findings = open_findings(state)
    context = _context(
        state,
        spec,
        iteration=iteration,
        open_findings=findings,
        verification=_verification_summaries(state),
        commands=_commands(state),
    )
    finding_ids = {f.id for f in findings}
    run = _run(state, spec, context, lambda impl: check_implementation(impl, finding_ids))
    # `review: None` — a review of the previous attempt says nothing about
    # this one, and must not be mistaken for it by `decide()`.
    facts: dict[str, Any] = {
        **_run_facts(state, run),
        "iteration": iteration,
        "implementation": run.artifact,
        "review": None,
        "diff": None,
    }
    if run.artifact is None:
        return _advance(state, **facts)

    if run.artifact.plan_invalid is not None:
        discard_changes(ws)
        return _advance(state, **facts)

    commit = commit_all(ws, f"devloop: {task.title} (iteration {iteration})")
    if not commit.files_changed:
        facts["diff"] = DiffSummary()
        disputes = run.artifact.disputed_findings
        facts["escalation_reason"] = (
            "the developer disputes " + "; ".join(f"{d.finding_id}: {d.reason}" for d in disputes)
            if disputes
            else "the developer made no changes and did not declare the plan invalid"
        )
        return _advance(state, **facts)

    publish_branch(ws, state.get("branch") or f"devloop/{task.external_id}")
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
def review(state: DevLoopState) -> dict[str, Any]:
    ws = _workspace(state)
    diff_rel = f"{IN_DIR}/diff.patch"
    (ws / diff_rel).parent.mkdir(parents=True, exist_ok=True)
    (ws / diff_rel).write_text(diff_patch(ws, state.get("base_commit") or "HEAD"))

    impl = state.get("implementation")
    context = _context(
        state,
        roles.REVIEWER,
        diff_path=diff_rel,
        deviations=impl.deviations if impl else [],
        disputed_findings=impl.disputed_findings if impl else [],
        developer_notes=impl.notes_for_reviewer if impl else "",
        verification=_verification_summaries(state),
        commands=_commands(state),
    )
    run = _run(state, roles.REVIEWER, context, check_review)
    facts = _run_facts(state, run)
    if run.artifact is not None:
        facts["review"] = run.artifact
    return _advance(state, **facts)


def changes_required(state: DevLoopState) -> dict[str, Any]:
    """Pure routing gate: re-evaluates the guards in `decide()` (iteration
    cap, replan cap) once a change has been sent back."""

    return _advance(state)


@_escalate_on_error
def replanning(state: DevLoopState) -> dict[str, Any]:
    impl = state.get("implementation")
    if impl is not None and impl.plan_invalid is not None:
        reason = f"{impl.plan_invalid.reason}\nEvidence: {impl.plan_invalid.evidence}"
    else:
        reason = "\n".join(
            f"{f.id}: {f.description} -> {f.recommendation}"
            for f in open_findings(state)
            if f.category == FindingCategory.PLAN_CONFORMANCE
        )
    state = cast(DevLoopState, {**state, "replan_reason": reason})
    ws = _workspace(state)
    run = _run(
        state,
        roles.PLANNER,
        _context(state, roles.PLANNER, replan_reason=reason, commands=_commands(state)),
        lambda p: check_plan(p, ws),
    )
    facts: dict[str, Any] = {
        **_run_facts(state, run),
        "replan_reason": reason,
        "replan_count": state.get("replan_count", 0) + 1,
        # cleared so `PLANNING | REPLANNING` only advances on the new plan
        "plan": run.artifact,
    }
    return _advance(state, **facts)


def finalize(state: DevLoopState) -> dict[str, Any]:
    """Pauses for the human merge/cancel decision."""

    decision = interrupt(
        {
            "gate": "finalize",
            "task": state["task"].title,
            "branch": state.get("branch"),
            "inspect": (
                f"git -C {state['task'].repo_path} diff "
                f"{(state.get('base_commit') or '')[:10]}..{state.get('branch')}"
            ),
            "cost_usd": round(state.get("cost_usd", 0.0), 4),
            "review": state.get("review"),
            "answer": "approve | cancel",
        }
    )
    if _answer_text(decision).lower() == "cancel":
        return {"status": St.CANCELLED}
    return {"status": St.FINALIZED}


def escalate(state: DevLoopState) -> dict[str, Any]:
    """Pauses until a human cancels. Retrying from the escalated step is
    Phase 3; until then any other answer keeps the task parked here."""

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
