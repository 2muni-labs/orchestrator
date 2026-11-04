"""orch review save: store a review verdict and validate a BLOCK triage."""

import os
import sqlite3
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from orch import config, results, store
from orch.config import TargetPaths
from orch.models import Review, Severity, Task, TaskState, Verdict
from orch.redact import RedactionError, redact

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
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "+++":
        raise ReviewFlowError("Triage needs TOML front matter")
    end = next(
        (i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "+++"),
        None,
    )
    if end is None:
        raise ReviewFlowError("Triage front matter is not closed")
    try:
        data = tomllib.loads("".join(lines[1:end]))
    except tomllib.TOMLDecodeError as exc:
        raise ReviewFlowError("Invalid triage TOML") from exc
    if data.keys() - {"follow_up", "decisions"}:
        raise ReviewFlowError("Unknown triage key")
    tables = data.get("decisions")
    if not isinstance(tables, list) or not tables:
        raise ReviewFlowError("Triage needs decisions")
    decisions = []
    for table in tables:
        if not isinstance(table, dict) or table.keys() != {
            "severity",
            "path",
            "verdict",
            "decision",
        }:
            raise ReviewFlowError("Invalid triage decision fields")
        if table["severity"] not in ("P0", "P1"):
            raise ReviewFlowError("Triage severity must be P0 or P1")
        path = table["path"]
        if not isinstance(path, str) or not path:
            raise ReviewFlowError("Triage path must not be empty")
        try:
            verdict = TriageVerdict(table["verdict"])
        except ValueError as exc:
            raise ReviewFlowError("Invalid triage verdict") from exc
        decision = table["decision"]
        if (
            not isinstance(decision, str)
            or not decision.strip()
            or any(not " " <= char <= "~" for char in decision)
        ):
            raise ReviewFlowError("Triage decision must be one line of English")
        if verdict is TriageVerdict.INSUFFICIENT_TESTS and not path.startswith(
            "tests/"
        ):
            raise ReviewFlowError("Insufficient tests must name a tests/ path")
        decisions.append(
            TriageDecision(
                severity=Severity(table["severity"]),
                path=path,
                verdict=verdict,
                decision=decision,
            )
        )
    needs_follow_up = any(
        item.verdict is TriageVerdict.INSUFFICIENT_TESTS for item in decisions
    )
    follow_up = data.get("follow_up")
    if needs_follow_up:
        try:
            config.validate_task_id(follow_up)
        except config.ConfigError as exc:
            raise ReviewFlowError("Insufficient tests need a valid follow-up") from exc
    elif "follow_up" in data:
        raise ReviewFlowError("Follow-up requires an insufficient_tests decision")
    body = "".join(lines[end + 1 :])
    if any(char != "\n" and not " " <= char <= "~" for char in body):
        raise ReviewFlowError("Triage body must be English text")
    return Triage(decisions=tuple(decisions), follow_up=follow_up, body=body)


def load_triage(path: Path) -> Triage:
    """Read and parse a triage file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReviewFlowError("Cannot read triage as UTF-8") from exc
    return parse_triage(text)


def check_triage(
    conn: sqlite3.Connection, triage: Triage, review: Review, task: Task
) -> None:
    """Raise ReviewFlowError unless the triage settles the review's BLOCK."""
    if review.verdict is not Verdict.BLOCK:
        raise ReviewFlowError("Triage requires a BLOCK review")
    findings = Counter(
        (finding.severity, finding.path)
        for finding in review.findings
        if finding.severity in (Severity.P0, Severity.P1)
    )
    decisions = Counter((item.severity, item.path) for item in triage.decisions)
    if findings != decisions:
        raise ReviewFlowError("Triage must match every P0 and P1 finding exactly")
    if triage.follow_up is not None:
        follow_up = store.get_task(conn, triage.follow_up)
        if (
            triage.follow_up == task.id
            or follow_up is None
            or follow_up.target != task.target
            or follow_up.state is TaskState.DISCARDED
        ):
            raise ReviewFlowError("Invalid follow-up task")


def override_section(triage: Triage) -> str:
    """Return the pull request section that reports an overridden BLOCK."""
    lines = [OVERRIDE_HEADING, ""]
    if triage.follow_up is not None:
        lines.extend([f"Follow-up task: `{triage.follow_up}`", ""])
    lines.extend(
        f"- {item.severity} `{item.path}` ({item.verdict}): {item.decision}"
        for item in triage.decisions
    )
    if body := triage.body.strip():
        lines.extend(["", body])
    return "\n".join(lines)


def has_blocking_review(paths: TargetPaths, task_id: str) -> bool:
    """Return whether the task's current saved review has the verdict BLOCK."""
    path = paths.review_path(task_id)
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ReviewFlowError("Cannot access saved review") from exc
    try:
        review = results.load_review(path, task_id)
    except results.ReportError as exc:
        raise ReviewFlowError("Invalid saved review") from exc
    return review.verdict is Verdict.BLOCK


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
    task = store.get_task(conn, task_id)
    if task is None or task.state is not TaskState.REVIEWING:
        raise ReviewFlowError("Task must exist and be reviewing")
    try:
        review = results.review_from_text(text, task_id)
    except results.ReportError as exc:
        raise ReviewFlowError("Invalid review") from exc
    triage = None
    if triage_file is not None:
        triage = load_triage(triage_file)
        check_triage(conn, triage, review, task)
        for value in (*[item.decision for item in triage.decisions], triage.body):
            try:
                safe_value = redact(value, home=home, orch_home=orch_home)
            except RedactionError as exc:
                raise ReviewFlowError("Triage contains private information") from exc
            if safe_value != value:
                raise ReviewFlowError("Triage contains a personal path")

    destination = paths.triage_path(task_id)
    temporary = None
    try:
        if triage_file is None:
            destination.unlink(missing_ok=True)
        elif triage_file.resolve() != destination.resolve():
            content = triage_file.read_bytes()
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            os.replace(temporary, destination)
    except OSError as exc:
        raise ReviewFlowError("Cannot store triage") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    results.write_review(paths.review_path(task_id), review)

    next_state = (
        TaskState.READY
        if triage is not None or review.verdict is Verdict.PASS
        else TaskState.JUDGING
    )
    notes = ["BLOCK overridden"] if triage is not None else []
    counts = Counter(finding.severity for finding in review.findings)
    minor = ", ".join(
        f"{severity}: {counts[severity]}"
        for severity in (Severity.P2, Severity.P3)
        if counts[severity]
    )
    if minor:
        notes.append(minor)
    store.transition(
        conn,
        task_id,
        TaskState.REVIEWING,
        next_state,
        actor="orch",
        note="; ".join(notes) or None,
    )
    return next_state
