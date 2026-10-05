from __future__ import annotations

import json
from pathlib import Path

from devloop.agents import roles
from devloop.contracts.artifacts import EnvRecipe
from devloop.contracts.runs import Role
from devloop.runtimes.base import AgentInvocation, ToolPolicy
from devloop.runtimes.claude_cli import ClaudeCLIAgent, parse_result
from devloop.runtimes.opencode_cli import OpenCodeCLIAgent, parse_events


def invocation(tools: ToolPolicy, **kw: object) -> AgentInvocation:
    return AgentInvocation(
        role=Role.REVIEWER,
        prompt="p",
        workdir=Path("/ws"),
        tools=tools,
        log_path=Path("/log"),
        **kw,  # type: ignore[arg-type]
    )


def test_claude_read_only_role_can_only_write_its_output() -> None:
    argv = ClaudeCLIAgent("claude").build_argv(
        invocation(roles.REVIEWER.tools, model="sonnet", max_budget_usd=1.234)
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
    argv = ClaudeCLIAgent("claude").build_argv(invocation(roles.developer(env).tools))

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
