"""`DevelopmentContext` — what the orchestrator hands an agent.

Written to `<workspace>/.devloop/in/context.json` before every agent run and
referenced from the prompt by path, never inlined: the prompt stays small
and versionable, and the context is inspectable after the fact.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from devloop.contracts.artifacts import (
    Clarification,
    DesignResult,
    Deviation,
    Finding,
    FindingDispute,
    PlanInvalidation,
    PlanResult,
    QAResult,
    RequirementResult,
)
from devloop.contracts.runs import Role, Step, Workflow
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
    step: Step
    task: TaskInput
    iteration: int
    base_commit: str
    branch: str
    # The stages this task goes through. A workflow without a Product Owner
    # (e.g. a bug fix) has no `requirements`: the task itself is the spec.
    workflow: Workflow | None = None

    requirements: RequirementResult | None = None
    # Every question asked during the task and its answer (PO's or a human's).
    clarifications: list[Clarification] = Field(default_factory=list)
    # For the Product Owner: the Engineer's questions to answer now.
    questions: list[str] = Field(default_factory=list)
    # The UX stage's screens and guidelines, for UI workflows.
    design: DesignResult | None = None
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
    # For QA and the Reviewer: the findings this attempt was meant to fix.
    prior_findings: list[Finding] = Field(default_factory=list)
    # For QA: where the orchestrator is serving the app, and where to save
    # screenshots (both relative to the workspace).
    app_url: str | None = None
    evidence_dir: str | None = None
    # For the Reviewer: QA's report on this change, and the full diff.
    qa: QAResult | None = None
    diff_path: str | None = None
