"""The agent team, and records about *how* the work was done.

`TeamConfig` says which runtime/model runs each role; it is stored in graph
state so `devloop resume` — a separate process — runs the same team the
task started with.

`AgentRunRecord` is the raw material for the improvement engine: every
agent execution — including failed and repair attempts — with the prompt
version that drove it and what it cost. It is never read by `decide()`
except through the aggregated `cost_usd`.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Config models reject unknown keys: a typo like `enginer:` must fail loudly,
# not silently run the default team.
_STRICT = ConfigDict(extra="forbid")


class Role(StrEnum):
    """Who does the work. Each role has one config entry and, except the
    Reviewer, one long-lived agent session per task. Declaration order is
    the order a workflow runs its stages in."""

    PRODUCT_OWNER = "product_owner"
    UX = "ux"
    PLANNER = "planner"
    ENGINEER = "engineer"
    QA = "qa"
    REVIEWER = "reviewer"


class Step(StrEnum):
    """What a role is asked to do — one prompt and one contract each. Values
    name the prompt directory, so they never change once used (the Planner
    still runs `engineer_plan`: the engineering plan)."""

    REQUIREMENTS = "product_owner"
    PO_ANSWER = "po_answer"
    DESIGN = "ux_design"
    PLAN = "engineer_plan"
    IMPLEMENT = "engineer_implement"
    QA = "qa"
    REVIEW = "reviewer"


class McpServerConfig(BaseModel):
    """An extra MCP server for one role — e.g. Stitch for UX. Either a local
    `command` (stdio) or a remote `url` (streamable HTTP). Header values may
    reference environment variables as `${NAME}`, so secrets stay out of
    the config file; they're resolved only when the agent is launched."""

    model_config = _STRICT

    command: list[str] | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_transport(self) -> McpServerConfig:
        if (self.command is None) == (self.url is None):
            raise ValueError("an MCP server needs exactly one of `command` or `url`")
        if self.command is not None and not self.command:
            raise ValueError("`command` must not be empty")
        return self


class AgentConfig(BaseModel):
    model_config = _STRICT

    runtime: str = "claude"
    model: str | None = None
    # None: the step's default (10-30 min). Slow models need far longer.
    timeout_s: int | None = Field(default=None, ge=60)
    # name -> server, added to the role's own tools (QA always has Playwright)
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)


# A role left out of the config runs exactly as this one (resolved at use,
# so `--role-model engineer=opus` also reaches an unconfigured planner) —
# and a config written before the role existed keeps working.
ROLE_FALLBACK: dict[Role, Role] = {
    Role.PLANNER: Role.ENGINEER,
    Role.UX: Role.PRODUCT_OWNER,
}


class TeamConfig(BaseModel):
    model_config = _STRICT

    product_owner: AgentConfig = Field(default_factory=AgentConfig)
    ux: AgentConfig | None = None
    planner: AgentConfig | None = None
    engineer: AgentConfig = Field(default_factory=AgentConfig)
    qa: AgentConfig = Field(default_factory=AgentConfig)
    reviewer: AgentConfig = Field(default_factory=AgentConfig)

    def for_role(self, role: Role) -> AgentConfig:
        config: AgentConfig | None = getattr(self, role.value)
        if config is None:
            return self.for_role(ROLE_FALLBACK[role])
        return config

    def own(self, role: Role) -> AgentConfig:
        """The role's config, detached from its fallback first — for
        overriding one role without changing another."""

        if getattr(self, role.value) is None:
            setattr(self, role.value, self.for_role(role).model_copy(deep=True))
        return self.for_role(role)


class Workflow(BaseModel):
    """The stages one task goes through, chosen when it starts. Planner,
    Engineer and Reviewer are always in it; Product Owner, UX and QA are
    optional. Stored in state, so `decide()` routes on it as a fact."""

    name: str
    stages: list[Role]
    # how it was chosen, e.g. "--workflow bug", "label: bug", "default"
    selected_by: str = "default"

    def has(self, role: Role) -> bool:
        return role in self.stages


class PullRequestConfig(BaseModel):
    model_config = _STRICT

    enabled: bool = True
    draft: bool = False


class AgentRunRecord(BaseModel):
    task_id: str
    role: Role
    step: Step
    runtime: str
    model: str | None = None
    prompt_version: str
    iteration: int
    attempt: int  # 1 = first try, 2 = contract-repair retry
    ok: bool  # the CLI exited cleanly *and* the contract validated
    error: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    session_id: str = ""
    resumed: bool = False  # continued an existing session rather than starting fresh
    log_path: str = ""
    # exact input, prompt and output of this run: `context.json`, `prompt.md`,
    # `output.json` — what one agent handed the next, replayable for evals
    run_dir: str = ""
