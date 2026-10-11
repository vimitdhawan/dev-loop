"""The `gh` CLI, as the one way DevLoop talks to GitHub.

Auth is whatever `gh` already uses (`gh auth login` or `GH_TOKEN`); DevLoop
never sees or stores the token. `DEVLOOP_GH_BIN` swaps the binary (tests).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from devloop.errors import DevLoopError


class GitHubError(DevLoopError):
    pass


def gh(*args: str, cwd: Path | None = None) -> str:
    binary = os.environ.get("DEVLOOP_GH_BIN", "gh")
    try:
        proc = subprocess.run([binary, *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as exc:
        raise GitHubError(f"{binary} not found; install the GitHub CLI") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubError(f"gh {' '.join(args[:2])} timed out") from exc
    if proc.returncode != 0:
        raise GitHubError(f"gh {' '.join(args[:2])} failed: {proc.stderr.strip()[-500:]}")
    return proc.stdout.strip()
