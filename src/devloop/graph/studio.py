"""Entry point for LangGraph Studio (`langgraph dev`, see `langgraph.json`).

The same graph the CLI runs, compiled without a checkpointer: the LangGraph
dev server supplies its own persistence. Tasks started here therefore live
in the server's store, not `~/.devloop/checkpoints.sqlite` — drive them
from Studio (including answering interrupts), not with `devloop resume`.

Start a task from Studio's input form with just:

    {"task": {"title": "default rating", "description": "<the task markdown>"}}

Everything else (repo, base branch, team, PR, budget) comes from
`devloop.config.yaml`; add `"repo"`/`"base_branch"` under `task`, or a
`"team"` object, to override them for that run.
"""

from __future__ import annotations

from devloop.graph.build import build_graph

graph = build_graph()
