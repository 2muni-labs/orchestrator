"""Per-group plan files built from state.db and the task bodies' priority lines."""

import sqlite3
from datetime import datetime
from pathlib import Path


def refresh_plans(
    conn: sqlite3.Connection, orch_home: Path, target: str, now: datetime
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Write open groups' plan files and remove the others.

    Return the written keys and the removed keys.
    """
    raise NotImplementedError
