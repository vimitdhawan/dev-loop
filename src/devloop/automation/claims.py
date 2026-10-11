"""Which issues this machine is working on — so two `devloop watch`
processes (or one restarted mid-run) never start the same issue twice.

A row in `$DEVLOOP_HOME/automation.sqlite` per issue. Claiming is one
`BEGIN IMMEDIATE` transaction, so it's atomic across processes: an issue
is claimable unless a *live* process holds it. On GitHub, the
`devloop:in-progress` label does the same job across machines (best
effort — this is not a distributed lock, and doesn't try to be).
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from devloop.paths import devloop_home

RUNNING = "running"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    repo        TEXT NOT NULL,
    issue       INTEGER NOT NULL,
    task_id     TEXT NOT NULL,
    state       TEXT NOT NULL,
    pid         INTEGER NOT NULL,
    claimed_at  REAL NOT NULL,
    finished_at REAL,
    outcome     TEXT,
    PRIMARY KEY (repo, issue)
)
"""


@dataclass(frozen=True)
class Claim:
    repo: str
    issue: int
    task_id: str
    state: str
    pid: int
    outcome: str | None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


class Claims:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or devloop_home() / "automation.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def claim(self, repo: str, issue: int, task_id: str) -> bool:
        """True if this process now owns the issue."""

        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state, pid FROM claims WHERE repo = ? AND issue = ?", (repo, issue)
            ).fetchone()
            if row is not None and row["state"] == RUNNING and _alive(row["pid"]):
                db.execute("ROLLBACK")
                return False
            db.execute(
                "INSERT OR REPLACE INTO claims (repo, issue, task_id, state, pid, claimed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (repo, issue, task_id, RUNNING, os.getpid(), time.time()),
            )
            db.execute("COMMIT")
            return True
        finally:
            db.close()

    def finish(self, repo: str, issue: int, outcome: str) -> None:
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE claims SET state = 'finished', finished_at = ?, outcome = ? "
                "WHERE repo = ? AND issue = ?",
                (time.time(), outcome, repo, issue),
            )

    def release(self, repo: str, issue: int) -> None:
        with closing(self._connect()) as db:
            db.execute("DELETE FROM claims WHERE repo = ? AND issue = ?", (repo, issue))

    def get(self, repo: str, issue: int) -> Claim | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM claims WHERE repo = ? AND issue = ?", (repo, issue)
            ).fetchone()
        return _claim(row) if row else None

    def orphaned(self) -> list[Claim]:
        """Running claims whose process is gone — a watcher that crashed or
        was killed mid-task."""

        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM claims WHERE state = ?", (RUNNING,)).fetchall()
        return [_claim(r) for r in rows if not _alive(r["pid"])]


def _claim(row: sqlite3.Row) -> Claim:
    return Claim(
        repo=row["repo"],
        issue=row["issue"],
        task_id=row["task_id"],
        state=row["state"],
        pid=row["pid"],
        outcome=row["outcome"],
    )
