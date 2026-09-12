"""Agent output contracts.

Every agent writes exactly one JSON document to
`/workspace/.devloop/out/<role>.json` inside its sandbox. The orchestrator
reads that file and validates it against the matching model here. This is
the *only* channel an agent has to influence the workflow — an agent's
prose, reasoning, or opinion about "what should happen next" is never
consulted by `decide()`.

`ReviewResult.verdict` and `Finding.severity` are read directly by the
routing policy: treat them as a versioned API, not an implementation detail.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class RequirementStatus(StrEnum):
    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"


class RequirementResult(BaseModel):
    status: RequirementStatus
    summary: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)


class FileToChange(BaseModel):
    path: str
    reason: str


class TestPlanItem(BaseModel):
    type: str  # "unit" | "integration" | "e2e"
    description: str


class PlanResult(BaseModel):
    summary: str
    files_to_change: list[FileToChange] = Field(default_factory=list)
    implementation_steps: list[str] = Field(default_factory=list)
    tests: list[TestPlanItem] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class Severity(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class FindingCategory(StrEnum):
    CORRECTNESS = "correctness"
    SECURITY = "security"
    PERFORMANCE = "performance"
    ARCHITECTURE = "architecture"
    TESTING = "testing"
    MAINTAINABILITY = "maintainability"
    PLAN_CONFORMANCE = "plan_conformance"


class Finding(BaseModel):
    id: str
    severity: Severity
    category: FindingCategory
    file: str | None = None
    line: int | None = None
    description: str
    recommendation: str
    # set when this finding originated from a human (terminal in v0, PR
    # review comment in v1) rather than the Reviewer agent, and when it has
    # been resolved so a later repair pass doesn't re-open it.
    source: str = "reviewer"
    resolved: bool = False


class Verdict(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class ReviewResult(BaseModel):
    verdict: Verdict
    findings: list[Finding] = Field(default_factory=list)


class Deviation(BaseModel):
    """Declared divergence from the plan. The Reviewer checks plan-vs-diff
    conformance against these; undeclared drift is itself a finding."""

    step: str
    what_changed: str
    why: str


class ServiceSpec(BaseModel):
    name: str  # e.g. "postgres"
    image: str
    env: dict[str, str] = Field(default_factory=dict)
    ports: list[int] = Field(default_factory=list)


class EnvRecipe(BaseModel):
    """How this repo builds, tests, and what it needs. Produced once by the
    bootstrap agent, cached by repo + lockfile hash, and reused by every
    later task against the same repo."""

    image: str
    setup: list[str] = Field(default_factory=list)
    commands: dict[str, str] = Field(default_factory=dict)
    services: list[ServiceSpec] = Field(default_factory=list)
    required_secrets: list[str] = Field(default_factory=list)
    verified_at_commit: str


class TestFailure(BaseModel):
    name: str
    message: str


class TestRunResult(BaseModel):
    """Result of running one verification command (lint/typecheck/test:*).
    `phase` distinguishes a baseline run (on the untouched base commit) from
    a post-change run, so a failure can be attributed to the agent's diff
    rather than a pre-existing break in the repo."""

    type: str  # "lint" | "typecheck" | "unit" | "integration" | "e2e"
    command: str
    phase: str  # "baseline" | "post_change"
    passed: bool
    output: str = ""
    duration_ms: int = 0
    failures: list[TestFailure] = Field(default_factory=list)
