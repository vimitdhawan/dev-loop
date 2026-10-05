"""Open (or update) the PR for a task branch with the `gh` CLI.

Auth is whatever `gh` already uses (`gh auth login` or `GH_TOKEN`);
DevLoop never sees or stores the token. Idempotent: a PR that already
exists for the branch has its body updated instead of a second one being
opened, so a crash-resume of the publish node can't duplicate it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from devloop.errors import DevLoopError


class PullRequestError(DevLoopError):
    pass


def _gh(*args: str, cwd: Path) -> str:
    binary = os.environ.get("DEVLOOP_GH_BIN", "gh")
    try:
        proc = subprocess.run([binary, *args], cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise PullRequestError(f"{binary} not found; install the GitHub CLI") from exc
    if proc.returncode != 0:
        raise PullRequestError(f"gh {' '.join(args[:2])} failed: {proc.stderr.strip()[-500:]}")
    return proc.stdout.strip()


def open_or_update_pr(
    *,
    workspace: Path,
    repo: str,
    base: str,
    head: str,
    title: str,
    body_file: Path,
    draft: bool,
) -> str:
    existing = json.loads(
        _gh(
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            head,
            "--state",
            "open",
            "--json",
            "url",
            cwd=workspace,
        )
        or "[]"
    )
    if existing:
        url: str = existing[0]["url"]
        _gh("pr", "edit", url, "--body-file", str(body_file), cwd=workspace)
        return url

    args = [
        "pr",
        "create",
        "--repo",
        repo,
        "--base",
        base,
        "--head",
        head,
        "--title",
        title,
        "--body-file",
        str(body_file),
    ]
    if draft:
        args.append("--draft")
    # `gh pr create` prints the new PR's URL as its last line
    return _gh(*args, cwd=workspace).splitlines()[-1]
