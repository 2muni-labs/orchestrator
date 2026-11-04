"""Slot reservation, path locks, dependencies and the list of runnable tasks."""

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from orch.config import Limits
from orch.models import Task, TaskState
from orch.taskfile import TaskSpec


class DispatchError(Exception):
    """A task cannot be dispatched as it stands."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Decision:
    """Where a judged task ended up. waiting_for is empty when it is reserved."""

    state: TaskState
    waiting_for: tuple[str, ...] = ()


def globs_overlap(first: str, second: str) -> bool:
    """Return whether two path globs could reach the same file."""
    raise NotImplementedError


def expire_reservations(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> tuple[str, ...]:
    """Return expired reservations to queued, keeping their locks."""
    raise NotImplementedError


def try_reserve(
    conn: sqlite3.Connection,
    task_id: str,
    expected: TaskState,
    *,
    modify: Iterable[str],
    limits: Limits,
    ttl_min: int,
    now: datetime | None = None,
) -> Decision:
    """Judge one task atomically and move it to reserved or queued."""
    raise NotImplementedError


def dispatch(
    conn: sqlite3.Connection,
    spec: TaskSpec,
    *,
    limits: Limits,
    ttl_min: int,
    now: datetime | None = None,
) -> Decision:
    """Record a prepared task's test commit, then judge it."""
    raise NotImplementedError


def next_reservations(
    conn: sqlite3.Connection,
    *,
    modify_of: Callable[[Task], Iterable[str]],
    limits: Limits,
    ttl_min: int,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Judge all queued tasks, oldest first, and return the newly reserved ids."""
    raise NotImplementedError
