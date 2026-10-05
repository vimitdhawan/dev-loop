"""LangGraph checkpointer: Postgres when `DEVLOOP_DATABASE_URL` is set,
otherwise a SQLite file under `DEVLOOP_HOME` — v0 works with nothing
running.

`InMemorySaver` looks tempting but is wrong for a CLI: `devloop run` and
`devloop resume` are separate processes, so in-memory state is gone by the
time `resume` starts.
"""

from __future__ import annotations

import inspect
import os
from enum import Enum
from types import ModuleType
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

from devloop.contracts import artifacts, context, runs, state, status
from devloop.paths import devloop_home

_STATE_MODULES: tuple[ModuleType, ...] = (status, state, artifacts, runs, context)


def serde_allowlist() -> set[tuple[str, str]]:
    """Every model/enum our contract modules define. Our contracts are
    msgpack-serialized into graph state; they're our own trusted code, so
    they're allow-listed explicitly instead of relying on LangGraph's
    permissive (and warning) default. Built by reflection so a new
    contract type can't be forgotten here — forgetting one only shows up
    on `resume`, in a different process, which is the worst place."""

    allowed: set[tuple[str, str]] = set()
    for module in _STATE_MODULES:
        for name, obj in vars(module).items():
            if (
                inspect.isclass(obj)
                and obj.__module__ == module.__name__
                and issubclass(obj, (BaseModel, Enum))
            ):
                allowed.add((module.__name__, name))
    return allowed


def serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=serde_allowlist())


def _pin(saver: BaseCheckpointSaver[Any], cm: Any) -> BaseCheckpointSaver[Any]:
    """`from_conn_string` is a generator-based contextmanager; its
    `finally` closes the connection when the generator is garbage
    collected. Pin `cm` on the saver so it lives as long as the process."""

    saver._devloop_cm = cm  # type: ignore[attr-defined]
    return saver


def checkpointer() -> BaseCheckpointSaver[Any]:
    dsn = os.environ.get("DEVLOOP_DATABASE_URL")
    if dsn:
        from langgraph.checkpoint.postgres import PostgresSaver

        pg_cm = PostgresSaver.from_conn_string(dsn)
        pg_saver = pg_cm.__enter__()
        pg_saver.serde = serde()
        pg_saver.setup()
        return _pin(pg_saver, pg_cm)

    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = devloop_home() / "checkpoints.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite_cm = SqliteSaver.from_conn_string(str(db_path))
    sqlite_saver = sqlite_cm.__enter__()
    sqlite_saver.serde = serde()
    return _pin(sqlite_saver, sqlite_cm)
