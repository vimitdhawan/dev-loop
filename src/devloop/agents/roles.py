"""The team: each step's role, contract, tool policy and default timeout.

| Role          | Steps                   | Session                         |
|---------------|-------------------------|---------------------------------|
| Product Owner | requirements, po_answer | kept for the task               |
| Engineer      | plan, implement (+fix)  | kept for the task               |
| QA            | qa                      | kept across retests             |
| Reviewer      | review                  | **fresh every time**            |
"""

from __future__ import annotations

from devloop.agents.harness import StepSpec
from devloop.contracts.artifacts import (
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
SESSION_ROLES = frozenset({Role.PRODUCT_OWNER, Role.ENGINEER, Role.QA})

REQUIREMENTS = StepSpec(
    Role.PRODUCT_OWNER, Step.REQUIREMENTS, RequirementResult, _READ_ONLY, timeout_s=600
)
PO_ANSWER = StepSpec(Role.PRODUCT_OWNER, Step.PO_ANSWER, POAnswer, _READ_ONLY, timeout_s=600)
# Planning is read-only even though the same session implements next: the
# plan is checked before a single file changes.
PLAN = StepSpec(Role.ENGINEER, Step.PLAN, PlanResult, _READ_ONLY, timeout_s=900)
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
