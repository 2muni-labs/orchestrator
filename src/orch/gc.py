"""Remove expired files of finished tasks and the worktrees they left behind."""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from orch.config import Target

DEFAULT_RETENTION_DAYS = 30


@dataclass(frozen=True, slots=True, kw_only=True)
class GcReport:
    """What one pass removed or left. With dry_run, what it would have removed."""

    dry_run: bool
    removed_files: tuple[str, ...] = ()
    removed_worktrees: tuple[str, ...] = ()
    skipped_worktrees: tuple[str, ...] = ()
    unknown_worktrees: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def collect(
    conn: sqlite3.Connection,
    *,
    targets: Mapping[str, Target],
    orch_home: Path,
    now: datetime,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    dry_run: bool = False,
) -> GcReport:
    raise NotImplementedError
