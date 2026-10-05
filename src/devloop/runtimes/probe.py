"""Can this runtime + model actually do a DevLoop step?

A probe is one tiny, real agent run through the same path every step uses:
read `.devloop/in/context.json`, write a JSON document to `.devloop/out/`
with a file tool, nothing else. A model that can't do this — wrong id, no
tool calling, ignores instructions, too slow — will fail every real step
too, so finding out costs one probe instead of a task's budget.
"""

from __future__ import annotations

import json
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from devloop.contracts.runs import Role, Step
from devloop.errors import AgentError
from devloop.paths import devloop_home
from devloop.runtimes.base import IN_DIR, OUT_DIR, AgentInvocation, ToolPolicy
from devloop.runtimes.registry import get_runtime

_PROMPT = f"""Read the file `{IN_DIR}/context.json`. It contains a secret word.
Then use your file-writing tool to create `{OUT_DIR}/probe.json` containing
exactly this JSON, with the secret word filled in:

{{"word": "<the secret word>"}}

Do not run shell commands. Do not modify any other file. Reply "done" when finished."""


@dataclass
class ProbeResult:
    runtime: str
    model: str | None
    ok: bool
    seconds: float
    cost_usd: float = 0.0
    error: str = ""


def check_model(runtime: str, model: str | None) -> str | None:
    """Cheap static check (no model call) where the runtime supports one."""

    if model is None:
        return None
    checker: Callable[[str], str | None] | None = getattr(get_runtime(runtime), "check_model", None)
    return checker(model) if checker else None


def probe(runtime: str, model: str | None, *, timeout_s: int = 600) -> ProbeResult:
    problem = check_model(runtime, model)
    if problem:
        return ProbeResult(runtime, model, ok=False, seconds=0.0, error=problem)

    root = devloop_home() / "probes"
    root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="probe-", dir=root))
    word = secrets.token_hex(4)
    try:
        (workdir / IN_DIR).mkdir(parents=True)
        (workdir / OUT_DIR).mkdir(parents=True)
        (workdir / IN_DIR / "context.json").write_text(json.dumps({"secret_word": word}))
        invocation = AgentInvocation(
            role=Role.ENGINEER,
            step=Step.IMPLEMENT,
            prompt=_PROMPT,
            workdir=workdir,
            tools=ToolPolicy(can_edit=False),
            log_path=root / f"{workdir.name}.log",
            model=model,
            max_budget_usd=0.50,
            timeout_s=timeout_s,
        )
        started = time.monotonic()
        try:
            outcome = get_runtime(runtime).run(invocation)
        except AgentError as exc:
            return ProbeResult(runtime, model, ok=False, seconds=0.0, error=str(exc))
        seconds = time.monotonic() - started
        result = ProbeResult(runtime, model, ok=False, seconds=seconds, cost_usd=outcome.cost_usd)
        if not outcome.ok:
            result.error = outcome.error
            return result

        out = workdir / OUT_DIR / "probe.json"
        if not out.exists():
            result.error = "ran, but wrote no output file (no tool calling, or ignored the task)"
            return result
        try:
            got = json.loads(out.read_text()).get("word")
        except (json.JSONDecodeError, AttributeError):
            result.error = "wrote an output file that isn't the requested JSON object"
            return result
        if got != word:
            result.error = f"wrote {got!r}, not the word from the context file"
            return result
        result.ok = True
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
