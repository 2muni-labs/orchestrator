import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orch import show, store
from orch.config import TargetPaths, ensure_target
from orch.models import (
    Effort,
    FailureReason,
    Gate,
    Grade,
    Lane,
    Outcome,
    Run,
    Task,
    TaskState,
)

TARGET = "sample"
TASK = "feat-x"

TASK_KEYS = [
    "id",
    "target",
    "title",
    "grade",
    "lane",
    "model",
    "effort",
    "attempt",
    "depends_on",
    "milestone",
    "replaces",
    "base_sha",
    "test_sha",
    "gated_sha",
    "pushed_sha",
    "pr_url",
    "worktree",
    "created_at",
    "updated_at",
]
TASK_KEYS.insert(3, "state")

RUN_KEYS = [
    "run_id",
    "attempt",
    "lane",
    "model",
    "effort",
    "started_at",
    "ended_at",
    "exit_code",
    "outcome",
    "failure_reason",
    "worktree",
    "log_path",
]


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return store.connect(tmp_path / "state.db")


@pytest.fixture
def paths(tmp_path: Path) -> TargetPaths:
    return ensure_target(tmp_path / "runtime", TARGET, "owner/sample")


def make_task(**overrides: object) -> Task:
    values: dict[str, object] = {
        "id": TASK,
        "target": TARGET,
        "title": "Add feature X",
        "grade": Grade.L2,
        "lane": Lane.B,
        "model": "gpt-6.1-sol",
        "state": TaskState.PREPARED,
        "timeout_sec": 2700,
        "created_at": "2026-11-01T00:00:00+00:00",
        "updated_at": "2026-11-02T00:00:00+00:00",
    }
    return Task(**(values | overrides))  # type: ignore[arg-type]


def add(conn: sqlite3.Connection, task: Task) -> Task:
    store.insert_task(conn, task)
    return task


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def add_run(conn: sqlite3.Connection, task_id: str = TASK, **overrides: object) -> int:
    values: dict[str, object] = {
        "task_id": task_id,
        "attempt": 1,
        "lane": Lane.B,
        "model": "gpt-6.1-sol",
    }
    return store.insert_run(conn, Run(**(values | overrides)))  # type: ignore[arg-type]


SPEC = (
    "+++\n"
    'id = "feat-x"\n'
    'title = "Add feature X"\n'
    'grade = "L9"\n'
    "timeout_min = 45\n"
    'modify = ["src/a.py", "src/b.py"]\n'
    "review = true\n"
    'extra = { nested = "kept" }\n'
    "+++\n"
    "\n# Purpose\n\nbody\n+++\nnot = 1\n"
)


# Top level


def test_the_keys_come_in_the_contract_order(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task())
    assert list(show.show_task(conn, task, paths)) == [
        "task",
        "spec",
        "result",
        "gates",
        "review",
        "triage",
        "runs",
    ]


def test_a_bare_task_has_nulls_and_empty_lists(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task())
    data = show.show_task(conn, task, paths)
    assert data["spec"] is None
    assert data["result"] is None
    assert data["gates"] == []
    assert data["review"] is None
    assert data["triage"] is None
    assert data["runs"] == []


def test_the_output_is_json_serializable(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task(attempt=1, worktree="/w"))
    write(paths.tasks / f"{TASK}.md", "+++\nwhen = 2026-11-22T01:02:03Z\n+++\n")
    write(paths.results / f"{TASK}.json", '{"status": "DONE"}')
    store.record_gate_result(conn, TASK, 1, Gate.G1, passed=True, at="a")
    add_run(conn, outcome=Outcome.DONE)
    json.dumps(show.show_task(conn, task, paths))


# task


