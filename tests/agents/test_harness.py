"""The contract harness: validation, the single repair retry, and run
records — exercised with a fake runtime that writes scripted outputs."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from devloop.agents import roles
from devloop.agents.harness import run_role
from devloop.contracts.context import DevelopmentContext
from devloop.contracts.runs import Role
from devloop.contracts.state import TaskInput
from devloop.errors import AgentError
from devloop.runtimes.base import OUT_DIR, AgentInvocation, AgentOutcome
from devloop.runtimes.registry import register_runtime
from devloop.sandbox.workspace import is_dirty, prepare_workspace

Action = Callable[[AgentInvocation], AgentOutcome]

VALID_REVIEW = {"verdict": "approved", "findings": []}


class ScriptedAgent:
    name = "scripted"

    def __init__(self, actions: list[Action]) -> None:
        self.actions = actions
        self.prompts: list[str] = []
        self.budgets: list[float | None] = []

    def run(self, invocation: AgentInvocation) -> AgentOutcome:
        self.prompts.append(invocation.prompt)
        self.budgets.append(invocation.max_budget_usd)
        return self.actions.pop(0)(invocation)


def writes(doc: object, cost: float = 0.5) -> Action:
    def action(inv: AgentInvocation) -> AgentOutcome:
        out = inv.workdir / OUT_DIR / f"{inv.role.value}.json"
        out.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        return AgentOutcome(ok=True, cost_usd=cost, input_tokens=10, output_tokens=5)

    return action


def crashes(inv: AgentInvocation) -> AgentOutcome:
    return AgentOutcome(ok=False, error="exit 1: boom", cost_usd=0.1)


def raises(inv: AgentInvocation) -> AgentOutcome:
    raise AgentError("not installed")


def edits_repo(inv: AgentInvocation) -> AgentOutcome:
    (inv.workdir / "README.md").write_text("vandalised")
    return writes(VALID_REVIEW)(inv)


@pytest.fixture
def workspace(target_repo: Path) -> Path:
    return prepare_workspace(target_repo, "t1").path


def context() -> DevelopmentContext:
    task = TaskInput(external_id="t1", repo_path="/x", title="t", description="d")
    return DevelopmentContext(
        role=Role.REVIEWER, task=task, iteration=1, base_commit="abc", branch="b"
    )


def run(workspace: Path, agent: ScriptedAgent, **kwargs: object):  # type: ignore[no-untyped-def]
    register_runtime("scripted", agent)
    return run_role(
        roles.REVIEWER,
        context(),
        workspace=workspace,
        runtime="scripted",
        model=None,
        budget_left_usd=kwargs.pop("budget", 10.0),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def test_valid_output_first_try(workspace: Path) -> None:
    agent = ScriptedAgent([writes(VALID_REVIEW)])

    result = run(workspace, agent)

    assert result.artifact is not None and result.error is None
    assert [r.attempt for r in result.records] == [1]
    rec = result.records[0]
    assert rec.ok and rec.cost_usd == 0.5 and rec.prompt_version.startswith("v1+")
    assert json.loads((workspace / ".devloop/in/context.json").read_text())["role"] == "reviewer"
    jsonl = (Path(rec.log_path).parents[1] / "agent_runs.jsonl").read_text().splitlines()
    assert len(jsonl) == 1


def test_invalid_output_is_repaired_once(workspace: Path) -> None:
    agent = ScriptedAgent([writes({"verdict": "maybe"}), writes(VALID_REVIEW)])

    result = run(workspace, agent)

    assert result.artifact is not None
    assert [r.ok for r in result.records] == [False, True]
    assert "verdict" in result.records[0].error
    assert "Repair required" in agent.prompts[1] and "verdict" in agent.prompts[1]
    # the retry may only spend what's left of the budget
    assert agent.budgets == [10.0, 9.5]
    assert result.cost_usd == 1.0


def test_gives_up_after_one_repair(workspace: Path) -> None:
    agent = ScriptedAgent([writes("not json"), writes("{}")])

    result = run(workspace, agent)

    assert result.artifact is None
    assert result.error is not None and "still invalid after repair" in result.error
    assert len(result.records) == 2


def test_missing_output_counts_as_invalid(workspace: Path) -> None:
    agent = ScriptedAgent([lambda inv: AgentOutcome(ok=True), writes(VALID_REVIEW)])

    result = run(workspace, agent)

    assert "no output document" in result.records[0].error
    assert result.artifact is not None


def test_stale_output_from_a_previous_run_is_not_reused(workspace: Path) -> None:
    run(workspace, ScriptedAgent([writes(VALID_REVIEW)]))
    agent = ScriptedAgent([lambda inv: AgentOutcome(ok=True), lambda inv: AgentOutcome(ok=True)])

    result = run(workspace, agent)

    assert result.artifact is None


@pytest.mark.parametrize("action", [crashes, raises])
def test_crash_is_not_retried(workspace: Path, action: Action) -> None:
    agent = ScriptedAgent([action])

    result = run(workspace, agent)

    assert result.artifact is None
    assert result.error is not None and "reviewer agent failed" in result.error
    assert len(result.records) == 1 and not result.records[0].ok


def test_read_only_role_touching_the_repo_fails_and_is_reverted(workspace: Path) -> None:
    agent = ScriptedAgent([edits_repo])

    result = run(workspace, agent)

    assert result.artifact is None
    assert result.error is not None and "read-only" in result.error
    assert not is_dirty(workspace)


def test_deterministic_check_triggers_repair(workspace: Path) -> None:
    agent = ScriptedAgent([writes(VALID_REVIEW), writes(VALID_REVIEW)])
    calls: list[int] = []

    def check(_: object) -> list[str]:
        calls.append(1)
        return ["first look is never good enough"] if len(calls) == 1 else []

    result = run(workspace, agent, check=check)

    assert result.artifact is not None
    assert "first look is never good enough" in agent.prompts[1]
