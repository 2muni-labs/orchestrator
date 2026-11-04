"""orch pr, update and close: the flow around a task's pull request."""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from orch.config import Target
from orch.gate import GateReport
from orch.models import GateResult, Review, Task, TaskState
from orch.taskfile import TaskSpec


class PullRequestFlowError(Exception):
    """A pull request step cannot proceed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PullRequestReport:
    """How orch pr ended."""

    state: TaskState
    pr_url: str | None = None
    conflicts: tuple[str, ...] = ()
    detail: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateReport:
    """How orch update ended. Gates are None when the merge conflicted."""

    state: TaskState
    conflicts: tuple[str, ...] = ()
    gates: GateReport | None = None


def build_body(
    task: Task,
    *,
    changed: Iterable[str],
    gates: Iterable[GateResult],
    review: Review | None,
) -> str:
    """Return the minimal English pull request body."""
    raise NotImplementedError


def open_pull_request(
    conn: sqlite3.Connection,
    spec: TaskSpec,
    target: Target,
    *,
    token: str,
    log_path: Path,
    review_path: Path,
    home: Path,
    orch_home: Path,
    resolution: str | None = None,
) -> PullRequestReport:
    """Rebase, test and push a task, then open or update its pull request."""
    raise NotImplementedError


def update_pull_request(
    conn: sqlite3.Connection,
    spec: TaskSpec,
    target: Target,
    *,
    log_path: Path,
) -> UpdateReport:
    """Check an open pull request against the moved default branch, without pushing."""
    raise NotImplementedError


def close_task(
    conn: sqlite3.Connection, task_id: str, target: Target, *, token: str
) -> None:
    """Clean up a task whose pull request was merged, and move it to closed."""
    raise NotImplementedError
