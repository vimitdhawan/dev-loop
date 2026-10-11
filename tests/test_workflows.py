from __future__ import annotations

import pytest
from pydantic import ValidationError

from devloop.config import DevLoopConfig
from devloop.contracts.runs import Role
from devloop.contracts.state import TaskInput
from devloop.errors import DevLoopError
from devloop.workflows import DEFAULT_WORKFLOWS, WorkflowConfig, select_workflow


def task(workflow: str | None = None, labels: list[str] | None = None) -> TaskInput:
    return TaskInput(
        external_id="t",
        repo=".",
        title="t",
        description="d",
        workflow=workflow,
        labels=labels or [],
    )


def pick(t: TaskInput, config: DevLoopConfig | None = None) -> tuple[str, str]:
    config = config or DevLoopConfig()
    chosen = select_workflow(t, config.workflows, config.default_workflow)
    return chosen.name, chosen.selected_by


@pytest.mark.parametrize(
    "t,expected",
    [
        (task(), ("feature", "default")),
        (task(labels=["bug"]), ("bug", "label: bug")),
        (task(labels=["Bug", "devloop"]), ("bug", "label: bug")),
        (task(labels=["enhancement", "ui"]), ("ui_feature", "label: ui")),
        (task(labels=["bug", "ui"]), ("bug", "label: bug")),
        (task(labels=["refactor"]), ("refactor", "label: refactor")),
        (task(labels=["question"]), ("feature", "default")),
        (task(workflow="refactor", labels=["bug"]), ("refactor", "requested: refactor")),
    ],
    ids=[
        "no signal → default",
        "label",
        "labels are case-insensitive",
        "ui beats enhancement (definition order)",
        "bug beats ui (definition order)",
        "refactor",
        "unmapped label → default",
        "a requested workflow beats labels",
    ],
)
def test_selection_is_deterministic(t: TaskInput, expected: tuple[str, str]) -> None:
    assert pick(t) == expected


def test_default_workflows_match_the_documented_stages() -> None:
    stages = {name: [r.value for r in w.stages] for name, w in DEFAULT_WORKFLOWS.items()}
    assert stages["bug"] == ["planner", "engineer", "qa", "reviewer"]
    assert stages["feature"] == ["product_owner", "planner", "engineer", "qa", "reviewer"]
    assert stages["ui_feature"] == ["product_owner", "ux", "planner", "engineer", "qa", "reviewer"]
    assert stages["refactor"] == ["planner", "engineer", "qa", "reviewer"]


def test_configured_workflows_come_first_and_override_built_ins() -> None:
    config = DevLoopConfig.model_validate(
        {
            "workflows": {
                "hotfix": {"stages": ["planner", "engineer", "reviewer"], "labels": ["bug"]},
                "bug": {"stages": ["planner", "engineer", "reviewer"]},
            },
            "default_workflow": "hotfix",
        }
    )

    assert list(config.workflows)[:2] == ["hotfix", "bug"]
    assert pick(task(labels=["bug"]), config) == ("hotfix", "label: bug")
    assert config.workflows["bug"].stages == [Role.PLANNER, Role.ENGINEER, Role.REVIEWER]
    assert "feature" in config.workflows, "built-ins not redefined are kept"
    assert pick(task(), config) == ("hotfix", "default")


def test_unknown_requested_workflow_fails_loudly() -> None:
    with pytest.raises(DevLoopError, match="unknown workflow 'bugfix'"):
        pick(task(workflow="bugfix"))


def test_unknown_default_workflow_is_a_config_error() -> None:
    with pytest.raises(ValidationError, match="default_workflow"):
        DevLoopConfig.model_validate({"default_workflow": "nope"})


@pytest.mark.parametrize(
    "stages,error",
    [
        (["product_owner", "engineer", "reviewer"], "every workflow needs planner"),
        (["planner", "engineer"], "every workflow needs reviewer"),
        (["planner", "engineer", "engineer", "reviewer"], "listed twice"),
        (["engineer", "planner", "reviewer"], "stages run in this order"),
        (["planner", "engineer", "reviewer", "designer"], "Input should be"),
    ],
)
def test_invalid_stage_lists_are_rejected(stages: list[str], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        WorkflowConfig.model_validate({"stages": stages})
