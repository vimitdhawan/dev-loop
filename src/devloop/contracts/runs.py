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

from pydantic import BaseModel, ConfigDict, Field

# Config models reject unknown keys: a typo like `enginer:` must fail loudly,
# not silently run the default team.
_STRICT = ConfigDict(extra="forbid")


class Role(StrEnum):
    """Who does the work. Each role has one config entry and, except the
    Reviewer, one long-lived agent session per task."""

    PRODUCT_OWNER = "product_owner"
    ENGINEER = "engineer"
    QA = "qa"
    REVIEWER = "reviewer"


class Step(StrEnum):
    """What a role is asked to do — one prompt and one contract each. The
    Engineer plans and implements in the same session, as two steps with
    the deterministic plan check between them."""

    REQUIREMENTS = "product_owner"
    PO_ANSWER = "po_answer"
    PLAN = "engineer_plan"
    IMPLEMENT = "engineer_implement"
    QA = "qa"
    REVIEW = "reviewer"


class AgentConfig(BaseModel):
    model_config = _STRICT

    runtime: str = "claude"
    model: str | None = None
    # None: the step's default (10-30 min). Slow models need far longer.
    timeout_s: int | None = Field(default=None, ge=60)


class TeamConfig(BaseModel):
    model_config = _STRICT

    product_owner: AgentConfig = Field(default_factory=AgentConfig)
    engineer: AgentConfig = Field(default_factory=AgentConfig)
    qa: AgentConfig = Field(default_factory=AgentConfig)
    reviewer: AgentConfig = Field(default_factory=AgentConfig)

    def for_role(self, role: Role) -> AgentConfig:
        config: AgentConfig = getattr(self, role.value)
        return config


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
