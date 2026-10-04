"""The whole graph, end to end, against a real git repo and real check
commands — with the stub runtime standing in for the model. These are the
Phase 2 "done when" scenarios minus the model: a change lands on a branch,
and a rejected review exercises the repair cycle and the iteration cap."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langgraph.types import Command

from devloop.contracts.runs import Role, RuntimeConfig
from devloop.contracts.state import DevLoopState, TaskInput
from devloop.contracts.status import DevLoopStatus as St
from devloop.graph.build import build_graph
from devloop.runtimes.base import AgentInvocation, AgentOutcome
from devloop.runtimes.registry import register_runtime
from devloop.runtimes.stub import NOTES_FILE, StubAgent
from devloop.store.checkpointer import checkpointer
from tests.conftest import git

REJECT = {
    "verdict": "changes_requested",
    "findings": [
        {
            "id": "R1",
            "severity": "P1",
            "category": "correctness",
            "description": "missing edge case",
            "recommendation": "handle it",
        }
    ],
}


class Task:
    def __init__(self, repo: Path, runtime: str = "stub", task_id: str = "t1") -> None:
        self.task_id = task_id
        self.config = {"configurable": {"thread_id": task_id}}
        self.graph: Any = build_graph(checkpointer=checkpointer())
        self.initial: DevLoopState = {
            "task": TaskInput(
                external_id=task_id, repo_path=str(repo), title="add notes", description="d"
            ),
            "status": St.RECEIVED,
            "runtime": RuntimeConfig(default=runtime),
            "iteration": 0,
            "replan_count": 0,
            "cost_usd": 0.0,
            "budget_usd": 20.0,
            "baseline": [],
            "verification": [],
            "feedback": [],
            "agent_runs": [],
        }

    def start(self) -> dict[str, Any]:
        return self._drive(self.initial)

    def resume(self, answer: object) -> dict[str, Any]:
        return self._drive(Command(resume=answer))

    def _drive(self, value: object) -> dict[str, Any]:
        statuses: list[St] = []
        for update in self.graph.stream(value, self.config, stream_mode="values"):
            # an interrupt re-emits the current values; collapse repeats
            if not statuses or statuses[-1] != update["status"]:
                statuses.append(update["status"])
        state = dict(self.graph.get_state(self.config).values)
        state["_statuses"] = statuses
        return state


def stub(scripts: dict[Role, list[dict[str, Any]]]) -> str:
    register_runtime("scripted-stub", StubAgent(scripts))
    return "scripted-stub"


def test_happy_path_lands_a_branch_and_finalizes(target_repo: Path) -> None:
    task = Task(target_repo)

    paused = task.start()

    assert paused["status"] == St.READY_FOR_FINALIZE
    assert paused["_statuses"] == [
        St.RECEIVED,
        St.PLANNING,
        St.PLAN_READY,
        St.ENV_BOOTSTRAP,
        St.BASELINE,
        St.IMPLEMENTING,
        St.TESTING,
        St.REVIEWING,
        St.READY_FOR_FINALIZE,
    ]
    assert [r.role for r in paused["agent_runs"]] == [
        Role.REQUIREMENT,
        Role.PLANNER,
        Role.DEVELOPER,
        Role.REVIEWER,
    ]
    assert all(r.ok for r in paused["agent_runs"])
    # the change is visible in the user's repo while waiting on the human
    assert NOTES_FILE in git(target_repo, "diff", "--name-only", "main", "devloop/t1")
    assert git(target_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    done = task.resume("approve")
    assert done["status"] == St.FINALIZED


def test_cancel_at_finalize(target_repo: Path) -> None:
    task = Task(target_repo)
    task.start()

    assert task.resume({"answer": "cancel"})["status"] == St.CANCELLED


def test_review_rejection_runs_the_repair_cycle(target_repo: Path) -> None:
    task = Task(target_repo, runtime=stub({Role.REVIEWER: [REJECT]}))

    state = task.start()

    assert state["status"] == St.READY_FOR_FINALIZE
    assert state["iteration"] == 2
    devs = [r for r in state["agent_runs"] if r.role == Role.DEVELOPER]
    assert [r.iteration for r in devs] == [1, 2]
    notes = git(target_repo, "show", f"devloop/t1:{NOTES_FILE}")
    assert "iteration 1" in notes and "iteration 2" in notes


def test_iteration_cap_escalates(target_repo: Path) -> None:
    task = Task(target_repo, runtime=stub({Role.REVIEWER: [REJECT, REJECT, REJECT]}))

    state = task.start()

    assert state["status"] == St.ESCALATED
    assert state["iteration"] == 3
    assert task.resume({"answer": "cancel"})["status"] == St.CANCELLED


class BreaksThenFixes(StubAgent):
    """Developer that breaks the repo's only check on its first attempt and
    fixes it on the second."""

    def run(self, invocation: AgentInvocation) -> AgentOutcome:
        outcome = super().run(invocation)
        if invocation.role == Role.DEVELOPER:
            ctx = json.loads((invocation.workdir / ".devloop/in/context.json").read_text())
            broken = invocation.workdir / "BROKEN"
            if ctx["iteration"] == 1:
                broken.write_text("x")
            else:
                assert any(v["new_failure"] for v in ctx["verification"])
                broken.unlink()
        return outcome


def test_failing_checks_go_back_to_developer_without_review(target_repo: Path) -> None:
    agent = BreaksThenFixes()
    register_runtime("breaks", agent)
    task = Task(target_repo, runtime="breaks")

    state = task.start()

    assert state["status"] == St.READY_FOR_FINALIZE
    assert agent.calls.count(Role.REVIEWER) == 1, "red checks must not be reviewed"
    assert agent.calls.count(Role.DEVELOPER) == 2


def test_developer_declaring_plan_invalid_replans(target_repo: Path) -> None:
    invalid = {"summary": "x", "plan_invalid": {"reason": "wrong file", "evidence": "e"}}
    task = Task(target_repo, runtime=stub({Role.DEVELOPER: [invalid]}))

    state = task.start()

    assert state["status"] == St.READY_FOR_FINALIZE
    assert state["replan_count"] == 1
    assert "wrong file" in (state["replan_reason"] or "")
    assert [r.role for r in state["agent_runs"]].count(Role.PLANNER) == 2


def test_clarification_question_pauses_and_folds_the_answer_in(target_repo: Path) -> None:
    unclear = {"status": "needs_clarification", "summary": "s", "questions": ["JSON or YAML?"]}
    task = Task(target_repo, runtime=stub({Role.REQUIREMENT: [unclear]}))

    paused = task.start()
    assert paused["status"] == St.CLARIFICATION_REQUIRED

    state = task.resume({"answer": "JSON"})
    assert state["status"] == St.READY_FOR_FINALIZE
    assert any("JSON" in c for c in state["requirements"].constraints)


def test_repo_without_a_recipe_escalates_with_a_reason(target_repo: Path) -> None:
    (target_repo / "devloop.yml").unlink()
    git(target_repo, "commit", "-qam", "drop recipe")
    task = Task(target_repo)

    state = task.start()

    assert state["status"] == St.ESCALATED
    assert "devloop.yml" in state["escalation_reason"]


def test_invalid_agent_output_escalates_after_one_repair(target_repo: Path) -> None:
    task = Task(target_repo, runtime=stub({Role.PLANNER: [{"bad": 1}, {"bad": 2}]}))

    state = task.start()

    assert state["status"] == St.ESCALATED
    assert "planner output still invalid" in state["escalation_reason"]
    assert [r.ok for r in state["agent_runs"] if r.role == Role.PLANNER] == [False, False]


def test_resume_from_a_fresh_process(target_repo: Path) -> None:
    """`run` and `resume` are separate processes: a new graph and a new
    checkpointer must pick the task up, with every contract type intact."""

    Task(target_repo).start()

    fresh = Task(target_repo)
    restored = fresh.graph.get_state(fresh.config).values
    assert restored["agent_runs"][0].role == Role.REQUIREMENT
    assert restored["runtime"].default == "stub"
    assert fresh.resume("approve")["status"] == St.FINALIZED


def test_developer_disputing_a_finding_escalates_with_the_dispute(target_repo: Path) -> None:
    dispute = {
        "summary": "no change",
        "disputed_findings": [{"finding_id": "R1", "reason": "contradicts the requirements"}],
    }
    task = Task(
        target_repo,
        runtime=stub({Role.REVIEWER: [REJECT], Role.DEVELOPER: [_default_dev(), dispute]}),
    )

    state = task.start()

    assert state["status"] == St.ESCALATED
    assert "disputes R1: contradicts the requirements" in state["escalation_reason"]


def _default_dev() -> dict[str, Any]:
    return {"summary": "stub implementation appends a note"}
