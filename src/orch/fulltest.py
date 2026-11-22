"""Daily full test run of a target's default branch tip in a temporary worktree."""

import fcntl
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from orch import gate, gitops, sandbox, store
from orch.config import Target, TargetPaths
from orch.models import TaskState


class FulltestOutcome(StrEnum):
    """Outcome of one full test run."""

    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SETUP_FAILED = "setup_failed"
    SANDBOX_FAILED = "sandbox_failed"
    BUSY = "busy"
    ERROR = "error"


@dataclass(frozen=True, slots=True, kw_only=True)
class FulltestReport:
    """What one full test run of one target found."""

    target: str
    outcome: FulltestOutcome
    commit: str | None = None
    exit_code: int | None = None
    log_path: str | None = None
    started_at: str
    duration_sec: int = 0
    detail: str | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _cleanup(repo: Path, worktree: Path) -> None:
    """Attempt every cleanup step even when git removal fails."""
    try:
        gitops.remove_worktree(repo, worktree, force=True)
    except gitops.GitError, OSError:
        pass
    try:
        gitops.prune_worktrees(repo)
    except gitops.GitError, OSError:
        pass
    if worktree.exists():
        try:
            shutil.rmtree(worktree)
            gitops.prune_worktrees(repo)
        except gitops.GitError, OSError:
            pass


def _save_report(path: Path, report: FulltestReport) -> None:
    """Atomically replace the previous report without leaving temporary files."""
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = stream.name
            json.dump(asdict(report), stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def run_fulltest(
    conn: sqlite3.Connection,
    target: Target,
    paths: TargetPaths,
    *,
    timeout_sec: int,
    now: Callable[[], datetime] = _utc_now,
) -> FulltestReport:
    """Test the default branch tip of one target in a temporary worktree."""
    started = now()
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    directory = paths.root / "fulltest"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return FulltestReport(
                target=target.name,
                outcome=FulltestOutcome.BUSY,
                started_at=started.isoformat(),
                duration_sec=int((now() - started).total_seconds()),
                detail="locked",
            )
        try:
            if store.list_tasks(conn, states=(TaskState.GATING,)):
                return FulltestReport(
                    target=target.name,
                    outcome=FulltestOutcome.BUSY,
                    started_at=started.isoformat(),
                    duration_sec=int((now() - started).total_seconds()),
                    detail="gating",
                )
            commit = None
            log_path = None
            exit_code = None
            detail = None
            worktree = directory / f"wt-{stamp}"
            created = False
            try:
                gitops.fetch(target.path)
                commit = gitops.rev_parse(
                    target.path, f"origin/{target.default_branch}"
                )
                gitops.add_detached_worktree(target.path, worktree, commit)
                created = True
                log = paths.logs / f"fulltest.{stamp}.log"
                log_path = str(log)
                setup_code = gate.run_command(
                    target.setup_command,
                    worktree,
                    log,
                    sandbox=False,
                    timeout_sec=timeout_sec,
                )
                if setup_code != 0:
                    outcome = FulltestOutcome.SETUP_FAILED
                else:
                    try:
                        test_code = gate.run_command(
                            target.test_command,
                            worktree,
                            log,
                            sandbox=True,
                            timeout_sec=timeout_sec,
                        )
                    except sandbox.SandboxError:
                        outcome = FulltestOutcome.SANDBOX_FAILED
                    else:
                        if test_code is None:
                            outcome = FulltestOutcome.TIMEOUT
                        else:
                            exit_code = test_code
                            outcome = (
                                FulltestOutcome.PASSED
                                if test_code == 0
                                else FulltestOutcome.FAILED
                            )
            except (gitops.GitError, OSError) as exc:
                outcome = FulltestOutcome.ERROR
                detail = type(exc).__name__
            finally:
                if created or worktree.exists():
                    _cleanup(target.path, worktree)
            report = FulltestReport(
                target=target.name,
                outcome=outcome,
                commit=commit,
                exit_code=exit_code,
                log_path=log_path,
                started_at=started.isoformat(),
                duration_sec=int((now() - started).total_seconds()),
                detail=detail,
            )
            _save_report(directory / "last.json", report)
            return report
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
