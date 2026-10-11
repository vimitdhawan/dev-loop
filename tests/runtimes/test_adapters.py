from __future__ import annotations

import json
from pathlib import Path

import pytest

from devloop.agents import roles
from devloop.contracts.artifacts import EnvRecipe
from devloop.contracts.runs import McpServerConfig, Role, Step
from devloop.errors import AgentError
from devloop.runtimes.base import AgentInvocation, McpServer, ToolPolicy, resolve_mcp
from devloop.runtimes.claude_cli import ClaudeCLIAgent, parse_result
from devloop.runtimes.opencode_cli import OpenCodeCLIAgent, parse_events


def invocation(tools: ToolPolicy, **kw: object) -> AgentInvocation:
    return AgentInvocation(
        role=Role.REVIEWER,
        step=Step.REVIEW,
        prompt="p",
        workdir=Path("/ws"),
        tools=tools,
        log_path=Path("/log"),
        **kw,  # type: ignore[arg-type]
    )


def test_claude_read_only_role_can_only_write_its_output() -> None:
    argv = ClaudeCLIAgent("claude").build_argv(
        invocation(roles.REVIEW.tools, model="sonnet", max_budget_usd=1.234)
    )

    allowed = argv[argv.index("--allowedTools") + 1 :]
    assert "Edit(./.devloop/out/**)" in allowed
    assert "Edit" not in allowed and "Write" not in allowed
    assert "Bash(git diff*)" in allowed
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--model" in argv and "--max-budget-usd" in argv
    assert argv[argv.index("--max-budget-usd") + 1] == "1.23"
    assert "p" not in argv, "prompt goes via stdin"


def test_claude_developer_can_edit_and_run_repo_commands() -> None:
    env = EnvRecipe(
        image="host", setup=["uv sync"], commands={"unit": "uv run pytest"}, verified_at_commit="x"
    )
    argv = ClaudeCLIAgent("claude").build_argv(invocation(roles.implement(env).tools))

    allowed = argv[argv.index("--allowedTools") + 1 :]
    assert "Edit" in allowed and "Write" in allowed
    assert "Bash(uv run pytest*)" in allowed and "Bash(uv sync*)" in allowed
    assert not any("git push" in a or "git commit" in a for a in allowed)


def test_claude_parse_success() -> None:
    out = json.dumps(
        {
            "is_error": False,
            "total_cost_usd": 0.42,
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 5,
                "output_tokens": 7,
            },
            "duration_ms": 1500,
            "session_id": "s1",
        }
    )
    outcome = parse_result(0, out, "")
    assert outcome.ok and outcome.cost_usd == 0.42
    assert (outcome.input_tokens, outcome.output_tokens) == (115, 7)


def test_claude_parse_error_keeps_cost() -> None:
    out = json.dumps(
        {"is_error": True, "subtype": "error_max_budget_usd", "total_cost_usd": 2.0, "result": ""}
    )
    outcome = parse_result(1, out, "")
    assert not outcome.ok and outcome.cost_usd == 2.0
    assert "error_max_budget_usd" in outcome.error


def test_claude_parse_garbage() -> None:
    outcome = parse_result(127, "", "claude: not found")
    assert not outcome.ok and "not found" in outcome.error


def test_opencode_sums_step_costs_and_flags_errors() -> None:
    events = [
        {"type": "step_start", "sessionID": "ses_1"},
        {"type": "step_finish", "part": {"cost": 0.1, "tokens": {"input": 5, "output": 2}}},
        {
            "type": "step_finish",
            "part": {"cost": 0.2, "tokens": {"input": 1, "output": 1, "cache": {"read": 4}}},
        },
    ]
    out = "\n".join(json.dumps(e) for e in events) + "\nnot json\n"

    ok = parse_events(0, out, "")
    assert ok.ok and round(ok.cost_usd, 2) == 0.3
    assert (ok.input_tokens, ok.output_tokens, ok.session_id) == (10, 3, "ses_1")

    failed = parse_events(0, out + json.dumps({"type": "error", "error": {"m": "x"}}), "")
    assert not failed.ok


def test_opencode_permissions_never_ask() -> None:
    config = OpenCodeCLIAgent("opencode").permission_config(
        invocation(ToolPolicy(can_edit=True, bash_allow=("go test",)))
    )
    perms = config["permission"]
    assert isinstance(perms, dict)
    assert perms["bash"] == {"*": "deny", "go test*": "allow"}
    assert "ask" not in json.dumps(config)


