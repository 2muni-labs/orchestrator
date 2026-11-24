import json
import sqlite3
from pathlib import Path

import pytest

from orch import events, store
from orch.models import Effort, Grade, Lane, Task, TaskState

TARGET = "sample"
OTHER = "other"

EVENT_KEYS = [
    "schema_version",
    "id",
    "kind",
    "task_id",
    "target",
    "session_id",
    "from_state",
    "to_state",
    "actor",
    "note",
    "at",
]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def conn(db: Path) -> sqlite3.Connection:
    return store.connect(db)


def add_task(conn: sqlite3.Connection, task_id: str, target: str = TARGET) -> None:
    store.insert_task(
        conn,
        Task(
            id=task_id,
            target=target,
            title="Add feature",
            grade=Grade.L2,
            lane=Lane.B,
            model="gpt-6.1-sol",
            effort=Effort.MEDIUM,
            state=TaskState.PREPARED,
            timeout_sec=2700,
            created_at="2026-11-01T00:00:00+00:00",
            updated_at="2026-11-01T00:00:00+00:00",
        ),
    )


def move(
    conn: sqlite3.Connection,
    task_id: str,
    expected: TaskState,
    new: TaskState,
    *,
    note: str | None = None,
    at: str = "2026-11-22T01:00:00+00:00",
) -> None:
    store.transition(conn, task_id, expected, new, actor="orch", note=note, at=at)


def ids(lines: list[str]) -> list[int]:
    return [json.loads(line)["id"] for line in lines]


# read_events


def test_an_event_has_every_field_in_the_contract_order(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "feat-a")
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED, note="예약")
    found = events.read_events(conn, since=0, target=None)
    assert len(found) == 1
    assert list(found[0]) == EVENT_KEYS
    assert found[0] == {
        "schema_version": 2,
        "id": 1,
        "kind": "transition",
        "task_id": "feat-a",
        "target": TARGET,
        "session_id": None,
        "from_state": "prepared",
        "to_state": "reserved",
        "actor": "orch",
        "note": "예약",
        "at": "2026-11-22T01:00:00+00:00",
    }


def test_a_missing_note_is_null(conn: sqlite3.Connection) -> None:
    add_task(conn, "feat-a")
    move(conn, "feat-a", TaskState.PREPARED, TaskState.QUEUED)
    assert events.read_events(conn, since=0, target=None)[0]["note"] is None


def test_events_come_in_id_order_across_tasks_and_targets(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "feat-a")
    add_task(conn, "feat-b", OTHER)
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)
    move(conn, "feat-b", TaskState.PREPARED, TaskState.QUEUED)
    move(conn, "feat-a", TaskState.RESERVED, TaskState.RUNNING)
    found = events.read_events(conn, since=0, target=None)
    assert [(e["id"], e["task_id"], e["target"]) for e in found] == [
        (1, "feat-a", TARGET),
        (2, "feat-b", OTHER),
        (3, "feat-a", TARGET),
    ]


def test_since_skips_events_up_to_and_including_its_id(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "feat-a")
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)
    move(conn, "feat-a", TaskState.RESERVED, TaskState.RUNNING)
    move(conn, "feat-a", TaskState.RUNNING, TaskState.JUDGING)
    assert [e["id"] for e in events.read_events(conn, since=1, target=None)] == [2, 3]
    assert events.read_events(conn, since=3, target=None) == []
    assert events.read_events(conn, since=99, target=None) == []


def test_target_keeps_only_that_targets_tasks(conn: sqlite3.Connection) -> None:
    add_task(conn, "feat-a")
    add_task(conn, "feat-b", OTHER)
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)
    move(conn, "feat-b", TaskState.PREPARED, TaskState.QUEUED)
    found = events.read_events(conn, since=0, target=OTHER)
    assert [(e["id"], e["task_id"]) for e in found] == [(2, "feat-b")]
    assert events.read_events(conn, since=0, target="nobody") == []


def test_an_event_without_a_task_row_is_left_out(conn: sqlite3.Connection) -> None:
    add_task(conn, "feat-a")
    conn.execute(
        "INSERT INTO events (task_id, from_state, to_state, actor, note, at)"
        " VALUES ('ghost', 'prepared', 'queued', 'orch', NULL, 'x')"
    )
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)
    found = events.read_events(conn, since=0, target=None)
    assert [(e["id"], e["task_id"]) for e in found] == [(2, "feat-a")]


def wait(
    conn: sqlite3.Connection,
    kind: str,
    *,
    target: str = TARGET,
    session_id: str = "session-1",
    note: str = "permission_request",
    at: str = "2026-11-24T12:00:00+00:00",
) -> int:
    return store.record_wait(
        conn, kind=kind, target=target, session_id=session_id, note=note, at=at
    )


