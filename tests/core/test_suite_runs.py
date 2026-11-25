"""Stored test selections of G2 and G2_PRE_PR (m-p5-test-select-gate)."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from orch import store
from orch.models import Gate
from orch.store import SuiteRecord, clear_suite_runs, record_suite_run, suite_run


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = store.connect(tmp_path / "state.db")
    yield connection
    connection.close()


def test_the_suite_runs_table_is_the_v8_migration() -> None:
    assert len(store.MIGRATIONS) >= 8
    assert store.MIGRATIONS[7][0].lstrip().startswith("CREATE TABLE suite_runs")


def test_a_selection_round_trips(conn: sqlite3.Connection) -> None:
    record_suite_run(
        conn,
        "m-a",
        2,
        Gate.G2,
        full=False,
        groups=iter(["core", "gate"]),
        reasons=("core: path:src/a.py", "gate: also:core"),
        failed_tests=["tests/x.py::test_y"],
        at="2026-11-23T00:00:00+00:00",
    )
    assert suite_run(conn, "m-a", 2, Gate.G2) == SuiteRecord(
        task_id="m-a",
        attempt=2,
        gate=Gate.G2,
        full=False,
        groups=("core", "gate"),
        reasons=("core: path:src/a.py", "gate: also:core"),
        failed_tests=("tests/x.py::test_y",),
        at="2026-11-23T00:00:00+00:00",
    )


def test_lists_are_stored_as_json_arrays(conn: sqlite3.Connection) -> None:
    record_suite_run(
        conn,
        "m-a",
        1,
        Gate.G2_PRE_PR,
        full=True,
        groups=(),
        reasons=("no_groups",),
        failed_tests=(),
        at="t",
    )
    row = conn.execute(
        "SELECT gate, full, groups, reasons, failed_tests FROM suite_runs"
    ).fetchone()
    assert row[0] == "G2_PRE_PR" and row[1] == 1
    assert [json.loads(value) for value in row[2:]] == [[], ["no_groups"], []]


def test_a_missing_selection_is_none(conn: sqlite3.Connection) -> None:
    assert suite_run(conn, "m-a", 1, Gate.G2) is None


def test_a_new_record_replaces_the_old_one_of_the_same_key(
    conn: sqlite3.Connection,
) -> None:
    for full, reason in ((True, "old"), (False, "new")):
        record_suite_run(
            conn,
            "m-a",
            1,
            Gate.G2,
            full=full,
            groups=(),
            reasons=(reason,),
            failed_tests=(),
        )
    record = suite_run(conn, "m-a", 1, Gate.G2)
    assert record is not None and record.reasons == ("new",) and record.full is False
    assert conn.execute("SELECT COUNT(*) FROM suite_runs").fetchone()[0] == 1


def test_the_default_time_is_now_in_utc(conn: sqlite3.Connection) -> None:
    record_suite_run(
        conn, "m-a", 1, Gate.G2, full=True, groups=(), reasons=(), failed_tests=()
    )
    record = suite_run(conn, "m-a", 1, Gate.G2)
    assert record is not None
    at = datetime.fromisoformat(record.at)
    assert at.utcoffset() == timedelta(0)


def test_clear_removes_only_the_named_gates_of_one_attempt(
    conn: sqlite3.Connection,
) -> None:
    for task_id, attempt, gate in (
        ("m-a", 1, Gate.G2),
        ("m-a", 1, Gate.G2_PRE_PR),
        ("m-a", 2, Gate.G2),
        ("m-b", 1, Gate.G2),
    ):
        record_suite_run(
            conn,
            task_id,
            attempt,
            gate,
            full=True,
            groups=(),
            reasons=(),
            failed_tests=(),
        )
    clear_suite_runs(conn, "m-a", 1, (Gate.G2,))
    assert suite_run(conn, "m-a", 1, Gate.G2) is None
    assert suite_run(conn, "m-a", 1, Gate.G2_PRE_PR) is not None
    assert suite_run(conn, "m-a", 2, Gate.G2) is not None
    assert suite_run(conn, "m-b", 1, Gate.G2) is not None


def test_a_version_seven_database_gains_the_table(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    for statements in store.MIGRATIONS[:7]:
        for statement in statements:
            old.execute(statement)
    old.execute("PRAGMA user_version = 7")
    old.commit()
    old.close()
    conn = store.connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == len(store.MIGRATIONS)
        record_suite_run(
            conn, "m-a", 1, Gate.G2, full=True, groups=(), reasons=(), failed_tests=()
        )
        assert suite_run(conn, "m-a", 1, Gate.G2) is not None
    finally:
        conn.close()
