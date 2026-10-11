"""Driving a task through the graph until it finishes or waits on a human —
shared by `devloop run`, `devloop resume` and `devloop watch`, which differ
only in what they do with each node's update (render it, log it)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from devloop.contracts.status import DevLoopStatus

# (node name, the facts it produced)
OnUpdate = Callable[[str, dict[str, Any]], None]


@dataclass
class Outcome:
    task_id: str
    values: dict[str, Any]
    # payloads of the gates the task is waiting at; empty once it's done
    waiting: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> DevLoopStatus | None:
        status = self.values.get("status")
        return DevLoopStatus(status) if status else None


def thread(task_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": task_id}}


def drive(graph: Any, value: object, task_id: str, on_update: OnUpdate | None = None) -> Outcome:
    config = thread(task_id)
    for chunk in graph.stream(value, config, stream_mode="updates"):
        for node, update in chunk.items():
            if node != "__interrupt__" and on_update is not None and update:
                on_update(node, update)
    snapshot = graph.get_state(config)
    waiting = (
        [
            intr.value if isinstance(intr.value, dict) else {"value": intr.value}
            for task in snapshot.tasks
            for intr in task.interrupts
        ]
        if snapshot.next
        else []
    )
    return Outcome(task_id=task_id, values=dict(snapshot.values), waiting=waiting)