def test_a_wait_line_has_every_field_with_nulls_for_the_task(
    conn: sqlite3.Connection,
) -> None:
    wait(conn, "wait_start", note="idle_prompt")
    found = events.read_events(conn, since=0, target=None)
    assert [list(line) for line in found] == [EVENT_KEYS]
    assert found[0] == {
        "schema_version": 2,
        "id": 1,
        "kind": "wait_start",
        "task_id": None,
        "target": TARGET,
        "session_id": "session-1",
        "from_state": None,
        "to_state": None,
        "actor": "hook",
        "note": "idle_prompt",
        "at": "2026-11-24T12:00:00+00:00",
    }


def test_waits_and_transitions_share_one_id_order(conn: sqlite3.Connection) -> None:
    add_task(conn, "feat-a")
    wait(conn, "wait_start")
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)
    wait(conn, "wait_end", note="post_tool_use")
    found = events.read_events(conn, since=0, target=None)
    assert [(e["id"], e["kind"], e["task_id"]) for e in found] == [
        (1, "wait_start", None),
        (2, "transition", "feat-a"),
        (3, "wait_end", None),
    ]
    assert [e["id"] for e in events.read_events(conn, since=2, target=None)] == [3]


def test_target_filters_waits_by_their_own_target(conn: sqlite3.Connection) -> None:
    add_task(conn, "feat-a")
    wait(conn, "wait_start", target=OTHER)
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)
    wait(conn, "wait_start", target=TARGET, session_id="session-2")
    mine = events.read_events(conn, since=0, target=TARGET)
    assert [(e["id"], e["kind"]) for e in mine] == [
        (2, "transition"),
        (3, "wait_start"),
    ]
    other = events.read_events(conn, since=0, target=OTHER)
    assert [(e["id"], e["target"]) for e in other] == [(1, OTHER)]


def test_a_wait_row_needs_no_task_row(conn: sqlite3.Connection) -> None:
    wait(conn, "wait_start")
    conn.execute(
        "INSERT INTO events (task_id, from_state, to_state, actor, note, at)"
        " VALUES ('ghost', 'prepared', 'queued', 'orch', NULL, 'x')"
    )
    found = events.read_events(conn, since=0, target=None)
    assert [(e["id"], e["kind"]) for e in found] == [(1, "wait_start")]


def test_stream_writes_wait_lines_too(conn: sqlite3.Connection) -> None:
    wait(conn, "wait_start")
    wait(conn, "wait_end", note="stop")
    written: list[str] = []
    assert events.stream(conn, written.append) == 2
    assert [json.loads(line)["kind"] for line in written] == ["wait_start", "wait_end"]


def test_no_events_is_an_empty_list(conn: sqlite3.Connection) -> None:
    assert events.read_events(conn, since=0, target=None) == []


def test_reading_leaves_no_transaction_open(conn: sqlite3.Connection) -> None:
    add_task(conn, "feat-a")
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)
    events.read_events(conn, since=0, target=None)
    assert not conn.in_transaction


def test_reading_changes_nothing(conn: sqlite3.Connection) -> None:
    add_task(conn, "feat-a")
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)
    events.read_events(conn, since=0, target=None)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    task = store.get_task(conn, "feat-a")
    assert task is not None and task.state is TaskState.RESERVED


# stream without follow


def test_stream_writes_one_json_line_per_event(conn: sqlite3.Connection) -> None:
    add_task(conn, "feat-a")
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED, note="한글 메모")
    move(conn, "feat-a", TaskState.RESERVED, TaskState.RUNNING)
    written: list[str] = []
    last = events.stream(conn, written.append)
    assert last == 2
    assert len(written) == 2
    for chunk in written:
        assert chunk.endswith("\n") and chunk.count("\n") == 1
    assert written[0] == (
        json.dumps(
            events.read_events(conn, since=0, target=None)[0], ensure_ascii=False
        )
        + "\n"
    )
    assert "한글 메모" in written[0]
    assert ids(written) == [1, 2]
    assert list(json.loads(written[1])) == EVENT_KEYS


def test_stream_honours_since_and_target(conn: sqlite3.Connection) -> None:
    add_task(conn, "feat-a")
    add_task(conn, "feat-b", OTHER)
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)
    move(conn, "feat-b", TaskState.PREPARED, TaskState.QUEUED)
    move(conn, "feat-a", TaskState.RESERVED, TaskState.RUNNING)
    written: list[str] = []
    last = events.stream(conn, written.append, since=1, target=TARGET)
    assert ids(written) == [3]
    assert last == 3


