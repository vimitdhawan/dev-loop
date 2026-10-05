from __future__ import annotations

from pathlib import Path

import pytest

from devloop.agents.checks import (
    check_implementation,
    check_plan,
    check_po_answer,
    check_qa,
    check_requirements,
    check_review,
)
from devloop.contracts.artifacts import (
    Clarification,
    FileAction,
    FileToChange,
    Finding,
    FindingCategory,
    FindingDispute,
    ImplementationResult,
    PlanResult,
    POAnswer,
    QAResult,
    QAScenario,
    QAVerdict,
    RequirementResult,
    RequirementStatus,
    ReviewResult,
    Severity,
    Verdict,
)


def plan(*files: tuple[str, FileAction], steps: list[str] | None = None) -> PlanResult:
    return PlanResult(
        summary="s",
        files_to_change=[FileToChange(path=p, reason="r", action=a) for p, a in files],
        implementation_steps=["do it"] if steps is None else steps,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("")
    return tmp_path


@pytest.mark.parametrize(
    "candidate,expected_problem",
    [
        (plan(("src/app.py", FileAction.MODIFY)), None),
        (plan(("src/new.py", FileAction.CREATE)), None),
        (plan(("src/app.py", FileAction.DELETE)), None),
        (plan(("src/missing.py", FileAction.MODIFY)), "does not exist"),
        (plan(("src/app.py", FileAction.CREATE)), "already exists"),
        (plan(("../outside.py", FileAction.CREATE)), "inside the repository"),
        (plan(("/etc/passwd", FileAction.MODIFY)), "inside the repository"),
        (plan((".git/config", FileAction.MODIFY)), "managed by git"),
        (plan(), "files_to_change is empty"),
        (plan(("src/app.py", FileAction.MODIFY), steps=[]), "implementation_steps"),
        (
            plan(("src/app.py", FileAction.MODIFY), ("src/app.py", FileAction.MODIFY)),
            "more than once",
        ),
        (
            plan(*[(f"f{i}.py", FileAction.CREATE) for i in range(26)]),
            "too big",
        ),
    ],
)
def test_check_plan(repo: Path, candidate: PlanResult, expected_problem: str | None) -> None:
    problems = check_plan(candidate, repo)
    if expected_problem is None:
        assert problems == []
    else:
        assert any(expected_problem in p for p in problems), problems


def test_check_requirements() -> None:
    assert check_requirements(
        RequirementResult(status=RequirementStatus.NEEDS_CLARIFICATION, summary="s")
    )
    assert check_requirements(RequirementResult(status=RequirementStatus.READY, summary="s"))
    assert not check_requirements(
        RequirementResult(status=RequirementStatus.READY, summary="s", acceptance_criteria=["a"])
    )


def _finding(id_: str) -> Finding:
    return Finding(
        id=id_,
        severity=Severity.P2,
        category=FindingCategory.CORRECTNESS,
        description="d",
        recommendation="r",
    )


def test_check_review() -> None:
    assert check_review(ReviewResult(verdict=Verdict.CHANGES_REQUESTED))
    assert check_review(
        ReviewResult(verdict=Verdict.APPROVED, findings=[_finding("R1"), _finding("R1")])
    )
    assert not check_review(ReviewResult(verdict=Verdict.APPROVED, findings=[_finding("R1")]))


def test_check_implementation_rejects_disputes_of_unknown_findings() -> None:
    impl = ImplementationResult(
        summary="s", disputed_findings=[FindingDispute(finding_id="R9", reason="r")]
    )
    assert check_implementation(impl, {"R1"})
    assert not check_implementation(impl, {"R9"})
    assert not check_implementation(ImplementationResult(summary="s"), set())


def test_plan_waiting_on_questions_skips_the_file_checks(repo: Path) -> None:
    on_hold = PlanResult(summary="s", questions_for_po=["which format?"])
    assert check_plan(on_hold, repo) == []


def _scenario() -> QAScenario:
    return QAScenario(criterion="c", passed=True)


@pytest.mark.parametrize(
    "qa,ok",
    [
        (QAResult(verdict=QAVerdict.PASSED, scenarios=[_scenario()]), True),
        (QAResult(verdict=QAVerdict.PASSED), False),  # tested nothing
        (QAResult(verdict=QAVerdict.FAILED, scenarios=[_scenario()]), False),  # no bugs listed
        (
            QAResult(verdict=QAVerdict.FAILED, scenarios=[_scenario()], findings=[_finding("Q1")]),
            True,
        ),
        (QAResult(verdict=QAVerdict.BLOCKED), False),  # no reason
        (QAResult(verdict=QAVerdict.BLOCKED, notes="needs a login"), True),
        (QAResult(verdict=QAVerdict.SKIPPED), False),  # orchestrator-only verdict
    ],
)
def test_check_qa(qa: QAResult, ok: bool) -> None:
    assert (check_qa(qa) == []) is ok


def test_check_po_answer_requires_every_question_covered() -> None:
    questions = ["a?", "b?"]
    answered = Clarification(question="a?", answer="yes", answered_by="product_owner")

    assert check_po_answer(POAnswer(answers=[answered]), questions)
    assert not check_po_answer(POAnswer(answers=[answered], needs_human=["b?"]), questions)
