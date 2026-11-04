"""Gates G1 scope, G2 tests and G3 static checks, and the orch gate flow."""

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from orch.config import Target
from orch.models import Grade
from orch.taskfile import TaskSpec

RESULT_FILE = ".orch-result.json"
COMMAND_TIMEOUT_SEC = 15 * 60
DETAIL_LIMIT = 500
TEST_GLOBS = ("tests/**",)


class GateError(Exception):
    """The gates cannot run for this task as it stands."""


class GateStatus(StrEnum):
    """What happened to one gate."""

    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeViolations:
    """Changed paths that a task may not touch, sorted."""

    test_file_changed: tuple[str, ...] = ()
    shared_file_changed: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(
            self.test_file_changed or self.shared_file_changed or self.out_of_scope
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class GateReport:
    """The outcome of running the gates."""

    passed: bool
    gates: Mapping[str, GateStatus] = field(default_factory=dict)
    failed_gate: str | None = None
    detail: str | None = None
    log_path: str | None = None


def glob_matches(path: str, glob: str) -> bool:
    """Match a path: ** crosses directories, * and ? do not. The whole path counts."""
    raise NotImplementedError


def scope_violations(
    changed: Iterable[str],
    *,
    modify: Iterable[str],
    shared_paths: Iterable[str],
) -> ScopeViolations:
    """Compare changed paths with the allowed paths. The result file does not count."""
    raise NotImplementedError


def check_scope(
    worktree: Path,
    *,
    since: str,
    main_ref: str,
    modify: Iterable[str],
    shared_paths: Iterable[str],
) -> ScopeViolations:
    """Run the scope comparison on a worktree, looking through a temporary merge."""
    raise NotImplementedError


def run_command(command: str, worktree: Path, log_path: Path) -> int | None:
    """Run a shell command in its own group, logging output. None means a timeout."""
    raise NotImplementedError


def run_tests(
    spec: TaskSpec, target: Target, worktree: Path, log_path: Path
) -> str | None:
    """Run the G2 commands and return a failure summary, or None if they pass."""
    raise NotImplementedError


def run_gates(
    spec: TaskSpec,
    target: Target,
    worktree: Path,
    *,
    since: str,
    main_ref: str,
    log_path: Path,
) -> GateReport:
    """Run G1, G2 and G3, stopping at a failure, then check the scope again."""
    raise NotImplementedError


def review_required(grade: Grade) -> bool:
    """Return whether a passing task of this grade goes to cross-review."""
    raise NotImplementedError


def gate_task(
    conn: sqlite3.Connection,
    spec: TaskSpec,
    target: Target,
    *,
    main_ref: str,
    log_path: Path,
    home: Path,
    orch_home: Path,
) -> GateReport:
    """Run the gates on a judged task, record them, and commit on success."""
    raise NotImplementedError
