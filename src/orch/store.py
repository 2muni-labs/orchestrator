"""SQLite state store: schema, migrations, transactions and state transitions."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from orch.models import Task, TaskState

# MIGRATIONS[n] upgrades the schema from user_version n to n + 1.
MIGRATIONS: tuple[tuple[str, ...], ...] = ()


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
    raise NotImplementedError


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations and return the resulting schema version."""
    raise NotImplementedError


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a BEGIN IMMEDIATE transaction, or join the one already open."""
    raise NotImplementedError
    yield conn


def insert_task(conn: sqlite3.Connection, task: Task) -> None:
    """Insert a new task."""
    raise NotImplementedError


def get_task(conn: sqlite3.Connection, task_id: str) -> Task | None:
    """Return a task, or None if there is no such task."""
    raise NotImplementedError


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
    raise NotImplementedError
