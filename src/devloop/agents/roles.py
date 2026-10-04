"""Per-role contract, tool policy and timeout."""

from __future__ import annotations

from devloop.agents.harness import RoleSpec
from devloop.contracts.artifacts import (
    EnvRecipe,
    ImplementationResult,
    PlanResult,
    RequirementResult,
    ReviewResult,
)
from devloop.contracts.runs import Role
from devloop.runtimes.base import READ_ONLY_SHELL, ToolPolicy

_READ_ONLY = ToolPolicy(can_edit=False, bash_allow=READ_ONLY_SHELL)

REQUIREMENT = RoleSpec(Role.REQUIREMENT, RequirementResult, _READ_ONLY, timeout_s=600)
PLANNER = RoleSpec(Role.PLANNER, PlanResult, _READ_ONLY, timeout_s=900)
REVIEWER = RoleSpec(Role.REVIEWER, ReviewResult, _READ_ONLY, timeout_s=900)


def developer(env: EnvRecipe | None) -> RoleSpec[ImplementationResult]:
    """The only role that edits code. Its shell is limited to read-only
    commands plus the repo's own setup/check commands — enough to run the
    tests for its own feedback, not enough to commit, push or fetch."""

    commands = (*env.setup, *env.commands.values()) if env else ()
    return RoleSpec(
        Role.DEVELOPER,
        ImplementationResult,
        ToolPolicy(can_edit=True, bash_allow=(*READ_ONLY_SHELL, *commands)),
        read_only=False,
        timeout_s=30 * 60,
    )
