"""`devloop watch`: GitHub issues in, pull requests out.

Every `poll_interval_s`:

    list open issues with the configured labels  (sources.github)
      → drop ineligible ones, order deterministically
      → claim locally (claims.py) and label `devloop:in-progress`
      → run the task's workflow to a PR or a human gate (one thread each,
        up to `max_concurrent`)
      → swap the label for the outcome and comment on the issue

The outcome label (`pr-open`, `needs-human`, `failed`) keeps the issue
from being picked up again; removing it is how a human asks for a retry.
`devloop resume` on such a task updates the issue the same way.

One process, a thread pool and a SQLite file — no scheduler, no queue.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from devloop.automation.claims import Claims
from devloop.config import DevLoopConfig, GitHubConfig
from devloop.contracts.state import TaskInput
from devloop.contracts.status import DevLoopStatus as St
from devloop.errors import DevLoopError
from devloop.graph.execute import Outcome
from devloop.sources.github import (
    Issue,
    comment,
    eligible,
    ensure_labels,
    list_issues,
    set_labels,
    to_task,
)

log = logging.getLogger("devloop.watch")

RunTask = Callable[[TaskInput], Outcome]

_REASON_CHARS = 300


class Watcher:
    def __init__(
        self,
        config: DevLoopConfig,
        *,
        repo: str,
        repo_source: str,
        run_task: RunTask,
        claims: Claims | None = None,
    ) -> None:
        self.config = config
        self.github = config.github
        self.repo = repo
        self.repo_source = repo_source
        self.run_task = run_task
        self.claims = claims or Claims()
        self.pool = ThreadPoolExecutor(
            max_workers=self.github.max_concurrent, thread_name_prefix="devloop-task"
        )
        self.running: dict[Future[None], Issue] = {}
        self._stop = threading.Event()

    # --- lifecycle ---------------------------------------------------------

    def setup(self) -> None:
        ensure_labels(self.repo, self.github.status_labels.all())
        for orphan in self.claims.orphaned():
            if orphan.repo != self.repo:
                continue
            log.warning("%s#%d: the run for it was interrupted", orphan.repo, orphan.issue)
            self.claims.finish(orphan.repo, orphan.issue, "interrupted")
            self._update_issue(
                orphan.issue,
                self.github.status_labels.needs_human,
                f"⚠️ DevLoop task `{orphan.task_id}` was interrupted (the watcher stopped "
                f"mid-run). Continue it with `devloop resume {orphan.task_id}`, or remove "
                "the label to start over.",
            )

    def candidates(self) -> list[Issue]:
        return eligible(list_issues(self.repo, self.github.labels), self.github)

    def poll(self) -> list[Issue]:
        """One cycle: start as many eligible issues as there are free slots.
        Returns the issues started."""

        self.running = {f: i for f, i in self.running.items() if not f.done()}
        busy = {i.number for i in self.running.values()}
        free = self.github.max_concurrent - len(self.running)
        started: list[Issue] = []
        if free <= 0:
            return started
        for issue in self.candidates():
            if len(started) == free:
                break
            if issue.number not in busy and self._start(issue):
                started.append(issue)
        return started

    def run_forever(self) -> None:
        self.setup()
        while not self._stop.is_set():
            try:
                started = self.poll()
                if started:
                    log.info("started %s", ", ".join(i.ref for i in started))
            except DevLoopError as exc:
                # GitHub unreachable, gh logged out, …: try again next cycle
                log.error("poll failed: %s", exc)
            self._stop.wait(self.github.poll_interval_s)

    def stop(self) -> None:
        self._stop.set()

    def wait(self) -> None:
        for future in list(self.running):
            future.result()
        self.pool.shutdown(wait=True)

    # --- one issue ---------------------------------------------------------

    def _start(self, issue: Issue) -> bool:
        task_id = uuid.uuid4().hex[:8]
        if not self.claims.claim(self.repo, issue.number, task_id):
            return False
        task = to_task(
            issue,
            task_id=task_id,
            repo_source=self.repo_source,
            base_branch=self.config.repo.base_branch,
        )
        try:
            set_labels(
                self.repo, issue.number, add=[self.github.status_labels.in_progress], remove=[]
            )
            if self.github.comment:
                comment(
                    self.repo,
                    issue.number,
                    f"🤖 DevLoop picked this up as task `{task_id}`. "
                    f"Follow it with `devloop show {task_id}`.",
                )
        except DevLoopError as exc:
            log.error("%s: could not mark it in progress, skipping: %s", issue.ref, exc)
            self.claims.release(self.repo, issue.number)
            return False
        log.info("%s → task %s: %s", issue.ref, task_id, issue.title)
        self.running[self.pool.submit(self._run, issue, task)] = issue
        return True

    def _run(self, issue: Issue, task: TaskInput) -> None:
        try:
            outcome = self.run_task(task)
        except Exception as exc:  # one task's crash must not stop the watcher
            log.exception("%s: task %s crashed", issue.ref, task.external_id)
            self.claims.finish(self.repo, issue.number, "error")
            self._update_issue(
                issue.number,
                self.github.status_labels.failed,
                f"❌ DevLoop task `{task.external_id}` failed: {str(exc)[:_REASON_CHARS]}",
            )
            return
        status = outcome.status
        log.info("%s: task %s → %s", issue.ref, task.external_id, status)
        self.claims.finish(self.repo, issue.number, str(status))
        report(self.github, self.repo, issue.number, outcome)

    def _update_issue(self, number: int, label: str | None, message: str) -> None:
        update_issue(self.github, self.repo, number, label, message)


def update_issue(
    github: GitHubConfig, repo: str, number: int, label: str | None, message: str
) -> None:
    """Swap whatever status label the issue has for `label`, and comment.
    Failures are logged, not raised: the task's own state is already
    saved, and a missed label must not lose it."""

    labels = github.status_labels.all()
    try:
        set_labels(
            repo,
            number,
            add=[label] if label else [],
            remove=[lbl for lbl in labels if lbl != label],
        )
        if github.comment and message:
            comment(repo, number, message)
    except DevLoopError as exc:
        log.error("%s#%d: could not update the issue: %s", repo, number, exc)


def report(github: GitHubConfig, repo: str, number: int, outcome: Outcome) -> None:
    """Tell the issue where its task ended up."""

    label, message = describe(github, outcome)
    update_issue(github, repo, number, label, message)


def describe(github: GitHubConfig, outcome: Outcome) -> tuple[str | None, str]:
    labels = github.status_labels
    values, task_id = outcome.values, outcome.task_id
    gate = outcome.waiting[0] if outcome.waiting else {}
    match outcome.status:
        case St.READY_FOR_FINALIZE | St.FINALIZED:
            pr = values.get("pull_request_url")
            where = f"PR: {pr}" if pr else f"branch `{values.get('branch')}` pushed"
            return labels.pr_open, f"✅ Implemented, tested and approved by review — {where}"
        case St.CLARIFICATION_REQUIRED:
            questions = "\n".join(f"- {q}" for q in gate.get("questions") or [])
            return labels.needs_human, (
                f"❓ DevLoop task `{task_id}` needs an answer to continue:\n\n{questions}\n\n"
                f"Answer with `devloop resume {task_id} --answer '…'`."
            )
        case St.ESCALATED:
            reason = str(gate.get("reason") or values.get("escalation_reason") or "a guard tripped")
            return labels.needs_human, (
                f"⚠️ DevLoop task `{task_id}` stopped and needs a human: "
                f"{reason[:_REASON_CHARS]}\n\nInspect with `devloop show {task_id}`."
            )
        case St.CANCELLED:
            return labels.failed, f"DevLoop task `{task_id}` was cancelled."
        case _:
            return labels.needs_human, f"DevLoop task `{task_id}` stopped at {outcome.status}."
