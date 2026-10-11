"""Deterministic checks on agent artifacts — no model, no cost.

Each returns a list of human-readable problems; an empty list means pass.
The harness treats problems exactly like schema-validation errors: fed
back to the agent for its one repair retry.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path, PurePosixPath

from devloop.contracts.artifacts import (
    DesignResult,
    FileAction,
    Finding,
    ImplementationResult,
    PlanResult,
    POAnswer,
    QAResult,
    QAVerdict,
    RequirementResult,
    ReviewResult,
)

MAX_PLAN_FILES = 25
_FORBIDDEN_ROOTS = {".git", ".devloop"}


def check_plan(plan: PlanResult, workspace: Path) -> list[str]:
    if plan.questions_for_po:
        return []  # the plan is on hold until the questions are answered
    problems: list[str] = []
    if not plan.files_to_change:
        problems.append("files_to_change is empty; a plan must name at least one file")
    if len(plan.files_to_change) > MAX_PLAN_FILES:
        problems.append(
            f"plan touches {len(plan.files_to_change)} files (limit {MAX_PLAN_FILES}); "
            "the task is too big for one change — narrow the scope"
        )
    if not plan.implementation_steps:
        problems.append("implementation_steps is empty")

    for item in plan.files_to_change:
        rel = PurePosixPath(item.path)
        if rel.is_absolute() or ".." in rel.parts:
            problems.append(f"{item.path}: must be a relative path inside the repository")
            continue
        if rel.parts and rel.parts[0] in _FORBIDDEN_ROOTS:
            problems.append(f"{item.path}: {rel.parts[0]}/ is managed by git/DevLoop, not code")
            continue
        exists = (workspace / rel).is_file()
        if item.action == FileAction.CREATE and exists:
            problems.append(f"{item.path}: action is 'create' but the file already exists")
        if item.action in (FileAction.MODIFY, FileAction.DELETE) and not exists:
            problems.append(
                f"{item.path}: action is '{item.action.value}' but the file does not exist "
                "(use 'create' for new files)"
            )

    dupes = [p for p, n in Counter(f.path for f in plan.files_to_change).items() if n > 1]
    if dupes:
        problems.append(f"files listed more than once: {', '.join(dupes)}")
    return problems


def check_requirements(req: RequirementResult) -> list[str]:
    if req.status.value == "needs_clarification" and not req.questions:
        return ["status is needs_clarification but no questions were asked"]
    if req.status.value == "ready" and not req.acceptance_criteria:
        return ["status is ready but acceptance_criteria is empty"]
    return []


def check_design(design: DesignResult) -> list[str]:
    if design.questions_for_po:
        return []  # on hold until the questions are answered
    problems: list[str] = []
    if not design.screens:
        problems.append("screens is empty; describe every screen or component the change touches")
    dupes = [n for n, c in Counter(s.name for s in design.screens).items() if c > 1]
    if dupes:
        problems.append(f"screen names must be unique; duplicated: {', '.join(dupes)}")
    return problems


def _duplicate_ids(findings: list[Finding]) -> list[str]:
    dupes = [i for i, n in Counter(f.id for f in findings).items() if n > 1]
    return [f"finding ids must be unique; duplicated: {', '.join(dupes)}"] if dupes else []


def check_review(review: ReviewResult) -> list[str]:
    if review.verdict.value == "changes_requested" and not review.findings:
        return ["verdict is changes_requested but there are no findings to act on"]
    return _duplicate_ids(review.findings)


def check_qa(qa: QAResult) -> list[str]:
    if qa.verdict == QAVerdict.SKIPPED:
        return ["verdict 'skipped' is set by the orchestrator, not by QA"]
    if qa.verdict == QAVerdict.FAILED and not qa.findings:
        return ["verdict is failed but there are no findings describing the bugs"]
    if qa.verdict == QAVerdict.BLOCKED and not qa.notes:
        return ["verdict is blocked but notes don't say what blocked testing"]
    if qa.verdict != QAVerdict.BLOCKED and not qa.scenarios:
        return ["no scenarios: test every acceptance criterion and record each one"]
    return _duplicate_ids(qa.findings)


def check_po_answer(answer: POAnswer, questions: list[str]) -> list[str]:
    covered = {a.question for a in answer.answers} | set(answer.needs_human)
    missing = [q for q in questions if q not in covered]
    if missing:
        return [
            "every question must appear verbatim in answers[].question or needs_human; "
            f"missing: {missing}"
        ]
    return []


def check_implementation(impl: ImplementationResult, open_finding_ids: set[str]) -> list[str]:
    unknown = [d.finding_id for d in impl.disputed_findings if d.finding_id not in open_finding_ids]
    if unknown:
        known = ", ".join(sorted(open_finding_ids)) or "none"
        return [f"disputed_findings names unknown finding ids {unknown}; open findings: {known}"]
    return []
