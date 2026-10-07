"""The team: each step's role, contract, tool policy and default timeout.

| Role          | Steps                   | Session                         |
|---------------|-------------------------|---------------------------------|
| Product Owner | requirements, po_answer | kept for the task               |
| UX            | design                  | kept for the task               |
| Planner       | plan (+replan)          | kept for the task               |
| Engineer      | implement (+fix)        | kept for the task               |
| QA            | qa                      | kept across retests             |
| Reviewer      | review                  | **fresh every time**            |

A session only carries a role's memory of its *own* work. Everything one
role hands another — requirements, design, plan, findings — goes through
state and the context file, so each hand-off is visible and replaceable.
"""

from __future__ import annotations

from devloop.agents.harness import StepSpec
from devloop.contracts.artifacts import (
    DesignResult,
    EnvRecipe,
    ImplementationResult,
    PlanResult,
    POAnswer,
    QAResult,
    RequirementResult,
    ReviewResult,
)
from devloop.contracts.runs import Role, Step
from devloop.runtimes.base import READ_ONLY_SHELL, McpServer, ToolPolicy

_READ_ONLY = ToolPolicy(can_edit=False, bash_allow=READ_ONLY_SHELL)

# Roles whose agent session is resumed across steps.
SESSION_ROLES = frozenset({Role.PRODUCT_OWNER, Role.UX, Role.PLANNER, Role.ENGINEER, Role.QA})

REQUIREMENTS = StepSpec(
    Role.PRODUCT_OWNER, Step.REQUIREMENTS, RequirementResult, _READ_ONLY, timeout_s=600
)
PO_ANSWER = StepSpec(Role.PRODUCT_OWNER, Step.PO_ANSWER, POAnswer, _READ_ONLY, timeout_s=600)
# UX may save design files (e.g. exported from Stitch) under `.devloop/out/`;
# its design tools come from the role's `mcp_servers` config.
DESIGN = StepSpec(Role.UX, Step.DESIGN, DesignResult, _READ_ONLY, timeout_s=20 * 60)
# Read-only: the plan is checked before a single file changes, and the
# Engineer builds from the checked plan — not from the Planner's session.
PLAN = StepSpec(Role.PLANNER, Step.PLAN, PlanResult, _READ_ONLY, timeout_s=900)
REVIEW = StepSpec(Role.REVIEWER, Step.REVIEW, ReviewResult, _READ_ONLY, timeout_s=900)


def implement(env: EnvRecipe | None) -> StepSpec[ImplementationResult]:
    """The only step that edits code. Its shell is limited to read-only
    commands plus the repo's own setup/check commands — enough to run the
    tests for its own feedback, not enough to commit, push or fetch."""

    commands = (*env.setup, *env.commands.values()) if env else ()
    return StepSpec(
        Role.ENGINEER,
        Step.IMPLEMENT,
        ImplementationResult,
        ToolPolicy(can_edit=True, bash_allow=(*READ_ONLY_SHELL, *commands)),
        read_only=False,
        timeout_s=30 * 60,
    )


def playwright_server(output_dir: str) -> McpServer:
    return McpServer(
        name="playwright",
        command=(
            "npx",
            "-y",
            "@playwright/mcp@latest",
            "--headless",
            "--isolated",
            "--output-dir",
            output_dir,
        ),
    )


def qa(output_dir: str) -> StepSpec[QAResult]:
    """Black-box testing through a real browser against the app the
    orchestrator started. Read-only: QA reports bugs, it doesn't fix them."""

    return StepSpec(
        Role.QA,
        Step.QA,
        QAResult,
        ToolPolicy(
            can_edit=False,
            bash_allow=(*READ_ONLY_SHELL, "curl"),
            mcp_servers=(playwright_server(output_dir),),
        ),
        timeout_s=20 * 60,
    )
