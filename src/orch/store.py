"""SQLite state store: schema, migrations, transactions and state transitions."""

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from orch import models
from orch.models import Effort, Grade, Lane, Task, TaskState

# MIGRATIONS[n] upgrades the schema from user_version n to n + 1.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            target TEXT NOT NULL,
            title TEXT NOT NULL,
            grade TEXT NOT NULL,
            lane TEXT NOT NULL,
            model TEXT NOT NULL,
            effort TEXT,
            state TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0,
            timeout_sec INTEGER NOT NULL,
            base_sha TEXT,
            test_sha TEXT,
            depends_on TEXT,
            worktree TEXT,
            pr_url TEXT,
            slot_expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            attempt INTEGER NOT NULL,
            lane TEXT NOT NULL,
            model TEXT NOT NULL,
            effort TEXT,
            pid INTEGER,
            pid_started_at TEXT,
            started_at TEXT,
            ended_at TEXT,
            exit_code INTEGER,
            outcome TEXT,
            failure_reason TEXT
        )""",
        """CREATE TABLE locks (
            target TEXT NOT NULL,
            path_glob TEXT NOT NULL,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            PRIMARY KEY (target, path_glob, task_id)
        )""",
        """CREATE TABLE gate_results (
            task_id TEXT, attempt INTEGER, gate TEXT,
            passed INTEGER, detail TEXT, at TEXT,
            PRIMARY KEY (task_id, attempt, gate)
        )""",
        """CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT, from_state TEXT, to_state TEXT,
            actor TEXT, note TEXT, at TEXT
        )""",
    ),
)


class StoreError(Exception):
    """Base class for state store errors."""


class SchemaTooNewError(StoreError):
    """The database has a newer schema than this code knows."""


class TaskNotFoundError(StoreError):
    """No task has the given id."""


class IllegalTransitionError(StoreError):
    """The transition table does not allow this transition."""


class StateMismatchError(StoreError):
    """The task is not in the state the caller expected."""


def connect(path: Path) -> sqlite3.Connection:
    """Open the database, creating parents, and migrate it to the latest schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        deadline = time.monotonic() + 5
        while True:
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError as exc:
                # Concurrent first connections can bypass busy_timeout here.
                if str(exc) != "database is locked" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        conn.execute("PRAGMA foreign_keys = ON")
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations and return the resulting schema version."""
    with transaction(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise SchemaTooNewError(f"Unsupported schema version: {version}")
        while version < len(MIGRATIONS):
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
            version += 1
            conn.execute(f"PRAGMA user_version = {version}")
    return version


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a BEGIN IMMEDIATE transaction, or join the one already open."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def insert_task(conn: sqlite3.Connection, task: Task) -> None:
    """Insert a new task."""
    conn.execute(
        """INSERT INTO tasks (
            id, target, title, grade, lane, model, effort, state, attempt,
            timeout_sec, base_sha, test_sha, depends_on, worktree, pr_url,
            slot_expires_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task.id,
            task.target,
            task.title,
            task.grade.value,
            task.lane.value,
            task.model,
            task.effort.value if task.effort is not None else None,
            task.state.value,
            task.attempt,
            task.timeout_sec,
            task.base_sha,
            task.test_sha,
            json.dumps(list(task.depends_on)),
            task.worktree,
            task.pr_url,
            task.slot_expires_at,
            task.created_at,
            task.updated_at,
        ),
    )


def get_task(conn: sqlite3.Connection, task_id: str) -> Task | None:
    """Return a task, or None if there is no such task."""
    cursor = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    fields = dict(zip((column[0] for column in cursor.description), row, strict=True))
    fields["grade"] = Grade(fields["grade"])
    fields["lane"] = Lane(fields["lane"])
    fields["state"] = TaskState(fields["state"])
    if fields["effort"] is not None:
        fields["effort"] = Effort(fields["effort"])
    fields["depends_on"] = (
        tuple(json.loads(fields["depends_on"]))
        if fields["depends_on"] is not None
        else ()
    )
    return Task(**fields)


def transition(
    conn: sqlite3.Connection,
    task_id: str,
    expected: TaskState,
    new: TaskState,
    *,
    actor: str,
    note: str | None = None,
    at: str | None = None,
) -> None:
    """Move a task from expected to new, atomically with its event and lock release."""
    if not models.can_transition(expected, new):
        raise IllegalTransitionError(f"Illegal transition: {expected} -> {new}")
    if at is None:
        at = datetime.now(UTC).isoformat()
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
            (new.value, at, task_id, expected.value),
        )
        if cursor.rowcount == 0:
            row = conn.execute(
                "SELECT state FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(task_id)
            raise StateMismatchError(
                f"Task {task_id}: expected {expected}, found {row[0]}"
            )
        conn.execute(
            """INSERT INTO events (task_id, from_state, to_state, actor, note, at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, expected.value, new.value, actor, note, at),
        )
        if new in models.TERMINAL_STATES:
            conn.execute("DELETE FROM locks WHERE task_id = ?", (task_id,))