def test_the_task_section_has_every_field_in_order(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(
        conn,
        make_task(
            state=TaskState.PR_OPEN,
            grade=Grade.L3,
            effort=Effort.HIGH,
            attempt=2,
            depends_on=("dep-a", "dep-b"),
            milestone="M17",
            replaces="feat-old",
            base_sha="b" * 40,
            test_sha="t" * 40,
            gated_sha="g" * 40,
            pushed_sha="p" * 40,
            pr_url="https://github.com/owner/sample/pull/7",
            worktree="/runtime/worktrees/feat-x",
            slot_expires_at="2026-11-03T00:00:00+00:00",
        ),
    )
    section = show.show_task(conn, task, paths)["task"]
    assert list(section) == TASK_KEYS
    assert section == {
        "id": TASK,
        "target": TARGET,
        "title": "Add feature X",
        "state": "pr_open",
        "grade": "L3",
        "lane": "B",
        "model": "gpt-6.1-sol",
        "effort": "high",
        "attempt": 2,
        "depends_on": ["dep-a", "dep-b"],
        "milestone": "M17",
        "replaces": "feat-old",
        "base_sha": "b" * 40,
        "test_sha": "t" * 40,
        "gated_sha": "g" * 40,
        "pushed_sha": "p" * 40,
        "pr_url": "https://github.com/owner/sample/pull/7",
        "worktree": "/runtime/worktrees/feat-x",
        "created_at": "2026-11-01T00:00:00+00:00",
        "updated_at": "2026-11-02T00:00:00+00:00",
    }


def test_missing_task_values_are_null(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task(lane=Lane.C, model="claude-sonnet-5-5"))
    section = show.show_task(conn, task, paths)["task"]
    for key in (
        "effort",
        "milestone",
        "replaces",
        "base_sha",
        "test_sha",
        "gated_sha",
        "pushed_sha",
        "pr_url",
        "worktree",
    ):
        assert section[key] is None, key
    assert section["depends_on"] == []
    assert section["attempt"] == 0
    assert section["lane"] == "C"


# spec


def test_the_spec_is_the_raw_front_matter(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task())
    write(paths.tasks / f"{TASK}.md", SPEC)
    assert show.show_task(conn, task, paths)["spec"] == {
        "id": "feat-x",
        "title": "Add feature X",
        "grade": "L9",
        "timeout_min": 45,
        "modify": ["src/a.py", "src/b.py"],
        "review": True,
        "extra": {"nested": "kept"},
    }


def test_spec_dates_become_strings(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task())
    write(paths.tasks / f"{TASK}.md", "+++\nwhen = 2026-11-22T01:02:03Z\n+++\nbody\n")
    spec = show.show_task(conn, task, paths)["spec"]
    assert spec == {"when": str(datetime(2026, 11, 22, 1, 2, 3, tzinfo=UTC))}


def test_spec_front_matter_lines_may_have_surrounding_spaces(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task())
    write(paths.tasks / f"{TASK}.md", "+++  \nid = 'feat-x'\n  +++\nbody\n")
    assert show.show_task(conn, task, paths)["spec"] == {"id": "feat-x"}


@pytest.mark.parametrize(
    "text",
    [
        "id = 'feat-x'\n",
        "\n+++\nid = 'feat-x'\n+++\n",
        "+++\nid = 'feat-x'\n",
        "+++\nid = \n+++\n",
        "+++\nid = 'a'\nid = 'b'\n+++\n",
        "",
    ],
    ids=["no-open", "blank-first", "unclosed", "bad-toml", "duplicate", "empty"],
)
def test_a_broken_spec_is_null(
    conn: sqlite3.Connection, paths: TargetPaths, text: str
) -> None:
    task = add(conn, make_task())
    write(paths.tasks / f"{TASK}.md", text)
    assert show.show_task(conn, task, paths)["spec"] is None


def test_an_unreadable_spec_is_null(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task())
    (paths.tasks / f"{TASK}.md").write_bytes(b"+++\nid = '\xff'\n+++\n")
    assert show.show_task(conn, task, paths)["spec"] is None
    (paths.tasks / f"{TASK}.md").unlink()
    (paths.tasks / f"{TASK}.md").mkdir()
    assert show.show_task(conn, task, paths)["spec"] is None


# result and review


def json_file(paths: TargetPaths, which: str) -> Path:
    if which == "result":
        return paths.results / f"{TASK}.json"
    return paths.review_path(TASK)


@pytest.mark.parametrize("which", ["result", "review"])
def test_a_stored_json_object_is_returned_as_is(
    conn: sqlite3.Connection, paths: TargetPaths, which: str
) -> None:
    task = add(conn, make_task())
    stored = {"task_id": TASK, "summary": "요약", "nested": {"a": [1, None, True]}}
    write(json_file(paths, which), json.dumps(stored, ensure_ascii=False))
    assert show.show_task(conn, task, paths)[which] == stored


@pytest.mark.parametrize("which", ["result", "review"])
@pytest.mark.parametrize("text", ["", "{", "[1, 2]", '"text"', "null", "3"], ids=repr)
def test_a_stored_file_that_is_not_a_json_object_is_null(
    conn: sqlite3.Connection, paths: TargetPaths, which: str, text: str
) -> None:
    task = add(conn, make_task())
    write(json_file(paths, which), text)
    assert show.show_task(conn, task, paths)[which] is None


@pytest.mark.parametrize("which", ["result", "review"])
def test_an_unreadable_stored_file_is_null(
    conn: sqlite3.Connection, paths: TargetPaths, which: str
) -> None:
    task = add(conn, make_task())
    path = json_file(paths, which)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"a": "\xff"}')
    assert show.show_task(conn, task, paths)[which] is None
    path.unlink()
    path.mkdir()
    assert show.show_task(conn, task, paths)[which] is None


def test_an_archived_review_is_not_the_current_one(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task(attempt=2))
    write(paths.review_path(TASK, 1), '{"verdict": "BLOCK"}')
    assert show.show_task(conn, task, paths)["review"] is None


# triage


def test_the_triage_is_its_absolute_path(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task())
    write(paths.triage_path(TASK), "# triage\n")
    triage = show.show_task(conn, task, paths)["triage"]
    assert triage == str(paths.triage_path(TASK))
    assert Path(str(triage)).is_absolute()


def test_a_triage_that_is_not_a_regular_file_is_null(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task())
    paths.triage_path(TASK).mkdir(parents=True)
    assert show.show_task(conn, task, paths)["triage"] is None


# gates


