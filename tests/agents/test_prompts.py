from __future__ import annotations

import re
from pathlib import Path

import pytest

from devloop.agents.prompts import load_prompt
from devloop.contracts.runs import Step


@pytest.mark.parametrize("step", list(Step))
def test_every_step_has_a_prompt_that_renders(step: Step) -> None:
    prompt = load_prompt(step)

    text = prompt.render(context_path="CTX", out_path="OUT", schema="SCHEMA")

    assert re.match(r"v\d+\+[0-9a-f]{8}$", prompt.version)
    assert "OUT" in text and "SCHEMA" in text and "$" not in text


def test_newest_version_wins_unless_pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    role_dir = tmp_path / "reviewer"
    role_dir.mkdir()
    (role_dir / "v1.md").write_text("one")
    (role_dir / "v2.md").write_text("two")
    (role_dir / "v10.md").write_text("ten")
    monkeypatch.setenv("DEVLOOP_PROMPTS_DIR", str(tmp_path))

    assert load_prompt(Step.REVIEW).version.startswith("v10+")
    monkeypatch.setenv("DEVLOOP_PROMPT_REVIEWER", "v2")
    assert load_prompt(Step.REVIEW).template == "two"


def test_version_hash_changes_when_text_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    role_dir = tmp_path / "reviewer"
    role_dir.mkdir()
    monkeypatch.setenv("DEVLOOP_PROMPTS_DIR", str(tmp_path))
    (role_dir / "v1.md").write_text("one")
    before = load_prompt(Step.REVIEW).version
    (role_dir / "v1.md").write_text("one, edited")
    assert load_prompt(Step.REVIEW).version != before
