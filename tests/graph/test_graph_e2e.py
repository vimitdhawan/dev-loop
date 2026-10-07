"""The whole graph, end to end, against real git repos, real check
commands and a real (tiny) app server — with the stub runtime standing in
for the models. Covers the team flow: Product Owner → Engineer (one
session) → checks → QA → fresh Reviewer → publish/PR, plus every way back."""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from langgraph.types import Command

from devloop.contracts.runs import AgentConfig, PullRequestConfig, Role, Step, TeamConfig, Workflow
from devloop.contracts.state import DevLoopState, TaskInput
from devloop.contracts.status import DevLoopStatus as St
from devloop.graph.build import build_graph
from devloop.runtimes.base import AgentInvocation, AgentOutcome
from devloop.runtimes.registry import register_runtime
from devloop.runtimes.stub import NOTES_FILE, StubAgent
from devloop.store import records
from devloop.store.checkpointer import checkpointer
from tests.conftest import git

BRANCH = "devloop/t1-add-notes"


def finding(id_: str, category: str = "correctness") -> dict[str, Any]:
    return {
        "id": id_,
        "severity": "P1",
        "category": category,
        "description": "missing edge case",
        "recommendation": "handle it",
    }


REJECT = {"verdict": "changes_requested", "findings": [finding("R1")]}
QA_FAIL = {
    "verdict": "failed",
    "scenarios": [{"criterion": "c", "steps": ["click"], "passed": False}],
    "findings": [finding("Q1", "functional")],
}


class Task:
    def __init__(
        self,
        repo: Path | str,
        runtime: str = "stub",
        *,
        pr: bool = True,
        base: str | None = None,
        workflow: Workflow | None = None,
    ) -> None:
        self.config = {"configurable": {"thread_id": "t1"}}
        self.graph: Any = build_graph(checkpointer=checkpointer())
        agent = AgentConfig(runtime=runtime)
        self.initial: DevLoopState = {
            "task": TaskInput(
                external_id="t1",
                repo=str(repo),
                base_branch=base,
                title="add notes",
                description="d",
            ),
            "status": St.RECEIVED,
            "team": TeamConfig(product_owner=agent, engineer=agent, qa=agent, reviewer=agent),
            "pull_request": PullRequestConfig(enabled=pr),
            "sessions": {},
            "pending_questions": [],
            "consult_return": None,
            "po_consultations": 0,
            "clarifications": [],
            "prior_findings": [],
            "iteration": 0,
            "replan_count": 0,
            "cost_usd": 0.0,
            "budget_usd": 20.0,
            "baseline": [],
            "verification": [],
            "feedback": [],
            "agent_runs": [],
        }
        if workflow is not None:
            self.initial["workflow"] = workflow

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


def stub(scripts: dict[Step, list[dict[str, Any]]], cls: type[StubAgent] = StubAgent) -> StubAgent:
    agent = cls(scripts)
    register_runtime("scripted", agent)
    return agent


def steps(state: dict[str, Any]) -> list[Step]:
    return [r.step for r in state["agent_runs"]]


