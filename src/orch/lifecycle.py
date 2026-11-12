"""orch prepare, retry, resume, discard and kill, and moving a blocked run."""

import os
import sqlite3
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from orch import (
    gate,
    github,
    gitops,
    models,
    results,
    runner,
    scheduler,
    store,
    taskfile,
)
from orch.config import Limits, Target, TargetPaths
from orch.models import (
    FailureReason,
    GateResult,
    Outcome,
    Review,
    Severity,
    Task,
    TaskState,
    Verdict,
)
from orch.scheduler import Decision


class LifecycleError(Exception):
    """A lifecycle command cannot proceed. The task's state did not change."""


def _read_spec(path: Path) -> tuple[str, taskfile.TaskSpec]:
    """Read a draft or saved spec without requiring its filename to match its id."""
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            text = stream.read()
        return text, taskfile.parse_task(text)
    except (OSError, UnicodeError, taskfile.TaskFileError) as exc:
        raise LifecycleError(str(exc)) from exc


def _write_text(path: Path, text: str) -> None:
    """Replace a text file atomically, removing the temporary file on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise


def _create_text(path: Path, text: str) -> None:
    """Publish a complete text file without replacing an existing entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise LifecycleError(f"Task spec already exists: {path}") from exc
    finally:
        os.unlink(temporary)


def _get_task(conn: sqlite3.Connection, task_id: str) -> Task:
    task = store.get_task(conn, task_id)
    if task is None:
        raise LifecycleError(f"Task not found: {task_id}")
    return task


def _reset(worktree: str, test_sha: str) -> None:
    try:
        gitops.reset_to(Path(worktree), test_sha)
    except gitops.GitError as exc:
        raise LifecycleError(str(exc)) from exc


def _block_review(paths: TargetPaths, task_id: str) -> Review | None:
    """Also accept the archived review after an interrupted retry."""
    for path in (paths.review_path(task_id), paths.review_path(task_id, 1)):
        try:
            review = results.load_review(path, task_id)
        except results.ReportError:
            continue
        if review.verdict == Verdict.BLOCK:
            return review
    return None


def _failure_section(
    paths: TargetPaths,
    task_id: str,
    failed_gates: tuple[GateResult, ...],
    review: Review | None,
) -> str:
    lines = ["\n\n# Previous failure (attempt 1)\n\n"]
    try:
        result = results.load_result(paths.results / f"{task_id}.json", task_id)
    except results.ReportError:
        pass
    else:
        lines.extend(
            (
                f"status: {result.status}\n",
                f"failure_reason: {result.failure_reason}\n",
                f"summary: {result.summary}\n",
                "unresolved:\n",
            )
        )
        lines.extend(f"- {item}\n" for item in result.unresolved)
    lines.extend(f"{item.gate}: {item.detail}\n" for item in failed_gates)
    if review is not None:
        lines.extend(
            f"{finding.severity} {finding.path}: {finding.summary}\n"
            for finding in review.findings
            if finding.severity in (Severity.P0, Severity.P1)
        )
    return "".join(lines)


def prepare_task(
    conn: sqlite3.Connection,
    spec_path: Path,
    target: Target,
    paths: TargetPaths,
    *,
    now: datetime | None = None,
) -> Task:
    """Validate a spec, create the task's worktree and register it as prepared."""
    text, spec = _read_spec(spec_path)
    if spec.target != target.name:
        raise LifecycleError(f"Task target must be {target.name}: {spec.id}")
    try:
        taskfile.check_modify(spec.modify, target.shared_paths)
    except taskfile.TaskFileError as exc:
        raise LifecycleError(str(exc)) from exc
    if store.get_task(conn, spec.id) is not None:
        raise LifecycleError(f"Task already registered: {spec.id}")
    missing = []
    for dependency_id in spec.depends_on:
        dependency = store.get_task(conn, dependency_id)
        if dependency is None or dependency.target != target.name:
            missing.append(dependency_id)
    if missing:
        raise LifecycleError(f"Dependency not registered: {', '.join(missing)}")

    saved_path = paths.tasks / f"{spec.id}.md"
    save_spec = spec_path.resolve() != saved_path.resolve()
    if save_spec and os.path.lexists(saved_path):
        raise LifecycleError(f"Task spec already exists: {saved_path}")

    worktree = paths.worktrees / spec.id
    branch = f"task/{spec.id}"
    try:
        gitops.fetch(target.path)
        base = gitops.rev_parse(target.path, f"origin/{target.default_branch}")
        gitops.create_worktree(target.path, worktree, branch, base)
    except gitops.GitError as exc:
        raise LifecycleError(str(exc)) from exc

    saved = False
    try:
        if save_spec:
            _create_text(saved_path, text)
            saved = True
        exit_code = gate.run_command(
            target.setup_command,
            worktree,
            paths.logs / f"{spec.id}.setup.log",
            sandbox=False,
        )
        if exit_code is None or exit_code != 0:
            raise LifecycleError(f"Setup failed: {spec.id} (exit code {exit_code})")
        at = (now if now is not None else datetime.now(UTC)).isoformat()
        task = Task(
            id=spec.id,
            target=spec.target,
            title=spec.title,
            grade=spec.grade,
            lane=spec.lane,
            model=spec.model,
            effort=spec.effort,
            state=TaskState.PREPARED,
            timeout_sec=spec.timeout_min * 60,
            attempt=0,
            base_sha=base,
            depends_on=spec.depends_on,
            worktree=str(worktree),
            created_at=at,
            updated_at=at,
        )
        with store.transaction(conn):
            store.insert_task(conn, task)
        return task
    except BaseException:
        # Finish every cleanup before propagating the first cleanup interrupt.
        cleanups = [
            lambda: gitops.remove_worktree(target.path, worktree, force=True),
            lambda: gitops.delete_branch(target.path, branch),
        ]
        if saved:
            cleanups.append(saved_path.unlink)
        interrupted: BaseException | None = None
        for cleanup in cleanups:
            try:
                cleanup()
            except Exception:
                pass
            except BaseException as exc:
                if interrupted is None:
                    interrupted = exc
        if interrupted is not None:
            raise interrupted from None
        raise


