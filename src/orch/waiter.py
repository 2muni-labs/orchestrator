"""orch wait: wait for a task's pull request to be merged or closed."""

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from orch import github, store
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
    started_at = clock()
    checks = 0
    consecutive_failures = 0
    pr_state: str | None = None
    while True:
        task = store.get_task(conn, task_id)
        if task is None:
            raise MergeWaitError(f"Task not found: {task_id}")
        if task.state not in {TaskState.PR_OPEN, TaskState.CONFLICT}:
            raise MergeWaitError(f"Task {task_id} cannot be waited on in {task.state}")
        if not task.pr_url:
            raise MergeWaitError(f"Task {task_id} has no pull request")

        checks += 1
        try:
            status = github.pull_request_status(url=task.pr_url, token=token())
        except (github.GitHubError, RuntimeError) as exc:
            consecutive_failures += 1
            if consecutive_failures >= CHECK_FAILURE_LIMIT:
                raise MergeCheckError(str(exc)) from exc
        else:
            consecutive_failures = 0
            pr_state = status.state
            if pr_state in {"MERGED", "CLOSED"}:
                return MergeWaitReport(
                    task_id=task_id,
                    state=task.state,
                    pr_url=task.pr_url,
                    pr_state=pr_state,
                    merged=status.merged,
                    merged_at=status.merged_at,
                    checks=checks,
                )

        remaining = max_wait_min * 60 - (clock() - started_at)
        if remaining <= 0:
            return MergeWaitReport(
                task_id=task_id,
                state=task.state,
                pr_url=task.pr_url,
                pr_state=pr_state,
                merged=False,
                merged_at=None,
                checks=checks,
            )
        sleep(min(interval_sec, remaining))
