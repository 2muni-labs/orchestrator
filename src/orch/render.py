"""Derived views of the state store: TODO.md, status rows and the PR body."""

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from orch.models import GateResult, Review, Task
from orch.review import Triage
from orch.scheduler import BlockedChain


def elapsed_sec(task: Task, now: datetime) -> int:
    """Return the whole seconds a task has taken."""
    raise NotImplementedError


def format_elapsed(seconds: int) -> str:
    """Return a short human form of a duration."""
    raise NotImplementedError


def status_row(task: Task, now: datetime) -> dict[str, object]:
    """Return one task of the orch status JSON output."""
    raise NotImplementedError


def status_lines(
    tasks: Iterable[Task], chains: Iterable[BlockedChain], now: datetime
) -> list[str]:
    """Return the lines of orch status --human."""
    raise NotImplementedError


def todo_text(
    tasks: Iterable[Task], chains: Iterable[BlockedChain], now: datetime
) -> str:
    """Return the contents of TODO.md."""
    raise NotImplementedError


def write_todo(path: Path, text: str) -> None:
    """Replace TODO.md atomically."""
    raise NotImplementedError


def build_body(
    task: Task,
    *,
    changed: Iterable[str],
    gates: Iterable[GateResult],
    review: Review | None,
    triage: Triage | None = None,
) -> str:
    """Return the minimal English pull request body."""
    raise NotImplementedError
