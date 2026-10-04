"""Where DevLoop keeps things on the control-plane machine.

Everything lives under `DEVLOOP_HOME` (default `~/.devloop`):

    checkpoints.sqlite        LangGraph state
    work/<task_id>/           per-task workspace — a clone of the target repo
    tasks/<task_id>/          per-task evidence: agent logs, agent_runs.jsonl
"""

from __future__ import annotations

import os
from pathlib import Path


def devloop_home() -> Path:
    return Path(os.environ.get("DEVLOOP_HOME", Path.home() / ".devloop"))


def workspace_dir(task_id: str) -> Path:
    return devloop_home() / "work" / task_id


def task_dir(task_id: str) -> Path:
    path = devloop_home() / "tasks" / task_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def prompts_dir() -> Path:
    """`prompts/` sits at the repo top level, not inside the package, so it
    can be the improvement engine's mutation target without touching code."""

    override = os.environ.get("DEVLOOP_PROMPTS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "prompts"
