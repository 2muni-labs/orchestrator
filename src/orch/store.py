"""SQLite state store: schema, migrations, transactions and state transitions."""

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from orch import models
from orch.models import (
    Effort,
    FailureReason,
    Gate,
    GateResult,
    Grade,
    Lane,
    Lock,
    Outcome,
    Run,
    Task,
    TaskState,
)

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
    ("ALTER TABLE tasks ADD COLUMN pushed_sha TEXT",),
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


class RunNotFoundError(StoreError):
    """No run has the given id."""


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
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                if time.monotonic() >= deadline:
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
            slot_expires_at, created_at, updated_at, pushed_sha
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            task.pushed_sha,
        ),
    )


def get_task(conn: sqlite3.Connection, task_id: str) -> Task | None:
    """Return a task, or None if there is no such task."""
    cursor = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    return _task_from_row(cursor, row)


def _task_from_row(cursor: sqlite3.Cursor, row: tuple[object, ...]) -> Task:
    """Restore a task's enum and sequence fields from a database row."""
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
    """Move a task from expected to new, atomically with its event and lock release.

    Join the caller's transaction if one is open. If the caller catches an error
    and continues, earlier statements in that transaction remain in effect.
    The caller must roll back the transaction after catching an error.
    """
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


def update_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    at: str | None = None,
    **fields: object,
) -> None:
    """Set non-state fields of a task and its updated_at."""
    allowed = (
        "attempt",
        "base_sha",
        "test_sha",
        "worktree",
        "pr_url",
        "pushed_sha",
        "slot_expires_at",
    )
    if not fields or any(name not in allowed for name in fields):
        raise StoreError("Expected one or more allowed non-state task fields")
    names = [name for name in allowed if name in fields]
    assignments = ", ".join(f"{name} = ?" for name in names)
    if at is None:
        at = datetime.now(UTC).isoformat()
    with transaction(conn):
        cursor = conn.execute(
            f"UPDATE tasks SET {assignments}, updated_at = ? WHERE id = ?",
            (*[fields[name] for name in names], at, task_id),
        )
        if cursor.rowcount == 0:
            raise TaskNotFoundError(task_id)


def list_tasks(
    conn: sqlite3.Connection,
    *,
    states: Iterable[TaskState] | None = None,
    target: str | None = None,
) -> tuple[Task, ...]:
    """Return tasks ordered by created_at, then id."""
    clauses: list[str] = []
    values: list[object] = []
    if states is not None:
        state_values = tuple(state.value for state in states)
        if not state_values:
            return ()
        placeholders = ", ".join("?" for _ in state_values)
        clauses.append(f"state IN ({placeholders})")
        values.extend(state_values)
    if target is not None:
        clauses.append("target = ?")
        values.append(target)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    cursor = conn.execute(f"SELECT * FROM tasks{where} ORDER BY created_at, id", values)
    return tuple(_task_from_row(cursor, row) for row in cursor)


def add_locks(
    conn: sqlite3.Connection, target: str, task_id: str, globs: Iterable[str]
) -> None:
    """Register a task's locks, all or none. Existing ones are kept."""
    with transaction(conn):
        conn.executemany(
            """INSERT INTO locks (target, path_glob, task_id) VALUES (?, ?, ?)
            ON CONFLICT (target, path_glob, task_id) DO NOTHING""",
            ((target, path_glob, task_id) for path_glob in globs),
        )


def list_locks(conn: sqlite3.Connection, target: str) -> tuple[Lock, ...]:
    """Return a target's locks ordered by task_id, then path_glob."""
    rows = conn.execute(
        """SELECT target, path_glob, task_id FROM locks
        WHERE target = ? ORDER BY task_id, path_glob""",
        (target,),
    )
    return tuple(
        Lock(target=target, path_glob=path_glob, task_id=task_id)
        for target, path_glob, task_id in rows
    )


def insert_run(conn: sqlite3.Connection, run: Run) -> int:
    """Insert a run and return its run_id. The run's own run_id is ignored."""
    cursor = conn.execute(
        """INSERT INTO runs (
            task_id, attempt, lane, model, effort, pid, pid_started_at,
            started_at, ended_at, exit_code, outcome, failure_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run.task_id,
            run.attempt,
            run.lane.value,
            run.model,
            run.effort.value if run.effort is not None else None,
            run.pid,
            run.pid_started_at,
            run.started_at,
            run.ended_at,
            run.exit_code,
            run.outcome.value if run.outcome is not None else None,
            run.failure_reason.value if run.failure_reason is not None else None,
        ),
    )
    return cursor.lastrowid


def set_run_process(
    conn: sqlite3.Connection, run_id: int, *, pid: int, pid_started_at: str
) -> None:
    """Record the process of a run."""
    cursor = conn.execute(
        "UPDATE runs SET pid = ?, pid_started_at = ? WHERE run_id = ?",
        (pid, pid_started_at, run_id),
    )
    if cursor.rowcount == 0:
        raise RunNotFoundError(run_id)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    ended_at: str,
    exit_code: int | None,
    outcome: Outcome,
    failure_reason: FailureReason | None = None,
) -> None:
    """Record how a run ended. A run ends only once."""
    with transaction(conn):
        cursor = conn.execute(
            """UPDATE runs
            SET ended_at = ?, exit_code = ?, outcome = ?, failure_reason = ?
            WHERE run_id = ? AND ended_at IS NULL""",
            (
                ended_at,
                exit_code,
                outcome.value,
                failure_reason.value if failure_reason is not None else None,
                run_id,
            ),
        )
        if cursor.rowcount == 0:
            row = conn.execute(
                "SELECT ended_at FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(run_id)
            raise StateMismatchError(f"Run {run_id} already ended at {row[0]}")


def latest_run(conn: sqlite3.Connection, task_id: str) -> Run | None:
    """Return the task's run with the highest run_id, or None."""
    cursor = conn.execute(
        "SELECT * FROM runs WHERE task_id = ? ORDER BY run_id DESC LIMIT 1",
        (task_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    fields = dict(zip((column[0] for column in cursor.description), row, strict=True))
    fields["lane"] = Lane(fields["lane"])
    if fields["effort"] is not None:
        fields["effort"] = Effort(fields["effort"])
    if fields["outcome"] is not None:
        fields["outcome"] = Outcome(fields["outcome"])
    if fields["failure_reason"] is not None:
        fields["failure_reason"] = FailureReason(fields["failure_reason"])
    return Run(**fields)


def record_gate_result(
    conn: sqlite3.Connection,
    task_id: str,
    attempt: int,
    gate: Gate,
    *,
    passed: bool,
    detail: str | None = None,
    at: str | None = None,
) -> None:
    """Record a gate result, replacing an earlier one for the same key."""
    if at is None:
        at = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO gate_results
        (task_id, attempt, gate, passed, detail, at) VALUES (?, ?, ?, ?, ?, ?)""",
        (task_id, attempt, gate.value, int(passed), detail, at),
    )


def gate_results(
    conn: sqlite3.Connection, task_id: str, attempt: int
) -> tuple[GateResult, ...]:
    """Return an attempt's gate results in recording order."""
    rows = conn.execute(
        """SELECT task_id, attempt, gate, passed, detail, at FROM gate_results
        WHERE task_id = ? AND attempt = ? ORDER BY rowid""",
        (task_id, attempt),
    )
    return tuple(
        GateResult(
            task_id=task_id,
            attempt=attempt,
            gate=Gate(gate),
            passed=bool(passed),
            detail=detail,
            at=at,
        )
        for task_id, attempt, gate, passed, detail, at in rows
    )