def with_app(repo: Path) -> int:
    """Give the target repo a browser-testable app: a static file server."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    (repo / "devloop.yml").write_text(
        "commands:\n  unit: test ! -f BROKEN\n"
        f"app:\n  start: {sys.executable} -m http.server {port} --bind 127.0.0.1\n"
        f"  url: http://127.0.0.1:{port}/\n  ready_timeout_s: 15\n"
    )
    git(repo, "commit", "-qam", "add app")
    return port


# --- the happy path --------------------------------------------------------


def test_happy_path_publishes_the_branch_and_finalizes(target_repo: Path) -> None:
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
        St.QA_TESTING,
        St.REVIEWING,
        St.PUBLISHING,
        St.READY_FOR_FINALIZE,
    ]
    assert steps(paused) == [Step.REQUIREMENTS, Step.PLAN, Step.IMPLEMENT, Step.REVIEW]
    assert paused["qa"].verdict == "skipped", "no app: section, so no browser QA"
    assert NOTES_FILE in git(target_repo, "diff", "--name-only", "main", BRANCH)
    assert git(target_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert paused["pull_request_url"] is None, "a local origin gets no PR"
    summary = (Path(os.environ["DEVLOOP_HOME"]) / "tasks/t1/summary.md").read_text()
    assert "Acceptance criteria" in summary and "Plan" in summary

    assert task.resume("approve")["status"] == St.FINALIZED


def test_cancel_at_finalize(target_repo: Path) -> None:
    task = Task(target_repo)
    task.start()

    assert task.resume({"answer": "cancel"})["status"] == St.CANCELLED


def test_each_role_keeps_its_own_session_and_reviewer_never_resumes(target_repo: Path) -> None:
    agent = stub({Step.REVIEW: [REJECT]})
    task = Task(target_repo, runtime="scripted")

    state = task.start()

    by_step: dict[Step, list[str | None]] = {}
    for step, session in agent.sessions:
        by_step.setdefault(step, []).append(session)
    engineer = state["sessions"]["engineer"]
    assert by_step[Step.PLAN] == [None]
    assert state["sessions"]["planner"] != engineer, "the plan is handed over, not the session"
    assert by_step[Step.IMPLEMENT] == [None, engineer], "the engineer fixes in its own session"
    assert by_step[Step.REVIEW] == [None, None], "every review starts fresh"
    assert "reviewer" not in state["sessions"]


# --- the ways back ---------------------------------------------------------


def test_review_rejection_runs_the_repair_cycle(target_repo: Path) -> None:
    task = Task(target_repo, runtime="scripted")
    stub({Step.REVIEW: [REJECT]})

    state = task.start()

    assert state["status"] == St.READY_FOR_FINALIZE
    assert state["iteration"] == 2
    assert [f.id for f in state["prior_findings"]] == ["R1"]
    notes = git(target_repo, "show", f"{BRANCH}:{NOTES_FILE}")
    assert "iteration 1" in notes and "iteration 2" in notes


def test_iteration_cap_escalates(target_repo: Path) -> None:
    stub({Step.REVIEW: [REJECT, REJECT, REJECT]})
    task = Task(target_repo, runtime="scripted")

    state = task.start()

    assert state["status"] == St.ESCALATED
    assert state["iteration"] == 3
    assert task.resume({"answer": "cancel"})["status"] == St.CANCELLED


class BreaksThenFixes(StubAgent):
    """Engineer that breaks the repo's only check on its first attempt and
    fixes it on the second."""

    def run(self, invocation: AgentInvocation) -> AgentOutcome:
        outcome = super().run(invocation)
        if invocation.step == Step.IMPLEMENT:
            ctx = json.loads((invocation.workdir / ".devloop/in/context.json").read_text())
            broken = invocation.workdir / "BROKEN"
            if ctx["iteration"] == 1:
                broken.write_text("x")
            else:
                assert any(v["new_failure"] for v in ctx["verification"])
                broken.unlink()
        return outcome


def test_failing_checks_go_back_to_engineer_without_qa_or_review(target_repo: Path) -> None:
    agent = stub({}, BreaksThenFixes)
    task = Task(target_repo, runtime="scripted")

    state = task.start()

    assert state["status"] == St.READY_FOR_FINALIZE
    assert agent.calls.count(Step.REVIEW) == 1, "red checks must not be reviewed"
    assert agent.calls.count(Step.IMPLEMENT) == 2


def test_engineer_declaring_plan_invalid_replans(target_repo: Path) -> None:
    invalid = {"summary": "x", "plan_invalid": {"reason": "wrong file", "evidence": "e"}}
    task = Task(target_repo, runtime="scripted")
    stub({Step.IMPLEMENT: [invalid]})

    state = task.start()

    assert state["status"] == St.READY_FOR_FINALIZE
    assert state["replan_count"] == 1
    assert steps(state).count(Step.PLAN) == 2


def test_engineer_disputing_a_finding_escalates_with_the_dispute(target_repo: Path) -> None:
    dispute = {
        "summary": "no change",
        "disputed_findings": [{"finding_id": "R1", "reason": "contradicts the requirements"}],
    }
    stub({Step.REVIEW: [REJECT], Step.IMPLEMENT: [{"summary": "first"}, dispute]})
    task = Task(target_repo, runtime="scripted")

    state = task.start()

    assert state["status"] == St.ESCALATED
    assert "disputes R1: contradicts the requirements" in state["escalation_reason"]


# --- product owner and humans ----------------------------------------------


def test_initial_clarification_pauses_and_folds_the_answer_in(target_repo: Path) -> None:
    unclear = {"status": "needs_clarification", "summary": "s", "questions": ["JSON or YAML?"]}
    stub({Step.REQUIREMENTS: [unclear]})
    task = Task(target_repo, runtime="scripted")

    assert task.start()["status"] == St.CLARIFICATION_REQUIRED

    state = task.resume({"answer": "JSON"})
    assert state["status"] == St.READY_FOR_FINALIZE
    assert any("JSON" in c for c in state["requirements"].constraints)
    assert state["clarifications"][0].answered_by == "human"


def test_planner_question_is_answered_by_the_po_in_its_own_session(target_repo: Path) -> None:
    asks = {"summary": "on hold", "questions_for_po": ["Which date format?"]}
    agent = stub({Step.PLAN: [asks]})
    task = Task(target_repo, runtime="scripted")

    state = task.start()

    assert state["status"] == St.READY_FOR_FINALIZE
    assert St.CONSULTING_PO in state["_statuses"]
    assert steps(state)[:4] == [Step.REQUIREMENTS, Step.PLAN, Step.PO_ANSWER, Step.PLAN]
    assert [c.question for c in state["clarifications"]] == ["Which date format?"]
    assert state["clarifications"][0].answered_by == "product_owner"
    po_session = state["sessions"]["product_owner"]
    assert (Step.PO_ANSWER, po_session) in agent.sessions, "PO answers in its own session"
    planner = state["sessions"]["planner"]
    assert [s for st, s in agent.sessions if st == Step.PLAN] == [None, planner]


def test_question_the_po_cant_answer_goes_to_a_human_then_back(target_repo: Path) -> None:
    asks = {"summary": "s", "questions_for_po": ["What is the refund policy?"]}
    defer = {"answers": [], "needs_human": ["What is the refund policy?"]}
    stub({Step.IMPLEMENT: [asks], Step.PO_ANSWER: [defer]})
    task = Task(target_repo, runtime="scripted")

    paused = task.start()
    assert paused["status"] == St.CLARIFICATION_REQUIRED
    assert paused["pending_questions"] == ["What is the refund policy?"]
    assert paused["iteration"] == 0, "asking a question is not an implementation attempt"

    state = task.resume({"answer": "30 days"})
    assert state["status"] == St.READY_FOR_FINALIZE
    assert state["clarifications"][-1].answer == "30 days"
    assert state["iteration"] == 1


# --- QA in the browser -----------------------------------------------------


def test_qa_bug_goes_back_to_engineer_then_qa_retests(target_repo: Path) -> None:
    with_app(target_repo)
    agent = stub({Step.QA: [QA_FAIL]})
    task = Task(target_repo, runtime="scripted")

    state = task.start()

    assert state["status"] == St.READY_FOR_FINALIZE
    assert [s for s in steps(state) if s in (Step.IMPLEMENT, Step.QA, Step.REVIEW)] == [
        Step.IMPLEMENT,
        Step.QA,
        Step.IMPLEMENT,
        Step.QA,
        Step.REVIEW,
    ]
    assert [(f.id, f.source) for f in state["prior_findings"]] == [("Q1", "qa")]
    qa_sessions = [s for st, s in agent.sessions if st == Step.QA]
    assert qa_sessions == [None, state["sessions"]["qa"]], "QA retests in its own session"


def test_app_that_wont_start_is_a_bug_for_the_engineer(target_repo: Path) -> None:
    (target_repo / "devloop.yml").write_text(
        "commands:\n  unit: 'true'\napp:\n  start: exit 1\n  url: http://127.0.0.1:9/\n"
    )
    git(target_repo, "commit", "-qam", "broken app")
    task = Task(target_repo)

    state = task.start()

    assert state["status"] == St.ESCALATED, "same failure every attempt → iteration cap"
    assert state["iteration"] == 3
    assert [f.id for f in state["prior_findings"]] == ["QA-APP"]


# --- sources and sinks -----------------------------------------------------


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    calls = tmp_path / "gh-calls.txt"
    script = tmp_path / "gh"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {calls}\n'
        'case "$1 $2" in\n'
        '  "pr list") echo "[]" ;;\n'
        '  "pr create") echo "https://github.com/org/repo/pull/7" ;;\n'
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("DEVLOOP_GH_BIN", str(script))
    return calls


def github_like_remote(target_repo: Path, tmp_path: Path) -> Path:
    # a bare repo whose path looks like a GitHub remote, so the PR path runs
    remote = tmp_path / "github.com" / "org" / "repo.git"
    remote.parent.mkdir(parents=True)
    git(tmp_path, "clone", "-q", "--bare", str(target_repo), str(remote))
    return remote


def test_fresh_clone_from_url_pushes_branch_and_opens_pr(
    target_repo: Path, tmp_path: Path, fake_gh: Path
) -> None:
    remote = github_like_remote(target_repo, tmp_path)
    task = Task(str(remote), base="main")

    state = task.start()

    assert state["status"] == St.READY_FOR_FINALIZE
    assert state["pull_request_url"] == "https://github.com/org/repo/pull/7"
    assert git(remote, "rev-parse", BRANCH)
    create = [c for c in fake_gh.read_text().splitlines() if c.startswith("pr create")]
    assert len(create) == 1
    assert "--repo org/repo --base main --head " + BRANCH in create[0]


def test_pr_can_be_disabled(target_repo: Path, tmp_path: Path, fake_gh: Path) -> None:
    remote = github_like_remote(target_repo, tmp_path)

    state = Task(str(remote), pr=False).start()

    assert state["pull_request_url"] is None
    assert git(remote, "rev-parse", BRANCH), "the branch is still pushed"
    assert not fake_gh.exists()


# --- failure handling and persistence --------------------------------------


def test_repo_without_a_recipe_escalates_with_a_reason(target_repo: Path) -> None:
    (target_repo / "devloop.yml").unlink()
    git(target_repo, "commit", "-qam", "drop recipe")

    state = Task(target_repo).start()

    assert state["status"] == St.ESCALATED
    assert "devloop.yml" in state["escalation_reason"]


def test_invalid_agent_output_escalates_after_one_repair(target_repo: Path) -> None:
    stub({Step.PLAN: [{"bad": 1}, {"bad": 2}]})

    state = Task(target_repo, runtime="scripted").start()

    assert state["status"] == St.ESCALATED
    assert "engineer_plan output still invalid" in state["escalation_reason"]
    assert [r.ok for r in state["agent_runs"] if r.step == Step.PLAN] == [False, False]


def test_resume_from_a_fresh_process(target_repo: Path) -> None:
    """`run` and `resume` are separate processes: a new graph and a new
    checkpointer must pick the task up, with every contract type intact."""

    Task(target_repo).start()

    fresh = Task(target_repo)
    restored = fresh.graph.get_state(fresh.config).values
    assert restored["agent_runs"][0].step == Step.REQUIREMENTS
    assert restored["team"].engineer.runtime == "stub"
    assert restored["sessions"]["engineer"].startswith("stub-")
    assert fresh.resume("approve")["status"] == St.FINALIZED


def test_graph_runs_from_raw_studio_style_input(
    target_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same graph LangGraph Studio runs, started with plain JSON."""

    (tmp_path / "devloop.config.yaml").write_text(
        "agents: {product_owner: {runtime: stub}, engineer: {runtime: stub}, "
        "qa: {runtime: stub}, reviewer: {runtime: stub}}\n"
        f"repo: {{url: {target_repo}}}\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEVLOOP_CONFIG", raising=False)
    graph: Any = build_graph(checkpointer=checkpointer())
    config = {"configurable": {"thread_id": "studio-1"}}

    list(graph.stream({"task": {"title": "from studio", "description": "d"}}, config))
    state = graph.get_state(config).values

    assert state["status"] == St.READY_FOR_FINALIZE
    assert state["task"].source == "studio"
    assert state["branch"].endswith("-from-studio")


