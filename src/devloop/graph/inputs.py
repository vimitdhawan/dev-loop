"""Building the initial state of a task — the one place it happens, for
every entry point (`devloop run`, `devloop watch`, LangGraph Studio).

`new_task_state` takes typed inputs and picks the task's workflow.
`normalize_input` accepts the raw JSON a UI like LangGraph Studio sends —
`{"task": {"title": ..., "description": ...}}` is enough, plus optionally
`"workflow": "bug"` or `"labels": [...]` — and fills everything else from
`devloop.config.yaml`.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from devloop.config import DevLoopConfig, load_config
from devloop.contracts.runs import PullRequestConfig, TeamConfig
from devloop.contracts.state import DevLoopState, TaskInput
from devloop.contracts.status import DevLoopStatus
from devloop.errors import DevLoopError
from devloop.workflows import select_workflow


def new_task_state(
    task: TaskInput, config: DevLoopConfig, *, budget_usd: float | None = None
) -> DevLoopState:
    return {
        "task": task,
        "status": DevLoopStatus.RECEIVED,
        "workflow": select_workflow(task, config.workflows, config.default_workflow),
        "team": config.agents,
        "pull_request": config.pull_request,
        "sessions": {},
        "pending_questions": [],
        "consult_return": None,
        "po_consultations": 0,
        "clarifications": [],
        "prior_findings": [],
        "iteration": 0,
        "replan_count": 0,
        "cost_usd": 0.0,
        "budget_usd": budget_usd if budget_usd is not None else config.budget_usd,
        "baseline": [],
        "verification": [],
        "feedback": [],
        "agent_runs": [],
    }


def is_raw_input(state: DevLoopState) -> bool:
    """True when the graph was started with plain JSON instead of typed
    state — e.g. from LangGraph Studio's input form."""

    return isinstance(state.get("task"), dict)


def normalize_input(state: DevLoopState) -> DevLoopState:
    raw: dict[str, Any] = dict(state)
    task_raw: dict[str, Any] = dict(raw.get("task") or {})
    config = load_config()

    if not task_raw.get("description"):
        raise DevLoopError("task.description is required")
    source = task_raw.get("repo") or config.repo.url
    if not source:
        raise DevLoopError("no repo: set task.repo, DEVLOOP_REPO_URL, or repo.url in the config")
    if Path(source).exists():
        source = str(Path(source).resolve())
    task = TaskInput(
        external_id=task_raw.get("external_id") or uuid.uuid4().hex[:8],
        source=task_raw.get("source") or "studio",
        repo=source,
        base_branch=task_raw.get("base_branch") or config.repo.base_branch,
        title=task_raw.get("title") or "untitled task",
        description=task_raw["description"],
        workflow=task_raw.get("workflow") or None,
        labels=list(task_raw.get("labels") or []),
        url=task_raw.get("url"),
    )
    if "team" in raw:
        config.agents = TeamConfig.model_validate(raw["team"])
    if "pull_request" in raw:
        config.pull_request = PullRequestConfig.model_validate(raw["pull_request"])
    return new_task_state(task, config, budget_usd=raw.get("budget_usd"))
