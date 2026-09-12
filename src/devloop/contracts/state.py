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
    PlanResult,
    RequirementResult,
    ReviewResult,
    TestRunResult,
)
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

    requirements: RequirementResult | None
    plan: PlanResult | None
    env: EnvRecipe | None
    baseline: Annotated[list[TestRunResult], operator.add]
    diff: DiffSummary | None
    verification: Annotated[list[TestRunResult], operator.add]
    review: ReviewResult | None
    feedback: Annotated[list[Finding], operator.add]

    iteration: int
    replan_count: int
    cost_usd: float
    budget_usd: float

    knowledge_used: list[str]
