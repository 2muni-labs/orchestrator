"""orch track sync: keep issues and project Status in step with state.db (ADR 0024)."""

import fcntl
import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from orch import config, github, store, tracking
from orch.models import Task, TaskState
from orch.redact import RedactionError
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
        return {
            "issue_state": self.issue_state,
            "reason": self.reason,
            "status": self.status,
            "pr_url": self.pr_url,
            "task_id": self.task_id,
        }


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
    return f"<!-- orch-task:{target}:{record_id} -->"


def task_wanted(state: TaskState) -> tuple[str, str | None, str]:
    """Return the issue state, close reason and Status for a task state."""
    if state == TaskState.PREPARED:
        return "open", None, "Todo"
    if state in {TaskState.BLOCKED, TaskState.CONFLICT, TaskState.ORPHANED}:
        return "open", None, "Blocked"
    if state == TaskState.CLOSED:
        return "closed", "completed", "Done"
    if state == TaskState.DISCARDED:
        return "closed", "not_planned", "Cancelled"
    return "open", None, "In Progress"


def compute_wanted(
    record: Record, *, tasks: Sequence[Task], pr_state: str | None
) -> Wanted | None:
    """Return what a record should show, or None to leave it alone."""
    pr_url = record.pr_url
    task_id = None
    if record.completion_owner == "task" and tasks:
        task = max(tasks, key=lambda task: (task.updated_at, task.id))
        issue_state, reason, status = task_wanted(task.state)
        pr_url = task.pr_url or record.pr_url
        task_id = task.id
    elif record.completion_owner == "harness" and pr_state is not None:
        issue_state, reason, status = {
            "OPEN": ("open", None, "In Progress"),
            "MERGED": ("closed", "completed", "Done"),
            "CLOSED": ("closed", "not_planned", "Cancelled"),
        }[pr_state]
    elif record.completion_owner in {"manual", "aggregate"}:
        if record.accepted_at is None:
            return None
        issue_state, reason, status = "closed", "completed", "Done"
    elif record.issue_url is None:
        issue_state, reason, status = "open", None, "Todo"
    else:
        return None
    return Wanted(
        issue_state=issue_state,
        reason=reason,
        status=status,
        pr_url=pr_url,
        task_id=task_id,
    )


def progress_block(wanted: Wanted, *, now: datetime) -> str:
    """Return the managed block text for a record."""
    stamp = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")
    return "\n".join(
        (
            "orch 자동 기록 (이 블록은 orch track sync가 고칩니다)",
            f"상태: {wanted.status}",
            f"작업: {wanted.task_id or '-'}",
            f"PR: {wanted.pr_url or '-'}",
            f"갱신: {stamp}",
        )
    )


class _RecordError(Exception):
    """A record failed a sync-specific check; its message is an error code."""


def _error_code(error: Exception) -> str:
    if isinstance(error, _RecordError):
        return str(error)
    if isinstance(error, github.GitHubNotFoundError):
        return "not_found"
    if isinstance(error, RedactionError):
        return "redaction_failed"
    if isinstance(error, tracking.TrackingError):
        return "tracking_error"
    return "api_error"


def _issue_number(api: object, *, repo: str, url: str) -> int:
    try:
        return api.issue_number(github=repo, url=url)
    except github.GitHubError:
        raise _RecordError("scope_mismatch") from None


def _replace_block(api: object, body: str, block: str) -> str:
    try:
        return api.replace_managed_block(body, block)
    except ValueError:
        raise _RecordError("block_malformed") from None


def _dry_run_actions(
    record: Record,
    wanted: Wanted,
    *,
    api: object,
    repo: str,
    project: github.Project,
    token: str,
) -> tuple[str, ...]:
    if record.issue_url is None:
        return "create_issue", "add_item", "set_status"
    number = _issue_number(api, repo=repo, url=record.issue_url)
    issue = api.get_issue(github=repo, number=number, token=token)
    actions = []
    if wanted.issue_state == "closed" and (
        issue.state != "closed" or issue.state_reason != wanted.reason
    ):
        actions.append("close")
    elif wanted.issue_state == "open" and issue.state != "open":
        actions.append("reopen")
    item = api.issue_project_item(
        github=repo, number=number, project=project, token=token
    )
    if item is None:
        actions.extend(("add_item", "set_status"))
    elif item.status != wanted.status:
        actions.append("set_status")
    return tuple(actions)


