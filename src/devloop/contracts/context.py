"""`DevelopmentContext` — what the orchestrator hands an agent.

Written to `<workspace>/.devloop/in/context.json` before every agent run and
referenced from the prompt by path, never inlined: the prompt stays small
and versionable, and the context is inspectable after the fact.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from devloop.contracts.artifacts import (
    Deviation,
    Finding,
    FindingDispute,
    PlanInvalidation,
    PlanResult,
    RequirementResult,
)
from devloop.contracts.runs import Role
from devloop.contracts.state import TaskInput


class VerificationSummary(BaseModel):
    """A compact view of one orchestrator-run check. Output is tail-trimmed
    so a noisy test suite can't blow up the agent's context."""

    type: str
    command: str
    passed: bool
    new_failure: bool
    failures: list[str] = Field(default_factory=list)
    output_tail: str = ""


class DevelopmentContext(BaseModel):
    role: Role
    task: TaskInput
    iteration: int
    base_commit: str
    branch: str

    requirements: RequirementResult | None = None
    plan: PlanResult | None = None
    # Why the previous plan was thrown away, so the next one doesn't repeat it.
    replan_reason: str | None = None
    # Unresolved findings the Developer must address on a repair pass.
    open_findings: list[Finding] = Field(default_factory=list)
    verification: list[VerificationSummary] = Field(default_factory=list)
    deviations: list[Deviation] = Field(default_factory=list)
    plan_invalid: PlanInvalidation | None = None
    disputed_findings: list[FindingDispute] = Field(default_factory=list)
    developer_notes: str = ""
    # Commands the orchestrator will run to verify; the Developer may run
    # these for its own feedback, but only the orchestrator's runs count.
    commands: dict[str, str] = Field(default_factory=dict)
    # For the Reviewer: path (relative to the workspace) of the full diff.
    diff_path: str | None = None
