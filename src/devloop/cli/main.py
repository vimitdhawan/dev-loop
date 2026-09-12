"""`devloop` local CLI — the v0 source and sink.

`devloop run` drives a task from a markdown file straight through the
graph to a local branch: no GitHub App, no webhook, no public endpoint.
Human gates (`clarify`, `finalize`, `escalate`) are answered right here in
the terminal via LangGraph's `interrupt()`/`Command(resume=...)`.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Any

import typer
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from rich import print as rprint
from rich.panel import Panel

from devloop.contracts.state import BUDGET_USD_DEFAULT, DevLoopState, TaskInput
from devloop.contracts.status import DevLoopStatus
from devloop.graph.build import build_graph

app = typer.Typer(no_args_is_help=True)


def _serde() -> object:
    """Our contracts (`devloop.contracts.*`) are msgpack-serialized as part
    of graph state. They're our own trusted code, not arbitrary input, so
    we explicitly allow-list every module that defines a state/artifact
    type rather than either blocking checkpoint resume (LangGraph's future
    strict default) or leaving `allowed_msgpack_modules=True`, which still
    warns on every unregistered type it lets through."""

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    allowed = {
        ("devloop.contracts.status", "DevLoopStatus"),
        ("devloop.contracts.state", "TaskInput"),
        ("devloop.contracts.state", "DiffSummary"),
        ("devloop.contracts.artifacts", "RequirementStatus"),
        ("devloop.contracts.artifacts", "RequirementResult"),
        ("devloop.contracts.artifacts", "FileToChange"),
        ("devloop.contracts.artifacts", "TestPlanItem"),
        ("devloop.contracts.artifacts", "PlanResult"),
        ("devloop.contracts.artifacts", "Severity"),
        ("devloop.contracts.artifacts", "FindingCategory"),
        ("devloop.contracts.artifacts", "Finding"),
        ("devloop.contracts.artifacts", "Verdict"),
        ("devloop.contracts.artifacts", "ReviewResult"),
        ("devloop.contracts.artifacts", "Deviation"),
        ("devloop.contracts.artifacts", "ServiceSpec"),
        ("devloop.contracts.artifacts", "EnvRecipe"),
        ("devloop.contracts.artifacts", "TestFailure"),
        ("devloop.contracts.artifacts", "TestRunResult"),
    }
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


def _pin(saver: BaseCheckpointSaver[Any], cm: Any) -> BaseCheckpointSaver[Any]:
    """`from_conn_string` is a generator-based contextmanager; its
    `finally` closes the connection when the generator is garbage
    collected. Pin `cm` on the saver we hand back so it stays alive for
    the life of the process instead of closing under us."""

    saver._devloop_cm = cm  # type: ignore[attr-defined]
    return saver


def _checkpointer() -> BaseCheckpointSaver[Any]:
    """Postgres if configured, else a local SQLite file — matches the
    plan's "keep it local" bias: v0 works with nothing running.

    `InMemorySaver` looks tempting here but is wrong for a CLI: `devloop
    run` and `devloop resume` are two separate process invocations, so
    anything held only in that process's memory is gone by the time
    `resume` starts. SQLite is a file, survives across invocations, and
    still needs no server."""

    import os

    dsn = os.environ.get("DEVLOOP_DATABASE_URL")
    if dsn:
        from langgraph.checkpoint.postgres import PostgresSaver

        pg_cm = PostgresSaver.from_conn_string(dsn)
        pg_saver = pg_cm.__enter__()
        pg_saver.serde = _serde()  # type: ignore[assignment]
        pg_saver.setup()
        return _pin(pg_saver, pg_cm)

    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = Path.home() / ".devloop" / "checkpoints.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite_cm = SqliteSaver.from_conn_string(str(db_path))
    sqlite_saver = sqlite_cm.__enter__()
    sqlite_saver.serde = _serde()  # type: ignore[assignment]
    return _pin(sqlite_saver, sqlite_cm)


def _print_state(state: dict[str, Any]) -> None:
    status = state.get("status")
    rprint(f"[bold cyan]-> {status}[/bold cyan]")


@app.command()
def run(
    repo: Annotated[Path, typer.Option(help="Path to the target git repository")],
    task_file: Annotated[Path, typer.Option("--task", help="Markdown file describing the task")],
    budget_usd: Annotated[float, typer.Option(help="Escalate once cost reaches this")] = (
        BUDGET_USD_DEFAULT
    ),
) -> None:
    """Start a new task and drive it to a human gate or completion."""

    if not task_file.exists():
        raise typer.BadParameter(f"task file not found: {task_file}")

    title = task_file.stem.replace("-", " ").replace("_", " ")
    description = task_file.read_text()
    task_id = uuid.uuid4().hex[:8]

    task = TaskInput(
        external_id=task_id,
        source="local",
        repo_path=str(repo.resolve()),
        title=title,
        description=description,
    )

    graph = build_graph(checkpointer=_checkpointer())
    config = {"configurable": {"thread_id": task_id}}
    initial: DevLoopState = {
        "task": task,
        "status": DevLoopStatus.RECEIVED,
        "iteration": 0,
        "replan_count": 0,
        "cost_usd": 0.0,
        "budget_usd": budget_usd,
        "baseline": [],
        "verification": [],
        "feedback": [],
    }

    rprint(Panel(f"task [bold]{task_id}[/bold]: {title}\nrepo: {task.repo_path}"))
    _drive(graph, initial, config, task_id)


@app.command()
def resume(
    task_id: Annotated[str, typer.Argument(help="Task id printed by `devloop run`")],
    answer: Annotated[
        str, typer.Option(help="Answer to feed back at a clarify/finalize/escalate gate")
    ] = "",
) -> None:
    """Resume a task paused at a human gate."""

    graph = build_graph(checkpointer=_checkpointer())
    config = {"configurable": {"thread_id": task_id}}
    payload: object = {"answer": answer} if answer else "approve"
    _drive(graph, Command(resume=payload), config, task_id)


def _drive(
    graph: Any, input_or_command: object, config: dict[str, Any], task_id: str
) -> None:
    for update in graph.stream(input_or_command, config, stream_mode="values"):
        _print_state(update)

    snapshot = graph.get_state(config)
    if snapshot.next:
        rprint(
            Panel(
                f"[yellow]paused[/yellow] — resume with:\n"
                f"  devloop resume {task_id} --answer '...'",
                title="waiting on a human",
            )
        )
    else:
        final_status = snapshot.values.get("status")
        rprint(Panel(f"[bold green]done[/bold green]: {final_status}"))


if __name__ == "__main__":
    app()