def test_gates_cover_every_attempt_in_order(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task(attempt=2))
    add(conn, make_task(id="other"))
    record = store.record_gate_result
    record(conn, TASK, 2, Gate.G1, passed=True, at="t21")
    record(conn, TASK, 1, Gate.G1, passed=True, at="t11")
    record(conn, TASK, 1, Gate.G2, passed=False, detail="2 failed", at="t12")
    record(conn, TASK, 2, Gate.G2, passed=True, at="t22")
    record(conn, TASK, 2, Gate.G3, passed=True, at="t23")
    record(conn, TASK, 3, Gate.G1, passed=True, at="beyond")
    record(conn, "other", 1, Gate.G1, passed=True, at="other")
    assert show.show_task(conn, task, paths)["gates"] == [
        {"attempt": 1, "gate": "G1", "passed": True, "detail": None, "at": "t11"},
        {
            "attempt": 1,
            "gate": "G2",
            "passed": False,
            "detail": "2 failed",
            "at": "t12",
        },
        {"attempt": 2, "gate": "G1", "passed": True, "detail": None, "at": "t21"},
        {"attempt": 2, "gate": "G2", "passed": True, "detail": None, "at": "t22"},
        {"attempt": 2, "gate": "G3", "passed": True, "detail": None, "at": "t23"},
    ]


def test_gates_of_attempt_zero_are_empty(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task(attempt=0))
    store.record_gate_result(conn, TASK, 1, Gate.G1, passed=True, at="x")
    assert show.show_task(conn, task, paths)["gates"] == []


# runs


def test_runs_list_each_run_with_its_numbered_log(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task(attempt=2, worktree="/runtime/worktrees/feat-x"))
    add(conn, make_task(id="other"))
    first = add_run(
        conn,
        effort=Effort.MEDIUM,
        started_at="s1",
        ended_at="e1",
        exit_code=0,
        outcome=Outcome.BLOCKED,
        failure_reason=FailureReason.OUT_OF_SCOPE,
        pid=123,
    )
    add_run(conn, "other", started_at="other")
    second = add_run(conn, attempt=2, started_at="s2")
    write(paths.logs / f"{TASK}.1.log", "log one\n")
    write(paths.logs / "other.2.log", "not ours\n")
    runs = show.show_task(conn, task, paths)["runs"]
    assert [list(run) for run in runs] == [RUN_KEYS, RUN_KEYS]
    assert runs == [
        {
            "run_id": first,
            "attempt": 1,
            "lane": "B",
            "model": "gpt-6.1-sol",
            "effort": "medium",
            "started_at": "s1",
            "ended_at": "e1",
            "exit_code": 0,
            "outcome": "BLOCKED",
            "failure_reason": "out_of_scope",
            "worktree": "/runtime/worktrees/feat-x",
            "log_path": str(paths.logs / f"{TASK}.1.log"),
        },
        {
            "run_id": second,
            "attempt": 2,
            "lane": "B",
            "model": "gpt-6.1-sol",
            "effort": None,
            "started_at": "s2",
            "ended_at": None,
            "exit_code": None,
            "outcome": None,
            "failure_reason": None,
            "worktree": "/runtime/worktrees/feat-x",
            "log_path": None,
        },
    ]


def test_the_log_number_is_the_run_position_not_the_run_id(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task(attempt=1))
    add(conn, make_task(id="other"))
    for _ in range(3):
        add_run(conn, "other")
    run_id = add_run(conn)
    assert run_id == 4
    write(paths.logs / f"{TASK}.1.log", "x\n")
    write(paths.logs / f"{TASK}.4.log", "x\n")
    runs = show.show_task(conn, task, paths)["runs"]
    assert runs[0]["log_path"] == str(paths.logs / f"{TASK}.1.log")


def test_a_log_that_is_not_a_regular_file_is_null(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task(attempt=1))
    add_run(conn)
    (paths.logs / f"{TASK}.1.log").mkdir(parents=True)
    assert show.show_task(conn, task, paths)["runs"][0]["log_path"] is None


def test_a_task_id_with_dots_finds_its_own_logs(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task(id="m.1", attempt=1))
    add_run(conn, "m.1")
    write(paths.logs / "m.1.1.log", "x\n")
    runs = show.show_task(conn, task, paths)["runs"]
    assert runs[0]["log_path"] == str(paths.logs / "m.1.1.log")


# Read only


def test_show_changes_neither_the_database_nor_the_files(
    conn: sqlite3.Connection, paths: TargetPaths
) -> None:
    task = add(conn, make_task(attempt=1, worktree="/w"))
    write(paths.tasks / f"{TASK}.md", SPEC)
    write(paths.results / f"{TASK}.json", "{}")
    add_run(conn)
    before_changes = conn.total_changes
    before_files = sorted((str(p), p.stat().st_mtime_ns) for p in paths.root.rglob("*"))
    show.show_task(conn, task, paths)
    assert conn.total_changes == before_changes
    assert conn.in_transaction is False
    assert (
        sorted((str(p), p.stat().st_mtime_ns) for p in paths.root.rglob("*"))
        == before_files
    )