def settle_run(conn: sqlite3.Connection, task_id: str) -> TaskState:
    """Move a judging task whose last run was blocked to blocked; return its state."""
    task = _get_task(conn, task_id)
    if task.state == TaskState.JUDGING:
        run = store.latest_run(conn, task_id)
        if run is not None and run.outcome in (Outcome.BLOCKED, Outcome.BLOCKED_QUOTA):
            store.transition(
                conn, task_id, TaskState.JUDGING, TaskState.BLOCKED, actor="orch"
            )
            return TaskState.BLOCKED
    return task.state


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
    task = _get_task(conn, task_id)
    if task.state != TaskState.JUDGING or task.attempt != 1:
        raise LifecycleError(f"Retry needs a judging task on attempt 1: {task_id}")
    if not task.worktree or not task.test_sha:
        raise LifecycleError(f"Task needs a worktree and test commit: {task_id}")
    run = store.latest_run(conn, task_id)
    if run is None or run.outcome not in (
        Outcome.FAILED,
        Outcome.PARTIAL,
        Outcome.DONE,
    ):
        raise LifecycleError(f"No failed run to retry: {task_id}")
    if run.attempt != task.attempt:
        raise LifecycleError(f"Run attempt does not match task attempt: {task_id}")
    failed_gates = tuple(
        item
        for item in store.gate_results(conn, task_id, task.attempt)
        if not item.passed
    )
    review = _block_review(paths, task_id)
    if run.outcome == Outcome.DONE and not failed_gates and review is None:
        raise LifecycleError(f"No failure evidence to retry: {task_id}")
    spec_path = paths.tasks / f"{task_id}.md"
    text, spec = _read_spec(spec_path)

    _reset(task.worktree, task.test_sha)
    if "# Previous failure (attempt 1)" not in text:
        _write_text(
            spec_path, text + _failure_section(paths, task_id, failed_gates, review)
        )
    for current, archive in (
        (paths.review_path(task_id), paths.review_path(task_id, 1)),
        (paths.triage_path(task_id), paths.triage_path(task_id, 1)),
    ):
        if current.exists():
            archive.parent.mkdir(parents=True, exist_ok=True)
            os.replace(current, archive)
    with store.transaction(conn):
        store.update_task(
            conn,
            task_id,
            attempt=2,
            at=(now if now is not None else datetime.now(UTC)).isoformat(),
        )
        return scheduler.try_reserve(
            conn,
            task_id,
            TaskState.JUDGING,
            modify=spec.modify,
            limits=limits,
            ttl_min=ttl_min,
            now=now,
        )


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
    task = _get_task(conn, task_id)
    if task.state not in (TaskState.BLOCKED, TaskState.ORPHANED):
        raise LifecycleError(f"Resume needs a blocked or orphaned task: {task_id}")
    if not task.worktree or not task.test_sha:
        raise LifecycleError(f"Task needs a worktree and test commit: {task_id}")
    _, spec = _read_spec(paths.tasks / f"{task_id}.md")
    discarded_dependency = False
    for dependency_id in task.depends_on:
        dependency = store.get_task(conn, dependency_id)
        if dependency is not None and dependency.state == TaskState.DISCARDED:
            discarded_dependency = True
            break
    if not keep and not discarded_dependency:
        _reset(task.worktree, task.test_sha)
    return scheduler.try_reserve(
        conn,
        task_id,
        task.state,
        modify=spec.modify,
        limits=limits,
        ttl_min=ttl_min,
        now=now,
    )


