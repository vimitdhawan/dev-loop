"""LangGraph state shape.

`operator.add`-reduced lists accumulate across nodes/iterations (LangGraph
merges partial-state updates a node returns into these via the reducer).
Everything else is last-write-wins, which is fine because only one node
writes each of those fields per step.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from pydantic import BaseModel

from devloop.contracts.artifacts import (
    Clarification,
    Deviation,
    EnvRecipe,
    Finding,
    ImplementationResult,
    PlanResult,
    QAResult,
    RequirementResult,
    ReviewResult,
    TestRunResult,
)
from devloop.contracts.runs import AgentRunRecord, PullRequestConfig, TeamConfig
from devloop.contracts.status import DevLoopStatus

BUDGET_USD_DEFAULT = 20.0
MAX_REVIEW_ITERATIONS = 3
MAX_REPLANS = 2
MAX_PO_CONSULTATIONS = 3


class TaskInput(BaseModel):
    """What a source (local CLI in v0, GitHub App later) hands the graph to
    start a task."""

    external_id: str
    source: str = "local"
    # A local path or a clone URL. Either way the task works in a fresh
    # clone; the user's checkout is never touched.
    repo: str
    # Branch to fork from; None = the repo's default branch.
    base_branch: str | None = None
    title: str
    description: str


class DiffSummary(BaseModel):
    files_changed: list[str] = []
    insertions: int = 0
    deletions: int = 0
    deviations: list[Deviation] = []


class DevLoopState(TypedDict, total=False):
    task: TaskInput
    status: DevLoopStatus
    team: TeamConfig
    pull_request: PullRequestConfig

    # Per-task workspace (a clone of the target repo) and where it forked.
    workspace_path: str | None
    base_branch: str | None
    base_commit: str | None
    branch: str | None
    # role -> agent session id, so the Product Owner, Engineer and QA each
    # keep their context across steps. The Reviewer is never stored here:
    # every review starts fresh.
    sessions: dict[str, str]

    requirements: RequirementResult | None
    # Questions the Engineer is waiting on, the status to return to once
    # they're answered, and every answer so far (PO's or a human's).
    pending_questions: list[str]
    consult_return: DevLoopStatus | None
    po_consultations: int
    clarifications: Annotated[list[Clarification], operator.add]

    plan: PlanResult | None
    # Why the last plan was abandoned; handed to the Engineer on a replan.
    replan_reason: str | None
    env: EnvRecipe | None
    baseline: Annotated[list[TestRunResult], operator.add]
    implementation: ImplementationResult | None
    diff: DiffSummary | None
    verification: Annotated[list[TestRunResult], operator.add]
    qa: QAResult | None
    review: ReviewResult | None
    # The findings the latest implementation attempt was asked to fix, so
    # QA and a fresh Reviewer can check they actually were.
    prior_findings: list[Finding]
    feedback: Annotated[list[Finding], operator.add]
    pull_request_url: str | None

    iteration: int
    replan_count: int
    cost_usd: float
    budget_usd: float
    agent_runs: Annotated[list[AgentRunRecord], operator.add]
    # Set by a node when a side effect failed in a way only a human can fix
    # (agent CLI crashed, contract invalid after repair, git refused).
    escalation_reason: str | None

    knowledge_used: list[str]
