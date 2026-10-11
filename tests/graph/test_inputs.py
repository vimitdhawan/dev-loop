"""Starting a task from raw JSON — what LangGraph Studio's input form sends."""

from __future__ import annotations

from pathlib import Path

import pytest

from devloop.contracts.state import DevLoopState
from devloop.contracts.status import DevLoopStatus as St
from devloop.errors import DevLoopError
from devloop.graph.inputs import is_raw_input, normalize_input


@pytest.fixture(autouse=True)
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_repo: Path) -> None:
    (tmp_path / "devloop.config.yaml").write_text(
        f"agents: {{engineer: {{runtime: stub, model: m}}}}\n"
        f"repo: {{url: {target_repo}, base_branch: main}}\nbudget_usd: 3\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEVLOOP_CONFIG", raising=False)
    monkeypatch.delenv("DEVLOOP_REPO_URL", raising=False)


def test_minimal_input_is_filled_from_the_config(target_repo: Path) -> None:
    raw: DevLoopState = {"task": {"title": "t", "description": "d"}}  # type: ignore[typeddict-item]
    assert is_raw_input(raw)

    state = normalize_input(raw)

    assert not is_raw_input(state)
    assert state["status"] == St.RECEIVED
    assert state["task"].repo == str(target_repo) and state["task"].base_branch == "main"
    assert len(state["task"].external_id) == 8
    assert state["team"].engineer.runtime == "stub" and state["budget_usd"] == 3
    assert state["agent_runs"] == [] and state["sessions"] == {}


def test_input_overrides_the_config() -> None:
    raw = {
        "task": {"title": "t", "description": "d", "repo": "https://github.com/o/r.git"},
        "team": {"reviewer": {"runtime": "opencode"}},
        "budget_usd": 9,
    }

    state = normalize_input(raw)  # type: ignore[arg-type]

    assert state["task"].repo == "https://github.com/o/r.git"
    assert state["team"].reviewer.runtime == "opencode"
    assert state["team"].engineer.runtime == "claude", "an explicit team replaces the config's"
    assert state["budget_usd"] == 9


def test_description_is_required() -> None:
    with pytest.raises(DevLoopError, match="description"):
        normalize_input({"task": {"title": "t"}})  # type: ignore[typeddict-item]


@pytest.mark.parametrize(
    "task,expected",
    [
        ({"title": "t", "description": "d"}, ("feature", "default")),
        ({"title": "t", "description": "d", "labels": ["bug"]}, ("bug", "label: bug")),
        (
            {"title": "t", "description": "d", "workflow": "ui_feature"},
            ("ui_feature", "requested: ui_feature"),
        ),
    ],
)
def test_studio_input_picks_the_workflow(
    task: dict[str, object], expected: tuple[str, str]
) -> None:
    state = normalize_input({"task": task})  # type: ignore[typeddict-item]

    workflow = state["workflow"]
    assert workflow is not None and (workflow.name, workflow.selected_by) == expected


def test_studio_input_with_an_unknown_workflow_is_rejected() -> None:
    with pytest.raises(DevLoopError, match="unknown workflow"):
        normalize_input({"task": {"title": "t", "description": "d", "workflow": "x"}})  # type: ignore[typeddict-item]
