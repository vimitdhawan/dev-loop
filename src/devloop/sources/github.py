"""GitHub issues as a task source, through `gh`.

Which issues are eligible, and in what order, is decided here — purely,
from the issue list — so the selection is deterministic and testable:
an issue must be open, carry every configured label, and carry none of
the excluded or DevLoop status labels. Order: the earliest matching
`priority_labels` entry first, then oldest, then lowest number.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from devloop.config import GitHubConfig
from devloop.contracts.state import TaskInput
from devloop.gh import gh

_ISSUE_URL = re.compile(r"github\.com/([^/]+/[^/]+)/issues/(\d+)")


@dataclass(frozen=True)
class Issue:
    repo: str
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    created_at: str  # ISO 8601, sorts chronologically as a string
    url: str

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"


def list_issues(repo: str, labels: list[str], limit: int = 200) -> list[Issue]:
    args = ["issue", "list", "--repo", repo, "--state", "open", "--limit", str(limit)]
    for label in labels:
        args += ["--label", label]
    args += ["--json", "number,title,body,labels,createdAt,url"]
    return [
        Issue(
            repo=repo,
            number=int(raw["number"]),
            title=raw["title"],
            body=raw.get("body") or "",
            labels=tuple(lbl["name"] for lbl in raw.get("labels") or []),
            created_at=raw.get("createdAt") or "",
            url=raw.get("url") or "",
        )
        for raw in json.loads(gh(*args) or "[]")
    ]


def eligible(issues: list[Issue], config: GitHubConfig) -> list[Issue]:
    required = {lbl.lower() for lbl in config.labels}
    blocked = {lbl.lower() for lbl in (*config.exclude_labels, *config.status_labels.all())}
    priority = [lbl.lower() for lbl in config.priority_labels]

    def rank(issue: Issue) -> tuple[int, str, int]:
        labels = {lbl.lower() for lbl in issue.labels}
        first = next((i for i, lbl in enumerate(priority) if lbl in labels), len(priority))
        return first, issue.created_at, issue.number

    picked = [
        issue
        for issue in issues
        if required <= {lbl.lower() for lbl in issue.labels}
        and not blocked & {lbl.lower() for lbl in issue.labels}
    ]
    return sorted(picked, key=rank)


def to_task(issue: Issue, *, task_id: str, repo_source: str, base_branch: str | None) -> TaskInput:
    return TaskInput(
        external_id=task_id,
        source="github",
        repo=repo_source,
        base_branch=base_branch,
        title=issue.title,
        description=f"# {issue.title}\n\n{issue.body}\n\n_From {issue.url}_\n",
        labels=list(issue.labels),
        url=issue.url,
    )


def parse_issue_url(url: str | None) -> tuple[str, int] | None:
    match = _ISSUE_URL.search(url or "")
    return (match.group(1), int(match.group(2))) if match else None


def ensure_labels(repo: str, labels: list[str]) -> None:
    """Create the status labels if the repo doesn't have them (`--force`
    makes this idempotent): `gh issue edit --add-label` fails on a label
    that doesn't exist."""

    for label in labels:
        gh("label", "create", label, "--repo", repo, "--color", "5319e7", "--force")


def set_labels(repo: str, number: int, *, add: list[str], remove: list[str]) -> None:
    args = ["issue", "edit", str(number), "--repo", repo]
    for label in add:
        args += ["--add-label", label]
    for label in remove:
        args += ["--remove-label", label]
    gh(*args)


def comment(repo: str, number: int, body: str) -> None:
    gh("issue", "comment", str(number), "--repo", repo, "--body", body)
