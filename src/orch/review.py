"""orch review save: store a review verdict and validate a BLOCK triage."""

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from orch.config import TargetPaths
from orch.models import Review, Severity, Task, TaskState

OVERRIDE_LABEL = "review-overridden"
OVERRIDE_HEADING = "## Review BLOCK overridden"


class ReviewFlowError(Exception):
    """A review or triage was refused. The task's state did not change."""


class TriageVerdict(StrEnum):
    """Why a P0 or P1 finding does not stop the pull request."""

    WRONG_FINDING = "wrong_finding"
    INSUFFICIENT_TESTS = "insufficient_tests"


@dataclass(frozen=True, slots=True, kw_only=True)
class TriageDecision:
    """The main agent's judgment on one P0 or P1 finding."""

    severity: Severity
    path: str
    verdict: TriageVerdict
    decision: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Triage:
    """A parsed triage file."""

    decisions: tuple[TriageDecision, ...]
    follow_up: str | None = None
    body: str = ""


def parse_triage(text: str) -> Triage:
    """Parse a triage file and check everything that needs no review or database."""
    raise NotImplementedError


def load_triage(path: Path) -> Triage:
    """Read and parse a triage file."""
    raise NotImplementedError


def check_triage(
    conn: sqlite3.Connection, triage: Triage, review: Review, task: Task
) -> None:
    """Raise ReviewFlowError unless the triage settles the review's BLOCK."""
    raise NotImplementedError


def override_section(triage: Triage) -> str:
    """Return the pull request section that reports an overridden BLOCK."""
    raise NotImplementedError


def has_blocking_review(paths: TargetPaths, task_id: str) -> bool:
    """Return whether the task's current saved review has the verdict BLOCK."""
    raise NotImplementedError


def save_review(
    conn: sqlite3.Connection,
    task_id: str,
    text: str,
    *,
    paths: TargetPaths,
    home: Path,
    orch_home: Path,
    triage_file: Path | None = None,
) -> TaskState:
    """Validate and store a review, and move the task out of reviewing."""
    raise NotImplementedError
