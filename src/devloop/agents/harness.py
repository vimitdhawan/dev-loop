"""Runs one step of one role, end to end, identically for every runtime.

1. Write the `DevelopmentContext` to `.devloop/in/context.json`.
2. Remove any stale `.devloop/out/<step>.json`.
3. Run the agent CLI with the step's tool policy — resuming the role's
   session when one is given, so the agent keeps what it already learned.
4. Read and validate the output document (Pydantic, then an optional
   deterministic `check`, e.g. the plan check).
5. On a validation problem, retry **exactly once** in the same session,
   feeding the problems back. Then give up.

A crash, timeout or a read-only step touching the workspace is not
repaired — those aren't output-format problems, and retrying them just
spends money. Every attempt, failed or not, becomes an `AgentRunRecord`.

The context file is complete on every call, so a step works the same
whether its session was resumed or lost: a session is an optimisation,
never the source of truth.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError

from devloop.agents.prompts import load_prompt
from devloop.contracts.context import DevelopmentContext
from devloop.contracts.runs import AgentConfig, AgentRunRecord, Role, Step
from devloop.errors import AgentError
from devloop.paths import task_dir
from devloop.runtimes.base import IN_DIR, OUT_DIR, AgentInvocation, AgentOutcome, ToolPolicy
from devloop.runtimes.registry import get_runtime
from devloop.sandbox.workspace import discard_changes, is_dirty

log = logging.getLogger("devloop.agent")

MAX_ATTEMPTS = 2  # first try + one repair retry

_RESUMED_PREFIX = (
    "You are continuing the same task in this conversation. The context file "
    "has been rewritten with the latest state — read it again before acting.\n\n"
)


@dataclass(frozen=True)
class StepSpec[T: BaseModel]:
    role: Role
    step: Step
    contract: type[T]
    tools: ToolPolicy
    # Read-only steps must leave the working tree exactly as they found it.
    read_only: bool = True
    timeout_s: int = 600


@dataclass
class StepRun[T: BaseModel]:
    artifact: T | None = None
    error: str | None = None
    records: list[AgentRunRecord] = field(default_factory=list)
    # The session to resume next time this role is called.
    session_id: str | None = None

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.records)


def run_step[T: BaseModel](
    spec: StepSpec[T],
    context: DevelopmentContext,
    *,
    workspace: Path,
    agent_config: AgentConfig,
    budget_left_usd: float | None,
    session_id: str | None = None,
    check: Callable[[T], list[str]] | None = None,
) -> StepRun[T]:
    task_id = context.task.external_id
    in_dir, out_dir = workspace / IN_DIR, workspace / OUT_DIR
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (in_dir / "context.json").write_text(context.model_dump_json(indent=2))
    out_rel = f"{OUT_DIR}/{spec.step.value}.json"
    out_file = workspace / out_rel

    prompt = load_prompt(spec.step)
    base_text = prompt.render(
        context_path=f"{IN_DIR}/context.json",
        out_path=out_rel,
        schema=json.dumps(spec.contract.model_json_schema(), indent=2),
    )
    agent = get_runtime(agent_config.runtime)
    result: StepRun[T] = StepRun(session_id=session_id)
    text = _RESUMED_PREFIX + base_text if session_id else base_text
    problems: list[str] = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        out_file.unlink(missing_ok=True)
        log_path = (
            task_dir(task_id) / "logs" / f"{spec.step.value}-i{context.iteration}-a{attempt}.log"
        )
        budget = None if budget_left_usd is None else max(budget_left_usd - result.cost_usd, 0.0)
        invocation = AgentInvocation(
            role=spec.role,
            step=spec.step,
            prompt=text,
            workdir=workspace,
            tools=spec.tools,
            log_path=log_path,
            model=agent_config.model,
            max_budget_usd=budget,
            timeout_s=agent_config.timeout_s or spec.timeout_s,
            resume_session=result.session_id,
        )
        log.info(
            "%s/%s (%s, attempt %d%s) — log: %s",
            spec.role.value,
            spec.step.value,
            agent.name,
            attempt,
            ", resumed session" if invocation.resume_session else "",
            log_path,
        )
        started = time.monotonic()
        try:
            outcome = agent.run(invocation)
        except AgentError as exc:
            outcome = AgentOutcome(ok=False, error=str(exc))
        if not outcome.duration_ms:
            outcome.duration_ms = int((time.monotonic() - started) * 1000)

        problems = []
        artifact: T | None = None
        fatal = not outcome.ok
        if not fatal and spec.read_only and is_dirty(workspace):
            discard_changes(workspace)
            outcome.error = f"{spec.step.value} is read-only but modified the workspace"
            fatal = True
        if not fatal:
            artifact, problems = _validate(out_file, spec.contract, check)

        error = outcome.error if fatal else "; ".join(problems)
        record = AgentRunRecord(
            task_id=task_id,
            role=spec.role,
            step=spec.step,
            runtime=agent.name,
            model=agent_config.model,
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
            resumed=invocation.resume_session is not None,
            log_path=str(log_path),
        )
        result.records.append(record)
        _append_jsonl(task_dir(task_id) / "agent_runs.jsonl", record)
        result.session_id = outcome.session_id or result.session_id

        if fatal:
            result.error = f"{spec.step.value} agent failed: {outcome.error}"
            return result
        if not problems:
            result.artifact = artifact
            return result
        # In a live session the agent still has the full prompt; repeating
        # it would only cost tokens. Without one, it needs everything again.
        text = _repair_prompt("" if result.session_id else base_text, out_rel, problems)

    result.error = f"{spec.step.value} output still invalid after repair: {'; '.join(problems)}"
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
    head = f"{base_text}\n\n" if base_text else ""
    return (
        f"{head}## Repair required\n\n"
        f"Your output document `{out_path}` was rejected:\n\n"
        f"{bullet_list}\n\n"
        "Any work already in the workspace is still there — do not redo it. "
        f"Fix the problems above and write the corrected document to `{out_path}`."
    )


def _append_jsonl(path: Path, record: AgentRunRecord) -> None:
    with path.open("a") as fh:
        fh.write(record.model_dump_json() + "\n")
