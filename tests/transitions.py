"""Check that a test's state-changing orch call asks for an allowed transition.

A stub fails before it reaches a refused transition, so a test that expects a
transition the state machine forbids would otherwise pass review and fail only
after the implementation exists. This check fails at the stub stage instead.

It checks only calls that run. A test with several calls must make every call
before it asserts on behavior the stub lacks; otherwise an early failure hides a
later call from this check.
"""

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from orch.models import TaskState, can_transition

# Where each state-changing command with a task id may move the task.
DESTINATIONS: dict[str, frozenset[TaskState]] = {
    "dispatch": frozenset({TaskState.RESERVED, TaskState.QUEUED}),
    "run": frozenset({TaskState.RUNNING}),
    "gate": frozenset({TaskState.GATING}),
    "review": frozenset({TaskState.READY, TaskState.JUDGING}),
    "pr": frozenset({TaskState.PR_OPEN, TaskState.CONFLICT}),
    "update": frozenset({TaskState.CONFLICT}),
    "close": frozenset({TaskState.CLOSED}),
    "discard": frozenset({TaskState.DISCARDED}),
    "retry": frozenset({TaskState.RESERVED, TaskState.QUEUED}),
    "resume": frozenset({TaskState.RESERVED, TaskState.QUEUED}),
    "kill": frozenset({TaskState.JUDGING}),
}


def task_of(args: Sequence[str]) -> tuple[str, str] | None:
    """Return the command and task id of a state-changing call, or None."""
    if not args or args[0] not in DESTINATIONS:
        return None
    index = 2 if args[0] == "review" else 1
    if len(args) <= index or args[index].startswith("-"):
        return None
    return args[0], args[index]


def check_transition(args: Sequence[str], state: str | None) -> None:
    """Fail the test if the call asks for a transition the state machine forbids.

    state is the task's current state, or None when the task does not exist.
    """
    call = task_of(args)
    if call is None or state is None:
        return
    command, task_id = call
    current = TaskState(state)
    if not any(can_transition(current, new) for new in DESTINATIONS[command]):
        wanted = ", ".join(sorted(DESTINATIONS[command]))
        pytest.fail(
            f"허용되지 않는 전이: {task_id} {current} -> {wanted} (orch {command}). "
            "거부를 기대하거나 가짜 구현을 쓰는 호출이면 unchecked=True로 표시하세요.",
            pytrace=False,
        )


def current_state(db: Path, args: Sequence[str]) -> str | None:
    """Return the state of the task a call names, or None if there is none yet."""
    call = task_of(args)
    if call is None or not db.is_file():
        return None
    try:
        conn = sqlite3.connect(db)
        try:
            row = conn.execute("SELECT state FROM tasks WHERE id = ?", (call[1],))
            found = row.fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return None if found is None else str(found[0])


def check_call(db: Path, args: Sequence[str], *, unchecked: bool = False) -> None:
    """Check a call before it runs.

    unchecked skips the check for a call that expects a refusal, or whose command
    a test has replaced with a fake.
    """
    if not unchecked:
        check_transition(args, current_state(db, args))
