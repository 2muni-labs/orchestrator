"""orch track sync: keep issues and project Status in step with state.db (ADR 0024)."""

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from orch import github
from orch.models import Task, TaskState
from orch.tracking import Record

SETTLE_SEC = 5
FAILURE_NOTIFY_AFTER = 3
LOCK_NAME = "track.lock"
FILE_NAME = "github-tracking.json"


@dataclass(frozen=True, slots=True, kw_only=True)
class Wanted:
    """The issue state, close reason, Status and summary a record should show."""

    issue_state: str
    reason: str | None
    status: str
    pr_url: str | None
    task_id: str | None

    def as_dict(self) -> dict[str, str | None]:
        """Return the five fields as the value stored after a verified write."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True, kw_only=True)
class Change:
    """What a dry run would change for one record."""

    record_id: str
    wanted: Wanted
    actions: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncReport:
    """The result of one sync run."""

    target: str
    outcome: str
    imported: bool
    checked: int
    skipped: int
    unchanged: int
    written: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]
    changes: tuple[Change, ...]
    notified: bool


def marker(target: str, record_id: str) -> str:
    """Return the issue body marker of a record."""
    raise NotImplementedError


def task_wanted(state: TaskState) -> tuple[str, str | None, str]:
    """Return the issue state, close reason and Status for a task state."""
    raise NotImplementedError


def compute_wanted(
    record: Record, *, tasks: Sequence[Task], pr_state: str | None
) -> Wanted | None:
    """Return what a record should show, or None to leave it alone."""
    raise NotImplementedError


def progress_block(wanted: Wanted, *, now: datetime) -> str:
    """Return the managed block text for a record."""
    raise NotImplementedError


def sync(
    *,
    conn: sqlite3.Connection,
    target: str,
    github_repo: str,
    orch_home: Path,
    home: Path,
    now: datetime,
    token_provider: Callable[[], str],
    notify_failure: Callable[[], object],
    dry_run: bool = False,
    api: object = github,
    sleep: Callable[[float], None] = time.sleep,
) -> SyncReport:
    """Run one sync of a target's records."""
    raise NotImplementedError
