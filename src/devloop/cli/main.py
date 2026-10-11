"""`devloop` CLI.

`devloop run` drives a task from a markdown file through its workflow's
agents to a pushed `devloop/<task-id>-<slug>` branch — and a PR when the
repo is on GitHub — printing what each agent hands the next as it lands.
`devloop watch` does the same for labelled GitHub issues, continuously.
Human gates (`clarify`, `finalize`, `escalate`) are answered with
`devloop resume`, via LangGraph's `interrupt()`/`Command(resume=...)`.

Who is on the team, and which workflow each kind of task gets, comes from
`devloop.config.yaml` (see `devloop.config.example.yaml`); flags override
it for one run.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from langgraph.types import Command
from rich import print as rprint
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from devloop.cli import render
from devloop.config import ConfigError, DevLoopConfig, config_path, load_config
from devloop.contracts.runs import Role
from devloop.contracts.state import TaskInput
from devloop.errors import DevLoopError
from devloop.graph.build import build_graph
from devloop.graph.execute import Outcome, drive
from devloop.graph.inputs import new_task_state
from devloop.paths import task_dir, workspace_dir
from devloop.runtimes.base import env_refs
from devloop.runtimes.probe import check_model
from devloop.runtimes.probe import probe as run_probe
from devloop.runtimes.registry import known_runtimes
from devloop.store import records
from devloop.store.checkpointer import checkpointer

app = typer.Typer(no_args_is_help=True)
console = Console()

# The state fields `show --json` can dump.
_FIELDS = (
    "task workflow requirements clarifications design plan implementation diff "
    "verification qa review feedback team agent_runs"
).split()


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
        # an explicit per-role override detaches the role from its fallback
        # (planner → engineer, ux → product owner); otherwise it follows it
        if role in runtimes or role in models:
            config.agents.own(role)
        if getattr(config.agents, role.value) is None:
            continue
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
    workflow: Annotated[
        str | None,
        typer.Option(help="Workflow to run: bug | feature | ui_feature | refactor | <yours>"),
    ] = None,
    label: Annotated[
        list[str] | None,
        typer.Option(help="Task label, repeatable; picks the workflow like an issue label"),
    ] = None,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Only print status changes, not artifacts")
    ] = False,
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
        workflow=workflow,
        labels=label or [],
    )

    try:
        initial = new_task_state(task, config, budget_usd=budget_usd)
    except DevLoopError as exc:
        raise typer.BadParameter(str(exc)) from exc
    chosen = initial.get("workflow")
    assert chosen is not None
    team = "\n".join(
        f"  {role.value:<14} {a.runtime}" + (f" · {a.model}" if a.model else "")
        for role in chosen.stages
        for a in [config.agents.for_role(role)]
    )
    rprint(
        Panel(
            f"task [bold]{task_id}[/bold]: {title}\n"
            f"repo: {task.repo}" + (f" @ {task.base_branch}" if task.base_branch else "") + "\n"
            f"workflow: {render.workflow_line(chosen)}\n"
            f"team:\n{team}"
        )
    )
    graph = build_graph(checkpointer=checkpointer())
    _finish(drive(graph, initial, task_id, _printer(quiet)))


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
    for role in Role:
        for name, server in config.agents.for_role(role).mcp_servers.items():
            missing = [ref for ref in env_refs(server) if ref not in os.environ]
            if missing:
                problems.append(f"{role.value}: MCP server {name!r} needs {', '.join(missing)}")
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


@app.command()
def watch(
    repo: Annotated[
        str | None, typer.Option(help="owner/name to watch (else github.repo / repo.url)")
    ] = None,
    once: Annotated[
        bool, typer.Option(help="One poll: start what's eligible, wait for it, exit (cron)")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option(help="List eligible issues in pick order, with their workflow")
    ] = False,
    interval: Annotated[
        int | None, typer.Option(help="Seconds between polls (config: poll_interval_s)", min=30)
    ] = None,
    max_concurrent: Annotated[
        int | None, typer.Option(help="Tasks at once (config: github.max_concurrent)", min=1)
    ] = None,
) -> None:
    """Run DevLoop on GitHub issues: poll for open issues with the configured
    labels, run each through its workflow, and label/comment the outcome."""

    from devloop.automation.watcher import Watcher
    from devloop.sandbox.workspace import github_slug
    from devloop.sources.github import to_task
    from devloop.workflows import select_workflow

    config = _load()
    gh_config = config.github
    if interval:
        gh_config.poll_interval_s = interval
    if max_concurrent:
        gh_config.max_concurrent = max_concurrent
    slug = repo or gh_config.repo or (github_slug(config.repo.url) if config.repo.url else None)
    if not slug:
        raise typer.BadParameter("no repo to watch: pass --repo, or set github.repo or repo.url")
    source = config.repo.url if config.repo.url and github_slug(config.repo.url) == slug else None
    source = source or f"https://github.com/{slug}.git"

    _setup_logging()
    graph = build_graph(checkpointer=checkpointer())

    def run_task(task: TaskInput) -> Outcome:
        def on_update(node: str, facts: dict[str, Any]) -> None:
            logging.getLogger("devloop.watch").info(
                "[%s] %s → %s", task.external_id, node, facts.get("status")
            )

        return drive(graph, new_task_state(task, config), task.external_id, on_update)

    watcher = Watcher(config, repo=slug, repo_source=source, run_task=run_task)
    if dry_run:
        table = Table("#", "issue", "labels", "workflow", title=f"eligible in {slug}, pick order")
        for i, issue in enumerate(watcher.candidates(), 1):
            task = to_task(issue, task_id="-", repo_source=source, base_branch=None)
            chosen = select_workflow(task, config.workflows, config.default_workflow)
            table.add_row(
                str(i),
                f"#{issue.number} {issue.title}",
                ", ".join(issue.labels),
                f"{chosen.name} ({chosen.selected_by})",
            )
        rprint(table)
        return

    _preflight(config)
    rprint(
        Panel(
            f"watching [bold]{slug}[/bold] for open issues labelled "
            f"{', '.join(gh_config.labels) or '(any)'}\n"
            f"every {gh_config.poll_interval_s}s, up to {gh_config.max_concurrent} at a time; "
            f"clone: {source}"
        )
    )
    watcher.setup()
    if once:
        started = watcher.poll()
        rprint(
            f"started {len(started)} issue(s)"
            + (f": {', '.join(i.ref for i in started)}" if started else "")
        )
        watcher.wait()
        return
    try:
        watcher.run_forever()
    except KeyboardInterrupt:
        rprint(
            f"stopping — waiting for {len(watcher.running)} running task(s); Ctrl-C again aborts"
        )
        watcher.stop()
        watcher.wait()


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
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Only print status changes, not artifacts")
    ] = False,
) -> None:
    """Resume a task paused at a human gate."""

    _setup_logging()
    graph = build_graph(checkpointer=checkpointer())
    if not graph.get_state({"configurable": {"thread_id": task_id}}).values:
        hint = (
            " (it ran in LangGraph Studio: answer it there)" if records.load_state(task_id) else ""
        )
        raise typer.BadParameter(f"no task with id {task_id}{hint}")
    payload: object = {"answer": answer} if answer else "approve"
    outcome = drive(graph, Command(resume=payload), task_id, _printer(quiet))
    _finish(outcome)
    _report_to_issue(outcome)


def _report_to_issue(outcome: Outcome) -> None:
    """A task from `devloop watch` tells its issue where it ended up, however
    it was resumed."""

    from devloop.automation.watcher import report
    from devloop.sources.github import parse_issue_url

    task = outcome.values.get("task")
    issue = parse_issue_url(task.url) if task is not None and task.source == "github" else None
    if issue is not None:
        report(_load().github, issue[0], issue[1], outcome)


def _task_values(task_id: str) -> tuple[dict[str, Any], str]:
    """The task's state from the CLI's checkpoints, else the record the
    graph wrote — which is all there is for a task run in Studio."""

    values = (
        build_graph(checkpointer=checkpointer())
        .get_state({"configurable": {"thread_id": task_id}})
        .values
    )
    if values:
        return dict(values), "checkpoint"
    recorded = records.load_state(task_id)
    if recorded:
        return dict(recorded), "record (tasks/<id>/state.json)"
    raise typer.BadParameter(f"no task with id {task_id}")


@app.command()
def show(
    task_id: Annotated[str, typer.Argument(help="Task id printed by `devloop run`")],
    run: Annotated[
        int | None,
        typer.Option("--run", help="Print agent run #N's exact input (context) and output"),
    ] = None,
    field: Annotated[
        str | None, typer.Option("--json", help=f"Dump one state field: {', '.join(_FIELDS)}")
    ] = None,
    events: Annotated[bool, typer.Option(help="Print the node-by-node timeline")] = False,
    artifacts: Annotated[
        bool, typer.Option(help="Print what each agent produced (--no-artifacts: just runs)")
    ] = True,
) -> None:
    """Everything a task's agents produced and handed on, every agent run,
    and its status — for tasks run from the CLI, `watch` or Studio."""

    values, source = _task_values(task_id)
    if field is not None:
        _dump_field(values, field)
        return
    if run is not None:
        _show_run(values, run)
        return

    workflow = values.get("workflow")
    header = [
        f"status: [bold]{values.get('status')}[/bold]",
        f"workflow: {render.workflow_line(workflow)}" if workflow else "workflow: feature (legacy)",
        f"pull request: {values.get('pull_request_url') or '—'}",
        f"branch: {values.get('branch')}  base: {(values.get('base_commit') or '')[:10]}",
        f"iteration: {values.get('iteration')}  replans: {values.get('replan_count')}",
        f"cost: ${values.get('cost_usd', 0.0):.4f} of ${values.get('budget_usd') or 0:.2f}",
        f"workspace: {workspace_dir(task_id)}",
        f"records: {task_dir(task_id)}  [dim](state from {source})[/dim]",
    ]
    if values.get("escalation_reason"):
        header.append(f"[red]escalation:[/red] {values['escalation_reason']}")
    rprint(Panel("\n".join(header), title=f"task {task_id}"))

    if artifacts:
        for panel in render.state(values):
            console.print(panel)
    if events:
        _print_events(task_id)

    table = Table(
        "#", "step", "iter", "try", "runtime", "model", "prompt", "ok", "cost $", "tok in/out", "s"
    )
    for i, r in enumerate(values.get("agent_runs", []), 1):
        table.add_row(
            str(i),
            r.step.value + (" ↻" if r.resumed else ""),
            str(r.iteration),
            str(r.attempt),
            r.runtime,
            r.model or "—",
            r.prompt_version,
            "✓" if r.ok else f"✗ {r.error[:40]}",
            f"{r.cost_usd:.4f}",
            f"{r.input_tokens}/{r.output_tokens}",
            f"{r.duration_ms / 1000:.0f}",
        )
    rprint(table)
    rprint(f"[dim]exact input/output of a run: devloop show {task_id} --run <#>[/dim]")


def _dump_field(values: dict[str, Any], field: str) -> None:
    if field not in _FIELDS:
        raise typer.BadParameter(f"--json takes one of: {', '.join(_FIELDS)}")
    value = values.get(field)

    def plain(v: Any) -> Any:
        if hasattr(v, "model_dump"):
            return v.model_dump(mode="json")
        if isinstance(v, list):
            return [plain(x) for x in v]
        return v

    typer.echo(json.dumps(plain(value), indent=2))


def _show_run(values: dict[str, Any], number: int) -> None:
    runs = values.get("agent_runs", [])
    if not 1 <= number <= len(runs):
        raise typer.BadParameter(f"--run takes 1..{len(runs)}")
    record = runs[number - 1]
    rprint(
        Panel(
            f"{record.role.value} / {record.step.value}  iteration {record.iteration}, "
            f"attempt {record.attempt}  {record.runtime} · {record.model or 'default model'}\n"
            f"prompt {record.prompt_version}  ok: {record.ok}  {record.error}\n"
            f"log: {record.log_path}",
            title=f"run #{number}",
        )
    )
    if not record.run_dir:
        rprint("[yellow]this run predates per-run records; only its log exists[/yellow]")
        return
    run_dir = Path(record.run_dir)
    for name, title in (("context.json", "input"), ("output.json", "output")):
        path = run_dir / name
        if path.exists():
            console.print(Panel(Syntax(path.read_text(), "json", word_wrap=True), title=title))
        else:
            rprint(f"[yellow]{title}: no {name} (the agent wrote none)[/yellow]")
    rprint(f"[dim]prompt: {run_dir / 'prompt.md'}[/dim]")


def _print_events(task_id: str) -> None:
    table = Table("time", "node", "→ status", "produced", "cost $", title="timeline")
    for e in records.load_events(task_id):
        table.add_row(
            e["ts"][11:19], e["node"], e["status"], ", ".join(e["produced"]), f"{e['cost_usd']:.4f}"
        )
    rprint(table)


# --- driving a task --------------------------------------------------------


def _printer(quiet: bool) -> Any:
    cost = 0.0

    def on_update(node: str, facts: dict[str, Any]) -> None:
        nonlocal cost
        cost = facts.get("cost_usd", cost)
        if quiet:
            rprint(f"[bold cyan]-> {facts.get('status')}[/bold cyan]  [dim]${cost:.4f}[/dim]")
            return
        for item in render.update(node, facts, cost):
            console.print(item)

    return on_update


def _finish(outcome: Outcome) -> None:
    if outcome.waiting:
        for gate in outcome.waiting:
            rprint(Panel(_format_interrupt(gate), title="waiting on a human"))
        rprint(f"resume with: [bold]devloop resume {outcome.task_id} --answer '...'[/bold]")
    else:
        rprint(Panel(f"[bold green]done[/bold green]: {outcome.status}"))
    rprint(f"[dim]inspect: devloop show {outcome.task_id}[/dim]")


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
