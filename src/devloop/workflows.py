"""Which stages a task goes through.

A workflow is an ordered subset of the team. Planner, Engineer and
Reviewer are always in it — nothing ships unplanned or unreviewed —
while Product Owner, UX and QA are optional:

    bug         planner → engineer → qa → reviewer
    feature     product_owner → planner → engineer → qa → reviewer
    ui_feature  product_owner → ux → planner → engineer → qa → reviewer
    refactor    planner → engineer → qa → reviewer

The graph doesn't change shape per workflow: the chosen `Workflow` is a
fact in state, and `decide()` skips the stages it doesn't list. So a new
workflow is a config entry, not code.

Selection is deterministic, first match wins:
1. the workflow named for the task (`--workflow`, Studio's `task.workflow`)
2. the first workflow, in config order, with a label the task carries
3. `default_workflow`
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from devloop.contracts.runs import Role, Workflow
from devloop.contracts.state import TaskInput
from devloop.errors import DevLoopError

REQUIRED_STAGES = (Role.PLANNER, Role.ENGINEER, Role.REVIEWER)
_ORDER = list(Role)


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stages: list[Role]
    # an issue with any of these labels gets this workflow (case-insensitive)
    labels: list[str] = Field(default_factory=list)

    @field_validator("stages")
    @classmethod
    def _valid_stages(cls, stages: list[Role]) -> list[Role]:
        missing = [r.value for r in REQUIRED_STAGES if r not in stages]
        if missing:
            raise ValueError(f"every workflow needs {', '.join(missing)}")
        if len(set(stages)) != len(stages):
            raise ValueError("a stage is listed twice")
        if stages != sorted(stages, key=_ORDER.index):
            # The graph runs stages in one fixed order; a config that reads
            # otherwise would be lying about what happens.
            raise ValueError(
                "stages run in this order, list them the same way: "
                + " → ".join(r.value for r in _ORDER)
            )
        return stages


DEFAULT_WORKFLOWS: dict[str, WorkflowConfig] = {
    "bug": WorkflowConfig(
        stages=[Role.PLANNER, Role.ENGINEER, Role.QA, Role.REVIEWER], labels=["bug"]
    ),
    "ui_feature": WorkflowConfig(stages=list(Role), labels=["ui", "ux", "design", "frontend"]),
    "feature": WorkflowConfig(
        stages=[Role.PRODUCT_OWNER, Role.PLANNER, Role.ENGINEER, Role.QA, Role.REVIEWER],
        labels=["feature", "enhancement"],
    ),
    "refactor": WorkflowConfig(
        stages=[Role.PLANNER, Role.ENGINEER, Role.QA, Role.REVIEWER],
        labels=["refactor", "tech-debt"],
    ),
}
DEFAULT_WORKFLOW = "feature"

# A task that predates workflows ran exactly this.
LEGACY = Workflow(
    name="feature",
    stages=[Role.PRODUCT_OWNER, Role.PLANNER, Role.ENGINEER, Role.QA, Role.REVIEWER],
    selected_by="default",
)


def merge_workflows(configured: dict[str, WorkflowConfig]) -> dict[str, WorkflowConfig]:
    """The operator's workflows first, in their order (so their label rules
    win), then any built-in one they didn't redefine."""

    return {**configured, **{k: v for k, v in DEFAULT_WORKFLOWS.items() if k not in configured}}


def select_workflow(
    task: TaskInput, workflows: dict[str, WorkflowConfig], default: str
) -> Workflow:
    def make(name: str, selected_by: str) -> Workflow:
        return Workflow(name=name, stages=list(workflows[name].stages), selected_by=selected_by)

    if task.workflow:
        if task.workflow not in workflows:
            raise DevLoopError(f"unknown workflow {task.workflow!r}; known: {', '.join(workflows)}")
        return make(task.workflow, f"requested: {task.workflow}")

    labels = {label.lower() for label in task.labels}
    for name, workflow in workflows.items():
        match = next((lbl for lbl in workflow.labels if lbl.lower() in labels), None)
        if match is not None:
            return make(name, f"label: {match}")
    return make(default, "default")