def test_bad_raw_input_escalates_instead_of_crashing() -> None:
    graph: Any = build_graph(checkpointer=checkpointer())
    config = {"configurable": {"thread_id": "studio-2"}}

    list(graph.stream({"task": {"title": "no description"}}, config))
    state = graph.get_state(config).values

    assert state["status"] == St.ESCALATED
    assert "description is required" in state["escalation_reason"]


# --- workflows ---------------------------------------------------------------

P, U, PL, E, Q, R = (
    Role.PRODUCT_OWNER,
    Role.UX,
    Role.PLANNER,
    Role.ENGINEER,
    Role.QA,
    Role.REVIEWER,
)


def run_context(state: dict[str, Any], step: Step) -> dict[str, Any]:
    """The context file the first run of `step` was handed."""

    record = next(r for r in state["agent_runs"] if r.step == step)
    context: dict[str, Any] = json.loads((Path(record.run_dir) / "context.json").read_text())
    return context


def test_bug_workflow_skips_the_product_owner(target_repo: Path) -> None:
    task = Task(target_repo, workflow=Workflow(name="bug", stages=[PL, E, Q, R]))

    state = task.start()

    assert state["status"] == St.READY_FOR_FINALIZE
    assert steps(state) == [Step.PLAN, Step.IMPLEMENT, Step.REVIEW]  # no app: QA skips itself
    assert state.get("requirements") is None
    assert run_context(state, Step.PLAN)["workflow"]["name"] == "bug"


