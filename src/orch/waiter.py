"""orch wait: wait for a task's pull request to be merged or closed."""

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from orch import github, prflow, store
from orch.config import Target
from orch.models import Grade, Task, TaskState

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


def _check_stalled_review(
    review: github.ReviewState,
    label: str,
    *,
    url: str,
    token: str,
    notify: Callable[[str], None],
    now: Callable[[], datetime],
) -> None:
    """Retry one stalled review, then ask the user if the retry also stalls."""
    added = [event for event in review.events if event.name == label and event.added]
    if not added or now() - max(event.at for event in added) < timedelta(minutes=30):
        return
    last_fix = max(
        (
            index
            for index, event in enumerate(review.events)
            if event.name == "changes-requested" and not event.added
        ),
        default=-1,
    )
    count = sum(
        event.name == label and event.added for event in review.events[last_fix + 1 :]
    )
    if count == 1:
        github.remove_label(url=url, name=label, token=token)
        github.add_label(url=url, name=label, token=token)
    elif count >= 2:
        notify("review_stalled")


def _check_review(
    task: Task,
    review: github.ReviewState,
    label: str | None,
    *,
    target: Target,
    token: str,
    notify: Callable[[str], None],
    now: Callable[[], datetime],
) -> tuple[Outcome | None, tuple[str, ...]]:
    """Apply the ordered merge-policy decisions for one successful PR query."""
    if review.state == "MERGED":
        return Outcome.MERGED, ()
    if review.state == "CLOSED":
        return Outcome.CLOSED, ()
    rules = github.check_rules(
        github=target.github, branch=target.default_branch, token=token
    )
    if not rules.names:
        raise MergeWaitError("The branch has no required checks")
    if "changes-requested" in review.labels:
        count = sum(
            event.name == "changes-requested" and event.added for event in review.events
        )
        if count == 1:
            return Outcome.CHANGES_REQUESTED, ()
        if count >= 2:
            notify("changes_requested_again")
        return None, ()
    comparison = None
    if rules.strict:
        comparison = github.compare(
            github=target.github,
            base=target.default_branch,
            head=review.head_sha,
            token=token,
        )
        if comparison.behind_by >= 1:
            return Outcome.BEHIND, ()
    if "needs-user" in review.labels:
        notify("user_wait")
        return None, ()
    if "review-overridden" in review.labels:
        return None, ()
    failed = tuple(
        name
        for name in rules.names
        if review.checks.get(name) == github.CheckResult.FAILED
    )
    if failed:
        return Outcome.CHECKS_FAILED, failed
    if any(
        review.checks.get(name) != github.CheckResult.PASSED for name in rules.names
    ):
        return None, ()
    assert task.pr_url is not None
    if label is None:
        if comparison is None:
            comparison = github.compare(
                github=target.github,
                base=target.default_branch,
                head=review.head_sha,
                token=token,
            )
        light = (
            task.grade in (Grade.L1, Grade.L2)
            and not comparison.truncated
            and not prflow.user_check(
                comparison.files, target.safety_paths, overridden=False
            )
        )
        github.add_label(
            url=task.pr_url,
            name="verify-light" if light else "verify-deep",
            token=token,
        )
    elif "user-approved" not in review.labels:
        _check_stalled_review(
            review, label, url=task.pr_url, token=token, notify=notify, now=now
        )
    return None, ()


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
    deadline = clock() + max_wait_min * 60
    checks = 0
    failures = 0
    review: github.ReviewState | None = None
    review_label: str | None = None
    notified: list[str] = []
    failed_checks: tuple[str, ...] = ()
    outcome = Outcome.LIMIT

    def notify_once(event: str) -> None:
        if event not in notified:
            notified.append(event)
            try:
                notify(event)
            except Exception:
                pass

    while True:
        task = store.get_task(conn, task_id)
        if task is None or task.state not in (TaskState.PR_OPEN, TaskState.CONFLICT):
            raise MergeWaitError(f"Task must be pr_open or conflict: {task_id}")
        if not task.pr_url:
            raise MergeWaitError(f"Task needs a pull request URL: {task_id}")
        checks += 1
        try:
            try:
                check_token = token()
            except RuntimeError:
                raise github.GitHubError("Token issuance failed") from None
            review = github.review_state(url=task.pr_url, token=check_token)
            review_label = next(
                (label for label in github.REVIEW_LABELS if label in review.labels),
                None,
            )
            decision, failed_checks = _check_review(
                task,
                review,
                review_label,
                target=target,
                token=check_token,
                notify=notify_once,
                now=now,
            )
        except github.GitHubError:
            failures += 1
            if failures >= CHECK_FAILURE_LIMIT:
                raise MergeCheckError("Ten consecutive merge checks failed") from None
        else:
            failures = 0
            if decision is not None:
                outcome = decision
                break
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(interval_sec, remaining))

    return MergeWaitReport(
        task_id=task_id,
        state=task.state,
        pr_url=task.pr_url,
        pr_state=review.state if review else None,
        merged=review.merged if review else False,
        merged_at=review.merged_at if review else None,
        checks=checks,
        outcome=outcome,
        review_label=review_label,
        failed_checks=failed_checks if outcome == Outcome.CHECKS_FAILED else (),
        notified=tuple(notified),
    )