def test_run_process_streams_to_log_and_kills_on_timeout(tmp_path: Path) -> None:
    from devloop.runtimes.base import run_process

    log_path = tmp_path / "agent.log"
    code, out, err = run_process(
        ["sh", "-c", "cat; echo started; sleep 30"],
        stdin="prompt-text\n",
        cwd=tmp_path,
        timeout_s=1,
        log_path=log_path,
    )

    assert code == -1 and "timed out after 1s" in err
    assert "prompt-text" in out and "started" in out
    assert log_path.read_text() == out


def test_run_process_reports_missing_binary(tmp_path: Path) -> None:
    from devloop.runtimes.base import run_process

    code, _, err = run_process(
        ["devloop-no-such-cli"], stdin="", cwd=tmp_path, timeout_s=5, log_path=tmp_path / "x.log"
    )
    assert code == -1 and "command not found" in err


def test_claude_resume_and_mcp(tmp_path: Path) -> None:
    inv = AgentInvocation(
        role=Role.QA,
        step=Step.QA,
        prompt="p",
        workdir=tmp_path,
        tools=roles.qa(str(tmp_path / "shots")).tools,
        log_path=tmp_path / "logs" / "qa.log",
        resume_session="sess-1",
    )
    argv = ClaudeCLIAgent("claude").build_argv(inv)

    assert argv[argv.index("--resume") + 1] == "sess-1"
    assert "--no-session-persistence" not in argv
    assert "mcp__playwright" in argv[argv.index("--allowedTools") + 1 :]
    config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())
    server = config["mcpServers"]["playwright"]
    assert server["command"] == "npx" and "--headless" in server["args"]
    assert str(tmp_path / "shots") in server["args"]


def test_opencode_resume_and_mcp(tmp_path: Path) -> None:
    inv = invocation(
        ToolPolicy(mcp_servers=(McpServer("playwright", ("npx", "pw")),)),
        resume_session="ses_1",
    )
    agent = OpenCodeCLIAgent("opencode")

    argv = agent.build_argv(inv)
    config = agent.permission_config(inv)

    assert argv[argv.index("--session") + 1] == "ses_1"
    assert config["mcp"] == {
        "playwright": {"type": "local", "command": ["npx", "pw"], "enabled": True}
    }


# --- remote MCP servers (e.g. Stitch for UX) --------------------------------

STITCH = McpServer(
    "stitch", url="https://stitch.example/mcp", headers=(("X-Goog-Api-Key", "secret-1"),)
)


def test_resolve_mcp_reads_header_secrets_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = McpServerConfig(url="https://s/mcp", headers={"X-Key": "Bearer ${STITCH_KEY}"})

    monkeypatch.delenv("STITCH_KEY", raising=False)
    with pytest.raises(AgentError, match="STITCH_KEY"):
        resolve_mcp("stitch", config)

    monkeypatch.setenv("STITCH_KEY", "abc")
    server = resolve_mcp("stitch", config)
    assert server.headers == (("X-Key", "Bearer abc"),)
    assert config.headers["X-Key"] == "Bearer ${STITCH_KEY}", "config keeps the reference"


def test_claude_remote_mcp_config_is_private_and_removed_after_the_run(tmp_path: Path) -> None:
    fake = tmp_path / "claude"
    seen = tmp_path / "seen.json"
    # the fake CLI copies the MCP config it was given, then reports success
    fake.write_text(
        "#!/bin/sh\n"
        'while [ "$1" != "--mcp-config" ]; do shift; done\n'
        f'cp "$2" {seen}\n'
        """echo '{"is_error": false, "total_cost_usd": 0, "session_id": "s"}'\n"""
    )
    fake.chmod(0o755)
    inv = AgentInvocation(
        role=Role.UX,
        step=Step.DESIGN,
        prompt="p",
        workdir=tmp_path,
        tools=ToolPolicy(mcp_servers=(STITCH,)),
        log_path=tmp_path / "logs" / "ux.log",
    )
    agent = ClaudeCLIAgent(str(fake))

    assert "mcp__stitch" in agent.build_argv(inv)
    assert oct(agent.write_mcp_config(inv).stat().st_mode & 0o777) == "0o600"
    outcome = agent.run(inv)

    assert outcome.ok
    server = json.loads(seen.read_text())["mcpServers"]["stitch"]
    assert server == {
        "type": "http",
        "url": "https://stitch.example/mcp",
        "headers": {"X-Goog-Api-Key": "secret-1"},
    }
    assert not agent.mcp_config_path(inv).exists(), "no credentials left next to the logs"


def test_opencode_remote_mcp_goes_through_the_environment_only() -> None:
    config = OpenCodeCLIAgent("opencode").permission_config(
        invocation(ToolPolicy(mcp_servers=(STITCH,)))
    )

    assert config["mcp"] == {
        "stitch": {
            "type": "remote",
            "url": "https://stitch.example/mcp",
            "headers": {"X-Goog-Api-Key": "secret-1"},
            "enabled": True,
        }
    }