def test_workflow_without_qa_never_enters_qa(target_repo: Path) -> None:
    with_app(target_repo)
    task = Task(target_repo, workflow=Workflow(name="docs", stages=[PL, E, R]))

    state = task.start()

    assert state["status"] == St.READY_FOR_FINALIZE
    assert St.QA_TESTING not in state["_statuses"]
    assert Step.QA not in steps(state)


def test_ui_workflow_hands_the_design_to_the_planner_and_engineer(target_repo: Path) -> None:
    task = Task(target_repo, workflow=Workflow(name="ui_feature", stages=list(Role)))

    state = task.start()

    assert state["status"] == St.READY_FOR_FINALIZE
    assert steps(state)[:3] == [Step.REQUIREMENTS, Step.DESIGN, Step.PLAN]
    assert St.DESIGNING in state["_statuses"]
    assert run_context(state, Step.PLAN)["design"]["screens"][0]["name"] == "notes panel"
    implement = run_context(state, Step.IMPLEMENT)
    assert implement["plan"] == state["plan"].model_dump(mode="json"), "the checked plan"
    assert implement["design"]["summary"] == state["design"].summary


def test_ux_question_is_answered_by_the_po_then_ux_continues(target_repo: Path) -> None:
    asks = {"summary": "on hold", "questions_for_po": ["Dark mode too?"]}
    agent = stub({Step.DESIGN: [asks]})
    task = Task(target_repo, runtime="scripted", workflow=Workflow(name="ui", stages=list(Role)))

    state = task.start()

    assert state["status"] == St.READY_FOR_FINALIZE
    assert steps(state)[:4] == [Step.REQUIREMENTS, Step.DESIGN, Step.PO_ANSWER, Step.DESIGN]
    ux = state["sessions"]["ux"]
    assert [s for st, s in agent.sessions if st == Step.DESIGN] == [None, ux]


