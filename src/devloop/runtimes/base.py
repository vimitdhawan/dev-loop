"""The `CodingAgent` seam.

An adapter's whole job: run one agent CLI non-interactively in a workspace
with a given tool policy, and report how the *process* went (exit, cost,
tokens). It does not read or validate the agent's output document — that
is the harness's job, identically for every runtime, which is what makes
swapping `claude` for `opencode` or `codex` a config edit.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from devloop.contracts.runs import Role

log = logging.getLogger("devloop.agent")

HEARTBEAT_S = 30
OUT_DIR = ".devloop/out"
IN_DIR = ".devloop/in"

# Read-only commands any role may use to orient itself (and to pipe a
# check command's output through, e.g. `pytest 2>&1 | tail -20`).
READ_ONLY_SHELL = (
    "git status",
    "git diff",
    "git log",
    "git show",
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "pwd",
)


@dataclass(frozen=True)
class ToolPolicy:
    # False: the agent may write only under `.devloop/out/`.
    can_edit: bool = False
    # Command prefixes the agent may run. Empty: no shell at all.
    bash_allow: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentInvocation:
    role: Role
    prompt: str
    workdir: Path
    tools: ToolPolicy
    log_path: Path
    model: str | None = None
    max_budget_usd: float | None = None
    timeout_s: int = 600


@dataclass
class AgentOutcome:
    ok: bool
    error: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    session_id: str = ""
    extra: dict[str, object] = field(default_factory=dict)


class CodingAgent(Protocol):
    name: str

    def run(self, invocation: AgentInvocation) -> AgentOutcome: ...


def run_process(
    argv: list[str],
    *,
    stdin: str,
    cwd: Path,
    timeout_s: int,
    log_path: Path,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a CLI and return (code, stdout, stderr).

    stdout streams straight into `log_path` (stderr next to it) while the
    agent runs, so a long run can be followed with `tail -f`; a heartbeat
    is logged every `HEARTBEAT_S`. A timeout kills the whole process group
    (agent CLIs spawn children) and is reported as exit code -1 rather than
    raised: a hung agent is an agent failure, not an orchestrator crash."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    err_path = log_path.with_suffix(".stderr.log")
    started = time.monotonic()
    with log_path.open("w") as out_fh, err_path.open("w") as err_fh:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=out_fh,
                stderr=err_fh,
                cwd=cwd,
                env=env,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError:
            return -1, "", f"{argv[0]}: command not found"

        assert proc.stdin is not None
        proc.stdin.write(stdin)
        proc.stdin.close()

        code: int | None = None
        while code is None:
            elapsed = time.monotonic() - started
            if elapsed >= timeout_s:
                _kill_group(proc)
                break
            try:
                code = proc.wait(timeout=min(HEARTBEAT_S, timeout_s - elapsed))
            except subprocess.TimeoutExpired:
                log.info(
                    "%s still running (%ds of %ds) — tail -f %s",
                    Path(argv[0]).name,
                    time.monotonic() - started,
                    timeout_s,
                    log_path,
                )

    out, err = log_path.read_text(), err_path.read_text()
    if code is None:
        return -1, out, f"timed out after {timeout_s}s\n{err}"
    return code, out, err


def _kill_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()
