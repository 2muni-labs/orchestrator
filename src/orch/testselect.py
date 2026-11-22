"""Choose the test groups a change needs from its paths and Python imports."""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from orch.config import SelectConfig


@dataclass(frozen=True, slots=True, kw_only=True)
class Selection:
    """The groups to run, by name, or a full run, with the reasons."""

    full: bool
    groups: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def select(
    worktree: Path, changed: Iterable[str], test_select: SelectConfig
) -> Selection:
    """Choose the test groups a change needs, or a full run."""
    raise NotImplementedError
