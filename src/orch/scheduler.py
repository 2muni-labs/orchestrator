"""Slot reservation, path locks, dependencies and the list of runnable tasks."""

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orch import gitops, store
from orch.config import Limits
from orch.models import Lane, Task, TaskState
from orch.taskfile import TaskSpec


class DispatchError(Exception):
    """A task cannot be dispatched as it stands."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Decision:
    """Where a judged task ended up. waiting_for is empty when it is reserved."""

    state: TaskState
    waiting_for: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class BlockedChain:
    """A task that cannot proceed because of tasks that are blocked or discarded."""

    task_id: str
    state: TaskState
    causes: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class NextReport:
    """What one pass over the queued tasks found."""

    reserved: tuple[str, ...] = ()
    newly_blocked: tuple[str, ...] = ()
    chains: tuple[BlockedChain, ...] = ()


def globs_overlap(first: str, second: str) -> bool:
    """Return whether two path globs could reach the same file."""
    first = first.casefold().rstrip("/")
    second = second.casefold().rstrip("/")
    first_variants = (_glob_states(first), _glob_states(first + "/**"))
    second_variants = (_glob_states(second), _glob_states(second + "/**"))
    return any(
        _automata_overlap(one, two) for one in first_variants for two in second_variants
    )


@dataclass(frozen=True, slots=True)
class _GlobState:
    """NFA edges: None matches a dot, empty text a non-slash, else a literal."""

    epsilon: tuple[int, ...] = ()
    edges: tuple[tuple[str | None, int], ...] = ()


def _glob_states(glob: str) -> tuple[_GlobState, ...]:
    """Compile glob tokens into a finite automaton without backtracking."""
    states: list[_GlobState] = []
    index = 0
    while index < len(glob):
        current = len(states)
        if glob.startswith("**/", index):
            # Either skip the entire directory prefix, or consume .* then /.
            states.append(_GlobState(epsilon=(current + 1, current + 2)))
            states.append(_GlobState(edges=((None, current + 1), ("/", current + 2))))
            index += 3
        elif glob.startswith("**", index):
            states.append(_GlobState(epsilon=(current + 1,), edges=((None, current),)))
            index += 2
        elif glob[index] == "*":
            states.append(_GlobState(epsilon=(current + 1,), edges=(("", current),)))
            index += 1
        else:
            label = "" if glob[index] == "?" else glob[index]
            states.append(_GlobState(edges=((label, current + 1),)))
            index += 1
    states.append(_GlobState())
    return tuple(states)


def _labels_overlap(first: str | None, second: str | None) -> bool:
    """Return whether two consuming edges share at least one character."""
    if first is None:
        return second != "\n"
    if second is None:
        return first != "\n"
    if first == "":
        return second != "/"
    if second == "":
        return first != "/"
    return first == second


def _automata_overlap(
    first: tuple[_GlobState, ...], second: tuple[_GlobState, ...]
) -> bool:
    """Search the product automaton, visiting each state pair at most once."""
    pending = [(0, 0)]
    visited: set[tuple[int, int]] = set()
    accepting = (len(first) - 1, len(second) - 1)
    while pending:
        pair = pending.pop()
        if pair in visited:
            continue
        visited.add(pair)
        if pair == accepting:
            return True
        one, two = pair
        pending.extend((next_one, two) for next_one in first[one].epsilon)
        pending.extend((one, next_two) for next_two in second[two].epsilon)
        for label_one, next_one in first[one].edges:
            for label_two, next_two in second[two].edges:
                if _labels_overlap(label_one, label_two):
                    pending.append((next_one, next_two))
    return False


def expire_reservations(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> tuple[str, ...]:
    """Return expired reservations to queued, keeping their locks."""
    if now is None:
        now = datetime.now(UTC)
    expired: list[str] = []
    with store.transaction(conn):
        for task in store.list_tasks(conn, states=[TaskState.RESERVED]):
            if (
                task.slot_expires_at is None
                or datetime.fromisoformat(task.slot_expires_at) <= now
            ):
                store.transition(
                    conn,
                    task.id,
                    TaskState.RESERVED,
                    TaskState.QUEUED,
                    actor="orch",
                    at=now.isoformat(),
                )
                store.update_task(
                    conn, task.id, slot_expires_at=None, at=now.isoformat()
                )
                expired.append(task.id)
    return tuple(expired)


def try_reserve(
    conn: sqlite3.Connection,
    task_id: str,
    expected: TaskState,
    *,
    modify: Iterable[str],
    limits: Limits,
    ttl_min: int,
    now: datetime | None = None,
) -> Decision:
    """Judge one task atomically and move it to reserved or queued."""
    modify = tuple(modify)
    if now is None:
        now = datetime.now(UTC)
    with store.transaction(conn):
        expire_reservations(conn, now=now)
        task = store.get_task(conn, task_id)
        if task is None:
            raise store.TaskNotFoundError(task_id)
        if task.state != expected:
            raise store.StateMismatchError(
                f"Task {task_id}: expected {expected}, found {task.state}"
            )
        return _judge(
            conn, task, modify=modify, limits=limits, ttl_min=ttl_min, now=now
        )


def _judge(
    conn: sqlite3.Connection,
    task: Task,
    *,
    modify: tuple[str, ...],
    limits: Limits,
    ttl_min: int,
    now: datetime,
) -> Decision:
    """Apply dependency, lock and slot rules inside the caller's transaction."""
    dependencies = []
    discarded = []
    for task_id in task.depends_on:
        dependency = store.get_task(conn, task_id)
        if dependency is not None and dependency.state == TaskState.DISCARDED:
            discarded.append(f"dep:{task_id}")
        if dependency is None or dependency.state != TaskState.CLOSED:
            dependencies.append(f"dep:{task_id}")
    if discarded:
        waiting_for = tuple(sorted(discarded))
        _queue(conn, task, waiting_for, now=now)
        store.transition(
            conn,
            task.id,
            TaskState.QUEUED,
            TaskState.BLOCKED,
            actor="orch",
            at=now.isoformat(),
        )
        return Decision(state=TaskState.BLOCKED, waiting_for=waiting_for)
    if dependencies:
        return _queue(conn, task, tuple(sorted(dependencies)), now=now)

    conflicts = {
        f"lock:{lock.task_id}"
        for lock in store.list_locks(conn, task.target)
        if lock.task_id != task.id
        and any(globs_overlap(path, lock.path_glob) for path in modify)
    }
    if conflicts:
        return _queue(conn, task, tuple(sorted(conflicts)), now=now)

    store.add_locks(conn, task.target, task.id, modify)
    active = [
        other
        for other in store.list_tasks(
            conn, states=[TaskState.RESERVED, TaskState.RUNNING]
        )
        if other.id != task.id
    ]
    waiting_for = []
    if len(active) >= limits.total:
        waiting_for.append("slot:total")
    if task.lane == Lane.B and sum(t.lane == Lane.B for t in active) >= limits.lane_b:
        waiting_for.append("slot:B")
    if waiting_for:
        return _queue(conn, task, tuple(waiting_for), now=now)

    store.transition(
        conn, task.id, task.state, TaskState.RESERVED, actor="orch", at=now.isoformat()
    )
    store.update_task(
        conn,
        task.id,
        slot_expires_at=(now + timedelta(minutes=ttl_min)).isoformat(),
        at=now.isoformat(),
    )
    return Decision(state=TaskState.RESERVED)


