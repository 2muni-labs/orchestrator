"""Parse and validate task spec front matter."""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from orch.models import Effort, Grade, Lane


class TaskFileError(Exception):
    """A task spec is invalid."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskSpec:
    """A parsed task spec."""

    id: str
    target: str
    grade: Grade
    lane: Lane
    model: str
    timeout_min: int
    modify: tuple[str, ...]
    read: tuple[str, ...]
    verify: str
    depends_on: tuple[str, ...]
    body: str
    effort: Effort | None = None
    tests: str | None = None


def parse_task(text: str) -> TaskSpec:
    """Parse a task spec and validate everything that needs no target settings."""
    raise NotImplementedError


def load_task(path: Path) -> TaskSpec:
    """Read a task spec file whose stem must equal the spec's id."""
    raise NotImplementedError


def check_modify(
    modify: Iterable[str],
    shared_paths: Iterable[str],
    test_paths: Iterable[str] = ("tests/",),
) -> None:
    """Raise TaskFileError if a modify glob can reach a shared or test path."""
    raise NotImplementedError
