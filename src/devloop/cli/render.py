"""What each agent produced, rendered for the terminal.

One renderer per state field, so the same view serves a live run (each
node's update as it lands) and `devloop show` (the whole state later):
the hand-off from one agent to the next is exactly what's printed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from rich.console import Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from devloop.contracts.artifacts import (
    Clarification,
    DesignResult,
    Finding,
    ImplementationResult,
    PlanResult,
    QAResult,
    QAVerdict,
    RequirementResult,
    RequirementStatus,
    ReviewResult,
    TestRunResult,
    Verdict,
)
from devloop.contracts.runs import Workflow
from devloop.contracts.state import DiffSummary


def _e(value: object) -> str:
    return escape(str(value))


def _bullets(items: list[str], mark: str = "•") -> list[str]:
    return [f"  {mark} {_e(item)}" for item in items]


def _panel(title: str, body: Sequence[RenderableType | str], style: str = "cyan") -> Panel:
    parts = [Text.from_markup(p) if isinstance(p, str) else p for p in body]
    return Panel(Group(*parts), title=title, title_align="left", border_style=style)


def workflow_line(workflow: Workflow) -> str:
    stages = " → ".join(r.value for r in workflow.stages)
    return f"[bold]{_e(workflow.name)}[/bold] ({_e(workflow.selected_by)}): {stages}"


def requirements(req: RequirementResult) -> Panel:
    body: list[RenderableType | str] = [_e(req.summary)]
    if req.acceptance_criteria:
        body += ["", "[bold]acceptance criteria[/bold]", *_bullets(req.acceptance_criteria)]
    if req.constraints:
        body += ["", "[bold]constraints[/bold]", *_bullets(req.constraints)]
    if req.status == RequirementStatus.NEEDS_CLARIFICATION:
        body += ["", "[bold yellow]questions for a human[/bold yellow]", *_bullets(req.questions)]
    return _panel("Product Owner → requirements", body)


def design(d: DesignResult) -> Panel:
    body: list[RenderableType | str] = [_e(d.summary)]
    if d.questions_for_po:
        body += ["", "[bold yellow]questions[/bold yellow]", *_bullets(d.questions_for_po)]
    for screen in d.screens:
        body += ["", f"[bold]{_e(screen.name)}[/bold] — {_e(screen.purpose)}"]
        body += _bullets(screen.layout, "▸")
        body += [f"  state: {_e(s)}" for s in screen.states]
        if screen.reuse:
            body.append(f"  reuse: {_e(', '.join(screen.reuse))}")
    if d.guidelines:
        body += ["", "[bold]guidelines[/bold]", *_bullets(d.guidelines)]
    if d.references:
        body += ["", "[bold]designs[/bold]"]
        body += [f"  {_e(r.tool)}: {_e(r.ref)} {_e(r.description)}" for r in d.references]
    return _panel("UX → design", body, "magenta")


def plan(p: PlanResult) -> Panel:
    body: list[RenderableType | str] = [_e(p.summary)]
    if p.questions_for_po:
        body += ["", "[bold yellow]questions[/bold yellow]", *_bullets(p.questions_for_po)]
        return _panel("Planner → questions", body, "yellow")
    files = Table("action", "file", "why", box=None, padding=(0, 1), show_header=False)
    for f in p.files_to_change:
        files.add_row(f"[dim]{f.action.value}[/dim]", _e(f.path), f"[dim]{_e(f.reason)}[/dim]")
    body += ["", "[bold]files[/bold]", files, "", "[bold]steps[/bold]"]
    body += [f"  {i}. {_e(step)}" for i, step in enumerate(p.implementation_steps, 1)]
    if p.tests:
        body += ["", "[bold]tests[/bold]"]
        body += [f"  [dim]{_e(t.type)}[/dim] {_e(t.description)}" for t in p.tests]
    if p.risks:
        body += ["", "[bold]risks[/bold]", *_bullets(p.risks)]
    return _panel("Planner → plan (handed to the Engineer)", body)


def implementation(impl: ImplementationResult, diff: DiffSummary | None = None) -> Panel:
    if impl.questions_for_po:
        return _panel(
            "Engineer → questions",
            ["[bold yellow]asks[/bold yellow]", *_bullets(impl.questions_for_po)],
            "yellow",
        )
    body: list[RenderableType | str] = [_e(impl.summary)]
    if diff is not None and diff.files_changed:
        body += [
            "",
            f"[bold]changed[/bold] [green]+{diff.insertions}[/green] [red]-{diff.deletions}[/red]",
            *_bullets(diff.files_changed),
        ]
    if impl.deviations:
        body += ["", "[bold]deviations from the plan[/bold]"]
        body += [f"  • {_e(d.step)}: {_e(d.what_changed)} ({_e(d.why)})" for d in impl.deviations]
    if impl.plan_invalid is not None:
        body += ["", f"[bold red]plan invalid[/bold red]: {_e(impl.plan_invalid.reason)}"]
    if impl.disputed_findings:
        body += ["", "[bold]disputed[/bold]"]
        body += [f"  • {_e(d.finding_id)}: {_e(d.reason)}" for d in impl.disputed_findings]
    if impl.notes_for_reviewer:
        body += ["", f"[dim]notes: {_e(impl.notes_for_reviewer)}[/dim]"]
    return _panel("Engineer → implementation", body)


def checks(runs: list[TestRunResult]) -> Panel:
    table = Table("check", "command", "result", box=None, padding=(0, 1))
    for r in runs:
        result = "[green]pass[/green]" if r.passed else "[red]fail[/red]"
        if r.failures:
            result += f" [dim]({len(r.failures)} failing)[/dim]"
        table.add_row(r.type, f"[dim]{_e(r.command)}[/dim]", result)
    phase = runs[0].phase if runs else ""
    return _panel(f"checks ({phase}, run by DevLoop)", [table], "blue")


def _finding(f: Finding) -> str:
    where = f" [dim]{_e(f.file)}{':' + str(f.line) if f.line else ''}[/dim]" if f.file else ""
    color = "red" if f.severity.value in ("P0", "P1") else "yellow"
    return (
        f"  [{color}]{f.severity.value}[/{color}] {_e(f.id)} {f.category.value}{where}: "
        f"{_e(f.description)}\n      → {_e(f.recommendation)}"
    )


def qa(result: QAResult) -> Panel:
    ok = result.verdict in (QAVerdict.PASSED, QAVerdict.SKIPPED)
    body: list[RenderableType | str] = []
    for s in result.scenarios:
        mark = "[green]✓[/green]" if s.passed else "[red]✗[/red]"
        body.append(f"  {mark} {_e(s.criterion)}")
        if s.evidence:
            body.append(f"      [dim]{_e(s.evidence)}[/dim]")
    if result.findings:
        body += ["", "[bold]bugs[/bold]", *(_finding(f) for f in result.findings)]
    if result.notes:
        body += (
            ["", f"[dim]{_e(result.notes)}[/dim]"] if body else [f"[dim]{_e(result.notes)}[/dim]"]
        )
    return _panel(f"QA → {result.verdict.value}", body or ["—"], "green" if ok else "red")


def review(result: ReviewResult) -> Panel:
    ok = result.verdict == Verdict.APPROVED
    body: list[RenderableType | str] = [_finding(f) for f in result.findings] or ["no findings"]
    return _panel(f"Reviewer → {result.verdict.value}", body, "green" if ok else "red")


def clarifications(items: list[Clarification]) -> Panel:
    body: list[RenderableType | str] = []
    for c in items:
        body += [
            f"[bold]Q[/bold] {_e(c.question)}",
            f"[bold]A[/bold] {_e(c.answer)} [dim]({_e(c.answered_by)})[/dim]",
        ]
    return _panel("answers", body, "yellow")


# --- a node's update, as it lands ------------------------------------------


def update(node: str, facts: dict[str, Any], cost_usd: float) -> list[RenderableType]:
    """Status line plus a panel for every artifact this node produced."""

    out: list[RenderableType] = [
        Text.from_markup(
            f"[dim]{node} →[/dim] [bold cyan]{facts.get('status')}[/bold cyan] "
            f"[dim]${cost_usd:.4f}[/dim]"
        )
    ]
    renderers: list[tuple[str, Callable[[Any], RenderableType | None]]] = [
        ("requirements", lambda v: requirements(v) if v else None),
        ("clarifications", lambda v: clarifications(v) if v else None),
        ("design", lambda v: design(v) if v else None),
        ("plan", lambda v: plan(v) if v else None),
        ("implementation", lambda v: implementation(v, facts.get("diff")) if v else None),
        ("verification", lambda v: checks(v) if v else None),
        ("qa", lambda v: qa(v) if v else None),
        ("review", lambda v: review(v) if v else None),
    ]
    for key, render in renderers:
        if key in facts and (panel := render(facts[key])) is not None:
            out.append(panel)
    if facts.get("pending_questions"):
        out.append(_panel("questions", _bullets(facts["pending_questions"]), "yellow"))
    if facts.get("pull_request_url"):
        out.append(Text.from_markup(f"[bold green]PR:[/bold green] {facts['pull_request_url']}"))
    if facts.get("escalation_reason"):
        out.append(
            Text.from_markup(f"[bold red]escalation:[/bold red] {_e(facts['escalation_reason'])}")
        )
    return out


def state(values: dict[str, Any]) -> list[RenderableType]:
    """Every hand-off in a task, in the order the team produced them."""

    out: list[RenderableType] = []
    if values.get("requirements"):
        out.append(requirements(values["requirements"]))
    if values.get("clarifications"):
        out.append(clarifications(values["clarifications"]))
    if values.get("design"):
        out.append(design(values["design"]))
    if values.get("plan"):
        out.append(plan(values["plan"]))
    if values.get("implementation"):
        out.append(implementation(values["implementation"], values.get("diff")))
    latest = [
        r
        for r in values.get("verification", [])
        if r.iteration == max(x.iteration for x in values["verification"])
    ]
    if latest:
        out.append(checks(latest))
    if values.get("qa"):
        out.append(qa(values["qa"]))
    if values.get("review"):
        out.append(review(values["review"]))
    return out
