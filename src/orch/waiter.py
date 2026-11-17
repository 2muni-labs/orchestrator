"""orch wait: wait for a task's pull request to be merged or closed."""

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from orch.config import Target
from orch.models import TaskState

CHECK_FAILURE_LIMIT = 10


class MergeWaitError(Exception):
    """The task cannot be waited on."""


class MergeCheckError(Exception):
    """Too many checks in a row failed."""


class Outcome(StrEnum):
    """How a wait ended."""

    MERGED = "merged"
    CLOSED = "closed"
    LIMIT = "limit"
    CHECKS_FAILED = "checks_failed"
    CHANGES_REQUESTED = "changes_requested"
    BEHIND = "behind"


@dataclass(frozen=True, slots=True, kw_only=True)
class MergeWaitReport:
    """How a wait ended. pr_state is None when no check has succeeded.

    review_label is the review label on the pull request at the last successful
    check, failed_checks the required checks that failed, and notified the events
    this run tried to notify, in order.
    """

    task_id: str
    state: TaskState
    pr_url: str
    pr_state: str | None
    merged: bool
    merged_at: str | None
    checks: int
    outcome: Outcome
    review_label: str | None
    failed_checks: tuple[str, ...]
    notified: tuple[str, ...]


def _now() -> datetime:
    return datetime.now(UTC)


def wait_for_merge(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    target: Target,
    token: Callable[[], str],
    notify: Callable[[str], None],
    interval_sec: int = 60,
    max_wait_min: int = 720,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = _now,
) -> MergeWaitReport:
    """Check a pull request until it is merged, closed, needs the main, or times out."""
    raise NotImplementedError
