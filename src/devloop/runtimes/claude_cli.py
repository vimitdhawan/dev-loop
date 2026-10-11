"""`claude -p` adapter.

Isolation choices, all deliberate:

- `--permission-mode dontAsk` + an explicit allowlist: anything not listed
  is denied rather than prompting a human who isn't there.
- Read-only roles get `Edit(./.devloop/out/**)` — they can write their
  contract document and nothing else. (A path-scoped `Edit(...)` rule
  covers every file-writing tool; a bare `Edit` rule does not cover
  `Write`, so editing roles get both.)
- `--setting-sources ""` and `--strict-mcp-config`: the target repo's
  `.claude/settings.json` (which can define hooks — arbitrary commands) and
  the operator's personal settings, plugins and MCP servers are not loaded.
  Runs are reproducible across machines, which the eval loop depends on.
  The repo's `CLAUDE.md` still loads: its conventions are wanted.
- `--max-budget-usd` caps a single run at what's left of the task budget.
- Sessions persist so a role can be resumed with `--resume <id>`; MCP
  servers (Playwright for QA) come only from the per-run `--mcp-config`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from devloop.runtimes.base import (
    OUT_DIR,
    AgentInvocation,
    AgentOutcome,
    run_process,
)


class ClaudeCLIAgent:
    name = "claude"

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or os.environ.get("DEVLOOP_CLAUDE_BIN", "claude")

    def build_argv(self, inv: AgentInvocation) -> list[str]:
        tools = ["Read", "Grep", "Glob", "Write", "Edit"]
        allowed = ["Read", "Grep", "Glob"]
        if inv.tools.can_edit:
            allowed += ["Edit", "Write"]
        else:
            allowed.append(f"Edit(./{OUT_DIR}/**)")
        if inv.tools.bash_allow:
            tools.append("Bash")
            allowed.extend(f"Bash({prefix}*)" for prefix in inv.tools.bash_allow)
        allowed.extend(f"mcp__{server.name}" for server in inv.tools.mcp_servers)

        argv = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--tools",
            *tools,
            "--allowedTools",
            *allowed,
        ]
        if inv.tools.mcp_servers:
            argv += ["--mcp-config", str(self.write_mcp_config(inv))]
        if inv.resume_session:
            argv += ["--resume", inv.resume_session]
        if inv.model:
            argv += ["--model", inv.model]
        if inv.max_budget_usd is not None:
            argv += ["--max-budget-usd", f"{inv.max_budget_usd:.2f}"]
        return argv

    def mcp_config_path(self, inv: AgentInvocation) -> Path:
        return inv.log_path.with_suffix(".mcp.json")

    def write_mcp_config(self, inv: AgentInvocation) -> Path:
        path = self.mcp_config_path(inv)
        path.parent.mkdir(parents=True, exist_ok=True)
        servers: dict[str, dict[str, object]] = {}
        for s in inv.tools.mcp_servers:
            if s.url:
                servers[s.name] = {"type": "http", "url": s.url, "headers": dict(s.headers)}
            else:
                servers[s.name] = {"command": s.command[0], "args": list(s.command[1:])}
        # owner-only: header values can be credentials
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"mcpServers": servers}, indent=2))
        return path

    def run(self, invocation: AgentInvocation) -> AgentOutcome:
        try:
            code, out, err = run_process(
                self.build_argv(invocation),
                stdin=invocation.prompt,  # stdin, not argv: no length limit, no flag parsing
                cwd=invocation.workdir,
                timeout_s=invocation.timeout_s,
                log_path=invocation.log_path,
            )
        finally:
            if any(s.headers for s in invocation.tools.mcp_servers):
                # don't leave credentials next to the logs
                self.mcp_config_path(invocation).unlink(missing_ok=True)
        return parse_result(code, out, err)


def parse_result(code: int, stdout: str, stderr: str) -> AgentOutcome:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return AgentOutcome(ok=False, error=f"exit {code}: {(stderr or stdout).strip()[-500:]}")

    usage = data.get("usage") or {}
    outcome = AgentOutcome(
        ok=code == 0 and not data.get("is_error", False),
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        input_tokens=int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        duration_ms=int(data.get("duration_ms") or 0),
        session_id=str(data.get("session_id") or ""),
        extra={
            "num_turns": data.get("num_turns"),
            "permission_denials": len(data.get("permission_denials") or []),
        },
    )
    if not outcome.ok:
        outcome.error = f"exit {code}, {data.get('subtype')}: {str(data.get('result'))[-500:]}"
    return outcome
