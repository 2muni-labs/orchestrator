"""Data classes, enumerations and the task state machine."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class TaskState(StrEnum):
    """Task lifecycle states."""


class Grade(StrEnum):
    """Task difficulty grades."""


class Lane(StrEnum):
    """Execution lanes that can modify code."""


class Effort(StrEnum):
    """Reasoning effort levels."""


class Outcome(StrEnum):
    """Result of one sub-agent run."""


class FailureReason(StrEnum):
    """Why a run failed or was blocked."""


class Gate(StrEnum):
    """Verification gates recorded in gate results."""


class Severity(StrEnum):
    """Review finding severities."""


class Verdict(StrEnum):
    """Review verdicts."""


class Reviewer(StrEnum):
    """Cross-review providers."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Task:
    """One row of the tasks table."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Run:
    """One row of the runs table."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Finding:
    """One review finding."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Review:
    """One review report."""


# The single definition of allowed state transitions.
TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {}

TERMINAL_STATES: frozenset[TaskState] = frozenset()


def can_transition(current: TaskState, new: TaskState) -> bool:
    """Return whether TRANSITIONS allows moving from current to new."""
    raise NotImplementedError
