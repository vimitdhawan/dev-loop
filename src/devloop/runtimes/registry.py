from __future__ import annotations

from collections.abc import Callable

from devloop.errors import AgentError
from devloop.runtimes.base import CodingAgent
from devloop.runtimes.claude_cli import ClaudeCLIAgent
from devloop.runtimes.codex_cli import CodexCLIAgent
from devloop.runtimes.opencode_cli import OpenCodeCLIAgent
from devloop.runtimes.stub import StubAgent

_FACTORIES: dict[str, Callable[[], CodingAgent]] = {
    "claude": ClaudeCLIAgent,
    "opencode": OpenCodeCLIAgent,
    "codex": CodexCLIAgent,
    "stub": StubAgent,
}
# One instance per runtime per process, so stateful runtimes (a scripted
# stub) keep their state across nodes.
_INSTANCES: dict[str, CodingAgent] = {}


def register_runtime(name: str, agent: CodingAgent) -> None:
    _INSTANCES[name] = agent


def get_runtime(name: str) -> CodingAgent:
    if name not in _INSTANCES:
        factory = _FACTORIES.get(name)
        if factory is None:
            raise AgentError(f"unknown runtime {name!r}; known: {', '.join(sorted(_FACTORIES))}")
        _INSTANCES[name] = factory()
    return _INSTANCES[name]


def known_runtimes() -> list[str]:
    return sorted(_FACTORIES)
