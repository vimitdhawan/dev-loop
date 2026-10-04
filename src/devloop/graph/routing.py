"""The routing policy.

`decide()` is the one piece of code allowed to choose the next
`DevLoopStatus`. It is a pure function — no I/O, no model calls — that
reads only facts already written into `DevLoopState` by a node. Nodes never
decide their own successor; they produce an artifact (or raise/escalate)
and `decide()` looks at what they produced.

Keeping this pure is what makes it table-testable, and table tests are
what let you change the policy with confidence instead of vibes.
"""

from __future__ import annotations

from collections.abc import Iterable

from devloop.contracts.artifacts import (
    Finding,
    FindingCategory,
    RequirementStatus,
    Severity,
    TestRunResult,
    Verdict,
)
from devloop.contracts.state import (
    MAX_REPLANS,
    MAX_REVIEW_ITERATIONS,
    DevLoopState,
)
from devloop.contracts.status import DevLoopStatus as St


def _budget_exceeded(state: DevLoopState) -> bool:
    budget = state.get("budget_usd")
    cost = state.get("cost_usd", 0.0)
    return budget is not None and cost >= budget


def decide(state: DevLoopState) -> St:
    """Given the current status and the facts accumulated so far, return
    the next status. Called after every node completes."""

    status = state["status"]

    # A budget breach or a failed side effect always wins, from any
    # in-flight status.
    if not status.is_terminal and not status.needs_human:
        if _budget_exceeded(state) or state.get("escalation_reason"):
            return St.ESCALATED

    match status:
        case St.RECEIVED | St.CLARIFICATION_REQUIRED:
            req = state.get("requirements")
            if req is not None and req.status == RequirementStatus.READY:
                return St.PLANNING
            return St.CLARIFICATION_REQUIRED

        case St.PLANNING | St.REPLANNING:
            return St.PLAN_READY if state.get("plan") else status

        case St.PLAN_READY:
            if state.get("env") is None:
                return St.ENV_BOOTSTRAP
            # A replan returns here; the base commit hasn't moved, so the
            # baseline already recorded is still the right one.
            return St.IMPLEMENTING if state.get("baseline") else St.BASELINE

        case St.ENV_BOOTSTRAP:
            env = state.get("env")
            # A recipe with no commands can't verify anything, so any
            # change it lets through would be unattributable.
            return St.BASELINE if env and env.commands else St.ESCALATED

        case St.BASELINE:
            # Pre-existing test failures are fine — we gate on "no *new*
            # failures", because real repos have flaky/pre-broken tests.
            # A broken *setup* is not: nothing after it would be attributable.
            if any(r.type == "setup" and not r.passed for r in state.get("baseline", [])):
                return St.ESCALATED
            return St.IMPLEMENTING

        case St.IMPLEMENTING:
            impl = state.get("implementation")
            if impl is not None and impl.plan_invalid is not None:
                if state.get("replan_count", 0) < MAX_REPLANS:
                    return St.REPLANNING
                return St.ESCALATED
            diff = state.get("diff")
            if diff is None:
                return St.IMPLEMENTING
            # The Developer ran and changed nothing without declaring the
            # plan invalid — looping would just burn budget.
            return St.TESTING if diff.files_changed else St.ESCALATED

        case St.TESTING:
            # Red checks go straight back to the Developer: reviewing a
            # change that already fails verification is wasted spend.
            return St.CHANGES_REQUIRED if new_failure_keys(state) else St.REVIEWING

        case St.REVIEWING:
            review = state.get("review")
            if review is None:
                return St.REVIEWING
            if (
                review.verdict == Verdict.APPROVED
                and not new_failure_keys(state)
                and not _blocking(review.findings)
            ):
                return St.READY_FOR_FINALIZE
            return St.CHANGES_REQUIRED

        case St.CHANGES_REQUIRED:
            if any(f.category == FindingCategory.PLAN_CONFORMANCE for f in open_findings(state)):
                if state.get("replan_count", 0) < MAX_REPLANS:
                    return St.REPLANNING
                return St.ESCALATED
            if state.get("iteration", 0) < MAX_REVIEW_ITERATIONS:
                return St.IMPLEMENTING
            return St.ESCALATED

        case St.READY_FOR_FINALIZE:
            # Waits for an explicit human decision (approve/cancel); a
            # bare re-decide() with no new input is idempotent.
            return St.READY_FOR_FINALIZE

        case St.ESCALATED | St.CANCELLED | St.FAILED | St.FINALIZED:
            return status

    raise ValueError(f"decide(): no rule for status {status!r}")


_BLOCKING = {Severity.P0, Severity.P1}


def _blocking(findings: Iterable[Finding]) -> bool:
    """An `approved` verdict that still carries an unresolved P0/P1 finding
    is self-contradictory; the severity wins."""

    return any(f.severity in _BLOCKING and not f.resolved for f in findings)


def open_findings(state: DevLoopState) -> list[Finding]:
    """Unresolved findings from the latest review plus human feedback —
    the one list the repair path works from, whatever its source."""

    review = state.get("review")
    reviewer = review.findings if review is not None else []
    return [f for f in [*reviewer, *state.get("feedback", [])] if not f.resolved]


def failure_keys(run: TestRunResult) -> set[str]:
    """Identity of what failed in one run. Named failures when the output
    could be parsed; otherwise the command itself, so a failure we can't
    parse (a compile error, a crashed linter) still counts."""

    if run.passed:
        return set()
    if run.failures:
        return {f"{run.type}::{f.name}" for f in run.failures}
    return {f"{run.type}::{run.command}"}


def latest_verification(state: DevLoopState) -> list[TestRunResult]:
    runs = state.get("verification", [])
    if not runs:
        return []
    latest = max(r.iteration for r in runs)
    return [r for r in runs if r.iteration == latest]


def new_failure_keys(state: DevLoopState) -> set[str]:
    """Failures in the *latest* verification that weren't already failing
    at baseline. This is what lets us gate on regressions instead of
    demanding an already-broken repo turn fully green — and comparing only
    the latest attempt means a failure the Developer fixed stops counting."""

    baseline = set().union(*(failure_keys(r) for r in state.get("baseline", [])))
    post = set().union(*(failure_keys(r) for r in latest_verification(state)))
    return post - baseline
