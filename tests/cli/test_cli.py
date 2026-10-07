"""The CLI end to end with the stub team: what a person actually sees."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devloop.cli.main import app

runner = CliRunner()


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_repo: Path) -> Path:
    work = tmp_path / "cwd"
    work.mkdir()
    (work / "task.md").write_text("Add a notes file.\n")
    monkeypatch.chdir(work)
    monkeypatch.delenv("DEVLOOP_CONFIG", raising=False)
    monkeypatch.setenv("COLUMNS", "200")
    return work


def start(target_repo: Path, *args: str) -> tuple[str, str]:
    result = runner.invoke(
        app, ["run", "--task", "task.md", "--repo", str(target_repo), "--runtime", "stub", *args]
    )
    assert result.exit_code == 0, result.output
    match = re.search(r"task (\w{8}):", result.output)
    assert match, result.output
    return match.group(1), result.output


def test_run_prints_each_agents_hand_off_as_it_lands(cwd: Path, target_repo: Path) -> None:
    _, out = start(target_repo)

    assert "workflow: feature (default)" in out
    for heading in (
        "Product Owner → requirements",
        "Planner → plan (handed to the Engineer)",
        "Engineer → implementation",
        "checks (post_change, run by DevLoop)",
        "QA → skipped",
        "Reviewer → approved",
    ):
        assert heading in out
    assert out.index("Planner → plan") < out.index("Engineer → implementation")
    assert "waiting on a human" in out


def test_bug_workflow_from_a_label_and_quiet_mode(cwd: Path, target_repo: Path) -> None:
    _, out = start(target_repo, "--label", "bug", "-q")

    assert "workflow: bug (label: bug): planner → engineer → qa → reviewer" in out
    assert "Product Owner" not in out and "Planner → plan" not in out
    assert "-> PLANNING" in out


def test_unknown_workflow_is_rejected_before_anything_runs(cwd: Path, target_repo: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--task",
            "task.md",
            "--repo",
            str(target_repo),
            "--runtime",
            "stub",
            "--workflow",
            "nope",
        ],
    )
    assert result.exit_code != 0 and "unknown workflow" in result.output


def test_show_reads_back_artifacts_runs_and_exact_hand_offs(cwd: Path, target_repo: Path) -> None:
    task_id, _ = start(target_repo, "--workflow", "ui_feature")

    shown = runner.invoke(app, ["show", task_id, "--events"])
    assert shown.exit_code == 0, shown.output
    assert "UX → design" in shown.output and "timeline" in shown.output
    assert "ux_design" in shown.output and "engineer_implement" in shown.output

    plan = json.loads(runner.invoke(app, ["show", task_id, "--json", "plan"]).output)
    assert plan["files_to_change"][0]["path"] == "DEVLOOP_NOTES.md"

    implement_run = runner.invoke(app, ["show", task_id, "--run", "4"])
    assert implement_run.exit_code == 0, implement_run.output
    assert "engineer / engineer_implement" in implement_run.output
    assert '"implementation_steps"' in implement_run.output, "the plan the Engineer was handed"


def test_show_falls_back_to_the_record_for_tasks_run_elsewhere(
    cwd: Path, target_repo: Path, devloop_home: Path
) -> None:
    task_id, _ = start(target_repo)
    (devloop_home / "checkpoints.sqlite").unlink()  # as if it ran in Studio's server

    shown = runner.invoke(app, ["show", task_id])

    assert shown.exit_code == 0, shown.output
    assert "state from record" in shown.output and "Reviewer → approved" in shown.output


def test_watch_dry_run_lists_issues_in_pick_order_with_their_workflow(
    cwd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issues = [
        {
            "number": 2,
            "title": "Dark mode",
            "body": "",
            "labels": [{"name": "devloop"}, {"name": "ui"}],
            "createdAt": "2026-10-02T00:00:00Z",
            "url": "u2",
        },
        {
            "number": 1,
            "title": "Crash on save",
            "body": "",
            "labels": [{"name": "devloop"}, {"name": "bug"}],
            "createdAt": "2026-10-01T00:00:00Z",
            "url": "u1",
        },
    ]
    data = tmp_path / "issues.json"
    data.write_text(json.dumps(issues))
    gh = tmp_path / "gh"
    gh.write_text(f"#!/bin/sh\ncat {data}\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("DEVLOOP_GH_BIN", str(gh))

    result = runner.invoke(app, ["watch", "--repo", "org/app", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert result.output.index("Crash on save") < result.output.index("Dark mode")
    assert "bug (label: bug)" in result.output and "ui_feature (label: ui)" in result.output
