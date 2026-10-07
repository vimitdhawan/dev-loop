"""The task's record on disk, written by the graph itself — so it exists
the same way whichever process ran the task (`devloop run`, `devloop
watch`, LangGraph Studio's server) and whichever checkpointer that used.

    tasks/<id>/state.json      the full state after the latest node
    tasks/<id>/events.jsonl    one line per node: what ran, what it produced
    tasks/<id>/runs/NNN-*/     every agent run's context, prompt and output

The checkpointer stays the source of truth for resuming; this is the
inspectable copy that `devloop show`, a TUI or a web UI read.
"""

from __future__ import annotations

import json
import operator
import os
import time
from collections.abc import Callable
from functools import cache, wraps
from pathlib import Path
from typing import Annotated, Any, cast, get_args, get_origin, get_type_hints

from pydantic import TypeAdapter

from devloop.contracts.state import DevLoopState, TaskInput
from devloop.paths import devloop_home, task_dir

STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"

Node = Callable[[DevLoopState], dict[str, Any]]

_BOOKKEEPING = frozenset({"status", "cost_usd", "sessions", "agent_runs"})


@cache
def _adapter() -> TypeAdapter[DevLoopState]:
    return TypeAdapter(DevLoopState)


@cache
def _accumulating_fields() -> frozenset[str]:
    """State fields LangGraph appends to (`Annotated[list, operator.add]`)
    rather than overwrites."""

    hints = get_type_hints(DevLoopState, include_extras=True)
    return frozenset(
        name
        for name, hint in hints.items()
        if get_origin(hint) is Annotated and operator.add in get_args(hint)[1:]
    )


def merge(state: DevLoopState, update: dict[str, Any]) -> DevLoopState:
    """What LangGraph will make of `update` applied to `state`."""

    merged: dict[str, Any] = dict(state)
    for key, value in update.items():
        if key in _accumulating_fields():
            merged[key] = [*merged.get(key, []), *value]
        else:
            merged[key] = value
    return cast(DevLoopState, merged)


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def record(node_name: str, state: DevLoopState, update: dict[str, Any]) -> None:
    merged = merge(state, update)
    task = merged.get("task")
    if not isinstance(task, TaskInput):
        return  # raw input that never became a task; nothing to record
    directory = task_dir(task.external_id)
    _write_atomic(directory / STATE_FILE, _adapter().dump_json(merged, indent=2))
    event = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "node": node_name,
        "from": str(state.get("status") or ""),
        "status": str(merged.get("status") or ""),
        # what this node added, not bookkeeping or cleared fields
        "produced": sorted(
            k for k, v in update.items() if k not in _BOOKKEEPING and v not in (None, [], {})
        ),
        "cost_usd": round(merged.get("cost_usd", 0.0), 4),
    }
    with (directory / EVENTS_FILE).open("a") as fh:
        fh.write(json.dumps(event) + "\n")


def recorded[N: Node](node_name: str, node: N) -> N:
    """Wrap a node so its result is recorded before LangGraph applies it.
    A node that pauses (`interrupt()`) raises instead of returning, so a
    gate is recorded once — when it's answered."""

    @wraps(node)
    def wrapper(state: DevLoopState) -> dict[str, Any]:
        update = node(state)
        record(node_name, state, update)
        return update

    return cast(N, wrapper)


def _existing(task_id: str) -> Path:
    return devloop_home() / "tasks" / task_id  # no mkdir: reading must not create


def load_state(task_id: str) -> DevLoopState | None:
    path = _existing(task_id) / STATE_FILE
    if not path.exists():
        return None
    return _adapter().validate_json(path.read_text())


def load_events(task_id: str) -> list[dict[str, Any]]:
    path = _existing(task_id) / EVENTS_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
