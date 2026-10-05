"""`devloop` local CLI — the v0 source and sink.

`devloop run` drives a task from a markdown file through the agent team to
a pushed `devloop/<task-id>-<slug>` branch — and a PR when the repo is on
GitHub. Human gates (`clarify`, `finalize`, `escalate`) are answered from
the terminal via LangGraph's `interrupt()`/`Command(resume=...)`.

Who is on the team comes from `devloop.config.yaml` (see
`devloop.config.example.yaml`); flags override it for one run.
"""

from __future__ import annotations

import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from langgraph.types import Command
from rich import print as rprint
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from devloop.config import ConfigError, DevLoopConfig, config_path, load_config
from devloop.contracts.runs import Role
from devloop.contracts.state import TaskInput
from devloop.graph.build import build_graph
from devloop.graph.inputs import new_task_state
from devloop.paths import workspace_dir
from devloop.runtimes.probe import check_model
from devloop.runtimes.probe import probe as run_probe
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


def _load() -> DevLoopConfig:
    try:
        return load_config()
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _role_overrides(items: list[str], flag: str) -> dict[Role, str]:
    overrides: dict[Role, str] = {}
    for item in items:
        role_name, sep, value = item.partition("=")
        if not sep or role_name not in [r.value for r in Role] or not value:
            raise typer.BadParameter(
                f"{flag} expects <role>=<value> with role in "
                f"{', '.join(r.value for r in Role)}; got {item!r}"
            )
        overrides[Role(role_name)] = value
    return overrides


def _apply_overrides(
    config: DevLoopConfig,
    *,
    runtime: str | None,
    model: str | None,
    role_runtime: list[str],
    role_model: list[str],
    agent_timeout: int | None,
) -> None:
    runtimes = _role_overrides(role_runtime, "--role-runtime")
    models = _role_overrides(role_model, "--role-model")
    known = known_runtimes()
    for role in Role:
        agent = config.agents.for_role(role)
        agent.runtime = runtimes.get(role) or runtime or agent.runtime
        agent.model = models.get(role) or model or agent.model
        agent.timeout_s = agent_timeout or agent.timeout_s
        if agent.runtime not in known:
            raise typer.BadParameter(
                f"{role.value}: unknown runtime {agent.runtime!r}; known: {', '.join(known)}"
            )


@app.command()
def run(
    task_file: Annotated[Path, typer.Option("--task", help="Markdown file describing the task")],
    repo: Annotated[
        str | None,
        typer.Option(help="Local path or clone URL (else DEVLOOP_REPO_URL / config repo.url)"),
    ] = None,
    base_branch: Annotated[
        str | None,
        typer.Option(help="Branch to fork from (else DEVLOOP_BASE_BRANCH / config / default)"),
    ] = None,
    budget_usd: Annotated[
        float | None, typer.Option(help="Escalate once cost reaches this (config: budget_usd)")
    ] = None,
    runtime: Annotated[
        str | None, typer.Option(help="Agent CLI for every role: claude | opencode | stub")
    ] = None,
    model: Annotated[str | None, typer.Option(help="Model for every role (e.g. sonnet)")] = None,
    role_runtime: Annotated[
        list[str] | None,
        typer.Option(help="Per-role runtime, repeatable: --role-runtime qa=opencode"),
    ] = None,
    role_model: Annotated[
        list[str] | None,
        typer.Option(help="Per-role model, repeatable: --role-model engineer=opus"),
    ] = None,
    agent_timeout: Annotated[
        int | None,
        typer.Option(help="Seconds each agent run may take, for every role", min=60),
    ] = None,
    pr: Annotated[
        bool | None, typer.Option("--pr/--no-pr", help="Open a PR when the repo is on GitHub")
    ] = None,
) -> None:
    """Start a new task and drive it to a human gate or completion."""

    if not task_file.exists():
        raise typer.BadParameter(f"task file not found: {task_file}")
    config = _load()
    _apply_overrides(
        config,
        runtime=runtime,
        model=model,
        role_runtime=role_runtime or [],
        role_model=role_model or [],
        agent_timeout=agent_timeout,
    )
    source = repo or config.repo.url
    if not source:
        raise typer.BadParameter("no repo: pass --repo, set DEVLOOP_REPO_URL, or set repo.url")
    if pr is not None:
        config.pull_request.enabled = pr
    _preflight(config)
    _setup_logging()

    title = task_file.stem.replace("-", " ").replace("_", " ")
    task_id = uuid.uuid4().hex[:8]
    task = TaskInput(
        external_id=task_id,
        source="local",
        repo=str(Path(source).resolve()) if Path(source).exists() else source,
        base_branch=base_branch or config.repo.base_branch,
        title=title,
        description=task_file.read_text(),
    )

    graph = build_graph(checkpointer=checkpointer())
    thread = {"configurable": {"thread_id": task_id}}
    initial = new_task_state(task, config, budget_usd=budget_usd)

    team = "\n".join(
        f"  {role.value:<14} {a.runtime}" + (f" · {a.model}" if a.model else "")
        for role in Role
        for a in [config.agents.for_role(role)]
    )
    rprint(
        Panel(
            f"task [bold]{task_id}[/bold]: {title}\n"
            f"repo: {task.repo}" + (f" @ {task.base_branch}" if task.base_branch else "") + "\n"
            f"team:\n{team}"
        )
    )
    _drive(graph, initial, thread, task_id)


