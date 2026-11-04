"""Validate, write and collect result and review reports."""

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from orch.models import (
    FailureReason,
    Finding,
    Outcome,
    Review,
    Reviewer,
    Severity,
    Verdict,
)

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


def _object(data: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(data, dict) or data.keys() != keys:
        raise ReportError("Report object has missing or unexpected keys")
    return data


def _string(value: object, field: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ReportError(
            f"{field} must be a string" + (" with content" if nonempty else "")
        )
    return value


def _enum[T: StrEnum](value: object, enum: type[T], field: str) -> T:
    text = _string(value, field)
    try:
        return enum(text)
    except ValueError as exc:
        raise ReportError(f"Invalid {field}") from exc


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ReportError(f"{field} must be a list of strings")
    return tuple(_string(item, field) for item in value)


def _task_id(value: object, task_id: str) -> str:
    text = _string(value, "task_id", nonempty=True)
    if text != task_id:
        raise ReportError("Report task_id does not match the expected task")
    return text


def parse_result(data: object, task_id: str) -> TaskResult:
    """Validate decoded JSON against schemas/result.json and the summary limit."""
    report = _object(
        data,
        {
            "task_id",
            "status",
            "failure_reason",
            "summary",
            "changed_paths",
            "out_of_scope_changes",
            "verification",
            "unresolved",
            "log_path",
        },
    )
    report_task_id = _task_id(report["task_id"], task_id)
    status = _enum(report["status"], Outcome, "status")
    reason = report["failure_reason"]
    failure_reason = (
        None if reason is None else _enum(reason, FailureReason, "failure_reason")
    )
    if (status in (Outcome.FAILED, Outcome.BLOCKED)) != (failure_reason is not None):
        raise ReportError("failure_reason is inconsistent with status")
    summary = _string(report["summary"], "summary")
    unresolved = _strings(report["unresolved"], "unresolved")
    if len(summary) + sum(map(len, unresolved)) > SUMMARY_LIMIT:
        raise ReportError("summary and unresolved exceed the combined limit")
    verification = _object(report["verification"], {"command", "passed"})
    if not isinstance(verification["passed"], bool):
        raise ReportError("verification.passed must be a boolean")
    return TaskResult(
        task_id=report_task_id,
        status=status,
        failure_reason=failure_reason,
        summary=summary,
        changed_paths=_strings(report["changed_paths"], "changed_paths"),
        out_of_scope_changes=_strings(
            report["out_of_scope_changes"], "out_of_scope_changes"
        ),
        verification_command=_string(verification["command"], "verification.command"),
        verification_passed=verification["passed"],
        unresolved=unresolved,
        log_path=_string(report["log_path"], "log_path"),
    )


def _decode_json(text: str) -> object:
    try:
        return json.loads(text)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ReportError("Report is not valid JSON") from exc


def _load_json(path: Path) -> object:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            content = stream.read(MAX_REPORT_BYTES + 1)
        if len(content) > MAX_REPORT_BYTES:
            raise ReportError("Report exceeds the byte limit")
        text = content.decode("utf-8")
    except (OSError, ValueError, UnicodeError) as exc:
        raise ReportError("Cannot read report as UTF-8") from exc
    return _decode_json(text)


def load_result(path: Path, task_id: str) -> TaskResult:
    """Read and validate a result file."""
    return parse_result(_load_json(path), task_id)


def _write_json(path: Path, data: Mapping[str, object]) -> None:
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_result(path: Path, result: TaskResult) -> None:
    """Write a result file atomically."""
    _write_json(path, result_to_json(result))


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
    return parse_result(
        {
            "task_id": task_id,
            "status": status,
            "failure_reason": failure_reason,
            "summary": _string(summary, "summary")[:SUMMARY_LIMIT],
            "changed_paths": [],
            "out_of_scope_changes": [],
            "verification": {"command": verification_command, "passed": False},
            "unresolved": [],
            "log_path": log_path,
        },
        task_id,
    )


def collect_result(
    source: Path, destination: Path, rejected: Path, task_id: str
) -> TaskResult:
    """Move a valid report to destination. An invalid one goes to rejected."""
    try:
        result = load_result(source, task_id)
    except ReportError:
        if os.path.lexists(source):
            rejected.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, rejected)
        raise
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
    return result


def parse_review(data: object, task_id: str) -> Review:
    """Validate decoded JSON against schemas/review.json."""
    report = _object(data, {"task_id", "reviewer", "verdict", "findings"})
    report_task_id = _task_id(report["task_id"], task_id)
    reviewer = _enum(report["reviewer"], Reviewer, "reviewer")
    verdict = _enum(report["verdict"], Verdict, "verdict")
    if not isinstance(report["findings"], list):
        raise ReportError("findings must be a list")
    findings = []
    for item in report["findings"]:
        finding = _object(item, {"severity", "path", "summary"})
        severity = _enum(finding["severity"], Severity, "severity")
        path = _string(finding["path"], "path", nonempty=True)
        if path.startswith(("/", "~")) or "\\" in path or ".." in path.split("/"):
            raise ReportError("Finding path must be repository relative")
        findings.append(
            Finding(
                severity=severity,
                path=path,
                summary=_string(finding["summary"], "summary", nonempty=True),
            )
        )
    blocking = any(f.severity in (Severity.P0, Severity.P1) for f in findings)
    if blocking != (verdict is Verdict.BLOCK):
        raise ReportError("verdict is inconsistent with findings")
    return Review(
        task_id=report_task_id,
        reviewer=reviewer,
        verdict=verdict,
        findings=tuple(findings),
    )


def review_from_text(text: str, task_id: str) -> Review:
    """Validate a review given as JSON text, such as a sub-agent's final message."""
    return parse_review(_decode_json(text), task_id)


def load_review(path: Path, task_id: str) -> Review:
    """Read and validate a review file."""
    return parse_review(_load_json(path), task_id)


def write_review(path: Path, review: Review) -> None:
    """Write a review file atomically."""
    _write_json(
        path,
        {
            "task_id": review.task_id,
            "reviewer": review.reviewer.value,
            "verdict": review.verdict.value,
            "findings": [
                {"severity": f.severity.value, "path": f.path, "summary": f.summary}
                for f in review.findings
            ],
        },
    )


def result_to_json(result: TaskResult) -> Mapping[str, object]:
    """Return the JSON form of a result, conforming to schemas/result.json."""
    return {
        "task_id": result.task_id,
        "status": result.status.value,
        "failure_reason": (
            None if result.failure_reason is None else result.failure_reason.value
        ),
        "summary": result.summary,
        "changed_paths": list(result.changed_paths),
        "out_of_scope_changes": list(result.out_of_scope_changes),
        "verification": {
            "command": result.verification_command,
            "passed": result.verification_passed,
        },
        "unresolved": list(result.unresolved),
        "log_path": result.log_path,
    }
