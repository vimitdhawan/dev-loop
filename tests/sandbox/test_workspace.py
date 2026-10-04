from __future__ import annotations

from pathlib import Path

import pytest

from devloop.errors import GitError
from devloop.sandbox.workspace import (
    commit_all,
    diff_stat,
    discard_changes,
    is_dirty,
    prepare_workspace,
    publish_branch,
)
from tests.conftest import git


def test_prepare_clones_base_branch_onto_task_branch(target_repo: Path) -> None:
    ws = prepare_workspace(target_repo, "t1")

    assert ws.branch == "devloop/t1"
    assert git(ws.path, "rev-parse", "--abbrev-ref", "HEAD") == "devloop/t1"
    assert ws.base_commit == git(target_repo, "rev-parse", "main")
    # the user's checkout is untouched
    assert git(target_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_prepare_is_idempotent(target_repo: Path) -> None:
    first = prepare_workspace(target_repo, "t1")
    (first.path / "work.txt").write_text("in progress")

    second = prepare_workspace(target_repo, "t1")

    assert second == first
    assert (second.path / "work.txt").exists(), "re-prepare must not wipe work"


def test_prepare_never_forks_from_a_devloop_branch(target_repo: Path) -> None:
    git(target_repo, "checkout", "-q", "-b", "devloop/old")
    (target_repo / "old.txt").write_text("old task")
    git(target_repo, "add", "-A")
    git(target_repo, "commit", "-q", "-m", "old task")

    ws = prepare_workspace(target_repo, "t2")

    assert not (ws.path / "old.txt").exists()


def test_prepare_rejects_non_git_dir(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        prepare_workspace(tmp_path, "t1")


def test_devloop_dir_is_never_committed(target_repo: Path) -> None:
    ws = prepare_workspace(target_repo, "t1")
    (ws.path / ".devloop" / "out").mkdir(parents=True)
    (ws.path / ".devloop" / "out" / "x.json").write_text("{}")

    assert not is_dirty(ws.path)
    assert commit_all(ws.path, "nothing").files_changed == []


def test_commit_publish_and_cumulative_diff(target_repo: Path) -> None:
    ws = prepare_workspace(target_repo, "t1")
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
    ws = prepare_workspace(target_repo, "t1")
    (ws.path / ".devloop").mkdir()
    (ws.path / ".devloop" / "keep").write_text("x")
    (ws.path / "README.md").write_text("changed")
    (ws.path / "new.txt").write_text("new")

    discard_changes(ws.path)

    assert not is_dirty(ws.path)
    assert (ws.path / ".devloop" / "keep").exists()
