"""`codex exec` adapter — not implemented yet.

Highest value later as the cross-check Reviewer (Claude implements, Codex
reviews). Registered so `--runtime codex` fails with a clear message
instead of an unknown-runtime error.
"""

from __future__ import annotations

from devloop.errors import AgentError
from devloop.runtimes.base import AgentInvocation, AgentOutcome


class CodexCLIAgent:
    name = "codex"

    def run(self, invocation: AgentInvocation) -> AgentOutcome:
        raise AgentError("the codex runtime is not implemented yet; use claude or opencode")
