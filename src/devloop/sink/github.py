"""Open (or update) the PR for a task branch with the `gh` CLI.

Idempotent: a PR that already exists for the branch has its body updated
instead of a second one being opened, so a crash-resume of the publish
node can't duplicate it.
"""

from __future__ import annotations

import json
from pathlib import Path

from devloop.gh import GitHubError, gh

PullRequestError = GitHubError


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
        gh(
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
        gh("pr", "edit", url, "--body-file", str(body_file), cwd=workspace)
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
    return gh(*args, cwd=workspace).splitlines()[-1]
