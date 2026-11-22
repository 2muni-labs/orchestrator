"""orch show: one task's record as JSON (technical design 4.13)."""

import json
import sqlite3
import tomllib
from datetime import date, datetime, time
from pathlib import Path

from orch import store
from orch.config import TargetPaths
from orch.models import Task


def show_task(
    conn: sqlite3.Connection, task: Task, paths: TargetPaths
) -> dict[str, object]:
    """Return the task's record in the order of schemas/cli/show.v1.json."""
    return {
        "task": {
            "id": task.id,
            "target": task.target,
            "title": task.title,
            "state": task.state.value,
            "grade": task.grade.value,
            "lane": task.lane.value,
            "model": task.model,
            "effort": task.effort.value if task.effort is not None else None,
            "attempt": task.attempt,
            "depends_on": list(task.depends_on),
            "milestone": task.milestone,
            "replaces": task.replaces,
            "base_sha": task.base_sha,
            "test_sha": task.test_sha,
            "gated_sha": task.gated_sha,
            "pushed_sha": task.pushed_sha,
            "pr_url": task.pr_url,
            "worktree": task.worktree,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        },
        "spec": _read_spec(paths.tasks / f"{task.id}.md"),
        "result": _read_object(paths.results / f"{task.id}.json"),
        "gates": [
            {
                "attempt": result.attempt,
                "gate": result.gate.value,
                "passed": result.passed,
                "detail": result.detail,
                "at": result.at,
            }
            for attempt in range(1, task.attempt + 1)
            for result in store.gate_results(conn, task.id, attempt)
        ],
        "review": _read_object(paths.review_path(task.id)),
        "triage": _file_path(paths.triage_path(task.id)),
        "runs": [
            {
                "run_id": run.run_id,
                "attempt": run.attempt,
                "lane": run.lane.value,
                "model": run.model,
                "effort": run.effort.value if run.effort is not None else None,
                "started_at": run.started_at,
                "ended_at": run.ended_at,
                "exit_code": run.exit_code,
                "outcome": run.outcome.value if run.outcome is not None else None,
                "failure_reason": (
                    run.failure_reason.value if run.failure_reason is not None else None
                ),
                "worktree": task.worktree,
                "log_path": _file_path(paths.logs / f"{task.id}.{number}.log"),
            }
            for number, run in enumerate(store.list_runs(conn, task.id), start=1)
        ],
    }


def _read_spec(path: Path) -> dict[str, object] | None:
    """Read front matter without validating the task specification."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if not lines or lines[0].strip() != "+++":
            return None
        end = next(
            (index for index in range(1, len(lines)) if lines[index].strip() == "+++"),
            None,
        )
        if end is None:
            return None
        data = tomllib.loads("".join(lines[1:end]))
    except OSError, UnicodeError, tomllib.TOMLDecodeError:
        return None
    return {key: _stringify_dates(value) for key, value in data.items()}


def _stringify_dates(value: object) -> object:
    """Make TOML dates and times JSON serializable at any nesting depth."""
    if isinstance(value, (datetime, date, time)):
        return str(value)
    if isinstance(value, dict):
        return {key: _stringify_dates(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stringify_dates(item) for item in value]
    return value


def _read_object(path: Path) -> dict[str, object] | None:
    """Read a stored JSON object, or return null for an unavailable record."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeError, json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _file_path(path: Path) -> str | None:
    """Return the absolute path only when it names a regular file."""
    try:
        return str(path.absolute()) if path.is_file() else None
    except OSError:
        return None
