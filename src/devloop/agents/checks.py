"""Deterministic checks on agent artifacts — no model, no cost.

Each returns a list of human-readable problems; an empty list means pass.
The harness treats problems exactly like schema-validation errors: fed
back to the agent for its one repair retry.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path, PurePosixPath

from devloop.contracts.artifacts import (
    FileAction,
    ImplementationResult,
    PlanResult,
    RequirementResult,
    ReviewResult,
)

MAX_PLAN_FILES = 25
_FORBIDDEN_ROOTS = {".git", ".devloop"}


def check_plan(plan: PlanResult, workspace: Path) -> list[str]:
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


def check_review(review: ReviewResult) -> list[str]:
    dupes = [i for i, n in Counter(f.id for f in review.findings).items() if n > 1]
    if dupes:
        return [f"finding ids must be unique; duplicated: {', '.join(dupes)}"]
    if review.verdict.value == "changes_requested" and not review.findings:
        return ["verdict is changes_requested but there are no findings to act on"]
    return []


def check_implementation(impl: ImplementationResult, open_finding_ids: set[str]) -> list[str]:
    unknown = [d.finding_id for d in impl.disputed_findings if d.finding_id not in open_finding_ids]
    if unknown:
        known = ", ".join(sorted(open_finding_ids)) or "none"
        return [f"disputed_findings names unknown finding ids {unknown}; open findings: {known}"]
    return []