def _team_pairs(config: DevLoopConfig) -> dict[tuple[str, str | None], list[str]]:
    """Distinct (runtime, model) pairs on the team, with the roles using each."""

    pairs: dict[tuple[str, str | None], list[str]] = {}
    for role in Role:
        agent = config.agents.for_role(role)
        pairs.setdefault((agent.runtime, agent.model), []).append(role.value)
    return pairs


def _preflight(config: DevLoopConfig) -> None:
    """Fail before cloning anything if a model id can't possibly work."""

    problems = [
        f"{', '.join(roles)}: {problem}"
        for (runtime, model), roles in _team_pairs(config).items()
        if (problem := check_model(runtime, model))
    ]
    if problems:
        raise typer.BadParameter("\n".join(problems))


@app.command()
def probe(
    runtime: Annotated[
        str | None, typer.Option(help="Probe this runtime instead of the configured team")
    ] = None,
    model: Annotated[
        list[str] | None, typer.Option(help="Model to probe, repeatable (needs --runtime)")
    ] = None,
    timeout: Annotated[int, typer.Option(help="Seconds per probe", min=30)] = 600,
) -> None:
    """Check each runtime/model can do a DevLoop step: read the context
    file, write a JSON document with a file tool. One tiny real run each."""

    pairs: dict[tuple[str, str | None], list[str]]
    if runtime:
        models: list[str | None] = list(model) if model else [None]
        pairs = {(runtime, m): ["--model"] for m in models}
    else:
        pairs = _team_pairs(_load())
    rprint(f"probing {len(pairs)} runtime/model pair(s), in parallel …")
    with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
        futures = {
            pool.submit(run_probe, rt, m, timeout_s=timeout): roles
            for (rt, m), roles in pairs.items()
        }
        results = [(f.result(), roles) for f, roles in futures.items()]

    table = Table("runtime", "model", "used by", "result", "time", "cost $")
    for result, roles in sorted(results, key=lambda r: (not r[0].ok, r[0].seconds)):
        table.add_row(
            result.runtime,
            result.model or "(default)",
            ", ".join(roles),
            "[green]✓ works[/green]" if result.ok else f"[red]✗[/red] {result.error[:70]}",
            f"{result.seconds:.0f}s",
            f"{result.cost_usd:.4f}",
        )
    rprint(table)
    if not all(r.ok for r, _ in results):
        raise typer.Exit(1)


@app.command("config")
def show_config() -> None:
    """Print the effective configuration and where it came from."""

    path = config_path()
    rprint(f"[dim]config file: {path or '(none — defaults)'}[/dim]")
    rprint(yaml.safe_dump(_load().model_dump(mode="json"), sort_keys=False))


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
            f"pull request: {values.get('pull_request_url') or '—'}\n"
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
    table = Table("step", "iter", "try", "runtime", "prompt", "ok", "cost $", "tokens in/out", "s")
    for r in values.get("agent_runs", []):
        table.add_row(
            r.step.value + (" ↻" if r.resumed else ""),
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
