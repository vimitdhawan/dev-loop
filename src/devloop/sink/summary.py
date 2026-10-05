"""The change's review surface: requirements, plan, deviations and
evidence, rendered as markdown. Used as the PR body and written to
`~/.devloop/tasks/<id>/summary.md` — the plan is what a human reviews,
not just the diff."""

from __future__ import annotations

from devloop.contracts.artifacts import Finding, QAVerdict
from devloop.contracts.state import DevLoopState
from devloop.graph.routing import latest_verification


def render_summary(state: DevLoopState) -> str:
    task = state["task"]
    req = state.get("requirements")
    plan = state.get("plan")
    diff = state.get("diff")
    qa = state.get("qa")
    review = state.get("review")
    out: list[str] = []

    out.append(f"## {task.title}\n")
    if req is not None:
        out.append(f"{req.summary}\n")
        out.append("### Acceptance criteria\n")
        out += [f"- {c}" for c in req.acceptance_criteria]
        out.append("")
        if req.constraints:
            out.append("### Constraints\n")
            out += [f"- {c}" for c in req.constraints]
            out.append("")

    clarifications = state.get("clarifications", [])
    if clarifications:
        out.append("### Clarifications\n")
        out += [f"- **{c.question}** — {c.answer} _({c.answered_by})_" for c in clarifications]
        out.append("")

    if plan is not None:
        out.append("### Plan\n")
        out.append(f"{plan.summary}\n")
        out += [f"{i}. {step}" for i, step in enumerate(plan.implementation_steps, 1)]
        out.append("")
        if plan.risks:
            out.append("**Risks:** " + "; ".join(plan.risks) + "\n")

    if diff is not None and diff.deviations:
        out.append("### Deviations from the plan\n")
        out += [f"- **{d.step}** — {d.what_changed} (why: {d.why})" for d in diff.deviations]
        out.append("")

    runs = latest_verification(state)
    if runs:
        out.append("### Checks (run by DevLoop, not the agent)\n")
        out.append("| check | command | result |")
        out.append("|---|---|---|")
        out += [
            f"| {r.type} | `{r.command}` | {'✅ pass' if r.passed else '❌ fail'} |" for r in runs
        ]
        out.append("")

    if qa is not None:
        out.append("### QA\n")
        if qa.verdict == QAVerdict.SKIPPED:
            out.append("_Skipped — the repo's `devloop.yml` has no `app:` section._\n")
        else:
            out += [
                f"- {'✅' if s.passed else '❌'} {s.criterion}"
                + (f" — {s.evidence}" if s.evidence else "")
                for s in qa.scenarios
            ]
            out.append("")

    if review is not None:
        out.append(f"### Review: {review.verdict.value}\n")
        remaining = [f for f in review.findings if not f.resolved]
        if remaining:
            out.append("Non-blocking notes:\n")
            out += [_finding_line(f) for f in remaining]
            out.append("")

    runs_by_role: dict[str, float] = {}
    for record in state.get("agent_runs", []):
        runs_by_role[record.role.value] = runs_by_role.get(record.role.value, 0.0) + record.cost_usd
    out.append("---")
    out.append(
        f"🤖 DevLoop task `{task.external_id}` · {state.get('iteration', 0)} implementation "
        f"attempt(s) · {state.get('replan_count', 0)} replan(s) · "
        f"${state.get('cost_usd', 0.0):.2f} "
        f"({', '.join(f'{k} ${v:.2f}' for k, v in runs_by_role.items())})"
    )
    return "\n".join(out) + "\n"


def _finding_line(f: Finding) -> str:
    where = f" `{f.file}{':' + str(f.line) if f.line else ''}`" if f.file else ""
    return f"- **{f.severity.value}** {f.category.value}{where}: {f.description}"
