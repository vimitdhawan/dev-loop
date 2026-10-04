"""Records about *how* the work was done, as opposed to the work itself.

`AgentRunRecord` is the raw material for the improvement engine: every
agent execution — including failed and repair attempts — with the prompt
version that drove it and what it cost. It is never read by `decide()`
except through the aggregated `cost_usd`.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Role(StrEnum):
    REQUIREMENT = "requirement"
    PLANNER = "planner"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"


class RuntimeConfig(BaseModel):
    """Which agent CLI (and model) runs each role. Stored in graph state so
    `devloop resume` — a separate process — runs the same runtimes the task
    started with."""

    default: str = "claude"
    model: str | None = None
    # per-role overrides, e.g. {"reviewer": "opencode"} for a cross-check
    roles: dict[Role, str] = Field(default_factory=dict)
    # Overrides every role's default timeout — slow (e.g. free-tier) models
    # need far longer than the defaults.
    agent_timeout_s: int | None = None

    def runtime_for(self, role: Role) -> str:
        return self.roles.get(role, self.default)


class AgentRunRecord(BaseModel):
    task_id: str
    role: Role
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
    log_path: str = ""
