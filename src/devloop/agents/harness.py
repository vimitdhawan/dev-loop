"""Runs one role, end to end, identically for every runtime.

1. Write the `DevelopmentContext` to `.devloop/in/context.json`.
2. Remove any stale `.devloop/out/<role>.json`.
3. Run the agent CLI with the role's tool policy.
4. Read and validate the output document (Pydantic, then an optional
   deterministic `check`, e.g. the plan check).
5. On a validation problem, retry **exactly once**, feeding the problems
   back. Then give up.

A crash, timeout or a read-only role touching the workspace is not
repaired — those aren't output-format problems, and retrying them just
spends money. Every attempt, failed or not, becomes an `AgentRunRecord`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from devloop.agents.prompts import load_prompt
from devloop.contracts.context import DevelopmentContext
from devloop.contracts.runs import AgentRunRecord, Role
from devloop.errors import AgentError
from devloop.paths import task_dir
from devloop.runtimes.base import IN_DIR, OUT_DIR, AgentInvocation, AgentOutcome, ToolPolicy
from devloop.runtimes.registry import get_runtime
from devloop.sandbox.workspace import discard_changes, is_dirty

T = TypeVar("T", bound=BaseModel)

log = logging.getLogger("devloop.agent")

MAX_ATTEMPTS = 2  # first try + one repair retry


@dataclass(frozen=True)
class RoleSpec[T: BaseModel]:
    role: Role
    contract: type[T]
    tools: ToolPolicy
    # Read-only roles must leave the working tree exactly as they found it.
    read_only: bool = True
    timeout_s: int = 600


@dataclass
class RoleRun[T: BaseModel]:
    artifact: T | None = None
    error: str | None = None
    records: list[AgentRunRecord] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.records)


def run_role[T: BaseModel](
    spec: RoleSpec[T],
    context: DevelopmentContext,
    *,
    workspace: Path,
    runtime: str,
    model: str | None,
    budget_left_usd: float | None,
    check: Callable[[T], list[str]] | None = None,
    timeout_s: int | None = None,
) -> RoleRun[T]:
    task_id = context.task.external_id
    in_dir, out_dir = workspace / IN_DIR, workspace / OUT_DIR
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (in_dir / "context.json").write_text(context.model_dump_json(indent=2))
    out_file = out_dir / f"{spec.role.value}.json"

    prompt = load_prompt(spec.role)
    base_text = prompt.render(
        context_path=f"{IN_DIR}/context.json",
        out_path=f"{OUT_DIR}/{spec.role.value}.json",
        schema=json.dumps(spec.contract.model_json_schema(), indent=2),
    )
    agent = get_runtime(runtime)
    result: RoleRun[T] = RoleRun()
    text = base_text

    for attempt in range(1, MAX_ATTEMPTS + 1):
        out_file.unlink(missing_ok=True)
        log_path = (
            task_dir(task_id) / "logs" / f"{spec.role.value}-i{context.iteration}-a{attempt}.log"
        )
        budget = None if budget_left_usd is None else max(budget_left_usd - result.cost_usd, 0.0)
        invocation = AgentInvocation(
            role=spec.role,
            prompt=text,
            workdir=workspace,
            tools=spec.tools,
            log_path=log_path,
            model=model,
            max_budget_usd=budget,
            timeout_s=timeout_s or spec.timeout_s,
        )
        log.info(
            "%s agent (%s, attempt %d) — log: %s", spec.role.value, agent.name, attempt, log_path
        )
        try:
            outcome = agent.run(invocation)
        except AgentError as exc:
            outcome = AgentOutcome(ok=False, error=str(exc))

        problems: list[str] = []
        artifact: T | None = None
        fatal = not outcome.ok
        if not fatal and spec.read_only and is_dirty(workspace):
            discard_changes(workspace)
            outcome.error = f"{spec.role.value} is read-only but modified the workspace"
            fatal = True
        if not fatal:
            artifact, problems = _validate(out_file, spec.contract, check)

        error = outcome.error if fatal else "; ".join(problems)
        record = AgentRunRecord(
            task_id=task_id,
            role=spec.role,
            runtime=agent.name,
            model=model,
            prompt_version=prompt.version,
            iteration=context.iteration,
            attempt=attempt,
            ok=not error,
            error=error,
            cost_usd=outcome.cost_usd,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            duration_ms=outcome.duration_ms,
            session_id=outcome.session_id,
            log_path=str(log_path),
        )
        result.records.append(record)
        _append_jsonl(task_dir(task_id) / "agent_runs.jsonl", record)

        if fatal:
            result.error = f"{spec.role.value} agent failed: {outcome.error}"
            return result
        if not problems:
            result.artifact = artifact
            return result
        text = _repair_prompt(base_text, f"{OUT_DIR}/{spec.role.value}.json", problems)

    result.error = f"{spec.role.value} output still invalid after repair: {'; '.join(problems)}"
    return result


def _validate[T: BaseModel](
    out_file: Path, contract: type[T], check: Callable[[T], list[str]] | None
) -> tuple[T | None, list[str]]:
    if not out_file.exists():
        return None, [f"no output document was written to {OUT_DIR}/{out_file.name}"]
    try:
        artifact = contract.model_validate_json(out_file.read_text())
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        ]
    problems = check(artifact) if check else []
    return (None, problems) if problems else (artifact, [])


def _repair_prompt(base_text: str, out_path: str, problems: list[str]) -> str:
    bullet_list = "\n".join(f"- {p}" for p in problems)
    return (
        f"{base_text}\n\n"
        "## Repair required\n\n"
        f"A previous attempt at this task wrote `{out_path}`, but it was rejected:\n\n"
        f"{bullet_list}\n\n"
        "Any work already in the workspace is still there — do not redo it. "
        f"Fix the problems above and write the corrected document to `{out_path}`."
    )


def _append_jsonl(path: Path, record: AgentRunRecord) -> None:
    with path.open("a") as fh:
        fh.write(record.model_dump_json() + "\n")
