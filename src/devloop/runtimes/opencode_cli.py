"""`opencode run` adapter.

Permissions are passed as inline config (`OPENCODE_CONFIG_CONTENT`) with
every rule set to `allow` or `deny` — never `ask`, since nobody is there
to answer. opencode's edit permission is not path-scoped here, so a
read-only role *can* technically edit; the harness's post-run dirty check
is what enforces read-only for this runtime.

`--format json` emits one JSON event per line; cost and tokens are summed
from `step_finish` events.
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess

from devloop.runtimes.base import AgentInvocation, AgentOutcome, run_process


class OpenCodeCLIAgent:
    name = "opencode"

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or os.environ.get("DEVLOOP_OPENCODE_BIN", "opencode")

    def build_argv(self, inv: AgentInvocation) -> list[str]:
        argv = [self.binary, "run", "--format", "json", "--dir", str(inv.workdir)]
        if inv.resume_session:
            argv += ["--session", inv.resume_session]
        if inv.model:
            argv += ["--model", inv.model]
        return argv

    def permission_config(self, inv: AgentInvocation) -> dict[str, object]:
        bash: dict[str, str] = {"*": "deny"}
        bash.update({f"{prefix}*": "allow" for prefix in inv.tools.bash_allow})
        config: dict[str, object] = {
            "permission": {"edit": "allow", "bash": bash, "webfetch": "deny"},
        }
        if inv.tools.mcp_servers:
            # passed via the environment, never written to disk
            config["mcp"] = {
                s.name: (
                    {"type": "remote", "url": s.url, "headers": dict(s.headers), "enabled": True}
                    if s.url
                    else {"type": "local", "command": list(s.command), "enabled": True}
                )
                for s in inv.tools.mcp_servers
            }
        return config

    def check_model(self, model: str) -> str | None:
        """None if opencode knows `model`, else what's wrong. opencode wants
        `provider/model`; given `meta/x` for `nvidia/meta/x` it reads `meta`
        as the provider and fails mid-run with an opaque server error."""

        try:
            proc = subprocess.run(
                [self.binary, "models"], capture_output=True, text=True, timeout=120
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return f"could not list opencode models: {exc}"
        known = [line.strip() for line in proc.stdout.splitlines() if "/" in line]
        if model in known:
            return None
        hints = [m for m in known if m.endswith("/" + model)] or difflib.get_close_matches(
            model, known, n=3
        )
        hint = f" — did you mean {', '.join(hints)}?" if hints else ""
        return f"opencode has no model {model!r} (ids are provider/model){hint}"

    def run(self, invocation: AgentInvocation) -> AgentOutcome:
        env = {
            **os.environ,
            "OPENCODE_CONFIG_CONTENT": json.dumps(self.permission_config(invocation)),
        }
        code, out, err = run_process(
            self.build_argv(invocation),
            stdin=invocation.prompt,
            cwd=invocation.workdir,
            timeout_s=invocation.timeout_s,
            log_path=invocation.log_path,
            env=env,
        )
        return parse_events(code, out, err)


def parse_events(code: int, stdout: str, stderr: str) -> AgentOutcome:
    outcome = AgentOutcome(ok=code == 0)
    errors: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        outcome.session_id = outcome.session_id or str(event.get("sessionID") or "")
        if event.get("type") == "error":
            errors.append(json.dumps(event.get("error"))[:500])
        if event.get("type") == "step_finish":
            part = event.get("part") or {}
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            outcome.cost_usd += float(part.get("cost") or 0.0)
            outcome.input_tokens += int(tokens.get("input") or 0) + int(cache.get("read") or 0)
            outcome.output_tokens += int(tokens.get("output") or 0)

    if errors:
        outcome.ok = False
    if not outcome.ok:
        outcome.error = "; ".join(errors) or f"exit {code}: {stderr.strip()[-500:]}"
    return outcome
