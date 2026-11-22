"""orch show: one task's record as JSON (technical design 4.13)."""

import sqlite3

from orch.config import TargetPaths
from orch.models import Task


def show_task(
    conn: sqlite3.Connection, task: Task, paths: TargetPaths
) -> dict[str, object]:
    """Return the task's record in the order of schemas/cli/show.v1.json."""
    raise NotImplementedError
