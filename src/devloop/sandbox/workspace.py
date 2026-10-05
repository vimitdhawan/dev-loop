"""Per-task workspace: a private clone of the target repo.

Agents never touch the user's checkout. Each task gets
`~/.devloop/work/<task_id>`, cloned from the target repo's base branch, on
a `devloop/<task_id>` branch. The orchestrator — never an agent — commits,
and publishes the branch back to the target repo so a human can inspect it
with plain `git log -p` while the task waits at the merge gate.

This is the same `/workspace` layout the Docker sandbox will bind-mount,
so moving execution into a container changes how commands are *run*, not
what the workspace looks like.

Every operation here is idempotent so a crash-resume of the calling node
is safe: re-preparing an existing workspace reuses it, re-publishing a
branch force-updates it to the same commit.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from devloop.errors import GitError
from devloop.paths import workspace_dir

_BASE_REF = "refs/devloop/base"
_BOT_NAME = "DevLoop"
_BOT_EMAIL = "devloop@localhost"


def _git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


@dataclass(frozen=True)
class Workspace:
    path: Path
    base_commit: str
    branch: str


@dataclass
class CommitResult:
    files_changed: list[str] = field(default_factory=list)
    insertions: int = 0
    deletions: int = 0


def default_branch(repo_path: Path) -> str:
    """The branch every task forks from. Never a `devloop/*` branch —
    otherwise a second task silently forks off the first task's branch
    instead of the base, carrying its changes along uninvited."""

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


def prepare_workspace(source_repo: Path, task_id: str) -> Workspace:
    branch = f"devloop/{task_id}"
    path = workspace_dir(task_id)

    if (path / ".git").exists():
        return Workspace(path=path, base_commit=_git(path, "rev-parse", _BASE_REF), branch=branch)

    if not (source_repo / ".git").exists():
        raise GitError(f"{source_repo} is not a git repository")

    base = default_branch(source_repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--quiet", "--branch", base, str(source_repo), str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(f"git clone {source_repo} failed: {result.stderr.strip()}")

    base_commit = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", _BASE_REF, base_commit)
    _git(path, "checkout", "--quiet", "-b", branch)
    _git(path, "config", "user.name", _BOT_NAME)
    _git(path, "config", "user.email", _BOT_EMAIL)
    # Agent inputs/outputs live in the workspace but must never be committed.
    exclude = path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as fh:
        fh.write("\n.devloop/\n")
    return Workspace(path=path, base_commit=base_commit, branch=branch)


def is_dirty(path: Path) -> bool:
    return bool(_git(path, "status", "--porcelain"))


def discard_changes(path: Path) -> None:
    """Back to the last commit. Ignored files (including `.devloop/`) stay."""

    _git(path, "reset", "--quiet", "--hard")
    _git(path, "clean", "--quiet", "-fd")


def _numstat(output: str) -> CommitResult:
    stat = CommitResult()
    for line in output.splitlines():
        ins, dele, file_path = line.split("\t", 2)
        stat.insertions += int(ins) if ins != "-" else 0
        stat.deletions += int(dele) if dele != "-" else 0
        stat.files_changed.append(file_path)
    return stat


def commit_all(path: Path, message: str) -> CommitResult:
    """Commit everything the agent left in the working tree. Returns an
    empty result — and makes no commit — when there was nothing to commit."""

    _git(path, "add", "--all")
    stat = _numstat(_git(path, "diff", "--cached", "--numstat"))
    if stat.files_changed:
        _git(path, "commit", "--quiet", "--no-verify", "-m", message)
    return stat


def diff_stat(path: Path, base_commit: str) -> CommitResult:
    """Cumulative change on the task branch relative to where it forked."""

    return _numstat(_git(path, "diff", "--numstat", base_commit, "HEAD"))


def diff_patch(path: Path, base_commit: str) -> str:
    return _git(path, "diff", base_commit, "HEAD")


def publish_branch(path: Path, branch: str) -> None:
    """Make the task branch visible in the user's repo. Force is safe: the
    `devloop/<task_id>` branch is owned by exactly this task."""

    _git(path, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{branch}")
