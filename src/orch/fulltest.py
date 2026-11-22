"""Daily full test run of a target's default branch tip in a temporary worktree."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from orch.config import Target, TargetPaths


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


def run_fulltest(
    conn: sqlite3.Connection,
    target: Target,
    paths: TargetPaths,
    *,
    timeout_sec: int,
    now: Callable[[], datetime] = _utc_now,
) -> FulltestReport:
    """Test the default branch tip of one target in a temporary worktree."""
    raise NotImplementedError
