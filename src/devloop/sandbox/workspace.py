"""Per-task workspace: a private clone of the target repo.

Agents never touch the user's checkout. Each task gets a fresh clone at
`~/.devloop/work/<task_id>` — from a local path or a remote URL, of the
base branch the user chose (or the repo's default) — on a
`devloop/<task_id>-<slug>` branch. The orchestrator — never an agent —
commits, and publishes the branch to `origin` (the local repo, or GitHub)
once the change is approved.

This is the same `/workspace` layout the Docker sandbox will bind-mount,
so moving execution into a container changes how commands are *run*, not
what the workspace looks like.

Every operation here is idempotent so a crash-resume of the calling node
is safe: re-preparing an existing workspace reuses it, re-publishing a
branch force-updates it to the same commit.
"""

from __future__ import annotations

import re
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


def branch_name(task_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].rstrip("-")
    return f"devloop/{task_id}-{slug}" if slug else f"devloop/{task_id}"


def prepare_workspace(
    source: str, task_id: str, *, branch: str, base_branch: str | None = None
) -> tuple[Workspace, str]:
    """Clone `source` (a path or a URL) at `base_branch` and create the task
    branch. Returns the workspace and the base branch actually used.
    Idempotent: an existing workspace for the task is reused as is."""

    path = workspace_dir(task_id)
    if (path / ".git").exists():
        base = _git(path, "config", "--get", "devloop.base-branch")
        workspace = Workspace(
            path=path, base_commit=_git(path, "rev-parse", _BASE_REF), branch=branch
        )
        return workspace, base

    local = Path(source).expanduser()
    if local.exists():
        if not _is_git_repo(local):
            raise GitError(f"{local} is not a git repository")
        source = str(local.resolve())
        base_branch = base_branch or default_branch(local)

    path.parent.mkdir(parents=True, exist_ok=True)
    clone = ["git", "clone", "--quiet"]
    if base_branch:
        clone += ["--branch", base_branch]
    result = subprocess.run([*clone, source, str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise GitError(f"git clone {source} failed: {result.stderr.strip()}")

    base = base_branch or _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if base.startswith("devloop/"):
        raise GitError(f"refusing to fork a task from another task's branch ({base})")
    base_commit = _git(path, "rev-parse", "HEAD")
    _git(path, "update-ref", _BASE_REF, base_commit)
    _git(path, "config", "devloop.base-branch", base)
    _git(path, "checkout", "--quiet", "-b", branch)
    _git(path, "config", "user.name", _BOT_NAME)
    _git(path, "config", "user.email", _BOT_EMAIL)
    # Agent inputs/outputs live in the workspace but must never be committed.
    exclude = path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as fh:
        fh.write("\n.devloop/\n")
    return Workspace(path=path, base_commit=base_commit, branch=branch), base


def _is_git_repo(path: Path) -> bool:
    """True for a working copy *or* a bare repo (e.g. a local mirror)."""

    probe = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"], capture_output=True, text=True
    )
    return probe.returncode == 0


def origin_url(path: Path) -> str:
    return _git(path, "remote", "get-url", "origin")


def github_slug(url: str) -> str | None:
    """`owner/repo` for a GitHub remote (https or ssh), else None."""

    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


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
    """Push the task branch to `origin`. Force is safe: the
    `devloop/<task_id>-*` branch is owned by exactly this task."""

    _git(path, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{branch}")
