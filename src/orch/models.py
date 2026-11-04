"""Data classes, enumerations and the task state machine."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class TaskState(StrEnum):
    """Task lifecycle states."""

    PREPARED = "prepared"
    QUEUED = "queued"
    RESERVED = "reserved"
    RUNNING = "running"
    JUDGING = "judging"
    GATING = "gating"
    REVIEWING = "reviewing"
    READY = "ready"
    CONFLICT = "conflict"
    PR_OPEN = "pr_open"
    CLOSED = "closed"
    BLOCKED = "blocked"
    DISCARDED = "discarded"
    ORPHANED = "orphaned"


class Grade(StrEnum):
    """Task difficulty grades."""

    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L3R = "L3R"


class Lane(StrEnum):
    """Execution lanes that can modify code."""

    B = "B"
    C = "C"
    D = "D"


class Effort(StrEnum):
    """Reasoning effort levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class Outcome(StrEnum):
    """Result of one sub-agent run."""

    DONE = "DONE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    BLOCKED_QUOTA = "BLOCKED_QUOTA"


class FailureReason(StrEnum):
    """Why a run failed or was blocked."""

    REPORT_MISSING = "report_missing"
    TIMEOUT = "timeout"
    KILLED = "killed"
    ABORTED = "aborted"
    SPEC_MISMATCH = "spec_mismatch"
    OUT_OF_SCOPE = "out_of_scope"


class Gate(StrEnum):
    """Verification gates recorded in gate results."""

    G1 = "G1"
    G2 = "G2"
    G3 = "G3"
    G2_PRE_PR = "G2_PRE_PR"


class Severity(StrEnum):
    """Review finding severities."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class Verdict(StrEnum):
    """Review verdicts."""

    PASS = "PASS"
    BLOCK = "BLOCK"


class Reviewer(StrEnum):
    """Cross-review providers."""

    CLAUDE_REVIEWER = "claude-reviewer"
    CODEX_REVIEW = "codex-review"


@dataclass(frozen=True, slots=True, kw_only=True)
class Task:
    """One row of the tasks table."""

    id: str
    target: str
    title: str
    grade: Grade
    lane: Lane
    model: str
    state: TaskState
    timeout_sec: int
    created_at: str
    updated_at: str
    effort: Effort | None = None
    attempt: int = 0
    base_sha: str | None = None
    test_sha: str | None = None
    depends_on: tuple[str, ...] = ()
    worktree: str | None = None
    pr_url: str | None = None
    pushed_sha: str | None = None
    slot_expires_at: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Run:
    """One row of the runs table."""

    task_id: str
    attempt: int
    lane: Lane
    model: str
    run_id: int | None = None
    effort: Effort | None = None
    pid: int | None = None
    pid_started_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    exit_code: int | None = None
    outcome: Outcome | None = None
    failure_reason: FailureReason | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Finding:
    """One review finding."""

    severity: Severity
    path: str
    summary: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Review:
    """One review report."""

    task_id: str
    reviewer: Reviewer
    verdict: Verdict
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Lock:
    """One row of the locks table."""

    target: str
    path_glob: str
    task_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class GateResult:
    """One row of the gate_results table."""

    task_id: str
    attempt: int
    gate: Gate
    passed: bool
    at: str
    detail: str | None = None


# The single definition of allowed state transitions.
TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = MappingProxyType(
    {
        TaskState.PREPARED: frozenset(
            {TaskState.RESERVED, TaskState.QUEUED, TaskState.DISCARDED}
        ),
        TaskState.QUEUED: frozenset(
            {TaskState.RESERVED, TaskState.DISCARDED, TaskState.BLOCKED}
        ),
        TaskState.RESERVED: frozenset(
            {TaskState.RUNNING, TaskState.QUEUED, TaskState.DISCARDED}
        ),
        TaskState.RUNNING: frozenset({TaskState.JUDGING, TaskState.ORPHANED}),
        TaskState.JUDGING: frozenset(
            {
                TaskState.GATING,
                TaskState.RESERVED,
                TaskState.QUEUED,
                TaskState.DISCARDED,
                TaskState.BLOCKED,
            }
        ),
        TaskState.GATING: frozenset(
            {TaskState.REVIEWING, TaskState.READY, TaskState.JUDGING}
        ),
        TaskState.REVIEWING: frozenset({TaskState.READY, TaskState.JUDGING}),
        TaskState.READY: frozenset(
            {TaskState.PR_OPEN, TaskState.CONFLICT, TaskState.JUDGING}
        ),
        TaskState.CONFLICT: frozenset(
            {TaskState.PR_OPEN, TaskState.DISCARDED, TaskState.JUDGING}
        ),
        TaskState.PR_OPEN: frozenset(
            {TaskState.CONFLICT, TaskState.CLOSED, TaskState.DISCARDED}
        ),
        TaskState.CLOSED: frozenset(),
        TaskState.BLOCKED: frozenset(
            {TaskState.RESERVED, TaskState.QUEUED, TaskState.DISCARDED}
        ),
        TaskState.DISCARDED: frozenset(),
        TaskState.ORPHANED: frozenset(
            {TaskState.RESERVED, TaskState.QUEUED, TaskState.DISCARDED}
        ),
    }
)

TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.CLOSED, TaskState.DISCARDED}
)


def can_transition(current: TaskState, new: TaskState) -> bool:
    """Return whether TRANSITIONS allows moving from current to new."""
    return new in TRANSITIONS.get(current, frozenset())