def test_without_a_product_owner_questions_go_to_a_human(target_repo: Path) -> None:
    asks = {"summary": "s", "questions_for_po": ["Which endpoint is broken?"]}
    stub({Step.PLAN: [asks]})
    task = Task(
        target_repo, runtime="scripted", workflow=Workflow(name="bug", stages=[PL, E, Q, R])
    )

    paused = task.start()
    assert paused["status"] == St.CLARIFICATION_REQUIRED
    assert Step.PO_ANSWER not in steps(paused)

    state = task.resume({"answer": "/api/slots"})
    assert state["status"] == St.READY_FOR_FINALIZE
    assert state["clarifications"][0].answer == "/api/slots"
    assert state["clarifications"][0].answered_by == "human"


# --- the task record -----------------------------------------------------------


def test_every_node_and_agent_run_is_recorded(target_repo: Path) -> None:
    state = Task(target_repo).start()

    recorded = records.load_state("t1")
    assert recorded is not None
    assert recorded["status"] == St.READY_FOR_FINALIZE
    assert recorded["plan"] == state["plan"]
    assert len(recorded["agent_runs"]) == len(state["agent_runs"]), "appended, not overwritten"

    events = records.load_events("t1")
    assert [e["node"] for e in events][:2] == ["ingest", "plan"]
    assert "requirements" in events[0]["produced"]
    assert events[-1]["status"] == "READY_FOR_FINALIZE"

    for run in state["agent_runs"]:
        run_dir = Path(run.run_dir)
        assert (run_dir / "context.json").exists() and (run_dir / "prompt.md").exists()
        assert json.loads((run_dir / "output.json").read_text())
    assert run_context(state, Step.REVIEW)["role"] == "reviewer"
