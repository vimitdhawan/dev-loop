"""A runtime that makes no model calls.

It writes the same contract documents a real agent would, through the
same files, so the whole graph — harness, validation, sessions, git,
verification, routing — runs end to end for free and deterministically.
`devloop run --runtime stub` and the graph integration tests both use it.

Outputs can be scripted per step (consumed in order, then the canned
default repeats) to drive specific paths such as a review rejection.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from devloop.contracts.runs import Step
from devloop.runtimes.base import IN_DIR, OUT_DIR, AgentInvocation, AgentOutcome

NOTES_FILE = "DEVLOOP_NOTES.md"


class StubAgent:
    name = "stub"

    def __init__(self, scripts: dict[Step, list[dict[str, Any]]] | None = None) -> None:
        self._scripts: dict[Step, list[dict[str, Any]]] = defaultdict(list)
        for step, outputs in (scripts or {}).items():
            self._scripts[step] = list(outputs)
        self.calls: list[Step] = []
        # (step, session id it resumed or None for a fresh session)
        self.sessions: list[tuple[Step, str | None]] = []

    def run(self, invocation: AgentInvocation) -> AgentOutcome:
        step = invocation.step
        self.calls.append(step)
        self.sessions.append((step, invocation.resume_session))
        ws = invocation.workdir
        context = json.loads((ws / IN_DIR / "context.json").read_text())

        if self._scripts[step]:
            output = self._scripts[step].pop(0)
        else:
            output = _DEFAULTS[step](ws, context)
        # a stub that asks, replans or disputes changes nothing, like a real one
        if step == Step.IMPLEMENT and not (
            output.get("plan_invalid")
            or output.get("disputed_findings")
            or output.get("questions_for_po")
        ):
            _touch_notes(ws, context)

        out = ws / OUT_DIR / f"{step.value}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output))
        invocation.log_path.parent.mkdir(parents=True, exist_ok=True)
        invocation.log_path.write_text(json.dumps({"stub": step.value, "output": output}))
        session = invocation.resume_session or f"stub-{uuid.uuid4().hex[:8]}"
        return AgentOutcome(ok=True, cost_usd=0.0, session_id=session)


def _touch_notes(ws: Path, context: dict[str, Any]) -> None:
    # Append, so every implementation attempt produces a real diff.
    with (ws / NOTES_FILE).open("a") as fh:
        fh.write(f"- iteration {context['iteration']}: {context['task']['title']}\n")


def _requirements(ws: Path, ctx: dict[str, Any]) -> dict[str, Any]:
    title = ctx["task"]["title"]
    return {
        "status": "ready",
        "summary": title,
        "acceptance_criteria": [f"'{title}' behaves as described"],
    }


def _po_answer(ws: Path, ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "answers": [
            {"question": q, "answer": "use the simplest option", "answered_by": "product_owner"}
            for q in ctx["questions"]
        ]
    }


def _plan(ws: Path, ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": f"Plan for: {ctx['task']['title']}",
        "files_to_change": [
            {
                "path": NOTES_FILE,
                "reason": "record what DevLoop did",
                "action": "modify" if (ws / NOTES_FILE).exists() else "create",
            }
        ],
        "implementation_steps": ["Append a note describing the completed task"],
        "tests": [{"type": "unit", "description": "existing checks stay green"}],
    }


def _implement(ws: Path, ctx: dict[str, Any]) -> dict[str, Any]:
    return {"summary": "stub implementation appends a note", "deviations": []}


def _qa(ws: Path, ctx: dict[str, Any]) -> dict[str, Any]:
    criteria = (ctx.get("requirements") or {}).get("acceptance_criteria", [])
    return {
        "verdict": "passed",
        "scenarios": [
            {"criterion": c, "steps": ["open the app"], "passed": True} for c in criteria
        ],
    }


def _review(ws: Path, ctx: dict[str, Any]) -> dict[str, Any]:
    return {"verdict": "approved", "findings": []}


_DEFAULTS = {
    Step.REQUIREMENTS: _requirements,
    Step.PO_ANSWER: _po_answer,
    Step.PLAN: _plan,
    Step.IMPLEMENT: _implement,
    Step.QA: _qa,
    Step.REVIEW: _review,
}
