import ast
import itertools
import random
import re
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orch import scheduler
from orch.config import Limits
from orch.models import Grade, Lane, Task, TaskState
from orch.scheduler import (
    BlockedChain,
    Decision,
    DispatchError,
    NextReport,
    blocked_chains,
    dispatch,
    expire_reservations,
    globs_overlap,
    try_reserve,
)
from orch.store import (
    StateMismatchError,
    TaskNotFoundError,
    connect,
    get_task,
    insert_task,
    list_locks,
    transition,
)
from orch.taskfile import TaskSpec

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
LIMITS = Limits(total=3, lane_b=2)
TTL = 10
TARGET = "sample"
THREADS = 8


def next_reservations(conn: sqlite3.Connection, **kwargs: object) -> tuple[str, ...]:
    """Return the reserved ids, which is all that most tests look at."""
    return scheduler.next_reservations(conn, **kwargs).reserved  # type: ignore[arg-type]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def conn(db_path: Path) -> sqlite3.Connection:
    return connect(db_path)


def add_task(
    conn: sqlite3.Connection,
    task_id: str,
    state: TaskState = TaskState.PREPARED,
    *,
    lane: Lane = Lane.B,
    depends_on: tuple[str, ...] = (),
    created_at: str = "2026-11-04T00:00:00+00:00",
    target: str = TARGET,
    slot_expires_at: str | None = None,
    worktree: str | None = None,
    base_sha: str | None = None,
) -> None:
    insert_task(
        conn,
        Task(
            id=task_id,
            target=target,
            title=task_id,
            grade=Grade.L2,
            lane=lane,
            model="m",
            state=state,
            timeout_sec=60,
            depends_on=depends_on,
            slot_expires_at=slot_expires_at,
            worktree=worktree,
            base_sha=base_sha,
            created_at=created_at,
            updated_at=created_at,
        ),
    )


def reserve(
    conn: sqlite3.Connection,
    task_id: str,
    modify: tuple[str, ...] = ("src/**",),
    expected: TaskState = TaskState.PREPARED,
    limits: Limits = LIMITS,
) -> Decision:
    return try_reserve(
        conn, task_id, expected, modify=modify, limits=limits, ttl_min=TTL, now=NOW
    )


def state_of(conn: sqlite3.Connection, task_id: str) -> TaskState:
    task = get_task(conn, task_id)
    assert task is not None
    return task.state


def holders(conn: sqlite3.Connection, target: str = TARGET) -> set[str]:
    return {lock.task_id for lock in list_locks(conn, target)}


def event_count(conn: sqlite3.Connection, task_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM events WHERE task_id = ?", (task_id,))
    return row.fetchone()[0]


def close_task(conn: sqlite3.Connection, task_id: str) -> None:
    """Walk a reserved task along the normal path to closed."""
    path = [
        TaskState.RESERVED,
        TaskState.RUNNING,
        TaskState.JUDGING,
        TaskState.GATING,
        TaskState.READY,
        TaskState.PR_OPEN,
        TaskState.CLOSED,
    ]
    for current, new in itertools.pairwise(path):
        transition(conn, task_id, current, new, actor="test")


# Glob overlap


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("src/a.py", "src/a.py"),
        ("src/**", "src/a.py"),
        ("src/**", "src/a/b/c.py"),
        ("src/*.py", "src/a.py"),
        ("src/a?.py", "src/ab.py"),
        ("src/*/x.py", "src/a/*"),
        ("src/**/x.py", "src/a/b/x.py"),
        ("src/**/x.py", "src/x.py"),
        ("**/x.py", "x.py"),
        ("**", "a/b"),
        ("*.py", "a.py"),
        ("src/**/a/**", "src/x/**/b.py"),
        ("a*c", "ab*"),
        # A plain path also reaches what is under it, in case it is a directory.
        ("src/auth", "src/auth/**"),
        ("src/auth", "src/auth/login.py"),
        ("src/auth/", "src/auth/login.py"),
        ("src/*", "src/auth/login.py"),
        ("src/**/x.py", "src/a/y.py"),
        # Case-insensitive file systems make these one file.
        ("SRC/A.py", "src/a.py"),
    ],
)
def test_globs_overlap(first: str, second: str) -> None:
    assert globs_overlap(first, second) is True
    assert globs_overlap(second, first) is True


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("src/a.py", "src/b.py"),
        ("src/**", "docs/**"),
        ("src/*.py", "src/*.txt"),
        ("src/?.py", "src/ab.py"),
        ("src/a/*.py", "src/b/*.py"),
        ("*.py", "src/a.txt"),
        ("src/auth", "src/authz"),
        ("src/auth/**", "src/authz/**"),
        ("a*c", "b*"),
        ("src/**/x.py", "docs/x.py"),
    ],
)
def test_globs_do_not_overlap(first: str, second: str) -> None:
    assert globs_overlap(first, second) is False
    assert globs_overlap(second, first) is False


