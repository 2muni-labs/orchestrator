import ast
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
from orch.models import Effort, Grade, Lane, Task, TaskState
from orch.store import (
    IllegalTransitionError,
    SchemaTooNewError,
    StateMismatchError,
    TaskNotFoundError,
    connect,
    get_task,
    insert_task,
    migrate,
    transaction,
    transition,
)

THREADS = 8

EXPECTED_COLUMNS = {
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


def test_schema_v1_matches_the_design(conn: sqlite3.Connection) -> None:
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
    assert len(store.MIGRATIONS) == 1
    assert user_version(conn) == 1


def test_migrate_is_idempotent(db_path: Path) -> None:
    conn = connect(db_path)
    insert_task(conn, make_task())
    assert migrate(conn) == 1
    conn.close()
    reopened = connect(db_path)
    assert user_version(reopened) == 1
    assert get_task(reopened, "feat-x") is not None


def test_new_migration_applies_once_and_keeps_data(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(db_path)
    insert_task(conn, make_task())
    added = ("CREATE TABLE extra (id INTEGER PRIMARY KEY)",)
    monkeypatch.setattr(store, "MIGRATIONS", (*store.MIGRATIONS, added))
    assert migrate(conn) == 2
    assert migrate(conn) == 2
    assert "extra" in table_names(conn)
    assert get_task(conn, "feat-x") is not None


def test_failed_migration_rolls_back_entirely(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(db_path)
    broken = ("CREATE TABLE half_done (id INTEGER PRIMARY KEY)", "THIS IS NOT SQL")
    monkeypatch.setattr(store, "MIGRATIONS", (*store.MIGRATIONS, broken))
    with pytest.raises(sqlite3.Error):
        migrate(conn)
    assert user_version(conn) == 1
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
    assert user_version(conn) == 1
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