def discard_task(
    conn: sqlite3.Connection,
    task_id: str,
    target: Target,
    *,
    token: str | None = None,
) -> None:
    """Remove a task's worktree and local branch, and move it to discarded."""
    task = _get_task(conn, task_id)
    if not models.can_transition(task.state, TaskState.DISCARDED):
        raise LifecycleError(f"Task cannot be discarded from {task.state}: {task_id}")
    if task.pr_url:
        if not token:
            raise LifecycleError(f"Token required to check pull request: {task_id}")
        try:
            status = github.pull_request_status(url=task.pr_url, token=token)
        except github.GitHubError as exc:
            raise LifecycleError(str(exc)) from exc
        if status.state != "CLOSED":
            raise LifecycleError(f"Pull request must be closed: {task_id}")
    try:
        if task.worktree and Path(task.worktree).exists():
            gitops.remove_worktree(target.path, Path(task.worktree), force=True)
        gitops.prune_worktrees(target.path)
        branch = f"task/{task_id}"
        try:
            gitops.rev_parse(target.path, f"refs/heads/{branch}")
        except gitops.GitError:
            pass
        else:
            gitops.delete_branch(target.path, branch)
    except gitops.GitError as exc:
        raise LifecycleError(str(exc)) from exc
    store.transition(conn, task_id, task.state, TaskState.DISCARDED, actor="orch")


@dataclass(frozen=True, slots=True, kw_only=True)
class KillReport:
    """How orch kill ended a running task."""

    task_id: str
    state: TaskState
    outcome: Outcome
    failure_reason: FailureReason | None
    finished_by: str
    signalled: bool


def kill_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    paths: TargetPaths,
    environ: Mapping[str, str],
    settle_sec: float = 30.0,
    grace_sec: float = runner.TERM_GRACE_SEC,
    poll_sec: float = 0.2,
    alive: Callable[[int, str], bool] = runner.process_alive,
) -> KillReport:
    """Stop a running task and record FAILED/aborted exactly once."""
    task = _get_task(conn, task_id)
    if task.state != TaskState.RUNNING:
        raise LifecycleError(f"Kill needs a running task: {task_id}")
    run = store.latest_run(conn, task_id)
    if run is None or run.ended_at is not None:
        raise LifecycleError(f"No unfinished run to kill: {task_id}")
    run_id = run.run_id
    if run_id is None:
        raise LifecycleError(f"Run has no id: {task_id}")

    signalled = False
    finished_by = "runner"
    if store.request_kill(conn, run_id, at=datetime.now(UTC).isoformat()):
        if run.pid is not None and run.pid_started_at:
            signalled = runner.stop_pid(
                run.pid,
                run.pid_started_at,
                alive=alive,
                grace_sec=grace_sec,
                poll_sec=poll_sec,
            )
        deadline = time.monotonic() + settle_sec
        while True:
            run = store.get_run(conn, run_id)
            if run is not None and run.ended_at is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with store.transaction(conn):
                    run = store.get_run(conn, run_id)
                    if run is not None and run.ended_at is None:
                        log_path = runner.latest_log_path(paths.logs, task_id)
                        rejected = runner.rejected_path(
                            log_path or paths.logs / f"{task_id}.1.log"
                        )
                        if task.worktree:
                            runner._reject_report(
                                Path(task.worktree) / ".orch-result.json", rejected
                            )
                        try:
                            _, spec = _read_spec(paths.tasks / f"{task_id}.md")
                            verify = spec.verify
                        except LifecycleError:
                            verify = ""
                        result = results.substitute_result(
                            task_id=task_id,
                            status=Outcome.FAILED,
                            failure_reason=FailureReason.ABORTED,
                            summary=runner.KILL_SUMMARY,
                            verification_command=verify,
                            log_path=(
                                runner.log_label(log_path, environ)
                                if log_path is not None
                                else ""
                            ),
                        )
                        results.write_result(paths.results / f"{task_id}.json", result)
                        ended_at = datetime.now(UTC).isoformat()
                        store.finish_run(
                            conn,
                            run_id,
                            ended_at=ended_at,
                            exit_code=None,
                            outcome=Outcome.FAILED,
                            failure_reason=FailureReason.ABORTED,
                        )
                        store.transition(
                            conn,
                            task_id,
                            TaskState.RUNNING,
                            TaskState.JUDGING,
                            actor="orch",
                            note="kill",
                            at=ended_at,
                        )
                        finished_by = "kill"
                break
            time.sleep(min(poll_sec, remaining))

    run = store.get_run(conn, run_id)
    task = _get_task(conn, task_id)
    if run is None or run.ended_at is None or run.outcome is None:
        raise LifecycleError(f"Run did not finish: {task_id}")
    return KillReport(
        task_id=task_id,
        state=task.state,
        outcome=run.outcome,
        failure_reason=run.failure_reason,
        finished_by=finished_by,
        signalled=signalled,
    )
