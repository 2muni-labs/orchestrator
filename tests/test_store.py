import ast
import dataclasses
import inspect
import sqlite3
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from orch import models, store
from orch.models import (
    CheckLevel,
    DoctorCheck,
    Effort,
    FailureReason,
    Gate,
    GateResult,
    Grade,
    Lane,
    Lock,
    Outcome,
    Pause,
    Run,
    Task,
    TaskState,
)
from orch.store import (
    IllegalTransitionError,
    RunNotFoundError,
    SchemaTooNewError,
    StateMismatchError,
    StoreError,
    TaskNotFoundError,
    add_locks,
    clear_gate_results,
    connect,
    finish_run,
    gate_results,
    get_run,
    get_task,
    insert_run,
    insert_task,
    latest_run,
    list_locks,
    list_tasks,
    migrate,
    record_gate_result,
    request_kill,
    set_run_process,
    transaction,
    transition,
    update_task,
)

THREADS = 8

EXPECTED_COLUMNS = {
    "pauses": {"reason", "since", "note"},
    "doctor_checks": {"target", "item", "level", "detail", "at"},
    "tasks": {
        "id",
        "target",
        "title",
        "grade",
        "lane",
        "model",
        "effort",
        "state",
        "attempt",
        "timeout_sec",
        "base_sha",
        "test_sha",
        "depends_on",
        "worktree",
        "pr_url",
        "pushed_sha",
        "gated_sha",
        "slot_expires_at",
        "created_at",
        "updated_at",
    },
    "runs": {
        "run_id",
        "task_id",
        "attempt",
        "lane",
        "model",
        "effort",
        "pid",
        "pid_started_at",
        "started_at",
        "ended_at",
        "exit_code",
        "outcome",
        "failure_reason",
        "kill_requested_at",
    },
    "locks": {"target", "path_glob", "task_id"},
    "gate_results": {"task_id", "attempt", "gate", "passed", "detail", "at"},
    "events": {"id", "task_id", "from_state", "to_state", "actor", "note", "at"},
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def conn(db_path: Path) -> sqlite3.Connection:
    return connect(db_path)


def make_task(task_id: str = "feat-x", state: TaskState = TaskState.PREPARED) -> Task:
    return Task(
        id=task_id,
        target="orchestrator",
        title="Feature X",
        grade=Grade.L2,
        lane=Lane.B,
        model="gpt-6.1-sol",
        state=state,
        timeout_sec=2700,
        created_at="2026-11-03T00:00:00+00:00",
        updated_at="2026-11-03T00:00:00+00:00",
    )


def user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return {row[0] for row in rows}


def events(conn: sqlite3.Connection, task_id: str) -> list[tuple[object, ...]]:
    rows = conn.execute(
        "SELECT from_state, to_state, actor, note, at FROM events "
        "WHERE task_id = ? ORDER BY id",
        (task_id,),
    )
    return [tuple(row) for row in rows]


def add_lock(conn: sqlite3.Connection, task_id: str, glob: str = "src/**") -> None:
    conn.execute(
        "INSERT INTO locks (target, path_glob, task_id) VALUES ('orchestrator', ?, ?)",
        (glob, task_id),
    )


def lock_owners(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT task_id FROM locks")}


def run_in_threads(work: Callable[[], None]) -> list[BaseException]:
    errors: list[BaseException] = []
    barrier = threading.Barrier(THREADS)

    def runner() -> None:
        barrier.wait()
        try:
            work()
        except BaseException as exc:  # noqa: B036
            errors.append(exc)

    threads = [threading.Thread(target=runner) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


# Connection settings


def test_connect_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "state.db"
    connect(path).close()
    assert path.is_file()


def test_connect_sets_wal_and_busy_timeout(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_connect_enforces_foreign_keys(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        add_lock(conn, "no-such-task")


def test_connection_leaves_transactions_to_the_store(conn: sqlite3.Connection) -> None:
    insert_task(conn, make_task())
    assert conn.in_transaction is False


# Schema and migrations


def test_schema_matches_the_design(conn: sqlite3.Connection) -> None:
    assert table_names(conn) == set(EXPECTED_COLUMNS)
    for table, columns in EXPECTED_COLUMNS.items():
        actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert actual == columns, table


def test_primary_keys_match_the_design(conn: sqlite3.Connection) -> None:
    def primary_key(table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})") if row[5]}

    assert primary_key("tasks") == {"id"}
    assert primary_key("runs") == {"run_id"}
    assert primary_key("locks") == {"target", "path_glob", "task_id"}
    assert primary_key("gate_results") == {"task_id", "attempt", "gate"}
    assert primary_key("events") == {"id"}


def test_version_is_the_number_of_migrations(conn: sqlite3.Connection) -> None:
    assert user_version(conn) == len(store.MIGRATIONS)


def test_migrate_is_idempotent(db_path: Path) -> None:
    conn = connect(db_path)
    insert_task(conn, make_task())
    assert migrate(conn) == len(store.MIGRATIONS)
    conn.close()
    reopened = connect(db_path)
    assert user_version(reopened) == len(store.MIGRATIONS)
    assert get_task(reopened, "feat-x") is not None


def test_new_migration_applies_once_and_keeps_data(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(db_path)
    insert_task(conn, make_task())
    latest = len(store.MIGRATIONS)
    added = ("CREATE TABLE extra (id INTEGER PRIMARY KEY)",)
    monkeypatch.setattr(store, "MIGRATIONS", (*store.MIGRATIONS, added))
    assert migrate(conn) == latest + 1
    assert migrate(conn) == latest + 1
    assert "extra" in table_names(conn)
    assert get_task(conn, "feat-x") is not None


def test_failed_migration_rolls_back_entirely(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(db_path)
    latest = len(store.MIGRATIONS)
    broken = ("CREATE TABLE half_done (id INTEGER PRIMARY KEY)", "THIS IS NOT SQL")
    monkeypatch.setattr(store, "MIGRATIONS", (*store.MIGRATIONS, broken))
    with pytest.raises(sqlite3.Error):
        migrate(conn)
    assert user_version(conn) == latest
    assert "half_done" not in table_names(conn)
    assert conn.in_transaction is False


def test_newer_schema_is_refused(db_path: Path) -> None:
    conn = connect(db_path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(SchemaTooNewError):
        connect(db_path)


def test_concurrent_first_connections_migrate_once(db_path: Path) -> None:
    def work() -> None:
        connect(db_path).close()

    assert run_in_threads(work) == []
    conn = connect(db_path)
    assert user_version(conn) == len(store.MIGRATIONS)
    assert table_names(conn) == set(EXPECTED_COLUMNS)


# Transactions


def test_transaction_commits(conn: sqlite3.Connection, db_path: Path) -> None:
    with transaction(conn):
        assert conn.in_transaction is True
        insert_task(conn, make_task())
    assert conn.in_transaction is False
    assert get_task(connect(db_path), "feat-x") is not None


def test_transaction_rolls_back_on_exception(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError), transaction(conn):
        insert_task(conn, make_task())
        raise ValueError
    assert conn.in_transaction is False
    assert get_task(conn, "feat-x") is None


def test_transaction_takes_the_write_lock_immediately(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    other = connect(db_path)
    other.execute("PRAGMA busy_timeout = 0")
    with transaction(conn):
        with pytest.raises(sqlite3.OperationalError):
            other.execute("BEGIN IMMEDIATE")


def test_nested_transaction_joins_the_outer_one(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError), transaction(conn):
        with transaction(conn):
            insert_task(conn, make_task())
        assert conn.in_transaction is True
        raise ValueError
    assert get_task(conn, "feat-x") is None


# Tasks


def test_task_round_trip_with_all_fields(conn: sqlite3.Connection) -> None:
    task = Task(
        id="it's; DROP TABLE tasks",
        target="orchestrator",
        title='Title "한글"',
        grade=Grade.L3R,
        lane=Lane.C,
        model="claude-sonnet-5-5",
        effort=Effort.XHIGH,
        state=TaskState.PR_OPEN,
        attempt=2,
        timeout_sec=60,
        base_sha="abc",
        test_sha="def",
        depends_on=("a", "b"),
        worktree="/tmp/w",
        pr_url="https://example.com/1",
        pushed_sha="a" * 40,
        gated_sha="b" * 40,
        slot_expires_at="2026-11-03T01:00:00+00:00",
        created_at="2026-11-03T00:00:00+00:00",
        updated_at="2026-11-03T00:30:00+00:00",
    )
    insert_task(conn, task)
    assert get_task(conn, task.id) == task


def test_task_round_trip_with_defaults(conn: sqlite3.Connection) -> None:
    task = make_task()
    insert_task(conn, task)
    loaded = get_task(conn, task.id)
    assert loaded == task
    assert loaded is not None
    assert isinstance(loaded.state, TaskState)
    assert isinstance(loaded.grade, Grade)
    assert isinstance(loaded.lane, Lane)
    assert loaded.depends_on == ()


def test_depends_on_is_stored_as_a_json_array(conn: sqlite3.Connection) -> None:
    task = Task(
        id="x",
        target="t",
        title="t",
        grade=Grade.L1,
        lane=Lane.B,
        model="m",
        state=TaskState.PREPARED,
        timeout_sec=1,
        depends_on=("a", "b"),
        created_at="c",
        updated_at="u",
    )
    insert_task(conn, task)
    row = conn.execute("SELECT depends_on FROM tasks WHERE id = 'x'").fetchone()
    assert row[0] == '["a", "b"]'


def test_get_missing_task_returns_none(conn: sqlite3.Connection) -> None:
    assert get_task(conn, "nope") is None


def test_duplicate_task_is_refused(conn: sqlite3.Connection) -> None:
    insert_task(conn, make_task())
    with pytest.raises(sqlite3.IntegrityError):
        insert_task(conn, make_task())


# Transitions


def test_transition_updates_state_and_records_an_event(
    conn: sqlite3.Connection,
) -> None:
    insert_task(conn, make_task())
    at = "2026-11-03T02:00:00+00:00"
    transition(
        conn,
        "feat-x",
        TaskState.PREPARED,
        TaskState.QUEUED,
        actor="main",
        note="n",
        at=at,
    )
    task = get_task(conn, "feat-x")
    assert task is not None
    assert task.state is TaskState.QUEUED
    assert task.updated_at == at
    assert events(conn, "feat-x") == [("prepared", "queued", "main", "n", at)]
    assert conn.in_transaction is False


def test_transition_defaults_to_the_current_utc_time(conn: sqlite3.Connection) -> None:
    insert_task(conn, make_task())
    transition(conn, "feat-x", TaskState.PREPARED, TaskState.QUEUED, actor="main")
    task = get_task(conn, "feat-x")
    assert task is not None
    assert datetime.fromisoformat(task.updated_at).utcoffset() is not None
    assert events(conn, "feat-x")[0][3:] == (None, task.updated_at)


def test_illegal_transition_is_refused_without_changes(
    conn: sqlite3.Connection,
) -> None:
    insert_task(conn, make_task())
    with pytest.raises(IllegalTransitionError):
        transition(conn, "feat-x", TaskState.PREPARED, TaskState.READY, actor="main")
    task = get_task(conn, "feat-x")
    assert task is not None
    assert task.state is TaskState.PREPARED
    assert events(conn, "feat-x") == []


def test_state_mismatch_is_refused_without_changes(conn: sqlite3.Connection) -> None:
    insert_task(conn, make_task(state=TaskState.QUEUED))
    with pytest.raises(StateMismatchError):
        transition(conn, "feat-x", TaskState.PREPARED, TaskState.QUEUED, actor="main")
    task = get_task(conn, "feat-x")
    assert task is not None
    assert task.state is TaskState.QUEUED
    assert events(conn, "feat-x") == []


def test_missing_task_is_reported(conn: sqlite3.Connection) -> None:
    with pytest.raises(TaskNotFoundError):
        transition(conn, "nope", TaskState.PREPARED, TaskState.QUEUED, actor="main")
    assert events(conn, "nope") == []


def test_transition_uses_the_shared_table(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert_task(conn, make_task("a", TaskState.CLOSED))
    insert_task(conn, make_task("b"))
    replacement = MappingProxyType({TaskState.CLOSED: frozenset({TaskState.PREPARED})})
    monkeypatch.setattr(models, "TRANSITIONS", replacement)
    transition(conn, "a", TaskState.CLOSED, TaskState.PREPARED, actor="main")
    with pytest.raises(IllegalTransitionError):
        transition(conn, "b", TaskState.PREPARED, TaskState.QUEUED, actor="main")


def test_transition_rolls_back_when_the_event_fails(conn: sqlite3.Connection) -> None:
    insert_task(conn, make_task())
    conn.execute("DROP TABLE events")
    with pytest.raises(sqlite3.OperationalError):
        transition(conn, "feat-x", TaskState.PREPARED, TaskState.QUEUED, actor="main")
    task = get_task(conn, "feat-x")
    assert task is not None
    assert task.state is TaskState.PREPARED
    assert conn.in_transaction is False


def test_transition_joins_a_callers_transaction(conn: sqlite3.Connection) -> None:
    insert_task(conn, make_task())
    with pytest.raises(ValueError), transaction(conn):
        transition(conn, "feat-x", TaskState.PREPARED, TaskState.QUEUED, actor="main")
        assert conn.in_transaction is True
        raise ValueError
    task = get_task(conn, "feat-x")
    assert task is not None
    assert task.state is TaskState.PREPARED
    assert events(conn, "feat-x") == []


@pytest.mark.parametrize(
    ("current", "terminal"),
    [(TaskState.PREPARED, TaskState.DISCARDED), (TaskState.PR_OPEN, TaskState.CLOSED)],
)
def test_terminal_state_releases_only_that_tasks_locks(
    conn: sqlite3.Connection, current: TaskState, terminal: TaskState
) -> None:
    insert_task(conn, make_task("a", current))
    insert_task(conn, make_task("b"))
    add_lock(conn, "a", "src/**")
    add_lock(conn, "a", "docs/**")
    add_lock(conn, "b", "tests/**")
    transition(conn, "a", current, terminal, actor="main")
    assert lock_owners(conn) == {"b"}


def test_other_states_keep_locks(conn: sqlite3.Connection) -> None:
    insert_task(conn, make_task())
    add_lock(conn, "feat-x")
    transition(conn, "feat-x", TaskState.PREPARED, TaskState.RESERVED, actor="main")
    assert lock_owners(conn) == {"feat-x"}


def test_concurrent_transitions_let_exactly_one_win(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    insert_task(conn, make_task())
    wins: list[int] = []

    def work() -> None:
        other = connect(db_path)
        try:
            transition(
                other, "feat-x", TaskState.PREPARED, TaskState.QUEUED, actor="main"
            )
            wins.append(1)
        except StateMismatchError:
            pass
        finally:
            other.close()

    assert run_in_threads(work) == []
    assert len(wins) == 1
    assert len(events(conn, "feat-x")) == 1


def test_concurrent_writers_on_different_tasks_all_succeed(
    conn: sqlite3.Connection, db_path: Path
) -> None:
    ids = iter(range(THREADS))
    lock = threading.Lock()
    for number in range(THREADS):
        insert_task(conn, make_task(f"t{number}"))

    def work() -> None:
        with lock:
            number = next(ids)
        other = connect(db_path)
        try:
            transition(
                other, f"t{number}", TaskState.PREPARED, TaskState.QUEUED, actor="main"
            )
        finally:
            other.close()

    assert run_in_threads(work) == []
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == THREADS


# Migration to schema v2


def test_v1_database_gains_pushed_sha_and_keeps_its_rows(db_path: Path) -> None:
    old = sqlite3.connect(db_path, isolation_level=None)
    for statement in store.MIGRATIONS[0]:
        old.execute(statement)
    old.execute("PRAGMA user_version = 1")
    old.execute(
        """INSERT INTO tasks (
            id, target, title, grade, lane, model, state, timeout_sec, depends_on,
            created_at, updated_at
        ) VALUES ('old', 't', 'Old', 'L2', 'B', 'm', 'prepared', 60, '[]', 'c', 'u')"""
    )
    old.close()
    conn = connect(db_path)
    assert user_version(conn) == len(store.MIGRATIONS)
    loaded = get_task(conn, "old")
    assert loaded is not None
    assert loaded.title == "Old"
    assert loaded.pushed_sha is None
    assert loaded.gated_sha is None


# Migration to schema v3


def test_v2_database_gains_gated_sha_and_keeps_its_rows(db_path: Path) -> None:
    old = sqlite3.connect(db_path, isolation_level=None)
    for migration in store.MIGRATIONS[:2]:
        for statement in migration:
            old.execute(statement)
    old.execute("PRAGMA user_version = 2")
    old.execute(
        """INSERT INTO tasks (
            id, target, title, grade, lane, model, state, timeout_sec, depends_on,
            created_at, updated_at, pushed_sha
        ) VALUES ('old', 't', 'Old', 'L2', 'B', 'm', 'pr_open', 60, '[]', 'c', 'u',
            'ppp')"""
    )
    old.close()
    conn = connect(db_path)
    assert user_version(conn) == len(store.MIGRATIONS)
    assert "gated_sha" in {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    loaded = get_task(conn, "old")
    assert loaded is not None
    assert loaded.state is TaskState.PR_OPEN
    assert loaded.pushed_sha == "ppp"
    assert loaded.gated_sha is None


def test_v3_migration_is_the_gated_sha_column_only() -> None:
    assert store.MIGRATIONS[2] == ("ALTER TABLE tasks ADD COLUMN gated_sha TEXT",)


def test_pr_open_can_move_to_conflict(conn: sqlite3.Connection) -> None:
    insert_task(conn, make_task(state=TaskState.PR_OPEN))
    transition(conn, "feat-x", TaskState.PR_OPEN, TaskState.CONFLICT, actor="orch")
    loaded = get_task(conn, "feat-x")
    assert loaded is not None
    assert loaded.state is TaskState.CONFLICT


# Retrying a busy database while opening it


class FlakyConnection:
    """Wraps a connection and fails PRAGMA journal_mode with the given errors."""

    def __init__(self, real: sqlite3.Connection, errors: list[Exception]) -> None:
        self.real = real
        self.errors = errors
        self.journal_attempts = 0

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        if sql.startswith("PRAGMA journal_mode"):
            self.journal_attempts += 1
            if self.errors:
                raise self.errors.pop(0)
        return self.real.execute(sql, *args)

    def __getattr__(self, name: str) -> object:
        return getattr(self.real, name)


def sqlite_error(code: int, message: str = "database is locked") -> sqlite3.Error:
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code  # type: ignore[attr-defined]
    return error


def flaky_connect(
    monkeypatch: pytest.MonkeyPatch, errors: list[Exception]
) -> list[FlakyConnection]:
    made: list[FlakyConnection] = []
    real_connect = sqlite3.connect

    def fake(*args: object, **kwargs: object) -> FlakyConnection:
        wrapper = FlakyConnection(real_connect(*args, **kwargs), errors)
        made.append(wrapper)
        return wrapper

    monkeypatch.setattr(store.sqlite3, "connect", fake)
    return made


@pytest.mark.parametrize(
    "code",
    [
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_BUSY | (1 << 8),  # An extended code: SQLITE_BUSY_RECOVERY.
        sqlite3.SQLITE_LOCKED
        | (1 << 8),  # An extended code: SQLITE_LOCKED_SHAREDCACHE.
    ],
)
def test_connect_retries_busy_and_locked_by_error_code(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    made = flaky_connect(monkeypatch, [sqlite_error(code, "some other wording")])
    connect(db_path)
    assert made[0].journal_attempts == 2


@pytest.mark.parametrize(
    "code", [sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_IOERR]
)
def test_connect_raises_other_errors_without_retry(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    # The wording matches the old text comparison, so only the code can tell them apart.
    error = sqlite_error(code)
    made = flaky_connect(monkeypatch, [error])
    with pytest.raises(sqlite3.OperationalError) as caught:
        connect(db_path)
    assert caught.value is error
    assert made[0].journal_attempts == 1


def test_connect_stops_retrying_at_the_deadline(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds + 1

    errors = [sqlite_error(sqlite3.SQLITE_BUSY) for _ in range(100)]
    made = flaky_connect(monkeypatch, errors)
    monkeypatch.setattr(store, "time", Clock())
    with pytest.raises(sqlite3.OperationalError):
        connect(db_path)
    # Five seconds at one second per attempt, whatever the exact boundary.
    assert 2 <= made[0].journal_attempts <= 8


# Updating task fields


def test_update_task_sets_fields_and_updated_at(conn: sqlite3.Connection) -> None:
    insert_task(conn, make_task())
    update_task(
        conn,
        "feat-x",
        at="2026-11-04T00:00:00+00:00",
        attempt=2,
        base_sha="b" * 40,
        test_sha="c" * 40,
        worktree="w",
        pr_url="https://example.com/1",
        pushed_sha="d" * 40,
        gated_sha="e" * 40,
        slot_expires_at="2026-11-04T00:10:00+00:00",
    )
    loaded = get_task(conn, "feat-x")
    assert loaded is not None
    assert loaded.attempt == 2
    assert loaded.base_sha == "b" * 40
    assert loaded.test_sha == "c" * 40
    assert loaded.worktree == "w"
    assert loaded.pr_url == "https://example.com/1"
    assert loaded.pushed_sha == "d" * 40
    assert loaded.gated_sha == "e" * 40
    assert loaded.slot_expires_at == "2026-11-04T00:10:00+00:00"
    assert loaded.updated_at == "2026-11-04T00:00:00+00:00"
    assert loaded.state is TaskState.PREPARED


def test_update_task_can_clear_a_field(conn: sqlite3.Connection) -> None:
    insert_task(conn, make_task())
    update_task(conn, "feat-x", slot_expires_at="soon")
    update_task(conn, "feat-x", slot_expires_at=None)
    loaded = get_task(conn, "feat-x")
    assert loaded is not None
    assert loaded.slot_expires_at is None


def test_update_task_defaults_to_the_current_utc_time(conn: sqlite3.Connection) -> None:
    insert_task(conn, make_task())
    update_task(conn, "feat-x", attempt=1)
    loaded = get_task(conn, "feat-x")
    assert loaded is not None
    assert datetime.fromisoformat(loaded.updated_at).utcoffset() is not None
    assert loaded.updated_at != make_task().updated_at


@pytest.mark.parametrize(
    "field",
    ["state", "id", "target", "created_at", "updated_at", "attempt = 9; --", "nope"],
)
def test_update_task_refuses_other_fields_without_changes(
    conn: sqlite3.Connection, field: str
) -> None:
    insert_task(conn, make_task())
    before = get_task(conn, "feat-x")
    with pytest.raises(StoreError):
        update_task(conn, "feat-x", **{field: "x"})
    assert get_task(conn, "feat-x") == before


def test_update_task_without_fields_is_refused(conn: sqlite3.Connection) -> None:
    insert_task(conn, make_task())
    with pytest.raises(StoreError):
        update_task(conn, "feat-x")


def test_update_missing_task_is_reported(conn: sqlite3.Connection) -> None:
    with pytest.raises(TaskNotFoundError):
        update_task(conn, "nope", attempt=1)


def test_update_task_rolls_back_with_a_callers_transaction(
    conn: sqlite3.Connection,
) -> None:
    insert_task(conn, make_task())
    with pytest.raises(LookupError):
        with transaction(conn):
            update_task(conn, "feat-x", attempt=2)
            raise LookupError("stop")
    loaded = get_task(conn, "feat-x")
    assert loaded is not None
    assert loaded.attempt == 0


# Listing tasks


def add_task(
    conn: sqlite3.Connection,
    task_id: str,
    state: TaskState = TaskState.QUEUED,
    created_at: str = "2026-11-03T00:00:00+00:00",
    target: str = "orchestrator",
) -> None:
    task = dataclasses.replace(
        make_task(task_id, state), created_at=created_at, target=target
    )
    insert_task(conn, task)


def test_list_tasks_orders_by_creation_then_id(conn: sqlite3.Connection) -> None:
    add_task(conn, "c", created_at="2026-11-03T00:00:01+00:00")
    add_task(conn, "b", created_at="2026-11-03T00:00:00+00:00")
    add_task(conn, "a", created_at="2026-11-03T00:00:00+00:00")
    assert [task.id for task in list_tasks(conn)] == ["a", "b", "c"]


def test_list_tasks_filters_by_state_and_target(conn: sqlite3.Connection) -> None:
    add_task(conn, "queued", TaskState.QUEUED)
    add_task(conn, "running", TaskState.RUNNING)
    add_task(conn, "closed", TaskState.CLOSED)
    add_task(conn, "elsewhere", TaskState.RUNNING, target="other")
    active = list_tasks(conn, states=[TaskState.QUEUED, TaskState.RUNNING])
    assert {task.id for task in active} == {"queued", "running", "elsewhere"}
    here = list_tasks(conn, states=[TaskState.RUNNING], target="orchestrator")
    assert [task.id for task in here] == ["running"]
    assert isinstance(active, tuple)
    assert isinstance(active[0], Task)


def test_list_tasks_with_no_states_returns_nothing(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    assert list_tasks(conn, states=[]) == ()


# Locks


def test_locks_are_listed_per_target(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    add_task(conn, "b", target="other")
    add_locks(conn, "orchestrator", "a", ["src/b/**", "src/a/**"])
    add_locks(conn, "other", "b", ["src/**"])
    assert list_locks(conn, "orchestrator") == (
        Lock(target="orchestrator", path_glob="src/a/**", task_id="a"),
        Lock(target="orchestrator", path_glob="src/b/**", task_id="a"),
    )


def test_add_locks_is_idempotent(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    add_locks(conn, "orchestrator", "a", ["src/**"])
    add_locks(conn, "orchestrator", "a", ["src/**"])
    assert len(list_locks(conn, "orchestrator")) == 1


def test_add_locks_for_a_missing_task_adds_none(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    with pytest.raises(sqlite3.IntegrityError):
        add_locks(conn, "orchestrator", "nope", ["src/**", "docs/**"])
    assert list_locks(conn, "orchestrator") == ()


def test_add_locks_is_all_or_nothing_in_its_own_transaction(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a")
    with pytest.raises(sqlite3.Error):
        add_locks(conn, "orchestrator", "a", ["src/**", None])  # type: ignore[list-item]
    assert list_locks(conn, "orchestrator") == ()


def test_terminal_transition_clears_locks_from_add_locks(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.PR_OPEN)
    add_locks(conn, "orchestrator", "a", ["src/**"])
    transition(conn, "a", TaskState.PR_OPEN, TaskState.CLOSED, actor="orch")
    assert list_locks(conn, "orchestrator") == ()


# Runs


def make_run(task_id: str = "a", attempt: int = 1) -> Run:
    return Run(
        task_id=task_id,
        attempt=attempt,
        lane=Lane.B,
        model="gpt-6.1-sol",
        effort=Effort.HIGH,
        started_at="2026-11-04T00:00:00+00:00",
    )


def test_run_round_trip(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    run_id = insert_run(conn, make_run())
    set_run_process(conn, run_id, pid=123, pid_started_at="Wed Nov  4 00:00:00 2026")
    finish_run(
        conn,
        run_id,
        ended_at="2026-11-04T00:10:00+00:00",
        exit_code=-9,
        outcome=Outcome.FAILED,
        failure_reason=FailureReason.KILLED,
    )
    run = latest_run(conn, "a")
    assert run == Run(
        run_id=run_id,
        task_id="a",
        attempt=1,
        lane=Lane.B,
        model="gpt-6.1-sol",
        effort=Effort.HIGH,
        pid=123,
        pid_started_at="Wed Nov  4 00:00:00 2026",
        started_at="2026-11-04T00:00:00+00:00",
        ended_at="2026-11-04T00:10:00+00:00",
        exit_code=-9,
        outcome=Outcome.FAILED,
        failure_reason=FailureReason.KILLED,
    )
    assert isinstance(run.lane, Lane)
    assert isinstance(run.effort, Effort)
    assert isinstance(run.outcome, Outcome)
    assert isinstance(run.failure_reason, FailureReason)


def test_unfinished_run_has_empty_result_fields(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    insert_run(conn, dataclasses.replace(make_run(), effort=None))
    run = latest_run(conn, "a")
    assert run is not None
    assert run.effort is None
    assert run.outcome is None
    assert run.failure_reason is None
    assert run.ended_at is None


def test_latest_run_is_the_newest_of_that_task(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    add_task(conn, "b")
    insert_run(conn, make_run("a", 1))
    newest = insert_run(conn, make_run("a", 2))
    insert_run(conn, make_run("b", 1))
    run = latest_run(conn, "a")
    assert run is not None
    assert (run.run_id, run.attempt) == (newest, 2)
    add_task(conn, "c")
    assert latest_run(conn, "c") is None


def test_run_of_a_missing_task_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        insert_run(conn, make_run("nope"))


def test_finishing_a_run_twice_is_refused_and_keeps_the_first(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a")
    run_id = insert_run(conn, make_run())
    finish_run(conn, run_id, ended_at="t1", exit_code=0, outcome=Outcome.DONE)
    with pytest.raises(StateMismatchError):
        finish_run(conn, run_id, ended_at="t2", exit_code=1, outcome=Outcome.FAILED)
    run = latest_run(conn, "a")
    assert run is not None
    assert (run.ended_at, run.outcome) == ("t1", Outcome.DONE)


def test_missing_run_is_reported(conn: sqlite3.Connection) -> None:
    with pytest.raises(RunNotFoundError):
        set_run_process(conn, 99, pid=1, pid_started_at="x")
    with pytest.raises(RunNotFoundError):
        finish_run(conn, 99, ended_at="t", exit_code=0, outcome=Outcome.DONE)
    assert issubclass(RunNotFoundError, StoreError)


def test_run_changes_join_a_callers_transaction(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    with pytest.raises(LookupError):
        with transaction(conn):
            insert_run(conn, make_run())
            raise LookupError("stop")
    assert latest_run(conn, "a") is None


# Kill requests (schema v4)


def test_a_new_run_has_no_kill_request(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    run_id = insert_run(conn, make_run())
    run = latest_run(conn, "a")
    assert run is not None
    assert run.kill_requested_at is None
    assert get_run(conn, run_id) == run


def test_get_run_returns_that_run_or_none(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    first = insert_run(conn, make_run("a", 1))
    insert_run(conn, make_run("a", 2))
    run = get_run(conn, first)
    assert run is not None
    assert (run.run_id, run.attempt, run.lane) == (first, 1, Lane.B)
    assert get_run(conn, 999) is None


def test_request_kill_marks_an_unfinished_run_once(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    run_id = insert_run(conn, make_run())
    assert request_kill(conn, run_id, at="t1") is True
    assert request_kill(conn, run_id, at="t2") is True
    run = get_run(conn, run_id)
    assert run is not None
    assert run.kill_requested_at == "t1"
    assert run.ended_at is None


def test_request_kill_writes_nothing_on_an_ended_run(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    run_id = insert_run(conn, make_run())
    finish_run(conn, run_id, ended_at="t0", exit_code=0, outcome=Outcome.DONE)
    assert request_kill(conn, run_id, at="t1") is False
    run = get_run(conn, run_id)
    assert run is not None
    assert run.kill_requested_at is None
    assert (run.ended_at, run.outcome) == ("t0", Outcome.DONE)


def test_request_kill_reports_a_missing_run(conn: sqlite3.Connection) -> None:
    with pytest.raises(RunNotFoundError):
        request_kill(conn, 99, at="t")


def test_a_v3_database_gains_the_kill_column(db_path: Path) -> None:
    old = sqlite3.connect(db_path, isolation_level=None)
    for statements in store.MIGRATIONS[:3]:
        for statement in statements:
            old.execute(statement)
    old.execute("PRAGMA user_version = 3")
    old.close()
    conn = connect(db_path)
    assert user_version(conn) == len(store.MIGRATIONS)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    assert "kill_requested_at" in columns
    assert store.MIGRATIONS[3] == (
        "ALTER TABLE runs ADD COLUMN kill_requested_at TEXT",
    )


# Gate results


def test_gate_results_keep_recording_order_and_types(conn: sqlite3.Connection) -> None:
    record_gate_result(conn, "a", 1, Gate.G1, passed=True, at="t1")
    record_gate_result(conn, "a", 1, Gate.G2, passed=False, detail="요약", at="t2")
    record_gate_result(conn, "a", 2, Gate.G1, passed=True, at="t3")
    record_gate_result(conn, "b", 1, Gate.G1, passed=True, at="t4")
    assert gate_results(conn, "a", 1) == (
        GateResult(task_id="a", attempt=1, gate=Gate.G1, passed=True, at="t1"),
        GateResult(
            task_id="a", attempt=1, gate=Gate.G2, passed=False, detail="요약", at="t2"
        ),
    )
    first = gate_results(conn, "a", 1)[0]
    assert first.passed is True
    assert isinstance(first.gate, Gate)
    assert gate_results(conn, "a", 3) == ()


def test_rerunning_a_gate_replaces_its_result(conn: sqlite3.Connection) -> None:
    record_gate_result(conn, "a", 1, Gate.G2_PRE_PR, passed=False, detail="x", at="t1")
    record_gate_result(conn, "a", 1, Gate.G2_PRE_PR, passed=True, at="t2")
    assert gate_results(conn, "a", 1) == (
        GateResult(task_id="a", attempt=1, gate=Gate.G2_PRE_PR, passed=True, at="t2"),
    )


def test_clear_gate_results_removes_only_the_named_gates_of_that_attempt(
    conn: sqlite3.Connection,
) -> None:
    record_gate_result(conn, "a", 1, Gate.G1, passed=True, at="t1")
    record_gate_result(conn, "a", 1, Gate.G2, passed=True, at="t2")
    record_gate_result(conn, "a", 1, Gate.G2_PRE_PR, passed=True, at="t3")
    record_gate_result(conn, "a", 2, Gate.G1, passed=True, at="t4")
    record_gate_result(conn, "b", 1, Gate.G1, passed=True, at="t5")
    clear_gate_results(conn, "a", 1, (Gate.G1, Gate.G2))
    assert [r.gate for r in gate_results(conn, "a", 1)] == [Gate.G2_PRE_PR]
    assert [r.gate for r in gate_results(conn, "a", 2)] == [Gate.G1]
    assert [r.gate for r in gate_results(conn, "b", 1)] == [Gate.G1]


def test_clear_gate_results_accepts_no_gates_and_missing_rows(
    conn: sqlite3.Connection,
) -> None:
    record_gate_result(conn, "a", 1, Gate.G1, passed=True, at="t1")
    clear_gate_results(conn, "a", 1, ())
    clear_gate_results(conn, "a", 1, iter([Gate.G3]))
    clear_gate_results(conn, "missing", 1, (Gate.G1,))
    assert [r.gate for r in gate_results(conn, "a", 1)] == [Gate.G1]
    assert conn.in_transaction is False


def test_gate_result_defaults_to_the_current_utc_time(conn: sqlite3.Connection) -> None:
    record_gate_result(conn, "a", 1, Gate.G1, passed=True)
    at = gate_results(conn, "a", 1)[0].at
    assert datetime.fromisoformat(at).utcoffset() is not None


# Conventions


def test_module_imports_only_the_standard_library_and_orch() -> None:
    tree = ast.parse(inspect.getsource(store))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imported.add(node.module.split(".")[0])
    assert imported - {"orch"} <= sys.stdlib_module_names


def test_module_keeps_no_copy_of_the_transition_rules() -> None:
    source = inspect.getsource(store)
    assert "frozenset" not in source
    assert "TRANSITIONS" not in source


# Schema v5: pauses and doctor checks


def table_columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, int]]:
    """Return each column's name and primary key position."""
    return [(row[1], row[5]) for row in conn.execute(f"PRAGMA table_info({table})")]


def test_the_v5_tables_match_the_design(conn: sqlite3.Connection) -> None:
    assert table_columns(conn, "pauses") == [("reason", 1), ("since", 0), ("note", 0)]
    assert table_columns(conn, "doctor_checks") == [
        ("target", 1),
        ("item", 2),
        ("level", 0),
        ("detail", 0),
        ("at", 0),
    ]


def test_a_v4_database_gains_the_v5_tables_and_keeps_its_data(db_path: Path) -> None:
    old = sqlite3.connect(db_path, isolation_level=None)
    for statements in store.MIGRATIONS[:4]:
        for statement in statements:
            old.execute(statement)
    old.execute("PRAGMA user_version = 4")
    old.execute(
        """INSERT INTO tasks (id, target, title, grade, lane, model, state,
        timeout_sec, created_at, updated_at)
        VALUES ('kept', 't', 'T', 'L2', 'B', 'm', 'closed', 60, 'c', 'u')"""
    )
    old.close()
    conn = connect(db_path)
    assert user_version(conn) == len(store.MIGRATIONS)
    assert get_task(conn, "kept") is not None
    assert store.list_pauses(conn) == ()
    assert store.list_doctor_checks(conn) == ()


def test_add_pause_records_a_new_reason(conn: sqlite3.Connection) -> None:
    assert store.add_pause(conn, "disk", since="t1", note="1 GB left") is True
    assert store.list_pauses(conn) == (
        Pause(reason="disk", since="t1", note="1 GB left"),
    )


def test_add_pause_keeps_the_existing_row(conn: sqlite3.Connection) -> None:
    store.add_pause(conn, "disk", since="t1", note="first")
    assert store.add_pause(conn, "disk", since="t2", note="second") is False
    assert store.list_pauses(conn) == (Pause(reason="disk", since="t1", note="first"),)


def test_a_pause_note_is_optional(conn: sqlite3.Connection) -> None:
    store.add_pause(conn, "disk", since="t1")
    assert store.list_pauses(conn) == (Pause(reason="disk", since="t1"),)


def test_remove_pause_reports_whether_a_row_was_removed(
    conn: sqlite3.Connection,
) -> None:
    store.add_pause(conn, "disk", since="t1")
    assert store.remove_pause(conn, "disk") is True
    assert store.remove_pause(conn, "disk") is False
    assert store.list_pauses(conn) == ()


def test_removing_one_pause_leaves_the_others(conn: sqlite3.Connection) -> None:
    store.add_pause(conn, "disk", since="t1")
    store.add_pause(conn, "codex_auth", since="t2")
    store.remove_pause(conn, "disk")
    assert [pause.reason for pause in store.list_pauses(conn)] == ["codex_auth"]


def test_pauses_are_ordered_by_reason(conn: sqlite3.Connection) -> None:
    store.add_pause(conn, "disk", since="t1")
    store.add_pause(conn, "claude_auth", since="t2")
    store.add_pause(conn, "codex_auth", since="t3")
    assert [pause.reason for pause in store.list_pauses(conn)] == [
        "claude_auth",
        "codex_auth",
        "disk",
    ]


def test_pause_writes_join_the_callers_transaction(conn: sqlite3.Connection) -> None:
    store.add_pause(conn, "kept", since="t0")
    with pytest.raises(RuntimeError), transaction(conn):
        store.add_pause(conn, "disk", since="t1")
        store.remove_pause(conn, "kept")
        raise RuntimeError
    assert [pause.reason for pause in store.list_pauses(conn)] == ["kept"]


def test_only_one_of_several_connections_adds_a_pause(db_path: Path) -> None:
    connect(db_path).close()
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def add() -> None:
        conn = connect(db_path)
        barrier.wait()
        results.append(store.add_pause(conn, "disk", since="t"))
        conn.close()

    threads = [threading.Thread(target=add) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False] * 7 + [True]


def record_check(
    conn: sqlite3.Connection,
    level: CheckLevel,
    *,
    target: str = "",
    item: str = "disk",
    detail: str | None = None,
    at: str = "t",
) -> CheckLevel | None:
    return store.record_doctor_check(
        conn, target=target, item=item, level=level, detail=detail, at=at
    )


def test_the_first_doctor_check_has_no_previous_level(
    conn: sqlite3.Connection,
) -> None:
    assert record_check(conn, CheckLevel.OK) is None


def test_a_doctor_check_returns_the_previous_level_as_an_enum(
    conn: sqlite3.Connection,
) -> None:
    record_check(conn, CheckLevel.WARN)
    previous = record_check(conn, CheckLevel.FAIL)
    assert previous is CheckLevel.WARN
    assert record_check(conn, CheckLevel.OK) is CheckLevel.FAIL


def test_a_doctor_check_replaces_the_row(conn: sqlite3.Connection) -> None:
    record_check(conn, CheckLevel.WARN, detail="9 GB", at="t1")
    record_check(conn, CheckLevel.OK, detail=None, at="t2")
    assert store.list_doctor_checks(conn) == (
        DoctorCheck(target="", item="disk", level=CheckLevel.OK, at="t2"),
    )


def test_doctor_checks_are_keyed_by_target_and_item(conn: sqlite3.Connection) -> None:
    record_check(conn, CheckLevel.FAIL, target="one", item="main_session")
    assert record_check(conn, CheckLevel.OK, target="two", item="main_session") is None
    assert record_check(conn, CheckLevel.OK, target="one", item="other") is None
    assert (
        record_check(conn, CheckLevel.OK, target="one", item="main_session")
        is CheckLevel.FAIL
    )


def test_doctor_checks_are_ordered_by_target_then_item(
    conn: sqlite3.Connection,
) -> None:
    record_check(conn, CheckLevel.OK, target="two", item="a")
    record_check(conn, CheckLevel.OK, target="one", item="b")
    record_check(conn, CheckLevel.OK, target="one", item="a")
    record_check(conn, CheckLevel.OK, target="", item="z")
    assert [(c.target, c.item) for c in store.list_doctor_checks(conn)] == [
        ("", "z"),
        ("one", "a"),
        ("one", "b"),
        ("two", "a"),
    ]


def test_a_doctor_check_joins_the_callers_transaction(
    conn: sqlite3.Connection,
) -> None:
    record_check(conn, CheckLevel.OK)
    with pytest.raises(RuntimeError), transaction(conn):
        record_check(conn, CheckLevel.FAIL)
        raise RuntimeError
    assert store.list_doctor_checks(conn)[0].level is CheckLevel.OK


def test_concurrent_doctor_checks_each_see_a_distinct_previous_row(
    db_path: Path,
) -> None:
    """Exactly one writer finds no row: the read and write are one transaction."""
    connect(db_path).close()
    results: list[CheckLevel | None] = []
    barrier = threading.Barrier(8)

    def write() -> None:
        conn = connect(db_path)
        barrier.wait()
        results.append(record_check(conn, CheckLevel.WARN))
        conn.close()

    threads = [threading.Thread(target=write) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(None) == 1
    assert results.count(CheckLevel.WARN) == 7
