from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devloop.runtimes import registry


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def devloop_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own DEVLOOP_HOME and a fresh runtime registry,
    so workspaces, logs and stateful stub runtimes never leak between tests."""

    home = tmp_path / "devloop-home"
    monkeypatch.setenv("DEVLOOP_HOME", str(home))
    monkeypatch.setattr(registry, "_INSTANCES", {})
    return home


DEVLOOP_YML = """\
commands:
  unit: test ! -f BROKEN
"""


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    """A minimal target repo whose only check is "no file named BROKEN"."""

    repo = tmp_path / "target"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("# target\n")
    (repo / "devloop.yml").write_text(DEVLOOP_YML)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo
