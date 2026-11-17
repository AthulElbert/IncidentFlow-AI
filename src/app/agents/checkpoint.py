"""Where an agent run's progress is saved between stages.

`memory` (the default) keeps it in the process, so a restart loses it and interrupted runs are escalated.
`sqlite` and `postgres` keep it on disk or in the database, so an interrupted run can be resumed.
"""
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from langgraph.checkpoint.memory import MemorySaver

CHECKPOINT_BACKENDS = frozenset({"memory", "sqlite", "postgres"})


@dataclass
class Checkpointing:
    saver: Any
    backend: str
    close: Callable[[], None]

    @property
    def durable(self) -> bool:
        return self.backend != "memory"


def build_checkpointing(backend: str, sqlite_file: str | Path = "", database_url: str = "") -> Checkpointing:
    if backend == "memory":
        return Checkpointing(MemorySaver(), "memory", lambda: None)

    if backend == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError as exc:
            raise ValueError("CHECKPOINT_BACKEND=sqlite needs the 'langgraph-checkpoint-sqlite' package") from exc
        path = Path(sqlite_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Runs execute on a thread pool; the saver serialises access to this one connection itself.
        conn = sqlite3.connect(str(path), check_same_thread=False)
        return Checkpointing(SqliteSaver(conn), "sqlite", conn.close)

    if backend == "postgres":
        if not database_url:
            raise ValueError("CHECKPOINT_BACKEND=postgres requires DATABASE_URL")
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg import Connection
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ValueError("CHECKPOINT_BACKEND=postgres needs the 'langgraph-checkpoint-postgres' package") from exc
        # The settings the library itself uses in `PostgresSaver.from_conn_string`.
        conn = Connection.connect(database_url, autocommit=True, prepare_threshold=0, row_factory=dict_row)
        saver = PostgresSaver(conn)
        saver.setup()
        return Checkpointing(saver, "postgres", conn.close)

    raise ValueError(f"CHECKPOINT_BACKEND must be one of {sorted(CHECKPOINT_BACKENDS)}, got '{backend}'")
