"""GitHub work tracking records in state.db and the github-tracking.json import."""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1
COMPLETION_OWNERS = frozenset({"task", "harness", "aggregate", "manual"})
ACCEPTABLE_OWNERS = frozenset({"manual", "aggregate"})


class TrackingError(Exception):
    """Base class for tracking errors."""


class TrackingFormatError(TrackingError):
    """The tracking file is malformed or has an unknown schema version."""


class TrackingConflictError(TrackingError):
    """The database already records another repository for the target."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Project:
    key: str
    title: str
    url: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Record:
    record_id: str
    title: str
    kind: str
    completion_owner: str
    project_key: str
    issue_url: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    accepted_at: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TrackingFile:
    target: str
    repository: str
    projects: tuple[Project, ...]
    records: tuple[Record, ...]
    tasks: Mapping[str, str]
    aliases: Mapping[str, str]
    dropped_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportReport:
    records_added: tuple[str, ...]
    records_updated: tuple[str, ...]
    records_unchanged: int
    projects: int
    tasks: int
    aliases: int
    dropped_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncState:
    record_id: str
    verified: Mapping[str, object] | None
    verified_at: str | None
    failures: int
    last_error: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncRun:
    target: str
    last_at: str
    last_success_at: str | None
    consecutive_failures: int
    last_error: str | None


def parse_file(text: str, *, target: str) -> TrackingFile:
    """Validate the text of a tracking file and return its contents."""
    raise NotImplementedError


def load_file(path: Path, *, target: str) -> TrackingFile:
    """Read a tracking file as UTF-8 and validate it."""
    raise NotImplementedError


def import_file(
    conn: sqlite3.Connection, path: Path, *, target: str, at: str
) -> ImportReport:
    """Import a tracking file idempotently in one transaction."""
    raise NotImplementedError


def repository(conn: sqlite3.Connection, target: str) -> str | None:
    """Return the repository recorded for the target, or None."""
    raise NotImplementedError


def list_projects(conn: sqlite3.Connection, target: str) -> tuple[Project, ...]:
    """Return the target's projects ordered by key."""
    raise NotImplementedError


def get_record(conn: sqlite3.Connection, target: str, record_id: str) -> Record | None:
    """Return one record, or None."""
    raise NotImplementedError


def list_records(conn: sqlite3.Connection, target: str) -> tuple[Record, ...]:
    """Return the target's records ordered by record id."""
    raise NotImplementedError


def resolve(conn: sqlite3.Connection, target: str, ref: str) -> Record | None:
    """Return the record with this id, else the one this alias names, else None."""
    raise NotImplementedError


def record_for_task(
    conn: sqlite3.Connection, target: str, task_id: str
) -> Record | None:
    """Return the record bound to an orch task, or None."""
    raise NotImplementedError


def task_ids(conn: sqlite3.Connection, target: str, record_id: str) -> tuple[str, ...]:
    """Return the task ids bound to a record, sorted."""
    raise NotImplementedError


def bind_task(
    conn: sqlite3.Connection, *, target: str, task_id: str, record_id: str
) -> bool:
    """Bind a task to a record. Returns whether the binding changed."""
    raise NotImplementedError


def accept_record(
    conn: sqlite3.Connection, *, target: str, record_id: str, at: str
) -> bool:
    """Record the acceptance of a manual or aggregate record. True: newly accepted."""
    raise NotImplementedError


def get_state(
    conn: sqlite3.Connection, target: str, record_id: str
) -> SyncState | None:
    """Return a record's sync state, or None."""
    raise NotImplementedError


def save_verified(
    conn: sqlite3.Connection,
    *,
    target: str,
    record_id: str,
    verified: Mapping[str, object],
    at: str,
) -> None:
    """Store the values read back from GitHub and clear the failure count."""
    raise NotImplementedError


def record_failure(
    conn: sqlite3.Connection, *, target: str, record_id: str, error: str, at: str
) -> int:
    """Count a failed sync of one record and return the new failure count."""
    raise NotImplementedError


def record_run(
    conn: sqlite3.Connection, *, target: str, at: str, error: str | None
) -> SyncRun:
    """Record the end of a sync run: success when error is None."""
    raise NotImplementedError


def get_run(conn: sqlite3.Connection, target: str) -> SyncRun | None:
    """Return the target's last sync run, or None."""
    raise NotImplementedError
