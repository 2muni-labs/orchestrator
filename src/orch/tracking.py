"""GitHub work tracking records in state.db and the github-tracking.json import."""

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from orch import store

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


_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
_RECORD_COLUMNS = (
    "record_id, title, kind, completion_owner, project_key, "
    "issue_url, branch, pr_url, accepted_at"
)
_RECORD_FIELDS = tuple(column.strip() for column in _RECORD_COLUMNS.split(","))


def _matches(value: object, pattern: str) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise TrackingFormatError(f"{name} must be an object")
    return value


def _keys(value: dict, required: set[str], optional: set[str], name: str) -> None:
    if not required <= value.keys() or value.keys() - required - optional:
        raise TrackingFormatError(f"Invalid keys in {name}")


def _title(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\n" not in value
        and "\r" not in value
    )


def parse_file(text: str, *, target: str) -> TrackingFile:
    """Validate the text of a tracking file and return its contents."""
    try:
        data = _object(json.loads(text), "tracking file")
    except (ValueError, TypeError) as exc:
        raise TrackingFormatError("Invalid JSON") from exc
    version = data.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise TrackingFormatError(f"Unsupported schema_version: {version!r}")
    _keys(
        data,
        {
            "schema_version",
            "target",
            "repository",
            "projects",
            "records",
            "tasks",
            "aliases",
        },
        {"seeded_at", "sync"},
        "tracking file",
    )
    if not isinstance(data["target"], str) or data["target"] != target:
        raise TrackingFormatError("Unexpected target")
    repo = data["repository"]
    if not _matches(repo, r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+"):
        raise TrackingFormatError("Invalid repository")
    groups = {
        name: _object(data[name], name)
        for name in ("projects", "records", "tasks", "aliases")
    }
    owner = repo.split("/")[0]
    projects = []
    for key, value in sorted(groups["projects"].items()):
        if not _matches(key, _ID):
            raise TrackingFormatError("Invalid project key")
        value = _object(value, f"project {key}")
        _keys(value, {"title", "url"}, set(), f"project {key}")
        url = value["url"]
        match = (
            re.fullmatch(
                r"https://github\.com/orgs/([A-Za-z0-9-]+)/projects/[1-9][0-9]*", url
            )
            if isinstance(url, str)
            else None
        )
        if (
            not _title(value["title"])
            or match is None
            or match[1].lower() != owner.lower()
        ):
            raise TrackingFormatError(f"Invalid project {key}")
        projects.append(Project(key=key, title=value["title"], url=url))
    records = []
    issues = set()
    for record_id, value in sorted(groups["records"].items()):
        if not _matches(record_id, _ID):
            raise TrackingFormatError("Invalid record id")
        value = _object(value, f"record {record_id}")
        _keys(
            value,
            {"title", "kind", "completion_owner", "project_key"},
            {"issue_url", "branch", "pr_url"},
            f"record {record_id}",
        )
        if not _title(value["title"]) or not _matches(
            value["kind"], r"[a-z][a-z0-9_-]{0,31}"
        ):
            raise TrackingFormatError(f"Invalid title or kind in {record_id}")
        if (
            not isinstance(value["completion_owner"], str)
            or value["completion_owner"] not in COMPLETION_OWNERS
        ):
            raise TrackingFormatError(f"Invalid completion_owner in {record_id}")
        if (
            not isinstance(value["project_key"], str)
            or value["project_key"] not in groups["projects"]
        ):
            raise TrackingFormatError(f"Unknown project_key in {record_id}")
        patterns = {
            "issue_url": rf"https://github\.com/{re.escape(repo)}/issues/[1-9][0-9]*",
            "pr_url": rf"https://github\.com/{re.escape(repo)}/pull/[1-9][0-9]*",
            "branch": rf"task/{_ID}",
        }
        for field, pattern in patterns.items():
            if field in value and not _matches(value[field], pattern):
                raise TrackingFormatError(f"Invalid {field} in {record_id}")
        issue = value.get("issue_url")
        if issue is not None:
            if issue in issues:
                raise TrackingFormatError(f"Duplicate issue_url: {issue}")
            issues.add(issue)
        records.append(Record(record_id=record_id, **value))
    tasks = {}
    for task_id, value in groups["tasks"].items():
        if not _matches(task_id, _ID):
            raise TrackingFormatError("Invalid task id")
        value = _object(value, f"task {task_id}")
        _keys(value, {"record_id"}, set(), f"task {task_id}")
        ref = value["record_id"]
        if not isinstance(ref, str) or ref not in groups["records"]:
            raise TrackingFormatError(f"Unknown record in task {task_id}")
        tasks[task_id] = ref
    aliases = {}
    for alias, ref in groups["aliases"].items():
        if (
            not _matches(alias, _ID)
            or not isinstance(ref, str)
            or ref not in groups["records"]
        ):
            raise TrackingFormatError(f"Invalid alias {alias}")
        aliases[alias] = ref
    return TrackingFile(
        target=target,
        repository=repo,
        projects=tuple(projects),
        records=tuple(records),
        tasks=tasks,
        aliases=aliases,
        dropped_keys=("sync",) if "sync" in data else (),
    )


def load_file(path: Path, *, target: str) -> TrackingFile:
    """Read a tracking file as UTF-8 and validate it."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise TrackingFormatError("Invalid UTF-8") from exc
    return parse_file(text, target=target)


def import_file(
    conn: sqlite3.Connection, path: Path, *, target: str, at: str
) -> ImportReport:
    """Import a tracking file idempotently in one transaction."""
    data = load_file(path, target=target)
    added = []
    updated = []
    unchanged = 0
    with store.transaction(conn):
        current_repo = repository(conn, target)
        if current_repo is not None and current_repo != data.repository:
            raise TrackingConflictError(f"Repository conflict for target {target}")
        conn.execute(
            """INSERT INTO tracking_targets VALUES (?, ?, ?)
            ON CONFLICT(target) DO UPDATE SET imported_at = excluded.imported_at""",
            (target, data.repository, at),
        )
        for project in data.projects:
            conn.execute(
                """INSERT INTO tracking_projects VALUES (?, ?, ?, ?)
                ON CONFLICT(target, key) DO UPDATE SET
                title = excluded.title, url = excluded.url""",
                (target, project.key, project.title, project.url),
            )
        for record in data.records:
            old = get_record(conn, target, record.record_id)
            values = tuple(getattr(record, field) for field in _RECORD_FIELDS[:-1])
            if old is None:
                added.append(record.record_id)
            elif values != tuple(getattr(old, field) for field in _RECORD_FIELDS[:-1]):
                updated.append(record.record_id)
            else:
                unchanged += 1
                continue
            conn.execute(
                """INSERT INTO tracking_records
                (target, record_id, title, kind, completion_owner, project_key,
                 issue_url, branch, pr_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target, record_id) DO UPDATE SET
                title = excluded.title, kind = excluded.kind,
                completion_owner = excluded.completion_owner,
                project_key = excluded.project_key, issue_url = excluded.issue_url,
                branch = excluded.branch, pr_url = excluded.pr_url""",
                (target, *values),
            )
        for table, column, bindings in (
            ("tracking_tasks", "task_id", data.tasks),
            ("tracking_aliases", "alias", data.aliases),
        ):
            for key, record_id in bindings.items():
                conn.execute(
                    f"""INSERT INTO {table} VALUES (?, ?, ?)
                    ON CONFLICT(target, {column}) DO UPDATE SET
                    record_id = excluded.record_id""",
                    (target, key, record_id),
                )
    return ImportReport(
        records_added=tuple(added),
        records_updated=tuple(updated),
        records_unchanged=unchanged,
        projects=len(data.projects),
        tasks=len(data.tasks),
        aliases=len(data.aliases),
        dropped_keys=data.dropped_keys,
    )


def repository(conn: sqlite3.Connection, target: str) -> str | None:
    """Return the repository recorded for the target, or None."""
    row = conn.execute(
        "SELECT repository FROM tracking_targets WHERE target = ?", (target,)
    ).fetchone()
    return row[0] if row else None


def list_projects(conn: sqlite3.Connection, target: str) -> tuple[Project, ...]:
    """Return the target's projects ordered by key."""
    return tuple(
        Project(key=key, title=title, url=url)
        for key, title, url in conn.execute(
            "SELECT key, title, url FROM tracking_projects "
            "WHERE target = ? ORDER BY key",
            (target,),
        )
    )


def _record(row: tuple) -> Record:
    return Record(**dict(zip(_RECORD_FIELDS, row, strict=True)))


def get_record(conn: sqlite3.Connection, target: str, record_id: str) -> Record | None:
    """Return one record, or None."""
    row = conn.execute(
        f"SELECT {_RECORD_COLUMNS} FROM tracking_records "
        "WHERE target = ? AND record_id = ?",
        (target, record_id),
    ).fetchone()
    return _record(row) if row else None


def list_records(conn: sqlite3.Connection, target: str) -> tuple[Record, ...]:
    """Return the target's records ordered by record id."""
    return tuple(
        _record(row)
        for row in conn.execute(
            f"SELECT {_RECORD_COLUMNS} FROM tracking_records "
            "WHERE target = ? ORDER BY record_id",
            (target,),
        )
    )


def resolve(conn: sqlite3.Connection, target: str, ref: str) -> Record | None:
    """Prefer a record id over an alias."""
    record = get_record(conn, target, ref)
    if record is not None:
        return record
    row = conn.execute(
        "SELECT record_id FROM tracking_aliases WHERE target = ? AND alias = ?",
        (target, ref),
    ).fetchone()
    return get_record(conn, target, row[0]) if row else None


def record_for_task(
    conn: sqlite3.Connection, target: str, task_id: str
) -> Record | None:
    """Return the record bound to an orch task, or None."""
    row = conn.execute(
        "SELECT record_id FROM tracking_tasks WHERE target = ? AND task_id = ?",
        (target, task_id),
    ).fetchone()
    return get_record(conn, target, row[0]) if row else None


def task_ids(conn: sqlite3.Connection, target: str, record_id: str) -> tuple[str, ...]:
    """Return the task ids bound to a record, sorted."""
    return tuple(
        row[0]
        for row in conn.execute(
            "SELECT task_id FROM tracking_tasks "
            "WHERE target = ? AND record_id = ? ORDER BY task_id",
            (target, record_id),
        )
    )


def _require_record(conn: sqlite3.Connection, target: str, record_id: str) -> Record:
    record = get_record(conn, target, record_id)
    if record is None:
        raise TrackingError(f"Unknown record: {record_id}")
    return record


def bind_task(
    conn: sqlite3.Connection, *, target: str, task_id: str, record_id: str
) -> bool:
    """Bind a task to a record and report whether the binding changed."""
    if not _matches(task_id, _ID):
        raise TrackingError("Invalid task id")
    with store.transaction(conn):
        _require_record(conn, target, record_id)
        old = record_for_task(conn, target, task_id)
        if old is not None and old.record_id == record_id:
            return False
        conn.execute(
            """INSERT INTO tracking_tasks VALUES (?, ?, ?)
            ON CONFLICT(target, task_id) DO UPDATE SET
            record_id = excluded.record_id""",
            (target, task_id, record_id),
        )
    return True


def accept_record(
    conn: sqlite3.Connection, *, target: str, record_id: str, at: str
) -> bool:
    """Record the first acceptance of a manual or aggregate record."""
    with store.transaction(conn):
        record = _require_record(conn, target, record_id)
        if record.completion_owner not in ACCEPTABLE_OWNERS:
            raise TrackingError("Record does not allow acceptance")
        if record.accepted_at is not None:
            return False
        conn.execute(
            "UPDATE tracking_records SET accepted_at = ? "
            "WHERE target = ? AND record_id = ?",
            (at, target, record_id),
        )
    return True


def get_state(
    conn: sqlite3.Connection, target: str, record_id: str
) -> SyncState | None:
    """Return a record's sync state, or None."""
    row = conn.execute(
        "SELECT verified, verified_at, failures, last_error FROM tracking_state "
        "WHERE target = ? AND record_id = ?",
        (target, record_id),
    ).fetchone()
    if row is None:
        return None
    return SyncState(
        record_id=record_id,
        verified=json.loads(row[0]) if row[0] is not None else None,
        verified_at=row[1],
        failures=row[2],
        last_error=row[3],
    )


def save_verified(
    conn: sqlite3.Connection,
    *,
    target: str,
    record_id: str,
    verified: Mapping[str, object],
    at: str,
) -> None:
    """Store verified values and clear failures."""
    with store.transaction(conn):
        _require_record(conn, target, record_id)
        encoded = json.dumps(dict(verified), sort_keys=True, ensure_ascii=False)
        conn.execute(
            """INSERT INTO tracking_state
            (target, record_id, verified, verified_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(target, record_id) DO UPDATE SET
            verified = excluded.verified, verified_at = excluded.verified_at,
            failures = 0, last_error = NULL""",
            (target, record_id, encoded, at),
        )


def _validate_error(error: str) -> None:
    if not _matches(error, r"[a-z][a-z0-9_]{0,63}"):
        raise ValueError("Error must be a code")


def record_failure(
    conn: sqlite3.Connection, *, target: str, record_id: str, error: str, at: str
) -> int:
    """Count a failed sync, retaining verified values."""
    _validate_error(error)
    with store.transaction(conn):
        _require_record(conn, target, record_id)
        row = conn.execute(
            """INSERT INTO tracking_state
            (target, record_id, failures, last_error) VALUES (?, ?, 1, ?)
            ON CONFLICT(target, record_id) DO UPDATE SET
            failures = tracking_state.failures + 1, last_error = excluded.last_error
            RETURNING failures""",
            (target, record_id, error),
        ).fetchone()
        return row[0]


def record_run(
    conn: sqlite3.Connection, *, target: str, at: str, error: str | None
) -> SyncRun:
    """Record a sync run's success or failure."""
    if error is not None:
        _validate_error(error)
    with store.transaction(conn):
        row = conn.execute(
            """INSERT INTO tracking_runs VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(target) DO UPDATE SET
            last_at = excluded.last_at,
            last_success_at = CASE WHEN excluded.last_error IS NULL
                THEN excluded.last_at ELSE tracking_runs.last_success_at END,
            consecutive_failures = CASE WHEN excluded.last_error IS NULL
                THEN 0 ELSE tracking_runs.consecutive_failures + 1 END,
            last_error = excluded.last_error
            RETURNING target, last_at, last_success_at,
                consecutive_failures, last_error""",
            (
                target,
                at,
                at if error is None else None,
                0 if error is None else 1,
                error,
            ),
        ).fetchone()
        return _run(row)


def _run(row: tuple) -> SyncRun:
    return SyncRun(
        target=row[0],
        last_at=row[1],
        last_success_at=row[2],
        consecutive_failures=row[3],
        last_error=row[4],
    )


def get_run(conn: sqlite3.Connection, target: str) -> SyncRun | None:
    """Return the target's last sync run, or None."""
    row = conn.execute(
        "SELECT target, last_at, last_success_at, consecutive_failures, last_error "
        "FROM tracking_runs WHERE target = ?",
        (target,),
    ).fetchone()
    return _run(row) if row else None


def set_issue_url(
    conn: sqlite3.Connection, *, target: str, record_id: str, issue_url: str
) -> None:
    """Record the issue of a record that had none, checking its repository."""
    raise NotImplementedError
