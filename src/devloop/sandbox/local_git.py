"""Phase-0 git isolation: direct git operations against the target repo,
on a dedicated branch, with no container involved.

This is a deliberate placeholder for the North Star's real sandbox
(`docker.py` + a clone of a bare mirror — see the plan's Part 5). It exists
so Phase 0 can prove a task really produces an inspectable branch, without
waiting on the Docker sandbox work that lands in Phase 1. Every caller goes
through this module, not raw subprocess calls, so swapping it out later is
a one-file change.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


@dataclass
class CommitResult:
    branch: str
    files_changed: list[str]
    insertions: int
    deletions: int


def ensure_clean_repo(repo_path: Path) -> None:
    if not (repo_path / ".git").exists():
        raise GitError(f"{repo_path} is not a git repository")
    status = _git(repo_path, "status", "--porcelain")
    if status:
        raise GitError(
            f"{repo_path} has uncommitted changes; refusing to run a task against a dirty repo"
        )


def _default_branch(repo_path: Path) -> str:
    """The branch every task branch forks from. Never a `devloop/*`
    branch — otherwise a second task silently forks off the first task's
    branch instead of the base, carrying its changes along uninvited."""

    for candidate in ("main", "master"):
        if _git(repo_path, "branch", "--list", candidate):
            return candidate
    current = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    if not current.startswith("devloop/"):
        return current
    raise GitError(
        "cannot determine the base branch: repo has no main/master and HEAD is "
        f"already on a devloop task branch ({current})"
    )


def create_task_branch(repo_path: Path, task_id: str) -> str:
    branch = f"devloop/{task_id}"
    base = _default_branch(repo_path)
    _git(repo_path, "checkout", base)

    existing = _git(repo_path, "branch", "--list", branch)
    if existing:
        _git(repo_path, "branch", "-D", branch)  # stale branch from a prior failed run
    _git(repo_path, "checkout", "-b", branch)
    return branch


def write_and_commit(
    repo_path: Path, branch: str, relative_path: str, content: str
) -> CommitResult:
    target = repo_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)

    _git(repo_path, "add", relative_path)
    stat = _git(repo_path, "diff", "--cached", "--numstat")
    if not stat:
        raise GitError(
            f"{relative_path} is identical to the base branch; nothing to commit "
            "(the stub implementation always produces the same note for the same task)"
        )
    insertions = deletions = 0
    files_changed: list[str] = []
    for line in stat.splitlines():
        ins, dele, path = line.split("\t")
        insertions += int(ins) if ins != "-" else 0
        deletions += int(dele) if dele != "-" else 0
        files_changed.append(path)

    _git(repo_path, "commit", "-m", f"devloop: {relative_path}")
    return CommitResult(
        branch=branch,
        files_changed=files_changed,
        insertions=insertions,
        deletions=deletions,
    )
