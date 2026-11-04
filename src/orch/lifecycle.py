"""orch prepare, retry, resume and discard, and the move of a blocked run to blocked."""

import sqlite3
from datetime import datetime
from pathlib import Path

from orch.config import Limits, Target, TargetPaths
from orch.models import Task, TaskState
from orch.scheduler import Decision


class LifecycleError(Exception):
    """A lifecycle command cannot proceed. The task's state did not change."""


def prepare_task(
    conn: sqlite3.Connection,
    spec_path: Path,
    target: Target,
    paths: TargetPaths,
    *,
    now: datetime | None = None,
) -> Task:
    """Validate a spec, create the task's worktree and register it as prepared."""
    raise NotImplementedError


def settle_run(conn: sqlite3.Connection, task_id: str) -> TaskState:
    """Move a judging task whose last run was blocked to blocked; return its state."""
    raise NotImplementedError


def retry_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    paths: TargetPaths,
    limits: Limits,
    ttl_min: int,
    now: datetime | None = None,
) -> Decision:
    """Start a failed task's second attempt from its test commit."""
    raise NotImplementedError


def resume_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    paths: TargetPaths,
    limits: Limits,
    ttl_min: int,
    keep: bool = False,
    now: datetime | None = None,
) -> Decision:
    """Restart a blocked or orphaned task without using an attempt."""
    raise NotImplementedError


def discard_task(
    conn: sqlite3.Connection,
    task_id: str,
    target: Target,
    *,
    token: str | None = None,
) -> None:
    """Remove a task's worktree and local branch, and move it to discarded."""
    raise NotImplementedError
