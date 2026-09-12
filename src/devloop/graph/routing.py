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

from devloop.contracts.artifacts import RequirementStatus, Verdict
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

    # A budget breach always wins, from any non-terminal status.
    if not status.is_terminal and not status.needs_human and _budget_exceeded(state):
        return St.ESCALATED

    match status:
        case St.RECEIVED:
            return St.PLANNING if state.get("requirements") else St.CLARIFICATION_REQUIRED

        case St.CLARIFICATION_REQUIRED:
            req = state.get("requirements")
            if req is None:
                return St.CLARIFICATION_REQUIRED
            return (
                St.PLANNING
                if req.status == RequirementStatus.READY
                else St.CLARIFICATION_REQUIRED
            )

        case St.PLANNING:
            return St.PLAN_READY if state.get("plan") else St.PLANNING

        case St.PLAN_READY:
            return St.ENV_BOOTSTRAP if state.get("env") is None else St.BASELINE

        case St.ENV_BOOTSTRAP:
            return St.BASELINE if state.get("env") else St.ESCALATED

        case St.BASELINE:
            # Baseline always proceeds to implementation: we gate on "no
            # *new* failures" at review time, not on a fully green baseline,
            # because real repos have flaky/pre-broken tests.
            return St.IMPLEMENTING

        case St.IMPLEMENTING:
            return St.TESTING if state.get("diff") else St.IMPLEMENTING

        case St.TESTING:
            return St.REVIEWING

        case St.REVIEWING:
            review = state.get("review")
            if review is None:
                return St.REVIEWING
            if review.verdict == Verdict.APPROVED and not _new_failures(state):
                return St.READY_FOR_FINALIZE
            return St.CHANGES_REQUIRED

        case St.CHANGES_REQUIRED:
            replan_requested = any(
                f.category.value == "plan_conformance" and not f.resolved
                for f in state.get("feedback", [])
            )
            if replan_requested:
                if state.get("replan_count", 0) < MAX_REPLANS:
                    return St.REPLANNING
                return St.ESCALATED
            if state.get("iteration", 0) < MAX_REVIEW_ITERATIONS:
                return St.IMPLEMENTING
            return St.ESCALATED

        case St.REPLANNING:
            return St.PLAN_READY if state.get("plan") else St.REPLANNING

        case St.READY_FOR_FINALIZE:
            # Waits for an explicit human decision (approve/cancel); a
            # bare re-decide() with no new input is idempotent.
            return St.READY_FOR_FINALIZE

        case St.ESCALATED | St.CANCELLED | St.FAILED | St.FINALIZED:
            return status

    raise ValueError(f"decide(): no rule for status {status!r}")


def _new_failures(state: DevLoopState) -> bool:
    """True if verification produced a failing test/check that wasn't
    already failing at baseline. This is what lets us gate on regressions
    instead of demanding an already-broken repo turn fully green."""

    baseline_failed = {
        f.name for run in state.get("baseline", []) if not run.passed for f in run.failures
    }
    post_failed = {
        f.name for run in state.get("verification", []) if not run.passed for f in run.failures
    }
    return bool(post_failed - baseline_failed)
