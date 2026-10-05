from __future__ import annotations

from pathlib import Path

import pytest

from devloop.errors import GitError
from devloop.sandbox.workspace import (
    branch_name,
    commit_all,
    diff_stat,
    discard_changes,
    github_slug,
    is_dirty,
    origin_url,
    prepare_workspace,
    publish_branch,
)
from tests.conftest import git


def test_prepare_clones_base_branch_onto_task_branch(target_repo: Path) -> None:
    ws = prepare_workspace(str(target_repo), "t1", branch="devloop/t1")[0]

    assert ws.branch == "devloop/t1"
    assert git(ws.path, "rev-parse", "--abbrev-ref", "HEAD") == "devloop/t1"
    assert ws.base_commit == git(target_repo, "rev-parse", "main")
    # the user's checkout is untouched
    assert git(target_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_prepare_is_idempotent(target_repo: Path) -> None:
    first = prepare_workspace(str(target_repo), "t1", branch="devloop/t1")[0]
    (first.path / "work.txt").write_text("in progress")

    second = prepare_workspace(str(target_repo), "t1", branch="devloop/t1")[0]

    assert second == first
    assert (second.path / "work.txt").exists(), "re-prepare must not wipe work"


def test_prepare_never_forks_from_a_devloop_branch(target_repo: Path) -> None:
    git(target_repo, "checkout", "-q", "-b", "devloop/old")
    (target_repo / "old.txt").write_text("old task")
    git(target_repo, "add", "-A")
    git(target_repo, "commit", "-q", "-m", "old task")

    ws = prepare_workspace(str(target_repo), "t2", branch="devloop/t2")[0]

    assert not (ws.path / "old.txt").exists()


def test_prepare_rejects_non_git_dir(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        prepare_workspace(str(tmp_path), "t1", branch="devloop/t1")


def test_devloop_dir_is_never_committed(target_repo: Path) -> None:
    ws = prepare_workspace(str(target_repo), "t1", branch="devloop/t1")[0]
    (ws.path / ".devloop" / "out").mkdir(parents=True)
    (ws.path / ".devloop" / "out" / "x.json").write_text("{}")

    assert not is_dirty(ws.path)
    assert commit_all(ws.path, "nothing").files_changed == []


def test_commit_publish_and_cumulative_diff(target_repo: Path) -> None:
    ws = prepare_workspace(str(target_repo), "t1", branch="devloop/t1")[0]
    (ws.path / "a.txt").write_text("a\n")
    first = commit_all(ws.path, "one")
    (ws.path / "b.txt").write_text("b\nb\n")
    commit_all(ws.path, "two")

    publish_branch(ws.path, ws.branch)
    publish_branch(ws.path, ws.branch)  # idempotent

    assert first.files_changed == ["a.txt"]
    total = diff_stat(ws.path, ws.base_commit)
    assert sorted(total.files_changed) == ["a.txt", "b.txt"]
    assert total.insertions == 3
    assert git(target_repo, "rev-parse", "devloop/t1") == git(ws.path, "rev-parse", "HEAD")


def test_discard_changes_keeps_devloop_dir(target_repo: Path) -> None:
    ws = prepare_workspace(str(target_repo), "t1", branch="devloop/t1")[0]
    (ws.path / ".devloop").mkdir()
    (ws.path / ".devloop" / "keep").write_text("x")
    (ws.path / "README.md").write_text("changed")
    (ws.path / "new.txt").write_text("new")

    discard_changes(ws.path)

    assert not is_dirty(ws.path)
    assert (ws.path / ".devloop" / "keep").exists()


def test_clones_the_requested_base_branch(target_repo: Path) -> None:
    git(target_repo, "checkout", "-q", "-b", "release")
    (target_repo / "release.txt").write_text("r")
    git(target_repo, "add", "-A")
    git(target_repo, "commit", "-q", "-m", "release only")
    git(target_repo, "checkout", "-q", "main")

    ws, base = prepare_workspace(str(target_repo), "t1", branch="devloop/t1", base_branch="release")

    assert base == "release"
    assert (ws.path / "release.txt").exists()
    assert ws.base_commit == git(target_repo, "rev-parse", "release")


def test_fresh_clone_from_a_url_and_push_back(target_repo: Path, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(target_repo), str(remote))
    url = remote.as_uri()  # file:// — exercised exactly like an https/ssh remote

    ws, base = prepare_workspace(url, "t1", branch="devloop/t1-x")
    (ws.path / "new.txt").write_text("n")
    commit_all(ws.path, "change")
    publish_branch(ws.path, ws.branch)

    assert base == "main"
    assert origin_url(ws.path) == url
    assert git(remote, "rev-parse", "devloop/t1-x") == git(ws.path, "rev-parse", "HEAD")
    # reusing the workspace reports the same base
    assert prepare_workspace(url, "t1", branch="devloop/t1-x")[1] == "main"


def test_refuses_to_fork_from_a_task_branch(target_repo: Path) -> None:
    git(target_repo, "branch", "devloop/old")
    with pytest.raises(GitError, match="another task"):
        prepare_workspace(str(target_repo), "t1", branch="devloop/t1", base_branch="devloop/old")


@pytest.mark.parametrize(
    "title,expected",
    [
        ("reusable time picker", "devloop/ab12-reusable-time-picker"),
        ("Fix: 500 on /api/Users!!", "devloop/ab12-fix-500-on-api-users"),
        ("???", "devloop/ab12"),
    ],
)
def test_branch_name(title: str, expected: str) -> None:
    assert branch_name("ab12", title) == expected


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://github.com/vimitdhawan/playrotation.git", "vimitdhawan/playrotation"),
        ("https://github.com/org/repo", "org/repo"),
        ("git@github.com:org/repo.git", "org/repo"),
        ("https://gitlab.com/org/repo.git", None),
        ("/Users/me/code/repo", None),
    ],
)
def test_github_slug(url: str, slug: str | None) -> None:
    assert github_slug(url) == slug
