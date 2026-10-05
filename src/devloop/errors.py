"""Exceptions a node turns into `escalation_reason` instead of crashing the
graph. Anything else is a bug in DevLoop and is allowed to propagate."""

from __future__ import annotations


class DevLoopError(RuntimeError):
    pass


class GitError(DevLoopError):
    pass


class EnvError(DevLoopError):
    pass


class AgentError(DevLoopError):
    """The agent CLI could not be run, crashed, or broke a role rule
    (e.g. a read-only role modified the workspace)."""


class ContractError(DevLoopError):
    """The agent ran but its output file is missing or invalid even after
    the one repair retry."""
