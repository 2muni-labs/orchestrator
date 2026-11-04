"""Validate, write and collect result and review reports."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from orch.models import FailureReason, Outcome, Review

SUMMARY_LIMIT = 700
MAX_REPORT_BYTES = 1024 * 1024


class ReportError(Exception):
    """A result or review report is missing or invalid."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskResult:
    """A validated result report."""

    task_id: str
    status: Outcome
    failure_reason: FailureReason | None
    summary: str
    changed_paths: tuple[str, ...]
    out_of_scope_changes: tuple[str, ...]
    verification_command: str
    verification_passed: bool
    unresolved: tuple[str, ...]
    log_path: str


def parse_result(data: object, task_id: str) -> TaskResult:
    """Validate decoded JSON against schemas/result.json and the summary limit."""
    raise NotImplementedError


def load_result(path: Path, task_id: str) -> TaskResult:
    """Read and validate a result file."""
    raise NotImplementedError


def write_result(path: Path, result: TaskResult) -> None:
    """Write a result file atomically."""
    raise NotImplementedError


def substitute_result(
    *,
    task_id: str,
    status: Outcome,
    failure_reason: FailureReason | None,
    summary: str,
    verification_command: str,
    log_path: str,
) -> TaskResult:
    """Build the result that orch records when a sub-agent could not report."""
    raise NotImplementedError


def collect_result(
    source: Path, destination: Path, rejected: Path, task_id: str
) -> TaskResult:
    """Move a valid report to destination. An invalid one goes to rejected."""
    raise NotImplementedError


def parse_review(data: object, task_id: str) -> Review:
    """Validate decoded JSON against schemas/review.json."""
    raise NotImplementedError


def review_from_text(text: str, task_id: str) -> Review:
    """Validate a review given as JSON text, such as a sub-agent's final message."""
    raise NotImplementedError


def load_review(path: Path, task_id: str) -> Review:
    """Read and validate a review file."""
    raise NotImplementedError


def write_review(path: Path, review: Review) -> None:
    """Write a review file atomically."""
    raise NotImplementedError


def result_to_json(result: TaskResult) -> Mapping[str, object]:
    """Return the JSON form of a result, conforming to schemas/result.json."""
    raise NotImplementedError
