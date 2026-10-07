"""Table-driven tests for the pure routing policy.

Every transition and every guard rejection listed in the plan gets a row
here. `decide()` takes no I/O, so these are the cheapest, highest-signal
tests in the system — if this file is green, the state machine cannot
skip a gate no matter what an agent's prose says.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from devloop.contracts.artifacts import (
    DesignResult,
    Deviation,
    EnvRecipe,
    Finding,
    FindingCategory,
    ImplementationResult,
    PlanInvalidation,
    PlanResult,
    QAResult,
    QAScenario,
    QAVerdict,
    RequirementResult,
    RequirementStatus,
    ReviewResult,
    Severity,
    TestFailure,
    TestRunResult,
    Verdict,
)
from devloop.contracts.runs import Role, Workflow
from devloop.contracts.state import DevLoopState, DiffSummary
from devloop.contracts.status import DevLoopStatus as St
from devloop.graph.routing import decide


def make_state(status: St, **overrides: Any) -> DevLoopState:
    base: DevLoopState = {
        "status": status,
        "iteration": 0,
        "replan_count": 0,
        "cost_usd": 0.0,
        "budget_usd": 20.0,
        "baseline": [],
        "verification": [],
        "feedback": [],
        "qa": QA_PASSED,
    }
    base.update(cast(DevLoopState, overrides))
    return base


QA_PASSED = QAResult(verdict=QAVerdict.PASSED, scenarios=[QAScenario(criterion="c", passed=True)])


def qa(verdict: QAVerdict, findings: list[Finding] | None = None) -> QAResult:
    return QAResult(verdict=verdict, findings=findings or [])


def req(status: RequirementStatus) -> RequirementResult:
    return RequirementResult(status=status, summary="x")


def review(verdict: Verdict, findings: list[Finding] | None = None) -> ReviewResult:
    return ReviewResult(verdict=verdict, findings=findings or [])


def make_test_run(
    name: str,
    passed: bool,
    *,
    type_: str = "unit",
    iteration: int = 1,
    parsed: bool = True,
) -> TestRunResult:
    return TestRunResult(
        type=type_,
        command="pytest",
        phase="post_change",
        iteration=iteration,
        passed=passed,
        failures=[] if passed or not parsed else [TestFailure(name=name, message="boom")],
    )


def finding(
    category: FindingCategory = FindingCategory.CORRECTNESS,
    severity: Severity = Severity.P1,
    resolved: bool = False,
) -> Finding:
    return Finding(
        id="f1",
        severity=severity,
        category=category,
        description="d",
        recommendation="r",
        resolved=resolved,
    )


def recipe(commands: dict[str, str] | None = None) -> EnvRecipe:
    return EnvRecipe(
        image="host",
        commands={"unit": "pytest"} if commands is None else commands,
        verified_at_commit="abc",
    )


def implementation(plan_invalid: bool = False) -> ImplementationResult:
    return ImplementationResult(
        summary="s",
        deviations=[Deviation(step="1", what_changed="x", why="y")],
        plan_invalid=PlanInvalidation(reason="r", evidence="e") if plan_invalid else None,
    )


_PLAN = PlanResult(summary="p")
_DESIGN = DesignResult(summary="d")

P, U, PL, E, Q, R = (
    Role.PRODUCT_OWNER,
    Role.UX,
    Role.PLANNER,
    Role.ENGINEER,
    Role.QA,
    Role.REVIEWER,
)
BUG = Workflow(name="bug", stages=[PL, E, Q, R])
UI = Workflow(name="ui_feature", stages=[P, U, PL, E, Q, R])
NO_QA = Workflow(name="docs", stages=[PL, E, R])

CASES: list[tuple[str, DevLoopState, St]] = [
    (
        "received with no requirements needs clarification",
        make_state(St.RECEIVED),
        St.CLARIFICATION_REQUIRED,
    ),
    (
        "received with ready requirements skips straight to planning",
        make_state(St.RECEIVED, requirements=req(RequirementStatus.READY)),
        St.PLANNING,
    ),
    (
        "clarification stays put until an answer arrives",
        make_state(St.CLARIFICATION_REQUIRED),
        St.CLARIFICATION_REQUIRED,
    ),
    (
        "clarification with a still-unready answer stays put",
        make_state(
            St.CLARIFICATION_REQUIRED,
            requirements=req(RequirementStatus.NEEDS_CLARIFICATION),
        ),
        St.CLARIFICATION_REQUIRED,
    ),
    (
        "clarification resolved moves to planning",
        make_state(St.CLARIFICATION_REQUIRED, requirements=req(RequirementStatus.READY)),
        St.PLANNING,
    ),
    (
        "planning without a plan stays put",
        make_state(St.PLANNING),
        St.PLANNING,
    ),
    (
        "plan ready with no cached env goes to bootstrap",
        make_state(St.PLAN_READY, env=None),
        St.ENV_BOOTSTRAP,
    ),
    (
        "bootstrap without a recipe escalates rather than proceeding blind",
        make_state(St.ENV_BOOTSTRAP),
        St.ESCALATED,
    ),
    (
        "baseline always proceeds to implementing, even with pre-existing failures",
        make_state(St.BASELINE, baseline=[make_test_run("already_broken", False)]),
        St.IMPLEMENTING,
    ),
    (
        "implementing without a diff stays put",
        make_state(St.IMPLEMENTING),
        St.IMPLEMENTING,
    ),
    (
        "testing with no failures proceeds to QA",
        make_state(St.TESTING, verification=[make_test_run("t", True)]),
        St.QA_TESTING,
    ),
    (
        "review approved with no new failures publishes",
        make_state(St.REVIEWING, review=review(Verdict.APPROVED)),
        St.PUBLISHING,
    ),
    (
        "review approved but a NEW failure appeared blocks finalize",
        make_state(
            St.REVIEWING,
            review=review(Verdict.APPROVED),
            baseline=[make_test_run("already_broken", False)],
            verification=[
                make_test_run("already_broken", False),
                make_test_run("new_break", False),
            ],
        ),
        St.CHANGES_REQUIRED,
    ),
    (
        "review approved with only pre-existing failures still publishes",
        make_state(
            St.REVIEWING,
            review=review(Verdict.APPROVED),
            baseline=[make_test_run("already_broken", False)],
            verification=[make_test_run("already_broken", False)],
        ),
        St.PUBLISHING,
    ),
    (
        "review changes_requested loops back",
        make_state(St.REVIEWING, review=review(Verdict.CHANGES_REQUESTED)),
        St.CHANGES_REQUIRED,
    ),
    (
        "changes_required under the iteration cap goes back to implementing",
        make_state(St.CHANGES_REQUIRED, iteration=1),
        St.IMPLEMENTING,
    ),
    (
        "changes_required at the iteration cap escalates",
        make_state(St.CHANGES_REQUIRED, iteration=3),
        St.ESCALATED,
    ),
    (
        "changes_required with an unresolved plan-conformance finding replans",
        make_state(
            St.CHANGES_REQUIRED,
            iteration=1,
            replan_count=0,
            feedback=[
                Finding(
                    id="f1",
                    severity=Severity.P1,
                    category=FindingCategory.PLAN_CONFORMANCE,
                    description="diverged",
                    recommendation="replan",
                )
            ],
        ),
        St.REPLANNING,
    ),
    (
        "changes_required with plan-conformance finding but replans exhausted escalates",
        make_state(
            St.CHANGES_REQUIRED,
            iteration=1,
            replan_count=2,
            feedback=[
                Finding(
                    id="f1",
                    severity=Severity.P1,
                    category=FindingCategory.PLAN_CONFORMANCE,
                    description="diverged",
                    recommendation="replan",
                )
            ],
        ),
        St.ESCALATED,
    ),
    (
        "changes_required ignores an already-resolved plan-conformance finding",
        make_state(
            St.CHANGES_REQUIRED,
            iteration=1,
            feedback=[
                Finding(
                    id="f1",
                    severity=Severity.P1,
                    category=FindingCategory.PLAN_CONFORMANCE,
                    description="diverged",
                    recommendation="replan",
                    resolved=True,
                )
            ],
        ),
        St.IMPLEMENTING,
    ),
    (
        "replanning without a plan stays put",
        make_state(St.REPLANNING),
        St.REPLANNING,
    ),
    (
        "ready_for_finalize is a stable human-gated wait state",
        make_state(St.READY_FOR_FINALIZE),
        St.READY_FOR_FINALIZE,
    ),
    (
        "escalated is terminal-ish and self-stable",
        make_state(St.ESCALATED),
        St.ESCALATED,
    ),
    (
        "cancelled never resumes",
        make_state(St.CANCELLED),
        St.CANCELLED,
    ),
    (
        "finalized never resumes",
        make_state(St.FINALIZED),
        St.FINALIZED,
    ),
    (
        "failed never resumes",
        make_state(St.FAILED),
        St.FAILED,
    ),
    (
        "budget breach escalates from an in-flight status regardless of local facts",
        make_state(St.IMPLEMENTING, cost_usd=25.0, budget_usd=20.0),
        St.ESCALATED,
    ),
    (
        "budget breach does not override a human-gated wait state",
        make_state(St.READY_FOR_FINALIZE, cost_usd=25.0, budget_usd=20.0),
        St.READY_FOR_FINALIZE,
    ),
    (
        "no budget configured never triggers the guard",
        make_state(St.IMPLEMENTING, cost_usd=999.0, budget_usd=None),
        St.IMPLEMENTING,
    ),
    # --- Phase 2 ---------------------------------------------------------
    (
        "received with requirements that need clarification pauses for a human",
        make_state(St.RECEIVED, requirements=req(RequirementStatus.NEEDS_CLARIFICATION)),
        St.CLARIFICATION_REQUIRED,
    ),
    (
        "a failed side effect escalates from an in-flight status",
        make_state(St.PLANNING, escalation_reason="planner crashed"),
        St.ESCALATED,
    ),
    (
        "a failed side effect does not override a human-gated wait state",
        make_state(St.READY_FOR_FINALIZE, escalation_reason="stale"),
        St.READY_FOR_FINALIZE,
    ),
    (
        "replanning with a new plan returns to plan_ready",
        make_state(St.REPLANNING, plan=_PLAN),
        St.PLAN_READY,
    ),
    (
        "plan ready with a recipe but no baseline runs the baseline",
        make_state(St.PLAN_READY, env=recipe()),
        St.BASELINE,
    ),
    (
        "plan ready after a replan skips the already-recorded baseline",
        make_state(St.PLAN_READY, env=recipe(), baseline=[make_test_run("t", True)]),
        St.IMPLEMENTING,
    ),
    (
        "bootstrap with a recipe proceeds to baseline",
        make_state(St.ENV_BOOTSTRAP, env=recipe()),
        St.BASELINE,
    ),
    (
        "bootstrap with a recipe that has no commands escalates — nothing could verify",
        make_state(St.ENV_BOOTSTRAP, env=recipe(commands={})),
        St.ESCALATED,
    ),
    (
        "baseline whose setup fails escalates — nothing after it is attributable",
        make_state(St.BASELINE, baseline=[make_test_run("x", False, type_="setup", parsed=False)]),
        St.ESCALATED,
    ),
    (
        "implementing with a diff proceeds to testing",
        make_state(St.IMPLEMENTING, diff=DiffSummary(files_changed=["a.py"])),
        St.TESTING,
    ),
    (
        "implementing that changed nothing escalates instead of looping",
        make_state(St.IMPLEMENTING, diff=DiffSummary(files_changed=[])),
        St.ESCALATED,
    ),
    (
        "developer declaring the plan invalid replans",
        make_state(St.IMPLEMENTING, implementation=implementation(plan_invalid=True)),
        St.REPLANNING,
    ),
    (
        "developer declaring the plan invalid with replans exhausted escalates",
        make_state(
            St.IMPLEMENTING, implementation=implementation(plan_invalid=True), replan_count=2
        ),
        St.ESCALATED,
    ),
    (
        "testing with a new failure skips review and goes back to the developer",
        make_state(St.TESTING, verification=[make_test_run("new_break", False)]),
        St.CHANGES_REQUIRED,
    ),
    (
        "testing with only pre-existing failures proceeds to QA",
        make_state(
            St.TESTING,
            baseline=[make_test_run("already_broken", False, iteration=0)],
            verification=[make_test_run("already_broken", False)],
        ),
        St.QA_TESTING,
    ),
    (
        "an unparseable failure where baseline passed counts as new",
        make_state(
            St.TESTING,
            baseline=[make_test_run("x", True, iteration=0)],
            verification=[make_test_run("x", False, parsed=False)],
        ),
        St.CHANGES_REQUIRED,
    ),
    (
        "a failure fixed in a later attempt no longer blocks",
        make_state(
            St.REVIEWING,
            review=review(Verdict.APPROVED),
            verification=[
                make_test_run("was_broken", False, iteration=1),
                make_test_run("was_broken", True, iteration=2),
            ],
        ),
        St.PUBLISHING,
    ),
    (
        "approved verdict with an unresolved P1 finding is overridden by severity",
        make_state(St.REVIEWING, review=review(Verdict.APPROVED, [finding()])),
        St.CHANGES_REQUIRED,
    ),
    (
        "approved verdict with only a P2 finding publishes",
        make_state(St.REVIEWING, review=review(Verdict.APPROVED, [finding(severity=Severity.P2)])),
        St.PUBLISHING,
    ),
    (
        "a reviewer plan-conformance finding replans",
        make_state(
            St.CHANGES_REQUIRED,
            iteration=1,
            review=review(Verdict.CHANGES_REQUESTED, [finding(FindingCategory.PLAN_CONFORMANCE)]),
        ),
        St.REPLANNING,
    ),
    # --- Phase 3: the team ----------------------------------------------
    (
        "engineer question while planning goes to the product owner",
        make_state(St.PLANNING, pending_questions=["JSON or YAML?"]),
        St.CONSULTING_PO,
    ),
    (
        "engineer question while implementing goes to the product owner",
        make_state(St.IMPLEMENTING, pending_questions=["q"]),
        St.CONSULTING_PO,
    ),
    (
        "engineer questions past the consultation cap escalate",
        make_state(St.PLANNING, pending_questions=["q"], po_consultations=3),
        St.ESCALATED,
    ),
    (
        "product owner answered everything: back to the step that asked",
        make_state(St.CONSULTING_PO, consult_return=St.IMPLEMENTING),
        St.IMPLEMENTING,
    ),
    (
        "product owner deferred a question: a human answers it",
        make_state(St.CONSULTING_PO, pending_questions=["q"], consult_return=St.PLANNING),
        St.CLARIFICATION_REQUIRED,
    ),
    (
        "clarification waits while an engineer question is still open",
        make_state(
            St.CLARIFICATION_REQUIRED,
            requirements=req(RequirementStatus.READY),
            pending_questions=["q"],
        ),
        St.CLARIFICATION_REQUIRED,
    ),
    (
        "a human answer to an engineer question returns to that step",
        make_state(
            St.CLARIFICATION_REQUIRED,
            requirements=req(RequirementStatus.READY),
            consult_return=St.REPLANNING,
        ),
        St.REPLANNING,
    ),
    (
        "QA in progress stays put",
        make_state(St.QA_TESTING, qa=None),
        St.QA_TESTING,
    ),
    (
        "QA passed goes to the reviewer",
        make_state(St.QA_TESTING),
        St.REVIEWING,
    ),
    (
        "QA skipped (no app) goes to the reviewer",
        make_state(St.QA_TESTING, qa=qa(QAVerdict.SKIPPED)),
        St.REVIEWING,
    ),
    (
        "QA failed goes back to the engineer",
        make_state(St.QA_TESTING, qa=qa(QAVerdict.FAILED, [finding(FindingCategory.FUNCTIONAL)])),
        St.CHANGES_REQUIRED,
    ),
    (
        "QA 'passed' with a P1 bug is overridden by severity",
        make_state(St.QA_TESTING, qa=qa(QAVerdict.PASSED, [finding(FindingCategory.FUNCTIONAL)])),
        St.CHANGES_REQUIRED,
    ),
    (
        "QA blocked escalates",
        make_state(St.QA_TESTING, qa=qa(QAVerdict.BLOCKED)),
        St.ESCALATED,
    ),
    (
        "reviewer approval without a QA pass cannot publish",
        make_state(St.REVIEWING, review=review(Verdict.APPROVED), qa=qa(QAVerdict.FAILED)),
        St.CHANGES_REQUIRED,
    ),
    (
        "QA findings are part of the repair list",
        make_state(
            St.CHANGES_REQUIRED,
            iteration=1,
            qa=qa(QAVerdict.FAILED, [finding(FindingCategory.PLAN_CONFORMANCE)]),
        ),
        St.REPLANNING,
    ),
    (
        "publishing goes to the human gate",
        make_state(St.PUBLISHING),
        St.READY_FOR_FINALIZE,
    ),
    # --- workflows ----------------------------------------------------------
    (
        "a workflow without a Product Owner plans straight from the task",
        make_state(St.RECEIVED, workflow=BUG),
        St.PLANNING,
    ),
    (
        "a UI workflow designs once the requirements are ready",
        make_state(St.RECEIVED, workflow=UI, requirements=req(RequirementStatus.READY)),
        St.DESIGNING,
    ),
    (
        "a UI workflow still clarifies unready requirements first",
        make_state(
            St.RECEIVED, workflow=UI, requirements=req(RequirementStatus.NEEDS_CLARIFICATION)
        ),
        St.CLARIFICATION_REQUIRED,
    ),
    (
        "a design hands over to planning",
        make_state(St.DESIGNING, workflow=UI, design=_DESIGN),
        St.PLANNING,
    ),
    (
        "designing stays put until there is a design",
        make_state(St.DESIGNING, workflow=UI),
        St.DESIGNING,
    ),
    (
        "UX questions go to the Product Owner",
        make_state(St.DESIGNING, workflow=UI, pending_questions=["which colour?"]),
        St.CONSULTING_PO,
    ),
    (
        "the Product Owner's answer returns to designing",
        make_state(St.CONSULTING_PO, workflow=UI, consult_return=St.DESIGNING),
        St.DESIGNING,
    ),
    (
        "a human's answer returns to designing",
        make_state(
            St.CLARIFICATION_REQUIRED,
            workflow=UI,
            requirements=req(RequirementStatus.READY),
            consult_return=St.DESIGNING,
        ),
        St.DESIGNING,
    ),
    (
        "without a Product Owner, a planner's question goes to a human",
        make_state(St.PLANNING, workflow=BUG, pending_questions=["which API?"]),
        St.CLARIFICATION_REQUIRED,
    ),
    (
        "without a Product Owner, the human's answer returns to the asker",
        make_state(St.CLARIFICATION_REQUIRED, workflow=BUG, consult_return=St.IMPLEMENTING),
        St.IMPLEMENTING,
    ),
    (
        "a workflow without QA goes from green checks to review",
        make_state(St.TESTING, workflow=NO_QA, qa=None),
        St.REVIEWING,
    ),
    (
        "a workflow without QA still sends red checks back",
        make_state(
            St.TESTING,
            workflow=NO_QA,
            qa=None,
            verification=[make_test_run("t", passed=False)],
        ),
        St.CHANGES_REQUIRED,
    ),
    (
        "a workflow without QA publishes on review approval alone",
        make_state(St.REVIEWING, workflow=NO_QA, qa=None, review=review(Verdict.APPROVED)),
        St.PUBLISHING,
    ),
    (
        "a workflow with QA still needs QA's pass",
        make_state(St.REVIEWING, workflow=BUG, qa=None, review=review(Verdict.APPROVED)),
        St.CHANGES_REQUIRED,
    ),
]


@pytest.mark.parametrize("name,state,expected", CASES, ids=[c[0] for c in CASES])
def test_decide(name: str, state: DevLoopState, expected: St) -> None:
    assert decide(state) == expected, name


def test_decide_is_pure_and_idempotent() -> None:
    """Calling decide() twice on the same state must be side-effect-free
    and must return the same answer both times — a property the LangGraph
    interrupt() re-execution caveat depends on for `clarify`/`finalize`."""

    state = make_state(St.REVIEWING, review=review(Verdict.APPROVED))
    first = decide(state)
    second = decide(state)
    assert first == second == St.PUBLISHING