def test_stream_with_nothing_new_returns_since(conn: sqlite3.Connection) -> None:
    written: list[str] = []
    assert events.stream(conn, written.append) == 0
    assert events.stream(conn, written.append, since=7) == 7
    assert written == []


def test_stream_without_follow_never_sleeps(conn: sqlite3.Connection) -> None:
    add_task(conn, "feat-a")
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)

    def sleep(seconds: float) -> None:
        raise AssertionError("slept without --follow")

    events.stream(conn, lambda line: None, sleep=sleep)


# stream with follow


class Clock:
    """A fake sleep and stop flag that run an action on each sleep."""

    def __init__(self, stop_after_checks: int) -> None:
        self.slept: list[float] = []
        self.checks = 0
        self.stop_after_checks = stop_after_checks
        self.on_sleep: list[object] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.on_sleep:
            action = self.on_sleep.pop(0)
            assert callable(action)
            action()

    def stopped(self) -> bool:
        self.checks += 1
        return self.checks >= self.stop_after_checks


def test_follow_stops_before_sleeping_when_already_stopped(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "feat-a")
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)
    clock = Clock(stop_after_checks=1)
    written: list[str] = []
    last = events.stream(
        conn, written.append, follow=True, sleep=clock.sleep, stopped=clock.stopped
    )
    assert ids(written) == [1]
    assert last == 1
    assert clock.slept == []


def test_follow_writes_a_new_event_from_another_connection_once(
    conn: sqlite3.Connection, db: Path
) -> None:
    add_task(conn, "feat-a")
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)

    def commit_elsewhere() -> None:
        other = store.connect(db)
        try:
            move(other, "feat-a", TaskState.RESERVED, TaskState.RUNNING)
        finally:
            other.close()

    # Checks: after read 1, after sleep 1, after read 2, after sleep 2,
    # after read 3, after sleep 3 (stop).
    clock = Clock(stop_after_checks=6)
    clock.on_sleep = [commit_elsewhere]
    written: list[str] = []
    last = events.stream(
        conn,
        written.append,
        follow=True,
        poll_sec=0.25,
        sleep=clock.sleep,
        stopped=clock.stopped,
    )
    assert ids(written) == [1, 2]
    assert last == 2
    assert clock.slept == [0.25, 0.25, 0.25]


def test_follow_checks_the_stop_flag_after_sleeping(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "feat-a")
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)
    clock = Clock(stop_after_checks=2)
    clock.on_sleep = [
        lambda: move(conn, "feat-a", TaskState.RESERVED, TaskState.RUNNING)
    ]
    written: list[str] = []
    last = events.stream(
        conn, written.append, follow=True, sleep=clock.sleep, stopped=clock.stopped
    )
    # Stopped during the sleep: the event committed then is not read.
    assert ids(written) == [1]
    assert last == 1
    assert clock.slept == [1.0]


def test_follow_honours_since_and_target(conn: sqlite3.Connection) -> None:
    add_task(conn, "feat-a")
    add_task(conn, "feat-b", OTHER)
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)

    def more() -> None:
        move(conn, "feat-b", TaskState.PREPARED, TaskState.QUEUED)
        move(conn, "feat-a", TaskState.RESERVED, TaskState.RUNNING)

    clock = Clock(stop_after_checks=4)
    clock.on_sleep = [more]
    written: list[str] = []
    last = events.stream(
        conn,
        written.append,
        since=0,
        target=TARGET,
        follow=True,
        sleep=clock.sleep,
        stopped=clock.stopped,
    )
    assert ids(written) == [1, 3]
    assert last == 3


def test_follow_with_nothing_new_writes_nothing_more(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "feat-a")
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)
    clock = Clock(stop_after_checks=8)
    written: list[str] = []
    last = events.stream(
        conn, written.append, follow=True, sleep=clock.sleep, stopped=clock.stopped
    )
    assert ids(written) == [1]
    assert last == 1
    assert len(clock.slept) == 4


def test_follow_leaves_no_transaction_open_while_sleeping(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "feat-a")
    move(conn, "feat-a", TaskState.PREPARED, TaskState.RESERVED)
    seen: list[bool] = []
    clock = Clock(stop_after_checks=4)
    clock.on_sleep = [
        lambda: seen.append(conn.in_transaction),
        lambda: seen.append(conn.in_transaction),
    ]
    events.stream(
        conn, lambda line: None, follow=True, sleep=clock.sleep, stopped=clock.stopped
    )
    assert seen == [False, False]
