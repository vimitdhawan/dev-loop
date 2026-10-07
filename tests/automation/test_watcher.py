"""`devloop watch` against a fake `gh`: which issues it picks, in what order,
how it marks them, and that one issue never runs twice."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from devloop.automation.claims import Claims
from devloop.automation.watcher import Watcher, describe
from devloop.config import DevLoopConfig, GitHubConfig
from devloop.contracts.state import TaskInput
from devloop.contracts.status import DevLoopStatus as St
from devloop.graph.execute import Outcome
from devloop.sources.github import Issue, eligible, parse_issue_url, to_task


def issue(number: int, labels: list[str], created: str = "2026-10-01T00:00:00Z") -> Issue:
    return Issue(
        repo="org/app",
        number=number,
        title=f"issue {number}",
        body="b",
        labels=tuple(labels),
        created_at=created,
        url=f"https://github.com/org/app/issues/{number}",
    )


# --- selection -------------------------------------------------------------


def test_only_labelled_issues_without_a_status_label_are_eligible() -> None:
    config = GitHubConfig(labels=["devloop"], exclude_labels=["wip"])
    issues = [
        issue(1, ["devloop"]),
        issue(2, []),
        issue(3, ["devloop", "WIP"]),
        issue(4, ["devloop", "devloop:in-progress"]),
        issue(5, ["devloop", "devloop:needs-human"]),
        issue(6, ["DevLoop", "bug"]),
    ]

    assert [i.number for i in eligible(issues, config)] == [1, 6]


def test_order_is_priority_then_oldest_then_lowest_number() -> None:
    config = GitHubConfig(labels=["devloop"], priority_labels=["p0", "p1"])
    issues = [
        issue(10, ["devloop"], "2026-09-01T00:00:00Z"),
        issue(11, ["devloop", "p1"], "2026-10-02T00:00:00Z"),
        issue(12, ["devloop", "p0"], "2026-10-03T00:00:00Z"),
        issue(9, ["devloop"], "2026-09-01T00:00:00Z"),
        issue(13, ["devloop", "p1"], "2026-10-01T00:00:00Z"),
    ]

    assert [i.number for i in eligible(issues, config)] == [12, 13, 11, 9, 10]


def test_issue_becomes_a_task_with_its_labels_and_url() -> None:
    task = to_task(
        issue(7, ["bug"]), task_id="abc", repo_source="https://x.git", base_branch="main"
    )

    assert (task.source, task.labels, task.base_branch) == ("github", ["bug"], "main")
    assert "issue 7" in task.description and task.url is not None
    assert parse_issue_url(task.url) == ("org/app", 7)


# --- claims ----------------------------------------------------------------


def test_a_claim_held_by_a_live_process_blocks_everyone_else(tmp_path: Path) -> None:
    claims = Claims(tmp_path / "a.sqlite")

    assert claims.claim("org/app", 1, "t1")
    assert not claims.claim("org/app", 1, "t2"), "same issue, while the first run is live"
    assert claims.claim("org/app", 2, "t3"), "a different issue is fine"

    claims.finish("org/app", 1, "READY_FOR_FINALIZE")
    assert claims.claim("org/app", 1, "t4"), "a finished claim doesn't block a retry"


def test_a_claim_whose_process_died_is_orphaned_and_claimable(tmp_path: Path) -> None:
    path = tmp_path / "a.sqlite"
    code = (
        "import sys; sys.path.insert(0, 'src');"
        "from pathlib import Path; from devloop.automation.claims import Claims;"
        f"Claims(Path({str(path)!r})).claim('org/app', 1, 'dead')"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).parents[2])
    claims = Claims(path)

    assert [c.task_id for c in claims.orphaned()] == ["dead"]
    assert claims.claim("org/app", 1, "t2")


def test_claims_are_atomic_across_threads(tmp_path: Path) -> None:
    claims = Claims(tmp_path / "a.sqlite")
    won: list[bool] = []
    threads = [
        threading.Thread(target=lambda i=i: won.append(claims.claim("org/app", 1, f"t{i}")))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert won.count(True) == 1


# --- the watcher, end to end against a fake gh ------------------------------


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    calls = tmp_path / "gh-calls.txt"
    issues = tmp_path / "issues.json"
    script = tmp_path / "gh"
    script.write_text(
        f'#!/bin/sh\necho "$@" >> {calls}\ncase "$1 $2" in\n  "issue list") cat {issues} ;;\nesac\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("DEVLOOP_GH_BIN", str(script))
    return {"calls": calls, "issues": issues}


def serve(path: Path, issues: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(issues))


def raw(number: int, labels: list[str]) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"issue {number}",
        "body": "fix it",
        "labels": [{"name": n} for n in labels],
        "createdAt": f"2026-10-0{number}T00:00:00Z",
        "url": f"https://github.com/org/app/issues/{number}",
    }


def calls(gh: dict[str, Path]) -> list[str]:
    return gh["calls"].read_text().splitlines() if gh["calls"].exists() else []


class FakeRun:
    def __init__(self, status: St = St.READY_FOR_FINALIZE) -> None:
        self.status = status
        self.tasks: list[TaskInput] = []

    def __call__(self, task: TaskInput) -> Outcome:
        self.tasks.append(task)
        values = {
            "task": task,
            "status": self.status,
            "pull_request_url": "https://github.com/org/app/pull/9",
            "escalation_reason": "budget",
        }
        waiting = [{"gate": "escalate", "reason": "budget"}] if self.status == St.ESCALATED else []
        return Outcome(task_id=task.external_id, values=values, waiting=waiting)


def make_watcher(run: FakeRun, **github: Any) -> Watcher:
    config = DevLoopConfig.model_validate({"github": {"labels": ["devloop"], **github}})
    return Watcher(
        config, repo="org/app", repo_source="https://github.com/org/app.git", run_task=run
    )


def test_poll_runs_the_first_eligible_issue_and_labels_the_outcome(gh: dict[str, Path]) -> None:
    serve(gh["issues"], [raw(2, ["devloop"]), raw(1, ["devloop", "bug"])])
    run = FakeRun()
    watcher = make_watcher(run)

    started = watcher.poll()
    watcher.wait()

    assert [i.number for i in started] == [1], "oldest first, one at a time by default"
    assert run.tasks[0].labels == ["devloop", "bug"]
    log = calls(gh)
    assert any(
        c.startswith("issue edit 1 --repo org/app --add-label devloop:in-progress") for c in log
    )
    final = [c for c in log if c.startswith("issue edit 1") and "devloop:pr-open" in c]
    assert final and "--remove-label devloop:in-progress" in final[-1]
    assert any("pull/9" in c for c in log if c.startswith("issue comment 1"))


def test_poll_respects_max_concurrent_and_skips_issues_already_running(
    gh: dict[str, Path],
) -> None:
    serve(gh["issues"], [raw(1, ["devloop"]), raw(2, ["devloop"]), raw(3, ["devloop"])])
    release = threading.Event()

    class Slow(FakeRun):
        def __call__(self, task: TaskInput) -> Outcome:
            release.wait(10)
            return super().__call__(task)

    run = Slow()
    watcher = make_watcher(run, max_concurrent=2)

    first = watcher.poll()
    second = watcher.poll()  # both slots busy; the label isn't visible to the fake gh
    release.set()
    watcher.wait()

    assert [i.number for i in first] == [1, 2]
    assert second == []
    assert sorted(t.title for t in run.tasks) == ["issue 1", "issue 2"]


def test_another_process_holding_the_issue_is_respected(gh: dict[str, Path]) -> None:
    serve(gh["issues"], [raw(1, ["devloop"])])
    Claims().claim("org/app", 1, "other")  # this pid is alive: as if another watcher had it
    run = FakeRun()

    assert make_watcher(run).poll() == []
    assert run.tasks == []


def test_crashing_task_marks_the_issue_failed_and_the_watcher_survives(
    gh: dict[str, Path],
) -> None:
    serve(gh["issues"], [raw(1, ["devloop"])])

    def boom(task: TaskInput) -> Outcome:
        raise RuntimeError("disk full")

    watcher = make_watcher(FakeRun())
    watcher.run_task = boom
    watcher.poll()
    watcher.wait()

    log = calls(gh)
    assert any("--add-label devloop:failed" in c for c in log)
    assert any("disk full" in c for c in log if c.startswith("issue comment"))
    assert Claims().get("org/app", 1).outcome == "error"  # type: ignore[union-attr]


def test_setup_creates_status_labels_and_flags_orphaned_runs(gh: dict[str, Path]) -> None:
    claims = Claims()
    claims.claim("org/app", 4, "lost")
    # pretend the claiming process died
    with __import__("sqlite3").connect(claims.path) as db:
        db.execute("UPDATE claims SET pid = ?", (2**22 + os.getpid(),))

    make_watcher(FakeRun()).setup()

    log = calls(gh)
    assert sum(c.startswith("label create devloop:") for c in log) == 4
    assert any("issue edit 4" in c and "devloop:needs-human" in c for c in log)
    assert any("devloop resume lost" in c for c in log if c.startswith("issue comment 4"))


@pytest.mark.parametrize(
    "status,label,text",
    [
        (St.READY_FOR_FINALIZE, "devloop:pr-open", "pull/9"),
        (St.ESCALATED, "devloop:needs-human", "budget"),
        (St.CANCELLED, "devloop:failed", "cancelled"),
    ],
)
def test_outcomes_map_to_status_labels(status: St, label: str, text: str) -> None:
    outcome = FakeRun(status)(TaskInput(external_id="t", repo=".", title="t", description="d"))

    got_label, message = describe(GitHubConfig(), outcome)

    assert got_label == label and text in message


def test_clarification_comment_lists_the_questions() -> None:
    outcome = Outcome(
        task_id="t",
        values={"status": St.CLARIFICATION_REQUIRED},
        waiting=[{"gate": "clarify", "questions": ["Which timezone?"]}],
    )

    label, message = describe(GitHubConfig(), outcome)

    assert label == "devloop:needs-human"
    assert "- Which timezone?" in message and "devloop resume t --answer" in message
