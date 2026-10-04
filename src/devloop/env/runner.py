"""Orchestrator-run verification. The only test results admissible as
facts: an agent may run tests for its own feedback, but `decide()` only
ever sees what this module recorded.

Runs on the host against the task workspace for now; the Docker sandbox
replaces `_run` with a `docker exec` and nothing else changes.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from devloop.contracts.artifacts import EnvRecipe, TestFailure, TestRunResult

COMMAND_TIMEOUT_S = 15 * 60
# State is checkpointed after every node; keep test output in it bounded.
OUTPUT_TAIL_CHARS = 8_000

_FAILURE_PATTERNS = [
    re.compile(r"^\s*--- FAIL: (\S+)", re.MULTILINE),  # go test
    re.compile(r"^FAILED (\S+)", re.MULTILINE),  # pytest -rf / -q summary
]


def run_checks(
    workspace: Path, recipe: EnvRecipe, *, phase: str, iteration: int, log_dir: Path
) -> list[TestRunResult]:
    """Setup first; when setup fails the checks can't mean anything, so
    only the setup result is returned."""

    log_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{phase}-{iteration}"
    results: list[TestRunResult] = []

    if recipe.setup:
        setup = _run(
            workspace,
            " && ".join(recipe.setup),
            type_="setup",
            phase=phase,
            iteration=iteration,
            log_path=log_dir / f"{tag}-setup.log",
        )
        results.append(setup)
        if not setup.passed:
            return results

    for name, command in recipe.commands.items():
        results.append(
            _run(
                workspace,
                command,
                type_=name,
                phase=phase,
                iteration=iteration,
                log_path=log_dir / f"{tag}-{name}.log",
            )
        )
    return results


def _run(
    workspace: Path, command: str, *, type_: str, phase: str, iteration: int, log_path: Path
) -> TestRunResult:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=True,  # commands come from the repo's own devloop.yml / manifests
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
            env={**os.environ, "CI": "true"},
        )
        output = proc.stdout + proc.stderr
        passed = proc.returncode == 0
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        output = (partial.decode() if isinstance(partial, bytes) else partial) + (
            f"\n[devloop] timed out after {COMMAND_TIMEOUT_S}s"
        )
        passed = False

    log_path.write_text(f"$ {command}\n{output}")
    return TestRunResult(
        type=type_,
        command=command,
        phase=phase,
        iteration=iteration,
        passed=passed,
        output=output[-OUTPUT_TAIL_CHARS:],
        duration_ms=int((time.monotonic() - started) * 1000),
        failures=[] if passed else parse_failures(output),
    )


def parse_failures(output: str) -> list[TestFailure]:
    names: dict[str, None] = {}
    for pattern in _FAILURE_PATTERNS:
        for match in pattern.finditer(output):
            names.setdefault(match.group(1), None)
    return [TestFailure(name=name, message="") for name in names]
