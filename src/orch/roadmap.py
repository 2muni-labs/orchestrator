"""orch roadmap: validate docs/roadmap.toml and report progress (4.13)."""

import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from orch.models import Task

ROADMAP_PATH: Final = "docs/roadmap.toml"
ROADMAP_FORMAT: Final = 1
ID_PATTERN: Final = r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}"
STATUSES: Final = ("done", "current", "planned")
GIT: Final = "/usr/bin/git"

Run = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True, slots=True)
class Problem:
    """One validation problem: where it is and a fixed reason word."""

    where: str
    reason: str


@dataclass(frozen=True, slots=True)
class Milestone:
    id: str
    title: str


@dataclass(frozen=True, slots=True)
class Phase:
    id: str
    title: str
    status: str
    exit: tuple[str, ...]
    milestones: tuple[Milestone, ...]


@dataclass(frozen=True, slots=True)
class Roadmap:
    phases: tuple[Phase, ...]


class RoadmapError(Exception):
    """The roadmap cannot be read or is invalid."""

    def __init__(
        self, error: str, message: str, problems: tuple[Problem, ...] = ()
    ) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.problems = problems


def parse_roadmap(text: str) -> Roadmap:
    """Parse and validate the roadmap, reporting every problem at once."""
    raise NotImplementedError


def read_roadmap(
    repo: Path, default_branch: str, *, run: Run = subprocess.run
) -> tuple[str, str, str]:
    """Return (ref, commit, text) of the roadmap on origin/<default branch>."""
    raise NotImplementedError


def chains(tasks: Sequence[Task]) -> list[tuple[Task, ...]]:
    """Group tasks linked by replaces, oldest first."""
    raise NotImplementedError


def progress_report(roadmap: Roadmap, tasks: Sequence[Task]) -> dict[str, object]:
    """Return phases with per-milestone progress, unknown milestones and the rest."""
    raise NotImplementedError


def build_report(
    conn: sqlite3.Connection,
    *,
    target: str,
    repo: Path,
    default_branch: str,
    run: Run = subprocess.run,
) -> dict[str, object]:
    """Return the orch roadmap report of one target."""
    raise NotImplementedError
