from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from devloop.runtimes.base import IN_DIR, OUT_DIR, AgentInvocation, AgentOutcome
from devloop.runtimes.opencode_cli import OpenCodeCLIAgent
from devloop.runtimes.probe import probe
from devloop.runtimes.registry import register_runtime


class Follows:
    """Does what the probe asks: copies the secret word into the output."""

    name = "follows"

    def run(self, inv: AgentInvocation) -> AgentOutcome:
        word = json.loads((inv.workdir / IN_DIR / "context.json").read_text())["secret_word"]
        (inv.workdir / OUT_DIR / "probe.json").write_text(json.dumps({"word": word}))
        return AgentOutcome(ok=True, cost_usd=0.01)


class Chats:
    """Answers in prose and never calls a tool — the classic weak model."""

    name = "chats"

    def run(self, inv: AgentInvocation) -> AgentOutcome:
        return AgentOutcome(ok=True)


class Guesses:
    name = "guesses"

    def run(self, inv: AgentInvocation) -> AgentOutcome:
        (inv.workdir / OUT_DIR / "probe.json").write_text('{"word": "pineapple"}')
        return AgentOutcome(ok=True)


@pytest.mark.parametrize(
    "agent,ok,error",
    [
        (Follows(), True, ""),
        (Chats(), False, "wrote no output file"),
        (Guesses(), False, "not the word from the context file"),
    ],
)
def test_probe(agent: object, ok: bool, error: str) -> None:
    register_runtime(agent.name, agent)  # type: ignore[attr-defined, arg-type]

    result = probe(agent.name, "some-model")  # type: ignore[attr-defined]

    assert result.ok is ok
    assert error in result.error


@pytest.fixture
def fake_opencode(tmp_path: Path) -> OpenCodeCLIAgent:
    script = tmp_path / "opencode"
    script.write_text(
        "#!/bin/sh\n"
        'echo "nvidia/meta/muse-glimmer-30b"\n'
        'echo "nvidia/z-ai/glm-5.3"\n'
        'echo "opencode/muse-spark-1.3-contributor-free"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return OpenCodeCLIAgent(str(script))


def test_opencode_model_check_accepts_known_ids(fake_opencode: OpenCodeCLIAgent) -> None:
    assert fake_opencode.check_model("nvidia/z-ai/glm-5.3") is None


def test_opencode_model_check_suggests_the_missing_provider(
    fake_opencode: OpenCodeCLIAgent,
) -> None:
    problem = fake_opencode.check_model("meta/muse-glimmer-30b")

    assert problem is not None
    assert "provider/model" in problem
    assert "did you mean nvidia/meta/muse-glimmer-30b" in problem


def test_unknown_model_fails_the_probe_without_a_run(fake_opencode: OpenCodeCLIAgent) -> None:
    register_runtime("opencode", fake_opencode)

    result = probe("opencode", "meta/muse-glimmer-30b")

    assert not result.ok and result.seconds == 0.0
