"""Phase-0 stub agents.

These are deliberately fake: no model call, no Docker sandbox. Their job
is to exercise every node, every contract, and every transition in the
graph so the machine itself — state, routing, persistence, interrupts,
git — is proven before any model spend happens (per the plan's Phase 0).

Each returns exactly the artifact a real agent would write to
`/workspace/.devloop/out/<role>.json`, so swapping a stub for
`devloop.runtimes.claude_cli` later changes nothing else in the graph.
"""

from __future__ import annotations

from devloop.contracts.artifacts import (
    EnvRecipe,
    FileToChange,
    PlanResult,
    RequirementResult,
    RequirementStatus,
    ReviewResult,
    TestPlanItem,
    TestRunResult,
    Verdict,
)
from devloop.contracts.state import TaskInput


def fake_requirement_agent(task: TaskInput) -> RequirementResult:
    return RequirementResult(
        status=RequirementStatus.READY,
        summary=task.title,
        acceptance_criteria=[f"'{task.title}' behaves as described"],
        constraints=[],
        questions=[],
    )


def fake_planner_agent(task: TaskInput, requirements: RequirementResult) -> PlanResult:
    return PlanResult(
        summary=f"Plan for: {requirements.summary}",
        files_to_change=[
            FileToChange(path=".devloop/task-note.md", reason="record what DevLoop did")
        ],
        implementation_steps=["Write a note describing the completed task"],
        tests=[TestPlanItem(type="unit", description="note file exists and is non-empty")],
        risks=[],
    )


def fake_bootstrap_agent(task: TaskInput) -> EnvRecipe:
    return EnvRecipe(
        image="scratch",
        setup=[],
        commands={"unit": "true"},
        services=[],
        required_secrets=[],
        verified_at_commit="HEAD",
    )


def fake_test_run(*, phase: str, passed: bool = True) -> TestRunResult:
    return TestRunResult(
        type="unit",
        command="true",
        phase=phase,
        passed=passed,
        output="stub: ok" if passed else "stub: failed",
        duration_ms=1,
    )


def fake_reviewer_agent() -> ReviewResult:
    return ReviewResult(verdict=Verdict.APPROVED, findings=[])
