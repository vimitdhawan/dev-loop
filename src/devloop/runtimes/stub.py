"""A runtime that makes no model calls.

It writes the same contract documents a real agent would, through the
same files, so the whole graph — harness, validation, git, verification,
routing — runs end to end for free and deterministically. This is the
Phase-0 walking skeleton promoted to a runtime: `devloop run --runtime
stub` and the graph integration tests both use it.

Outputs can be scripted per role (consumed in order, then the canned
default repeats) to drive specific paths such as a review rejection.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from devloop.contracts.runs import Role
from devloop.runtimes.base import IN_DIR, OUT_DIR, AgentInvocation, AgentOutcome

NOTES_FILE = "DEVLOOP_NOTES.md"


class StubAgent:
    name = "stub"

    def __init__(self, scripts: dict[Role, list[dict[str, Any]]] | None = None) -> None:
        self._scripts: dict[Role, list[dict[str, Any]]] = defaultdict(list)
        for role, outputs in (scripts or {}).items():
            self._scripts[role] = list(outputs)
        self.calls: list[Role] = []

    def run(self, invocation: AgentInvocation) -> AgentOutcome:
        role = invocation.role
        self.calls.append(role)
        ws = invocation.workdir
        context = json.loads((ws / IN_DIR / "context.json").read_text())

        if self._scripts[role]:
            output = self._scripts[role].pop(0)
        else:
            output = _DEFAULTS[role](ws, context)
        # a stub that replans or disputes changes nothing, like a real one would
        if role == Role.DEVELOPER and not (
            output.get("plan_invalid") or output.get("disputed_findings")
        ):
            _touch_notes(ws, context)

        out = ws / OUT_DIR / f"{role.value}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(output))
        invocation.log_path.parent.mkdir(parents=True, exist_ok=True)
        invocation.log_path.write_text(json.dumps({"stub": role.value, "output": output}))
        return AgentOutcome(ok=True, cost_usd=0.0, session_id="stub")


def _touch_notes(ws: Path, context: dict[str, Any]) -> None:
    # Append, so every implementation attempt produces a real diff.
    with (ws / NOTES_FILE).open("a") as fh:
        fh.write(f"- iteration {context['iteration']}: {context['task']['title']}\n")


def _requirement(ws: Path, ctx: dict[str, Any]) -> dict[str, Any]:
    title = ctx["task"]["title"]
    return {
        "status": "ready",
        "summary": title,
        "acceptance_criteria": [f"'{title}' behaves as described"],
    }


def _planner(ws: Path, ctx: dict[str, Any]) -> dict[str, Any]:
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


def _developer(ws: Path, ctx: dict[str, Any]) -> dict[str, Any]:
    return {"summary": "stub implementation appends a note", "deviations": []}


def _reviewer(ws: Path, ctx: dict[str, Any]) -> dict[str, Any]:
    return {"verdict": "approved", "findings": []}


_DEFAULTS = {
    Role.REQUIREMENT: _requirement,
    Role.PLANNER: _planner,
    Role.DEVELOPER: _developer,
    Role.REVIEWER: _reviewer,
}
