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


class ScreenDesign(BaseModel):
    """One screen or component the change adds or alters, specified well
    enough to build and to test against."""

    name: str
    purpose: str
    # layout and components, top to bottom, in words
    layout: list[str] = Field(default_factory=list)
    # e.g. "empty: shows 'No players yet' and an Add button"
    states: list[str] = Field(default_factory=list)
    interactions: list[str] = Field(default_factory=list)
    # existing components/files to reuse, so the plan builds on what's there
    reuse: list[str] = Field(default_factory=list)


class DesignReference(BaseModel):
    """A design made with a tool, e.g. a Stitch screen. `ref` is whatever
    finds it again: a project/screen id, a URL, a path under `.devloop/`."""

    tool: str  # "stitch" | "file" | "url" | ...
    ref: str
    description: str = ""


class DesignResult(BaseModel):
    """The UX stage's hand-off to the Planner. Text first — every later
    agent reads it — with tool-made designs as references alongside."""

    summary: str
    screens: list[ScreenDesign] = Field(default_factory=list)
    # visual decisions: spacing, colour tokens, typography, copy tone
    guidelines: list[str] = Field(default_factory=list)
    accessibility: list[str] = Field(default_factory=list)
    references: list[DesignReference] = Field(default_factory=list)
    # Non-empty: UX can't design without answers; they go to the Product Owner.
    questions_for_po: list[str] = Field(default_factory=list)


class FileAction(StrEnum):
    MODIFY = "modify"
    CREATE = "create"
    DELETE = "delete"


class FileToChange(BaseModel):
    path: str
    reason: str
    # `modify`/`delete` must name a file that exists at the base commit;
    # `create` must not. Checked deterministically before implementing.
    action: FileAction = FileAction.MODIFY


class TestPlanItem(BaseModel):
    type: str  # "unit" | "integration" | "e2e"
    description: str


class PlanResult(BaseModel):
    summary: str
    files_to_change: list[FileToChange] = Field(default_factory=list)
    implementation_steps: list[str] = Field(default_factory=list)
    tests: list[TestPlanItem] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    # Non-empty: the Engineer can't plan without answers. The rest of the
    # document is ignored and the questions go to the Product Owner.
    questions_for_po: list[str] = Field(default_factory=list)


class Clarification(BaseModel):
    question: str
    answer: str
    answered_by: str  # "product_owner" | "human"


class POAnswer(BaseModel):
    """The Product Owner answering the Engineer. A question it can't answer
    from the requirements and the code goes to a human instead of being
    guessed at."""

    answers: list[Clarification] = Field(default_factory=list)
    needs_human: list[str] = Field(default_factory=list)


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
    FUNCTIONAL = "functional"  # QA: the feature doesn't behave as required
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
    # who raised it — "reviewer", "qa", "orchestrator" or "human" — and
    # whether it has been resolved so a later repair pass doesn't re-open it.
    # One contract for every source means one repair path.
    source: str = "reviewer"
    resolved: bool = False


class Verdict(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class ReviewResult(BaseModel):
    verdict: Verdict
    findings: list[Finding] = Field(default_factory=list)


class QAVerdict(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    # QA couldn't test for a reason the Engineer can't fix (credentials,
    # external service down). Escalates.
    BLOCKED = "blocked"
    # Set by the orchestrator, never by the agent: the repo has no `app:`.
    SKIPPED = "skipped"


class QAScenario(BaseModel):
    criterion: str  # the acceptance criterion this scenario exercises
    steps: list[str] = Field(default_factory=list)
    passed: bool
    evidence: str = ""  # what was observed; screenshot path under .devloop/qa/


class QAResult(BaseModel):
    verdict: QAVerdict
    scenarios: list[QAScenario] = Field(default_factory=list)
    # Bugs, as findings, so they go down the same repair path as review
    # findings. Use ids Q1, Q2, …
    findings: list[Finding] = Field(default_factory=list)
    notes: str = ""


class Deviation(BaseModel):
    """Declared divergence from the plan. The Reviewer checks plan-vs-diff
    conformance against these; undeclared drift is itself a finding."""

    step: str
    what_changed: str
    why: str


class PlanInvalidation(BaseModel):
    """The Developer's way to say "this plan cannot work" with evidence,
    instead of silently drifting from it. Routes to REPLANNING."""

    reason: str
    evidence: str


class FindingDispute(BaseModel):
    """The Developer declining a finding — typically because it contradicts
    the requirements. Escalated to a human when nothing else changed."""

    finding_id: str
    reason: str


class ImplementationResult(BaseModel):
    """Written by the Developer. The orchestrator — not the agent — commits
    the working tree afterwards, so the diff itself is a measured fact and
    this document only carries what the diff cannot say."""

    summary: str
    deviations: list[Deviation] = Field(default_factory=list)
    plan_invalid: PlanInvalidation | None = None
    disputed_findings: list[FindingDispute] = Field(default_factory=list)
    notes_for_reviewer: str = ""
    # Non-empty: the Engineer stopped to ask. Nothing is committed; the
    # questions go to the Product Owner and the same session resumes.
    questions_for_po: list[str] = Field(default_factory=list)


class ServiceSpec(BaseModel):
    name: str  # e.g. "postgres"
    image: str
    env: dict[str, str] = Field(default_factory=dict)
    ports: list[int] = Field(default_factory=list)


class AppSpec(BaseModel):
    """How to run the app so QA can test it in a browser. Started and
    stopped by the orchestrator, never by the agent."""

    start: str
    url: str
    ready_timeout_s: int = 120
    env: dict[str, str] = Field(default_factory=dict)
    # run before `start` / after the app is stopped, e.g. `supabase start`
    setup: list[str] = Field(default_factory=list)
    teardown: list[str] = Field(default_factory=list)


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
    app: AppSpec | None = None


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
    # implementation attempt this run verified (0 for baseline); the
    # regression gate compares only the latest attempt against baseline.
    iteration: int = 0
    passed: bool
    output: str = ""
    duration_ms: int = 0
    failures: list[TestFailure] = Field(default_factory=list)