def _queue(
    conn: sqlite3.Connection,
    task: Task,
    waiting_for: tuple[str, ...],
    *,
    now: datetime,
) -> Decision:
    """Queue a task without recording a same-state transition."""
    if task.state != TaskState.QUEUED:
        store.transition(
            conn,
            task.id,
            task.state,
            TaskState.QUEUED,
            actor="orch",
            at=now.isoformat(),
        )
    if task.slot_expires_at is not None:
        store.update_task(conn, task.id, slot_expires_at=None, at=now.isoformat())
    return Decision(state=TaskState.QUEUED, waiting_for=waiting_for)


def dispatch(
    conn: sqlite3.Connection,
    spec: TaskSpec,
    *,
    limits: Limits,
    ttl_min: int,
    now: datetime | None = None,
) -> Decision:
    """Record a prepared task's test commit, then judge it."""
    task = store.get_task(conn, spec.id)
    if task is None:
        raise store.TaskNotFoundError(spec.id)
    if task.state != TaskState.PREPARED:
        raise store.StateMismatchError(
            f"Task {spec.id}: expected {TaskState.PREPARED}, found {task.state}"
        )
    if not task.worktree or not task.base_sha:
        raise DispatchError(f"Task {spec.id} needs a worktree and base commit")
    if spec.tests == "none":
        test_sha = task.base_sha
    else:
        commits = gitops.commits_since(Path(task.worktree), task.base_sha)
        if not commits:
            raise DispatchError(f"Task {spec.id} needs a test commit")
        test_sha = commits[0]
    if now is None:
        now = datetime.now(UTC)
    with store.transaction(conn):
        store.update_task(conn, spec.id, test_sha=test_sha, at=now.isoformat())
        return try_reserve(
            conn,
            spec.id,
            TaskState.PREPARED,
            modify=spec.modify,
            limits=limits,
            ttl_min=ttl_min,
            now=now,
        )


def blocked_chains(
    conn: sqlite3.Connection, *, target: str | None = None
) -> tuple[BlockedChain, ...]:
    """Return queued and blocked tasks held up by a blocked or discarded task."""
    raise NotImplementedError


def next_reservations(
    conn: sqlite3.Connection,
    *,
    modify_of: Callable[[Task], Iterable[str]],
    limits: Limits,
    ttl_min: int,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Judge all queued tasks, oldest first, and return the newly reserved ids."""
    if now is None:
        now = datetime.now(UTC)
    reserved: list[str] = []
    with store.transaction(conn):
        expire_reservations(conn, now=now)
        for task in store.list_tasks(conn, states=[TaskState.QUEUED]):
            decision = _judge(
                conn,
                task,
                modify=tuple(modify_of(task)),
                limits=limits,
                ttl_min=ttl_min,
                now=now,
            )
            if decision.state == TaskState.RESERVED:
                reserved.append(task.id)
    return tuple(reserved)
