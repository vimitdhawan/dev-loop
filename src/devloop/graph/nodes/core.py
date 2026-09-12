"""Graph node implementations.

Node granularity follows the plan's explicit rule: `interrupt()` re-runs
its node from the top on resume (checkpoints only exist at node
boundaries), so `clarify` and `finalize` do *nothing but* interrupt, and
every side effect (git, agent calls) lives in its own node keyed so a
resume can't repeat it.

Every node ends the same way: compute facts, then call `decide()` with
those facts folded into the state to get the next status, and return
`{"status": next_status, **facts}`. `decide()` never runs twice with
different inputs for the same transition — the node is the only place
facts are produced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from langgraph.types import interrupt

from devloop.contracts.artifacts import Deviation, RequirementResult, RequirementStatus
from devloop.contracts.state import DevLoopState, DiffSummary
from devloop.contracts.status import DevLoopStatus as St
from devloop.graph.nodes.stubs import (
    fake_bootstrap_agent,
    fake_planner_agent,
    fake_requirement_agent,
    fake_reviewer_agent,
    fake_test_run,
)
from devloop.graph.routing import decide
from devloop.sandbox.local_git import create_task_branch, ensure_clean_repo, write_and_commit


def _advance(state: DevLoopState, **facts: Any) -> dict[str, Any]:
    local = cast(DevLoopState, {**state, **facts})
    next_status = decide(local)
    return {**facts, "status": next_status}


def ingest(state: DevLoopState) -> dict[str, Any]:
    task = state["task"]
    ensure_clean_repo(Path(task.repo_path))
    requirements = fake_requirement_agent(task)
    return _advance(state, requirements=requirements)


def clarify(state: DevLoopState) -> dict[str, Any]:
    """Pauses for a human answer. Everything before `interrupt()` must be
    idempotent, because resuming replays this node from the top."""

    req = state.get("requirements")
    if req is not None and req.status == RequirementStatus.READY:
        # Already answered (e.g. a resume after the answer was supplied
        # out-of-band) — don't interrupt again.
        return _advance(state, requirements=req)

    answers: dict[str, str] = interrupt(
        {"questions": req.questions if req else [], "task": state["task"].title}
    )
    resolved = RequirementResult(
        status=RequirementStatus.READY,
        summary=req.summary if req else state["task"].title,
        acceptance_criteria=req.acceptance_criteria if req else [],
        constraints=req.constraints if req else [],
        questions=[],
    )
    del answers  # v0: answers just unblock; v1 folds them into acceptance_criteria
    return _advance(state, requirements=resolved)


def plan(state: DevLoopState) -> dict[str, Any]:
    task = state["task"]
    requirements = state["requirements"]
    assert requirements is not None
    plan_result = fake_planner_agent(task, requirements)
    return _advance(state, plan=plan_result)


def env_gate(state: DevLoopState) -> dict[str, Any]:
    """Pure routing gate for PLAN_READY: no side effects, just re-runs
    `decide()` to send the task to `env_bootstrap` (no cached recipe yet)
    or straight to `baseline` (recipe already cached from a prior task on
    this repo)."""

    return _advance(state)


def env_bootstrap(state: DevLoopState) -> dict[str, Any]:
    env = fake_bootstrap_agent(state["task"])
    return _advance(state, env=env)


def baseline(state: DevLoopState) -> dict[str, Any]:
    result = fake_test_run(phase="baseline", passed=True)
    return _advance(state, baseline=[result])


def implement(state: DevLoopState) -> dict[str, Any]:
    task = state["task"]
    plan_result = state["plan"]
    assert plan_result is not None
    repo_path = Path(task.repo_path)

    branch = create_task_branch(repo_path, task.external_id)
    note = (
        f"# DevLoop task {task.external_id}: {task.title}\n\n"
        f"{task.description}\n\n"
        f"## Plan\n{plan_result.summary}\n\n"
        + "\n".join(f"- {step}" for step in plan_result.implementation_steps)
    )
    commit = write_and_commit(repo_path, branch, ".devloop/task-note.md", note)

    diff = DiffSummary(
        files_changed=commit.files_changed,
        insertions=commit.insertions,
        deletions=commit.deletions,
        deviations=[
            Deviation(
                step="write note",
                what_changed="stub implementation writes a note instead of real code",
                why="Phase 0 proves the loop, not code generation",
            )
        ],
    )
    return _advance(state, diff=diff)


def verify(state: DevLoopState) -> dict[str, Any]:
    result = fake_test_run(phase="post_change", passed=True)
    return _advance(state, verification=[result])


def review(state: DevLoopState) -> dict[str, Any]:
    result = fake_reviewer_agent()
    iteration = state.get("iteration", 0) + 1
    return _advance(state, review=result, iteration=iteration)


def changes_required(state: DevLoopState) -> dict[str, Any]:
    """Pure routing gate: no side effects, just re-evaluates the guards in
    `decide()` (iteration cap, replan cap) now that `review` has run."""

    return _advance(state)


def replanning(state: DevLoopState) -> dict[str, Any]:
    task = state["task"]
    requirements = state["requirements"]
    assert requirements is not None
    plan_result = fake_planner_agent(task, requirements)
    replan_count = state.get("replan_count", 0) + 1
    return _advance(state, plan=plan_result, replan_count=replan_count)


def finalize(state: DevLoopState) -> dict[str, Any]:
    """Pauses for the human merge/cancel decision."""

    decision: str = interrupt(
        {
            "task": state["task"].title,
            "branch": f"devloop/{state['task'].external_id}",
            "review": state.get("review"),
        }
    )
    if decision == "cancel":
        return {"status": St.CANCELLED}
    return {"status": St.FINALIZED}


def escalate(state: DevLoopState) -> dict[str, Any]:
    interrupt(
        {
            "reason": "escalated",
            "cost_usd": state.get("cost_usd"),
            "iteration": state.get("iteration"),
        }
    )
    return {"status": St.ESCALATED}
