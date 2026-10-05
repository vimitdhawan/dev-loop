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
    Deviation,
    EnvRecipe,
    Finding,
    ImplementationResult,
    PlanResult,
    RequirementResult,
    ReviewResult,
    TestRunResult,
)
from devloop.contracts.runs import AgentRunRecord, RuntimeConfig
from devloop.contracts.status import DevLoopStatus

BUDGET_USD_DEFAULT = 20.0
MAX_REVIEW_ITERATIONS = 3
MAX_REPLANS = 2


class TaskInput(BaseModel):
    """What a source (local CLI in v0, GitHub App later) hands the graph to
    start a task."""

    external_id: str
    source: str = "local"
    repo_path: str
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
    runtime: RuntimeConfig

    # Per-task workspace (a clone of the target repo) and where it forked.
    workspace_path: str | None
    base_commit: str | None
    branch: str | None

    requirements: RequirementResult | None
    plan: PlanResult | None
    # Why the last plan was abandoned; handed to the Planner on a replan.
    replan_reason: str | None
    env: EnvRecipe | None
    baseline: Annotated[list[TestRunResult], operator.add]
    implementation: ImplementationResult | None
    diff: DiffSummary | None
    verification: Annotated[list[TestRunResult], operator.add]
    review: ReviewResult | None
    feedback: Annotated[list[Finding], operator.add]

    iteration: int
    replan_count: int
    cost_usd: float
    budget_usd: float
    agent_runs: Annotated[list[AgentRunRecord], operator.add]
    # Set by a node when a side effect failed in a way only a human can fix
    # (agent CLI crashed, contract invalid after repair, git refused).
    escalation_reason: str | None

    knowledge_used: list[str]