def _write_record(
    record: Record,
    wanted: Wanted,
    *,
    conn: sqlite3.Connection,
    target: str,
    repo: str,
    project: github.Project,
    token: str,
    api: object,
    home: Path,
    orch_home: Path,
    now: datetime,
    sleep: Callable[[float], None],
) -> None:
    """Write one record and verify the issue and Status by reading them back."""
    block = progress_block(wanted, now=now)
    created = False
    if record.issue_url is not None:
        number = _issue_number(api, repo=repo, url=record.issue_url)
        issue = api.get_issue(github=repo, number=number, token=token)
    else:
        issue_marker = marker(target, record.record_id)
        matches = api.find_issues_by_marker(
            github=repo, marker=issue_marker, token=token
        )
        if len(matches) > 1:
            raise _RecordError("duplicate_issues")
        if matches:
            issue = api.get_issue(github=repo, number=matches[0], token=token)
        else:
            issue = api.create_issue(
                github=repo,
                title=record.title,
                body=_replace_block(api, issue_marker + "\n", block),
                token=token,
                home=home,
                orch_home=orch_home,
            )
            created = True
        number = issue.number
        tracking.set_issue_url(
            conn, target=target, record_id=record.record_id, issue_url=issue.url
        )
    if not created:
        body = _replace_block(api, issue.body, block)
        if body != issue.body:
            api.update_issue_body(
                github=repo,
                number=number,
                body=body,
                token=token,
                home=home,
                orch_home=orch_home,
            )
    just_closed = wanted.issue_state == "closed" and (
        issue.state != "closed" or issue.state_reason != wanted.reason
    )
    if just_closed or (wanted.issue_state == "open" and issue.state != "open"):
        api.set_issue_state(
            github=repo,
            number=number,
            state=wanted.issue_state,
            reason=wanted.reason,
            token=token,
        )
    api.add_project_item(project=project, issue=issue, token=token)
    if just_closed:
        sleep(SETTLE_SEC)
    item = api.issue_project_item(
        github=repo, number=number, project=project, token=token
    )
    if item is None:
        raise _RecordError("item_missing")
    if item.status != wanted.status:
        api.set_item_status(
            project=project, item_id=item.id, status=wanted.status, token=token
        )
    issue = api.get_issue(github=repo, number=number, token=token)
    item = api.issue_project_item(
        github=repo, number=number, project=project, token=token
    )
    if wanted.status == "Cancelled" and item is not None and item.status == "Done":
        api.set_item_status(
            project=project, item_id=item.id, status="Cancelled", token=token
        )
        item = api.issue_project_item(
            github=repo, number=number, project=project, token=token
        )
    if (
        issue.state != wanted.issue_state
        or (wanted.issue_state == "closed" and issue.state_reason != wanted.reason)
        or item is None
        or item.status != wanted.status
    ):
        raise _RecordError("read_back_mismatch")


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
    imported = False
    checked = skipped = unchanged = 0
    written: list[str] = []
    failed: list[tuple[str, str]] = []
    changes: list[Change] = []
    notified = False
    at = now.isoformat()

    def report(outcome: str) -> SyncReport:
        return SyncReport(
            target=target,
            outcome=outcome,
            imported=imported,
            checked=checked,
            skipped=skipped,
            unchanged=unchanged,
            written=tuple(written),
            failed=tuple(failed),
            changes=tuple(changes),
            notified=notified,
        )

    def finish(error: str | None) -> SyncReport:
        nonlocal notified
        if not dry_run:
            run = tracking.record_run(conn, target=target, at=at, error=error)
            if error is not None and run.consecutive_failures == FAILURE_NOTIFY_AFTER:
                try:
                    notify_failure()
                except Exception:
                    pass
                else:
                    notified = True
        return report("failed" if error else "synced")

    def fail_record(record_id: str, error: str) -> None:
        failed.append((record_id, error))
        if not dry_run:
            tracking.record_failure(
                conn, target=target, record_id=record_id, error=error, at=at
            )

    root = config.target_paths(orch_home, target).root
    with ExitStack() as stack:
        if not dry_run:
            lock = stack.enter_context((root / LOCK_NAME).open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return report("locked")
            stack.callback(fcntl.flock, lock, fcntl.LOCK_UN)

        repo = tracking.repository(conn, target)
        source = None
        if repo is None:
            path = root / FILE_NAME
            if not path.exists():
                return report("not_configured")
            if dry_run:
                source = tracking.load_file(path, target=target)
                repo = source.repository
            else:
                tracking.import_file(conn, path, target=target, at=at)
                imported = True
                repo = tracking.repository(conn, target)
        if repo.lower() != github_repo.lower():
            failed.append(("*", "repository_mismatch"))
            return finish("repository_mismatch")
        try:
            token = token_provider()
        except Exception:
            failed.append(("*", "auth_failed"))
            return finish("auth_failed")

        records = (
            source.records
            if source is not None
            else tracking.list_records(conn, target)
        )
        projects = (
            source.projects
            if source is not None
            else tracking.list_projects(conn, target)
        )
        project_keys = {record.project_key for record in records}
        project_cache: dict[str, github.Project] = {}
        owner = repo.split("/")[0]
        for project in projects:
            if project.key not in project_keys:
                continue
            try:
                number = api.project_number(owner=owner, url=project.url)
                project_cache[project.key] = api.get_project(
                    owner=owner, number=number, token=token
                )
            except github.GitHubError:
                # A missing cache entry fails every wanted record in this project,
                # including records whose verified values have not changed.
                continue

        for record in sorted(records, key=lambda record: record.record_id):
            try:
                ids = (
                    tuple(
                        task_id
                        for task_id, ref in source.tasks.items()
                        if ref == record.record_id
                    )
                    if source is not None
                    else tracking.task_ids(conn, target, record.record_id)
                )
                tasks = [
                    task for task_id in ids if (task := store.get_task(conn, task_id))
                ]
                pr_state = None
                if record.completion_owner == "harness" and record.pr_url is not None:
                    pr_state = api.pull_request_status(
                        url=record.pr_url, token=token
                    ).state
                wanted = compute_wanted(record, tasks=tasks, pr_state=pr_state)
                if wanted is None:
                    skipped += 1
                    continue
                checked += 1
                if record.project_key not in project_cache:
                    raise _RecordError("project_error")
                project = project_cache[record.project_key]
                if dry_run:
                    actions = _dry_run_actions(
                        record, wanted, api=api, repo=repo, project=project, token=token
                    )
                    if actions:
                        changes.append(
                            Change(
                                record_id=record.record_id,
                                wanted=wanted,
                                actions=actions,
                            )
                        )
                    else:
                        unchanged += 1
                    continue
                state = tracking.get_state(conn, target, record.record_id)
                if (
                    state is not None
                    and state.failures == 0
                    and state.verified == wanted.as_dict()
                ):
                    unchanged += 1
                    continue
                _write_record(
                    record,
                    wanted,
                    conn=conn,
                    target=target,
                    repo=repo,
                    project=project,
                    token=token,
                    api=api,
                    home=home,
                    orch_home=orch_home,
                    now=now,
                    sleep=sleep,
                )
                tracking.save_verified(
                    conn,
                    target=target,
                    record_id=record.record_id,
                    verified=wanted.as_dict(),
                    at=at,
                )
                written.append(record.record_id)
            except (
                _RecordError,
                github.GitHubError,
                RedactionError,
                tracking.TrackingError,
            ) as exc:
                fail_record(record.record_id, _error_code(exc))
        if dry_run:
            return report("dry_run")
        return finish("records_failed" if failed else None)
