"""SQLite access: connections, transactions and schema migrations.

Every write runs inside ``BEGIN IMMEDIATE``. SQLite allows one writer at a
time, so taking the write lock at the start of the transaction (rather than at
the first write, as a deferred transaction would) means a balance read inside
the transaction cannot go stale before the transaction commits. That rule is
what makes the funds checks in ``service.py`` safe when several processes share
the file. Within one process, writers also queue on a lock before they begin,
which keeps waits short and fair. Readers use WAL mode and never block, or get
blocked by, the writer.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources

BUSY_TIMEOUT_MS = 5000


class DatabaseBusy(Exception):
    """The write lock could not be acquired within the busy timeout."""


class Database:
    def __init__(self, path: str, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

    def connection(self) -> sqlite3.Connection:
        """Return this thread's connection, opening it on first use."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.path,
                isolation_level=None,  # we issue BEGIN/COMMIT ourselves
                check_same_thread=False,
                timeout=self.busy_timeout_ms / 1000,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            # FULL makes every commit durable across power loss, at some cost
            # in throughput. For a ledger that is the right default.
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection()
        # Writers in this process queue on a lock first. SQLite's own busy handler
        # waits by sleeping and polling, which gives long and unfair tail latencies
        # under contention; the lock hands the turn straight to the next waiter.
        # BEGIN IMMEDIATE still guards against writers in other processes.
        if not self._write_lock.acquire(timeout=self.busy_timeout_ms / 1000):
            raise DatabaseBusy("timed out waiting for the write lock")
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc) or "busy" in str(exc):
                    raise DatabaseBusy(str(exc)) from exc
                raise
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            self._write_lock.release()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A consistent snapshot for multi-statement reads."""
        conn = self.connection()
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            for conn in self._all:
                conn.close()
            self._all.clear()
        self._local = threading.local()


def _statements(script: str) -> Iterator[str]:
    """Split a SQL script into statements, keeping trigger bodies whole."""
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement:
                yield statement
    if buffer.strip() and not buffer.strip().startswith("--"):
        raise ValueError(f"incomplete SQL statement at end of migration: {buffer.strip()[:60]}")


def migrations() -> list[tuple[str, str]]:
    folder = resources.files("ledger").joinpath("migrations")
    files = sorted((f for f in folder.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name)
    return [(f.name.removesuffix(".sql"), f.read_text(encoding="utf-8")) for f in files]


def migrate(db: Database) -> list[str]:
    """Apply pending migrations, each in its own transaction. Returns the names applied."""
    applied_now: list[str] = []
    with db.write() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
    for name, script in migrations():
        with db.write() as conn:
            done = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
            ).fetchone()
            if done:
                continue
            for statement in _statements(script):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (name,))
            applied_now.append(name)
    return applied_now
