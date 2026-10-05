"""Building the initial state of a task — the one place it happens, for
every entry point (the CLI today, LangGraph Studio, the GitHub App later).

`new_task_state` takes typed inputs. `normalize_input` accepts the raw JSON
a UI like LangGraph Studio sends — `{"task": {"title": ..., "description":
...}}` is enough — and fills everything else from `devloop.config.yaml`.
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


def new_task_state(
    task: TaskInput, config: DevLoopConfig, *, budget_usd: float | None = None
) -> DevLoopState:
    return {
        "task": task,
        "status": DevLoopStatus.RECEIVED,
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
    )
    if "team" in raw:
        config.agents = TeamConfig.model_validate(raw["team"])
    if "pull_request" in raw:
        config.pull_request = PullRequestConfig.model_validate(raw["pull_request"])
    return new_task_state(task, config, budget_usd=raw.get("budget_usd"))