def glob_regex(glob: str) -> re.Pattern[str]:
    """The path matching rule of gate G1: ** crosses directories, * and ? do not."""
    out = ""
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        elif glob[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(out)


def test_a_path_matched_by_both_globs_always_means_overlap() -> None:
    rng = random.Random(20261104)
    tokens = ["a", "b", "/", "*", "?", "**", "**/"]
    paths = [
        "".join(letters)
        for length in range(1, 6)
        for letters in itertools.product("ab/", repeat=length)
    ]
    witnessed = 0
    for _ in range(400):
        first = "".join(rng.choices(tokens, k=rng.randint(1, 4)))
        second = "".join(rng.choices(tokens, k=rng.randint(1, 4)))
        one, two = glob_regex(first), glob_regex(second)
        if any(one.fullmatch(path) and two.fullmatch(path) for path in paths):
            witnessed += 1
            assert globs_overlap(first, second), (first, second)
    assert witnessed > 50


def test_pathological_globs_finish_quickly() -> None:
    glob = "*a" * 30 + "/" + "**/" * 30
    assert globs_overlap(glob, glob) is True


# Judging one task


def test_free_task_is_reserved_with_locks_and_an_expiry(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a")
    assert reserve(conn, "a", ("src/**", "docs/**")) == Decision(
        state=TaskState.RESERVED
    )
    task = get_task(conn, "a")
    assert task is not None
    assert task.state is TaskState.RESERVED
    assert task.slot_expires_at == (NOW + timedelta(minutes=TTL)).isoformat()
    assert [lock.path_glob for lock in list_locks(conn, TARGET)] == [
        "docs/**",
        "src/**",
    ]
    assert event_count(conn, "a") == 1


def test_lane_b_limit_queues_with_locks(conn: sqlite3.Connection) -> None:
    for name in ("a", "b", "c"):
        add_task(conn, name)
    reserve(conn, "a", ("a/**",))
    reserve(conn, "b", ("b/**",))
    decision = reserve(conn, "c", ("c/**",))
    assert decision == Decision(state=TaskState.QUEUED, waiting_for=("slot:B",))
    assert holders(conn) == {"a", "b", "c"}
    task = get_task(conn, "c")
    assert task is not None
    assert task.slot_expires_at is None


def test_lane_c_is_not_limited_by_lane_b(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    add_task(conn, "b")
    add_task(conn, "c", lane=Lane.C)
    reserve(conn, "a", ("a/**",))
    reserve(conn, "b", ("b/**",))
    assert reserve(conn, "c", ("c/**",)).state is TaskState.RESERVED


def test_total_limit_counts_every_lane(conn: sqlite3.Connection) -> None:
    add_task(conn, "c1", lane=Lane.C)
    add_task(conn, "c2", lane=Lane.C)
    add_task(conn, "d1", lane=Lane.D)
    add_task(conn, "c3", lane=Lane.C)
    add_task(conn, "b1")
    for name in ("c1", "c2", "d1"):
        assert reserve(conn, name, (f"{name}/**",)).state is TaskState.RESERVED
    assert reserve(conn, "c3", ("c3/**",)) == Decision(
        state=TaskState.QUEUED, waiting_for=("slot:total",)
    )
    assert reserve(conn, "b1", ("b1/**",)).waiting_for == ("slot:total",)


def test_both_limits_are_reported(conn: sqlite3.Connection) -> None:
    for name in ("a", "b", "c"):
        add_task(conn, name)
    limits = Limits(total=2, lane_b=2)
    reserve(conn, "a", ("a/**",), limits=limits)
    reserve(conn, "b", ("b/**",), limits=limits)
    decision = reserve(conn, "c", ("c/**",), limits=limits)
    assert decision.waiting_for == ("slot:total", "slot:B")


def test_running_tasks_hold_slots_but_finished_ones_do_not(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "running", TaskState.RUNNING)
    add_task(conn, "reserved", TaskState.RESERVED, slot_expires_at=expiry(5))
    for state in (TaskState.JUDGING, TaskState.READY, TaskState.PR_OPEN):
        add_task(conn, state.value, state)
    add_task(conn, "new")
    assert reserve(conn, "new").waiting_for == ("slot:B",)
    transition(conn, "running", TaskState.RUNNING, TaskState.JUDGING, actor="test")
    assert reserve(conn, "new", expected=TaskState.QUEUED).state is TaskState.RESERVED


def test_slots_are_shared_across_targets(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.RUNNING, target="one")
    add_task(conn, "b", TaskState.RUNNING, target="two")
    add_task(conn, "c")
    assert reserve(conn, "c").waiting_for == ("slot:B",)


def test_locks_conflict_only_within_a_target(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", target="one")
    add_task(conn, "b", target="two")
    assert reserve(conn, "a", ("src/**",)).state is TaskState.RESERVED
    assert reserve(conn, "b", ("src/**",)).state is TaskState.RESERVED


def test_overlapping_task_is_queued_without_locks(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    add_task(conn, "b")
    reserve(conn, "a", ("src/a/**",))
    decision = reserve(conn, "b", ("src/**", "docs/**"))
    assert decision == Decision(state=TaskState.QUEUED, waiting_for=("lock:a",))
    assert holders(conn) == {"a"}


def test_conflicting_holders_are_each_reported_once_and_sorted(
    conn: sqlite3.Connection,
) -> None:
    for name in ("z", "m", "new"):
        add_task(conn, name, lane=Lane.C)
    reserve(conn, "z", ("z/**", "z2/**"))
    reserve(conn, "m", ("m/**",))
    decision = reserve(conn, "new", ("z/**", "z2/**", "m/**"))
    assert decision.waiting_for == ("lock:m", "lock:z")


def test_locks_are_checked_before_slots(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    add_task(conn, "b")
    add_task(conn, "c")
    reserve(conn, "a", ("a/**",))
    reserve(conn, "b", ("b/**",))
    assert reserve(conn, "c", ("a/**",)).waiting_for == ("lock:a",)
    assert holders(conn) == {"a", "b"}


def test_a_tasks_own_locks_do_not_block_it(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    add_task(conn, "b")
    add_task(conn, "c")
    reserve(conn, "b", ("b/**",))
    reserve(conn, "c", ("c/**",))
    assert reserve(conn, "a", ("a/**",)).state is TaskState.QUEUED
    assert "a" in holders(conn)
    transition(conn, "b", TaskState.RESERVED, TaskState.DISCARDED, actor="test")
    assert (
        reserve(conn, "a", ("a/**",), expected=TaskState.QUEUED).state
        is TaskState.RESERVED
    )


@pytest.mark.parametrize(
    "state", [TaskState.JUDGING, TaskState.BLOCKED, TaskState.ORPHANED]
)
def test_tasks_coming_back_keep_their_locks_and_can_be_reserved(
    conn: sqlite3.Connection, state: TaskState
) -> None:
    add_task(conn, "a", state)
    assert reserve(conn, "a", expected=state).state is TaskState.RESERVED
    assert state_of(conn, "a") is TaskState.RESERVED


def test_tasks_coming_back_without_a_slot_are_queued(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.JUDGING)
    add_task(conn, "b", TaskState.RUNNING)
    add_task(conn, "c", TaskState.RUNNING)
    decision = reserve(conn, "a", expected=TaskState.JUDGING)
    assert decision == Decision(state=TaskState.QUEUED, waiting_for=("slot:B",))
    assert state_of(conn, "a") is TaskState.QUEUED
    assert holders(conn) == {"a"}


def test_wrong_expected_state_changes_nothing(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.RUNNING)
    with pytest.raises(StateMismatchError):
        reserve(conn, "a")
    assert holders(conn) == set()
    assert event_count(conn, "a") == 0


def test_missing_task_is_reported(conn: sqlite3.Connection) -> None:
    with pytest.raises(TaskNotFoundError):
        reserve(conn, "nope")


def test_queued_task_that_stays_queued_records_no_event(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.QUEUED)
    add_task(conn, "b", TaskState.RUNNING)
    add_task(conn, "c", TaskState.RUNNING)
    assert reserve(conn, "a", expected=TaskState.QUEUED).state is TaskState.QUEUED
    assert event_count(conn, "a") == 0


def test_failure_rolls_back_everything(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_task(conn, "a")

    def fail(*args: object, **kwargs: object) -> None:
        raise LookupError("stop")

    monkeypatch.setattr(scheduler.store, "update_task", fail)
    with pytest.raises(LookupError):
        reserve(conn, "a")
    assert state_of(conn, "a") is TaskState.PREPARED
    assert holders(conn) == set()
    assert conn.in_transaction is False


# Dependencies


def test_unmet_dependency_queues_without_locks(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    add_task(conn, "b", depends_on=("a",))
    decision = reserve(conn, "b")
    assert decision == Decision(state=TaskState.QUEUED, waiting_for=("dep:a",))
    assert holders(conn) == set()


def test_only_closed_satisfies_a_dependency(conn: sqlite3.Connection) -> None:
    for state in (TaskState.PR_OPEN, TaskState.READY):
        add_task(conn, state.value, state)
    add_task(conn, "b", depends_on=("pr_open", "ready", "unknown"))
    assert reserve(conn, "b").waiting_for == (
        "dep:pr_open",
        "dep:ready",
        "dep:unknown",
    )
    assert holders(conn) == set()


def test_met_dependency_lets_the_task_through(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.CLOSED)
    add_task(conn, "b", depends_on=("a",))
    assert reserve(conn, "b").state is TaskState.RESERVED


def test_dependent_task_dispatched_first_does_not_deadlock(
    conn: sqlite3.Connection,
) -> None:
    """B depends on A, their paths overlap, and B is dispatched first."""
    add_task(conn, "a", created_at="2026-11-04T00:00:02+00:00")
    add_task(conn, "b", depends_on=("a",), created_at="2026-11-04T00:00:01+00:00")
    overlapping = ("src/shared.py",)
    modify_of = lambda task: overlapping  # noqa: E731

    first = reserve(conn, "b", overlapping)
    assert first == Decision(state=TaskState.QUEUED, waiting_for=("dep:a",))
    assert holders(conn) == set()

    # B holds no lock, so A can start.
    assert reserve(conn, "a", overlapping).state is TaskState.RESERVED
    assert holders(conn) == {"a"}

    # While A is active, re-judging B must not take a lock.
    args = {"modify_of": modify_of, "limits": LIMITS, "ttl_min": TTL, "now": NOW}
    assert next_reservations(conn, **args) == ()
    assert state_of(conn, "b") is TaskState.QUEUED
    assert holders(conn) == {"a"}

    close_task(conn, "a")
    assert holders(conn) == set()

    assert next_reservations(conn, **args) == ("b",)
    assert state_of(conn, "b") is TaskState.RESERVED
    assert holders(conn) == {"b"}


def test_a_queued_task_takes_locks_when_its_dependency_is_met_but_no_slot(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.CLOSED)
    add_task(conn, "b", TaskState.QUEUED, depends_on=("a",))
    add_task(conn, "x", TaskState.RUNNING)
    add_task(conn, "y", TaskState.RUNNING)
    decision = reserve(conn, "b", expected=TaskState.QUEUED)
    assert decision == Decision(state=TaskState.QUEUED, waiting_for=("slot:B",))
    assert holders(conn) == {"b"}


def test_a_lock_held_while_waiting_for_a_slot_blocks_others(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "x", TaskState.RUNNING)
    add_task(conn, "y", TaskState.RUNNING)
    add_task(conn, "waiting")
    add_task(conn, "later", lane=Lane.C)
    reserve(conn, "waiting", ("src/**",))
    assert reserve(conn, "later", ("src/a.py",)).waiting_for == ("lock:waiting",)


# Discarded dependencies


def events_of(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT from_state, to_state FROM events WHERE task_id = ? ORDER BY id",
        (task_id,),
    )
    return list(rows)


def test_a_prepared_task_with_a_discarded_dependency_passes_through_queued(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.DISCARDED)
    add_task(conn, "b", depends_on=("a",))
    decision = reserve(conn, "b")
    assert decision == Decision(state=TaskState.BLOCKED, waiting_for=("dep:a",))
    assert state_of(conn, "b") is TaskState.BLOCKED
    assert events_of(conn, "b") == [("prepared", "queued"), ("queued", "blocked")]
    assert holders(conn) == set()


def test_only_discarded_dependencies_are_reported_and_sorted(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "z", TaskState.DISCARDED)
    add_task(conn, "y", TaskState.DISCARDED)
    add_task(conn, "open", TaskState.PR_OPEN)
    add_task(conn, "done", TaskState.CLOSED)
    add_task(conn, "b", depends_on=("z", "open", "done", "y", "unknown"))
    assert reserve(conn, "b") == Decision(
        state=TaskState.BLOCKED, waiting_for=("dep:y", "dep:z")
    )


def test_resuming_with_the_dependency_still_discarded_blocks_again(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.DISCARDED)
    add_task(conn, "b", TaskState.BLOCKED, depends_on=("a",))
    decision = reserve(conn, "b", expected=TaskState.BLOCKED)
    assert decision == Decision(state=TaskState.BLOCKED, waiting_for=("dep:a",))
    assert state_of(conn, "b") is TaskState.BLOCKED
    assert events_of(conn, "b") == [("blocked", "queued"), ("queued", "blocked")]


def test_a_blocked_task_clears_its_expiry(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.DISCARDED)
    add_task(conn, "b", TaskState.QUEUED, depends_on=("a",), slot_expires_at=expiry(5))
    assert reserve(conn, "b", expected=TaskState.QUEUED).state is TaskState.BLOCKED
    task = get_task(conn, "b")
    assert task is not None
    assert task.slot_expires_at is None


def test_a_dependency_on_a_blocked_task_keeps_waiting(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.BLOCKED)
    add_task(conn, "b", depends_on=("a",))
    assert reserve(conn, "b") == Decision(
        state=TaskState.QUEUED, waiting_for=("dep:a",)
    )


def test_next_blocks_a_queued_task_and_reserves_the_others(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.DISCARDED)
    add_task(
        conn,
        "b",
        TaskState.QUEUED,
        depends_on=("a",),
        created_at="2026-11-04T00:00:01+00:00",
    )
    add_task(conn, "c", TaskState.QUEUED, created_at="2026-11-04T00:00:02+00:00")
    reserved = next_reservations(
        conn,
        modify_of=lambda task: (f"{task.id}/**",),
        limits=LIMITS,
        ttl_min=TTL,
        now=NOW,
    )
    assert reserved == ("c",)
    assert state_of(conn, "b") is TaskState.BLOCKED
    assert events_of(conn, "b") == [("queued", "blocked")]
    assert holders(conn) == {"c"}


def test_next_leaves_a_blocked_task_alone(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.DISCARDED)
    add_task(conn, "b", TaskState.BLOCKED, depends_on=("a",))
    assert (
        next_reservations(
            conn, modify_of=lambda task: ("b/**",), limits=LIMITS, ttl_min=TTL, now=NOW
        )
        == ()
    )
    assert events_of(conn, "b") == []


def test_a_blocked_task_holds_no_slot(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.DISCARDED)
    add_task(conn, "x", TaskState.RUNNING)
    add_task(conn, "b", depends_on=("a",))
    reserve(conn, "b")
    add_task(conn, "c")
    assert reserve(conn, "c").state is TaskState.RESERVED


# Expiry


def expiry(minutes: int) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat()


def test_expired_reservation_returns_to_queued_with_its_locks(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a")
    reserve(conn, "a")
    later = NOW + timedelta(minutes=TTL)
    assert expire_reservations(conn, now=later) == ("a",)
    task = get_task(conn, "a")
    assert task is not None
    assert task.state is TaskState.QUEUED
    assert task.slot_expires_at is None
    assert holders(conn) == {"a"}


def test_reservation_before_its_expiry_stays(conn: sqlite3.Connection) -> None:
    add_task(conn, "a")
    reserve(conn, "a")
    almost = NOW + timedelta(minutes=TTL) - timedelta(seconds=1)
    assert expire_reservations(conn, now=almost) == ()
    assert state_of(conn, "a") is TaskState.RESERVED


def test_expiry_compares_times_not_text(conn: sqlite3.Connection) -> None:
    # Later in real time, but sorts earlier as text because of its offset.
    add_task(conn, "a", TaskState.RESERVED, slot_expires_at="2026-11-04T11:30:00-02:00")
    assert expire_reservations(conn, now=NOW) == ()


def test_reservation_without_an_expiry_counts_as_expired(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.RESERVED)
    assert expire_reservations(conn, now=NOW) == ("a",)


def test_expiry_defaults_to_the_current_time(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.RESERVED, slot_expires_at=expiry(-60 * 24 * 365))
    assert expire_reservations(conn) == ("a",)


def test_judging_frees_expired_slots_first(conn: sqlite3.Connection) -> None:
    add_task(conn, "old1", TaskState.RESERVED, slot_expires_at=expiry(-1))
    add_task(conn, "old2", TaskState.RESERVED, slot_expires_at=expiry(0))
    add_task(conn, "new")
    assert reserve(conn, "new").state is TaskState.RESERVED
    assert state_of(conn, "old1") is TaskState.QUEUED
    assert state_of(conn, "old2") is TaskState.QUEUED


# Re-judging the queue


def test_next_reserves_oldest_first_up_to_the_limits(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "c", TaskState.QUEUED, created_at="2026-11-04T00:00:03+00:00")
    add_task(conn, "a", TaskState.QUEUED, created_at="2026-11-04T00:00:01+00:00")
    add_task(conn, "b", TaskState.QUEUED, created_at="2026-11-04T00:00:02+00:00")
    reserved = next_reservations(
        conn,
        modify_of=lambda task: (f"{task.id}/**",),
        limits=LIMITS,
        ttl_min=TTL,
        now=NOW,
    )
    assert reserved == ("a", "b")
    assert state_of(conn, "c") is TaskState.QUEUED
    assert holders(conn) == {"a", "b", "c"}


def test_next_with_an_empty_queue_returns_nothing(conn: sqlite3.Connection) -> None:
    assert (
        next_reservations(
            conn, modify_of=lambda task: (), limits=LIMITS, ttl_min=TTL, now=NOW
        )
        == ()
    )


def test_next_skips_a_blocked_older_task(conn: sqlite3.Connection) -> None:
    add_task(conn, "holder", lane=Lane.C)
    reserve(conn, "holder", ("src/**",))
    add_task(conn, "a", TaskState.QUEUED, created_at="2026-11-04T00:00:01+00:00")
    add_task(conn, "b", TaskState.QUEUED, created_at="2026-11-04T00:00:02+00:00")
    modify = {"a": ("src/**",), "b": ("docs/**",)}
    reserved = next_reservations(
        conn,
        modify_of=lambda task: modify[task.id],
        limits=LIMITS,
        ttl_min=TTL,
        now=NOW,
    )
    assert reserved == ("b",)


def test_next_returns_expired_reservations_to_the_queue_and_rejudges_them(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.RESERVED, slot_expires_at=expiry(-1))
    assert next_reservations(
        conn, modify_of=lambda task: ("a/**",), limits=LIMITS, ttl_min=TTL, now=NOW
    ) == ("a",)
    task = get_task(conn, "a")
    assert task is not None
    assert task.state is TaskState.RESERVED
    assert task.slot_expires_at == expiry(TTL)


def test_a_closed_lock_holder_releases_a_waiting_task(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a")
    add_task(conn, "b")
    reserve(conn, "a", ("src/**",))
    assert reserve(conn, "b", ("src/**",)).waiting_for == ("lock:a",)
    close_task(conn, "a")
    assert next_reservations(
        conn, modify_of=lambda task: ("src/**",), limits=LIMITS, ttl_min=TTL, now=NOW
    ) == ("b",)


def test_next_rolls_back_when_a_spec_cannot_be_read(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.QUEUED, created_at="2026-11-04T00:00:01+00:00")
    add_task(conn, "b", TaskState.QUEUED, created_at="2026-11-04T00:00:02+00:00")

    def modify_of(task: Task) -> tuple[str, ...]:
        if task.id == "b":
            raise LookupError("no spec")
        return ("a/**",)

    with pytest.raises(LookupError):
        next_reservations(
            conn, modify_of=modify_of, limits=LIMITS, ttl_min=TTL, now=NOW
        )
    assert state_of(conn, "a") is TaskState.QUEUED
    assert holders(conn) == set()


# Dispatch


def make_spec(task_id: str = "a", **overrides: object) -> TaskSpec:
    values: dict[str, object] = {
        "id": task_id,
        "target": TARGET,
        "grade": Grade.L2,
        "lane": Lane.B,
        "model": "m",
        "timeout_min": 45,
        "modify": ("src/**",),
        "read": ("src/**",),
        "verify": "true",
        "depends_on": (),
        "body": "",
    }
    values.update(overrides)
    return TaskSpec(**values)


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "gitconfig"
    config.write_text(
        "[user]\n\tname = t\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "base.txt").write_text("x\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    return path


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text("x\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", name)
    return git(repo, "rev-parse", "HEAD")


def run_dispatch(conn: sqlite3.Connection, spec: TaskSpec) -> Decision:
    return dispatch(conn, spec, limits=LIMITS, ttl_min=TTL, now=NOW)


def test_dispatch_records_the_first_commit_as_the_test_commit(
    conn: sqlite3.Connection, repo: Path
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    first = commit(repo, "test.txt")
    commit(repo, "later.txt")
    add_task(conn, "a", worktree=str(repo), base_sha=base)
    assert run_dispatch(conn, make_spec()) == Decision(state=TaskState.RESERVED)
    task = get_task(conn, "a")
    assert task is not None
    assert task.test_sha == first
    assert holders(conn) == {"a"}


def test_dispatch_without_a_test_commit_is_refused(
    conn: sqlite3.Connection, repo: Path
) -> None:
    add_task(conn, "a", worktree=str(repo), base_sha=git(repo, "rev-parse", "HEAD"))
    with pytest.raises(DispatchError):
        run_dispatch(conn, make_spec())
    assert state_of(conn, "a") is TaskState.PREPARED
    assert holders(conn) == set()


def test_dispatch_with_tests_none_uses_the_base_commit(
    conn: sqlite3.Connection, repo: Path
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    add_task(conn, "a", worktree=str(repo), base_sha=base)
    decision = run_dispatch(conn, make_spec(grade=Grade.L1, tests="none"))
    assert decision.state is TaskState.RESERVED
    task = get_task(conn, "a")
    assert task is not None
    assert task.test_sha == base


@pytest.mark.parametrize("field", ["worktree", "base_sha"])
def test_dispatch_needs_a_prepared_worktree(
    conn: sqlite3.Connection, repo: Path, field: str
) -> None:
    values = {"worktree": str(repo), "base_sha": git(repo, "rev-parse", "HEAD")}
    values[field] = None
    add_task(conn, "a", **values)
    with pytest.raises(DispatchError):
        run_dispatch(conn, make_spec())


def test_dispatch_of_a_missing_task_is_reported(conn: sqlite3.Connection) -> None:
    with pytest.raises(TaskNotFoundError):
        run_dispatch(conn, make_spec("nope"))


def test_dispatch_of_a_task_in_another_state_is_refused(
    conn: sqlite3.Connection, repo: Path
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "test.txt")
    add_task(conn, "a", TaskState.RUNNING, worktree=str(repo), base_sha=base)
    with pytest.raises(StateMismatchError):
        run_dispatch(conn, make_spec())
    task = get_task(conn, "a")
    assert task is not None
    assert task.test_sha is None


def test_dispatch_queues_with_the_test_commit_recorded(
    conn: sqlite3.Connection, repo: Path
) -> None:
    add_task(conn, "x", TaskState.RUNNING)
    add_task(conn, "y", TaskState.RUNNING)
    base = git(repo, "rev-parse", "HEAD")
    first = commit(repo, "test.txt")
    add_task(conn, "a", worktree=str(repo), base_sha=base)
    assert run_dispatch(conn, make_spec()).waiting_for == ("slot:B",)
    task = get_task(conn, "a")
    assert task is not None
    assert task.test_sha == first


def test_dispatch_blocks_on_a_discarded_dependency_with_the_test_commit_recorded(
    conn: sqlite3.Connection, repo: Path
) -> None:
    add_task(conn, "d", TaskState.DISCARDED)
    base = git(repo, "rev-parse", "HEAD")
    first = commit(repo, "test.txt")
    add_task(conn, "a", worktree=str(repo), base_sha=base, depends_on=("d",))
    decision = run_dispatch(conn, make_spec())
    assert decision == Decision(state=TaskState.BLOCKED, waiting_for=("dep:d",))
    task = get_task(conn, "a")
    assert task is not None
    assert task.state is TaskState.BLOCKED
    assert task.test_sha == first
    assert events_of(conn, "a") == [("prepared", "queued"), ("queued", "blocked")]


# Concurrency


def run_in_threads(work: Callable[[sqlite3.Connection, int], object], db: Path) -> list:
    barrier = threading.Barrier(THREADS)
    results: list[object] = [None] * THREADS
    errors: list[BaseException] = []

    def target(index: int) -> None:
        conn = connect(db)
        try:
            barrier.wait()
            results[index] = work(conn, index)
        except BaseException as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=target, args=(i,)) for i in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    return results


@pytest.mark.parametrize("round_number", range(3))
def test_concurrent_dispatches_never_exceed_the_lane_limit(
    conn: sqlite3.Connection, db_path: Path, round_number: int
) -> None:
    for index in range(THREADS):
        add_task(conn, f"t{index}")
    decisions = run_in_threads(lambda c, i: reserve(c, f"t{i}", (f"p{i}/**",)), db_path)
    reserved = [d for d in decisions if d.state is TaskState.RESERVED]
    assert len(reserved) == LIMITS.lane_b
    rows = conn.execute("SELECT COUNT(*) FROM tasks WHERE state = 'reserved'")
    assert rows.fetchone()[0] == LIMITS.lane_b


@pytest.mark.parametrize("round_number", range(3))
def test_concurrent_dispatches_on_one_path_let_one_hold_the_lock(
    conn: sqlite3.Connection, db_path: Path, round_number: int
) -> None:
    for index in range(THREADS):
        add_task(conn, f"t{index}", lane=Lane.C)
    decisions = run_in_threads(lambda c, i: reserve(c, f"t{i}", ("src/**",)), db_path)
    reserved = [d for d in decisions if d.state is TaskState.RESERVED]
    assert len(reserved) == 1
    assert len(holders(conn)) == 1


@pytest.mark.parametrize("round_number", range(3))
def test_concurrent_next_calls_reserve_each_task_once(
    conn: sqlite3.Connection, db_path: Path, round_number: int
) -> None:
    for index in range(THREADS):
        add_task(conn, f"t{index}", TaskState.QUEUED)
    results = run_in_threads(
        lambda c, i: next_reservations(
            c,
            modify_of=lambda task: (f"{task.id}/**",),
            limits=LIMITS,
            ttl_min=TTL,
            now=NOW,
        ),
        db_path,
    )
    reserved = [task_id for result in results for task_id in result]
    assert len(reserved) == LIMITS.lane_b
    assert len(set(reserved)) == LIMITS.lane_b


# Conventions


def test_module_imports_only_the_standard_library_and_orch() -> None:
    tree = ast.parse(Path(scheduler.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imported.add(node.module.split(".")[0])
    assert imported - {"orch"} <= sys.stdlib_module_names
    assert "subprocess" not in imported


# Blocked chains and the next report


def judge_all(conn: sqlite3.Connection) -> NextReport:
    return scheduler.next_reservations(
        conn,
        modify_of=lambda task: (f"{task.id}/**",),
        limits=LIMITS,
        ttl_min=TTL,
        now=NOW,
    )


def test_blocked_chains_is_empty_without_tasks(conn: sqlite3.Connection) -> None:
    assert blocked_chains(conn) == ()


def test_a_task_on_a_discarded_dependency_names_it(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.DISCARDED)
    add_task(conn, "b", TaskState.BLOCKED, depends_on=("a",))
    assert blocked_chains(conn) == (
        BlockedChain(task_id="b", state=TaskState.BLOCKED, causes=("a",)),
    )


def test_a_queued_task_on_a_blocked_dependency_names_it(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.BLOCKED)
    add_task(conn, "b", TaskState.QUEUED, depends_on=("a",))
    assert blocked_chains(conn) == (
        BlockedChain(task_id="b", state=TaskState.QUEUED, causes=("a",)),
    )


def test_a_long_chain_names_the_nearest_stopped_task(conn: sqlite3.Connection) -> None:
    """C waits for B, B for A, and A is blocked by the discarded Z."""
    add_task(conn, "z", TaskState.DISCARDED, created_at="2026-11-04T00:00:00+00:00")
    add_task(
        conn,
        "a",
        TaskState.BLOCKED,
        depends_on=("z",),
        created_at="2026-11-04T00:00:01+00:00",
    )
    add_task(
        conn,
        "b",
        TaskState.QUEUED,
        depends_on=("a",),
        created_at="2026-11-04T00:00:02+00:00",
    )
    add_task(
        conn,
        "c",
        TaskState.QUEUED,
        depends_on=("b",),
        created_at="2026-11-04T00:00:03+00:00",
    )
    assert blocked_chains(conn) == (
        BlockedChain(task_id="a", state=TaskState.BLOCKED, causes=("z",)),
        BlockedChain(task_id="b", state=TaskState.QUEUED, causes=("a",)),
        BlockedChain(task_id="c", state=TaskState.QUEUED, causes=("a",)),
    )


@pytest.mark.parametrize(
    "middle",
    [
        TaskState.PREPARED,
        TaskState.QUEUED,
        TaskState.RESERVED,
        TaskState.RUNNING,
        TaskState.JUDGING,
        TaskState.READY,
        TaskState.PR_OPEN,
    ],
)
def test_a_chain_passes_through_any_unfinished_task(
    conn: sqlite3.Connection, middle: TaskState
) -> None:
    add_task(conn, "a", TaskState.DISCARDED)
    add_task(conn, "b", middle, depends_on=("a",))
    add_task(conn, "c", TaskState.QUEUED, depends_on=("b",))
    assert BlockedChain(
        task_id="c", state=TaskState.QUEUED, causes=("a",)
    ) in blocked_chains(conn)


def test_causes_are_sorted_and_unique_across_a_diamond(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "x", TaskState.BLOCKED)
    add_task(conn, "a", TaskState.DISCARDED)
    add_task(conn, "l", TaskState.QUEUED, depends_on=("x", "a"))
    add_task(conn, "r", TaskState.PREPARED, depends_on=("x",))
    add_task(conn, "top", TaskState.QUEUED, depends_on=("l", "r"))
    chains = {chain.task_id: chain for chain in blocked_chains(conn)}
    assert chains["top"].causes == ("a", "x")


@pytest.mark.parametrize(
    "state", [TaskState.CLOSED, TaskState.RUNNING, TaskState.PR_OPEN]
)
def test_a_healthy_dependency_is_not_a_cause(
    conn: sqlite3.Connection, state: TaskState
) -> None:
    add_task(conn, "a", state)
    add_task(conn, "b", TaskState.QUEUED, depends_on=("a",))
    assert blocked_chains(conn) == ()


def test_a_task_blocked_by_its_own_run_is_not_listed(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.BLOCKED)
    assert blocked_chains(conn) == ()


def test_a_missing_dependency_is_not_a_cause(conn: sqlite3.Connection) -> None:
    add_task(conn, "b", TaskState.QUEUED, depends_on=("gone",))
    assert blocked_chains(conn) == ()


@pytest.mark.parametrize(
    "state", [TaskState.PREPARED, TaskState.RESERVED, TaskState.JUDGING]
)
def test_only_queued_and_blocked_tasks_are_listed(
    conn: sqlite3.Connection, state: TaskState
) -> None:
    add_task(conn, "a", TaskState.DISCARDED)
    add_task(conn, "b", state, depends_on=("a",))
    assert blocked_chains(conn) == ()


def test_a_dependency_cycle_ends(conn: sqlite3.Connection) -> None:
    """orch prepare prevents cycles, but a damaged database must not hang status."""
    add_task(conn, "a", TaskState.QUEUED, depends_on=("b",))
    add_task(conn, "b", TaskState.QUEUED, depends_on=("a",))
    assert blocked_chains(conn) == ()


def test_a_cycle_still_finds_a_cause(conn: sqlite3.Connection) -> None:
    add_task(conn, "z", TaskState.BLOCKED)
    add_task(conn, "a", TaskState.QUEUED, depends_on=("b",))
    add_task(conn, "b", TaskState.QUEUED, depends_on=("a", "z"))
    chains = {chain.task_id: chain.causes for chain in blocked_chains(conn)}
    assert chains == {"a": ("z",), "b": ("z",)}


def test_blocked_chains_filters_by_target(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.DISCARDED, target="one")
    add_task(conn, "b", TaskState.QUEUED, depends_on=("a",), target="one")
    add_task(conn, "c", TaskState.DISCARDED, target="two")
    add_task(conn, "d", TaskState.QUEUED, depends_on=("c",), target="two")
    assert [chain.task_id for chain in blocked_chains(conn, target="one")] == ["b"]
    assert [chain.task_id for chain in blocked_chains(conn, target="two")] == ["d"]
    assert [chain.task_id for chain in blocked_chains(conn)] == ["b", "d"]


def test_blocked_chains_uses_created_at_order(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.BLOCKED)
    add_task(
        conn,
        "later",
        TaskState.QUEUED,
        depends_on=("a",),
        created_at="2026-11-04T00:00:02+00:00",
    )
    add_task(
        conn,
        "earlier",
        TaskState.QUEUED,
        depends_on=("a",),
        created_at="2026-11-04T00:00:01+00:00",
    )
    assert [chain.task_id for chain in blocked_chains(conn)] == ["earlier", "later"]


def test_blocked_chains_changes_nothing(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.DISCARDED)
    add_task(conn, "b", TaskState.QUEUED, depends_on=("a",))
    before = event_count(conn, "b")
    blocked_chains(conn)
    assert state_of(conn, "b") is TaskState.QUEUED
    assert event_count(conn, "b") == before


def test_next_report_is_empty_without_queued_tasks(conn: sqlite3.Connection) -> None:
    assert judge_all(conn) == NextReport()


def test_next_reports_the_newly_reserved(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.QUEUED)
    assert judge_all(conn) == NextReport(reserved=("a",))


def test_next_reports_the_newly_blocked_and_the_chain_behind_them(
    conn: sqlite3.Connection,
) -> None:
    add_task(conn, "a", TaskState.DISCARDED, created_at="2026-11-04T00:00:00+00:00")
    add_task(
        conn,
        "b",
        TaskState.QUEUED,
        depends_on=("a",),
        created_at="2026-11-04T00:00:01+00:00",
    )
    add_task(
        conn,
        "c",
        TaskState.QUEUED,
        depends_on=("b",),
        created_at="2026-11-04T00:00:02+00:00",
    )
    add_task(conn, "d", TaskState.QUEUED, created_at="2026-11-04T00:00:03+00:00")
    assert judge_all(conn) == NextReport(
        reserved=("d",),
        newly_blocked=("b",),
        chains=(
            BlockedChain(task_id="b", state=TaskState.BLOCKED, causes=("a",)),
            BlockedChain(task_id="c", state=TaskState.QUEUED, causes=("b",)),
        ),
    )
    assert state_of(conn, "c") is TaskState.QUEUED


def test_next_does_not_report_an_old_block_as_new(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.DISCARDED)
    add_task(conn, "b", TaskState.QUEUED, depends_on=("a",))
    assert judge_all(conn).newly_blocked == ("b",)
    report = judge_all(conn)
    assert report.newly_blocked == ()
    assert report.chains == (
        BlockedChain(task_id="b", state=TaskState.BLOCKED, causes=("a",)),
    )


def test_next_chains_cover_every_target(conn: sqlite3.Connection) -> None:
    add_task(conn, "a", TaskState.BLOCKED, target="one")
    add_task(conn, "b", TaskState.QUEUED, depends_on=("a",), target="one")
    add_task(conn, "c", TaskState.BLOCKED, target="two")
    add_task(conn, "d", TaskState.QUEUED, depends_on=("c",), target="two")
    assert [chain.task_id for chain in judge_all(conn).chains] == ["b", "d"]
