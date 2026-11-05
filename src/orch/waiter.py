"""orch wait: wait for a task's pull request to be merged or closed."""

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from orch.models import TaskState

CHECK_FAILURE_LIMIT = 10


class MergeWaitError(Exception):
    """The task cannot be waited on."""


class MergeCheckError(Exception):
    """Too many checks in a row failed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class MergeWaitReport:
    """How a wait ended. pr_state is None when no check has succeeded."""

    task_id: str
    state: TaskState
    pr_url: str
    pr_state: str | None
    merged: bool
    merged_at: str | None
    checks: int


def wait_for_merge(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    token: Callable[[], str],
    interval_sec: int = 60,
    max_wait_min: int = 90,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> MergeWaitReport:
    """Check a pull request until it is merged, closed or the limit is reached."""
    raise NotImplementedError
