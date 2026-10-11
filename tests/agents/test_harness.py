"""The contract harness: validation, the single repair retry, and run
records — exercised with a fake runtime that writes scripted outputs."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from devloop.agents import roles
from devloop.agents.harness import run_step
from devloop.contracts.context import DevelopmentContext
from devloop.contracts.runs import AgentConfig, McpServerConfig, Role, Step
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
        self.resumed: list[str | None] = []

    def run(self, invocation: AgentInvocation) -> AgentOutcome:
        self.prompts.append(invocation.prompt)
        self.budgets.append(invocation.max_budget_usd)
        self.resumed.append(invocation.resume_session)
        return self.actions.pop(0)(invocation)


def writes(doc: object, cost: float = 0.5, session: str = "") -> Action:
    def action(inv: AgentInvocation) -> AgentOutcome:
        out = inv.workdir / OUT_DIR / f"{inv.step.value}.json"
        out.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        return AgentOutcome(
            ok=True, cost_usd=cost, input_tokens=10, output_tokens=5, session_id=session
        )

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
    return prepare_workspace(str(target_repo), "t1", branch="devloop/t1")[0].path


def context() -> DevelopmentContext:
    task = TaskInput(external_id="t1", repo="/x", title="t", description="d")
    return DevelopmentContext(
        role=Role.REVIEWER,
        step=Step.REVIEW,
        task=task,
        iteration=1,
        base_commit="abc",
        branch="b",
    )


def run(workspace: Path, agent: ScriptedAgent, **kwargs: object):  # type: ignore[no-untyped-def]
    register_runtime("scripted", agent)
    return run_step(
        roles.REVIEW,
        context(),
        workspace=workspace,
        agent_config=AgentConfig(runtime="scripted"),
        budget_left_usd=kwargs.pop("budget", 10.0),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def test_valid_output_first_try(workspace: Path) -> None:
    agent = ScriptedAgent([writes(VALID_REVIEW)])

    result = run(workspace, agent)

    assert result.artifact is not None and result.error is None
    assert [r.attempt for r in result.records] == [1]
    rec = result.records[0]
    assert rec.ok and rec.cost_usd == 0.5 and rec.prompt_version.startswith("v")
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


def test_resumes_the_given_session_and_says_so(workspace: Path) -> None:
    agent = ScriptedAgent([writes(VALID_REVIEW, session="s-1")])

    result = run(workspace, agent, session_id="s-1")

    assert agent.resumed == ["s-1"]
    assert agent.prompts[0].startswith("You are continuing the same task")
    assert result.records[0].resumed and result.session_id == "s-1"


def test_repair_retry_continues_the_same_session_with_a_short_prompt(workspace: Path) -> None:
    agent = ScriptedAgent(
        [writes({"verdict": "maybe"}, session="s-new"), writes(VALID_REVIEW, session="s-new")]
    )

    result = run(workspace, agent)

    assert agent.resumed == [None, "s-new"]
    assert agent.prompts[1].startswith("## Repair required"), "full prompt not resent"
    assert result.session_id == "s-new"


def test_repair_without_a_session_resends_the_full_prompt(workspace: Path) -> None:
    agent = ScriptedAgent([writes({"verdict": "maybe"}), writes(VALID_REVIEW)])

    run(workspace, agent)

    assert agent.prompts[1].startswith("# Role: Reviewer")


# --- per-run records and configured MCP servers ----------------------------


def test_every_attempt_keeps_its_exact_context_prompt_and_output(workspace: Path) -> None:
    agent = ScriptedAgent([writes({"verdict": "nope"}), writes(VALID_REVIEW)])

    result = run(workspace, agent)

    first, second = (Path(r.run_dir) for r in result.records)
    assert first.name.endswith("reviewer-a1") and second.name.endswith("reviewer-a2")
    assert json.loads((first / "output.json").read_text()) == {"verdict": "nope"}
    assert json.loads((second / "output.json").read_text()) == VALID_REVIEW
    assert json.loads((first / "context.json").read_text())["task"]["external_id"] == "t1"
    assert "Repair required" in (second / "prompt.md").read_text()


def test_role_mcp_servers_are_added_to_the_step_tools(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[AgentInvocation] = []

    def capture(inv: AgentInvocation) -> AgentOutcome:
        seen.append(inv)
        return writes(VALID_REVIEW)(inv)

    monkeypatch.setenv("STITCH_KEY", "k")
    register_runtime("scripted", ScriptedAgent([capture]))
    config = AgentConfig(
        runtime="scripted",
        mcp_servers={
            "stitch": McpServerConfig(url="https://s/mcp", headers={"X-Key": "${STITCH_KEY}"})
        },
    )

    run_step(roles.REVIEW, context(), workspace=workspace, agent_config=config, budget_left_usd=1)

    (server,) = seen[0].tools.mcp_servers
    assert (server.name, server.url, server.headers) == (
        "stitch",
        "https://s/mcp",
        (("X-Key", "k"),),
    )


def test_a_missing_mcp_secret_fails_the_step_without_running_it(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STITCH_KEY", raising=False)
    agent = ScriptedAgent([])
    register_runtime("scripted", agent)
    config = AgentConfig(
        runtime="scripted",
        mcp_servers={"stitch": McpServerConfig(url="https://s", headers={"K": "${STITCH_KEY}"})},
    )

    result = run_step(
        roles.REVIEW, context(), workspace=workspace, agent_config=config, budget_left_usd=1
    )

    assert result.error is not None and "STITCH_KEY" in result.error
    assert agent.prompts == [] and result.records == []
