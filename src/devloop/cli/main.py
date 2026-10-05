"""`devloop` local CLI — the v0 source and sink.

`devloop run` drives a task from a markdown file through the graph to a
`devloop/<task-id>` branch in the target repo: no GitHub App, no webhook,
no public endpoint. Human gates (`clarify`, `finalize`, `escalate`) are
answered from the terminal via LangGraph's `interrupt()`/`Command(resume=...)`.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Annotated, Any

import typer
from langgraph.types import Command
from rich import print as rprint
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from devloop.contracts.runs import Role, RuntimeConfig
from devloop.contracts.state import BUDGET_USD_DEFAULT, DevLoopState, TaskInput
from devloop.contracts.status import DevLoopStatus
from devloop.graph.build import build_graph
from devloop.paths import workspace_dir
from devloop.runtimes.registry import known_runtimes
from devloop.store.checkpointer import checkpointer

app = typer.Typer(no_args_is_help=True)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(show_path=False, markup=False)],
    )


def _runtime_config(
    runtime: str, model: str | None, role_runtime: list[str], agent_timeout: int | None
) -> RuntimeConfig:
    known = known_runtimes()
    if runtime not in known:
        raise typer.BadParameter(f"unknown runtime {runtime!r}; known: {', '.join(known)}")
    roles: dict[Role, str] = {}
    for item in role_runtime:
        role_name, sep, name = item.partition("=")
        if not sep or role_name not in Role.__members__.values() or name not in known:
            raise typer.BadParameter(
                f"--role-runtime expects <role>=<runtime> with role in "
                f"{', '.join(r.value for r in Role)} and runtime in {', '.join(known)}; "
                f"got {item!r}"
            )
        roles[Role(role_name)] = name
    return RuntimeConfig(default=runtime, model=model, roles=roles, agent_timeout_s=agent_timeout)


@app.command()
def run(
    repo: Annotated[Path, typer.Option(help="Path to the target git repository")],
    task_file: Annotated[Path, typer.Option("--task", help="Markdown file describing the task")],
    budget_usd: Annotated[float, typer.Option(help="Escalate once cost reaches this")] = (
        BUDGET_USD_DEFAULT
    ),
    runtime: Annotated[
        str, typer.Option(help="Agent CLI for every role: claude | opencode | stub")
    ] = "claude",
    model: Annotated[
        str | None, typer.Option(help="Model passed to the agent CLI (e.g. sonnet, opus)")
    ] = None,
    role_runtime: Annotated[
        list[str] | None,
        typer.Option(help="Per-role override, repeatable: --role-runtime reviewer=opencode"),
    ] = None,
    agent_timeout: Annotated[
        int | None,
        typer.Option(
            help="Seconds each agent run may take, for every role (defaults: 10-30 min by role)",
            min=60,
        ),
    ] = None,
) -> None:
    """Start a new task and drive it to a human gate or completion."""

    if not task_file.exists():
        raise typer.BadParameter(f"task file not found: {task_file}")
    _setup_logging()
    runtime_config = _runtime_config(runtime, model, role_runtime or [], agent_timeout)

    title = task_file.stem.replace("-", " ").replace("_", " ")
    task_id = uuid.uuid4().hex[:8]
    task = TaskInput(
        external_id=task_id,
        source="local",
        repo_path=str(repo.resolve()),
        title=title,
        description=task_file.read_text(),
    )

    graph = build_graph(checkpointer=checkpointer())
    config = {"configurable": {"thread_id": task_id}}
    initial: DevLoopState = {
        "task": task,
        "status": DevLoopStatus.RECEIVED,
        "runtime": runtime_config,
        "iteration": 0,
        "replan_count": 0,
        "cost_usd": 0.0,
        "budget_usd": budget_usd,
        "baseline": [],
        "verification": [],
        "feedback": [],
        "agent_runs": [],
    }

    rprint(
        Panel(
            f"task [bold]{task_id}[/bold]: {title}\n"
            f"repo: {task.repo_path}\n"
            f"runtime: {runtime}" + (f" ({model})" if model else "")
        )
    )
    _drive(graph, initial, config, task_id)


@app.command()
def resume(
    task_id: Annotated[str, typer.Argument(help="Task id printed by `devloop run`")],
    answer: Annotated[
        str, typer.Option(help="Answer for the gate: text at clarify, approve|cancel otherwise")
    ] = "",
) -> None:
    """Resume a task paused at a human gate."""

    _setup_logging()
    graph = build_graph(checkpointer=checkpointer())
    config = {"configurable": {"thread_id": task_id}}
    if not graph.get_state(config).values:
        raise typer.BadParameter(f"no task with id {task_id}")
    payload: object = {"answer": answer} if answer else "approve"
    _drive(graph, Command(resume=payload), config, task_id)


@app.command()
def show(task_id: Annotated[str, typer.Argument(help="Task id printed by `devloop run`")]) -> None:
    """Status, cost and every agent run of a task."""

    graph = build_graph(checkpointer=checkpointer())
    values = graph.get_state({"configurable": {"thread_id": task_id}}).values
    if not values:
        raise typer.BadParameter(f"no task with id {task_id}")

    rprint(
        Panel(
            f"status: [bold]{values.get('status')}[/bold]\n"
            f"branch: {values.get('branch')}  base: {(values.get('base_commit') or '')[:10]}\n"
            f"iteration: {values.get('iteration')}  replans: {values.get('replan_count')}\n"
            f"cost: ${values.get('cost_usd', 0.0):.4f} of ${values.get('budget_usd') or 0:.2f}\n"
            f"workspace: {workspace_dir(task_id)}"
            + (
                f"\n[red]escalation:[/red] {values['escalation_reason']}"
                if values.get("escalation_reason")
                else ""
            ),
            title=f"task {task_id}",
        )
    )
    table = Table("role", "iter", "try", "runtime", "prompt", "ok", "cost $", "tokens in/out", "s")
    for r in values.get("agent_runs", []):
        table.add_row(
            r.role.value,
            str(r.iteration),
            str(r.attempt),
            r.runtime,
            r.prompt_version,
            "✓" if r.ok else f"✗ {r.error[:40]}",
            f"{r.cost_usd:.4f}",
            f"{r.input_tokens}/{r.output_tokens}",
            f"{r.duration_ms / 1000:.0f}",
        )
    rprint(table)


def _drive(graph: Any, input_or_command: object, config: dict[str, Any], task_id: str) -> None:
    for update in graph.stream(input_or_command, config, stream_mode="values"):
        cost = update.get("cost_usd") or 0.0
        rprint(f"[bold cyan]-> {update.get('status')}[/bold cyan]  [dim]${cost:.4f}[/dim]")

    snapshot = graph.get_state(config)
    if snapshot.next:
        for task in snapshot.tasks:
            for intr in task.interrupts:
                rprint(Panel(_format_interrupt(intr.value), title="waiting on a human"))
        rprint(f"resume with: [bold]devloop resume {task_id} --answer '...'[/bold]")
    else:
        rprint(Panel(f"[bold green]done[/bold green]: {snapshot.values.get('status')}"))


def _format_interrupt(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value)
    lines = []
    for key, item in value.items():
        if item is None:
            continue
        if hasattr(item, "model_dump"):
            item = json.dumps(item.model_dump(mode="json"), indent=2)
        elif isinstance(item, list):
            item = "\n".join(f"  - {q}" for q in item) or "  (none)"
            lines.append(f"{key}:\n{item}")
            continue
        lines.append(f"{key}: {item}")
    return "\n".join(lines)


if __name__ == "__main__":
    app()
