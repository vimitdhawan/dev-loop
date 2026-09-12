"""Wires `DevLoopState`, the node implementations, and the pure `decide()`
policy into a compiled LangGraph graph.

Every node writes its own status via `decide()` (see `nodes.core._advance`);
this module's only job is routing — mapping the status a node just set to
the node that handles it next. `STATUS_TO_NODE` is deliberately total: if a
new status is added to the enum without a routing entry here, graph
compilation is where that should be caught, not a run in production.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from devloop.contracts.state import DevLoopState
from devloop.contracts.status import DevLoopStatus as St
from devloop.graph.nodes import core

STATUS_TO_NODE: dict[St, str] = {
    St.RECEIVED: "ingest",
    St.CLARIFICATION_REQUIRED: "clarify",
    St.PLANNING: "plan",
    St.PLAN_READY: "env_gate",
    St.ENV_BOOTSTRAP: "env_bootstrap",
    St.BASELINE: "baseline",
    St.IMPLEMENTING: "implement",
    St.TESTING: "verify",
    St.REVIEWING: "review",
    St.CHANGES_REQUIRED: "changes_required",
    St.REPLANNING: "replanning",
    St.READY_FOR_FINALIZE: "finalize",
    St.ESCALATED: "escalate",
    St.CANCELLED: END,
    St.FINALIZED: END,
    St.FAILED: END,
}


def _route(state: DevLoopState) -> str:
    status = state["status"]
    try:
        return STATUS_TO_NODE[status]
    except KeyError as exc:  # pragma: no cover - guarded by test_build.py
        raise ValueError(f"no route registered for status {status!r}") from exc


NODE_FUNCS = {
    "ingest": core.ingest,
    "clarify": core.clarify,
    "plan": core.plan,
    "env_gate": core.env_gate,
    "env_bootstrap": core.env_bootstrap,
    "baseline": core.baseline,
    "implement": core.implement,
    "verify": core.verify,
    "review": core.review,
    "changes_required": core.changes_required,
    "replanning": core.replanning,
    "finalize": core.finalize,
    "escalate": core.escalate,
}


def build_graph(checkpointer: BaseCheckpointSaver[Any] | None = None) -> object:
    """Compile the DevLoop graph. Pass a LangGraph checkpointer (e.g.
    `PostgresSaver` or `InMemorySaver`) to enable `interrupt()`/resume."""

    builder = StateGraph(DevLoopState)
    for name, fn in NODE_FUNCS.items():
        builder.add_node(name, fn)

    builder.set_entry_point("ingest")

    all_destinations = [*NODE_FUNCS.keys(), END]
    for name in NODE_FUNCS:
        builder.add_conditional_edges(name, _route, all_destinations)

    return builder.compile(checkpointer=checkpointer)
