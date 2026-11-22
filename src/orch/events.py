"""Read task state transitions from the events table as JSON Lines."""

import sqlite3
import time
from collections.abc import Callable


def read_events(
    conn: sqlite3.Connection, *, since: int, target: str | None
) -> list[dict[str, object]]:
    """Return the events after since in id order, for target's tasks or all."""
    raise NotImplementedError


def stream(
    conn: sqlite3.Connection,
    write: Callable[[str], None],
    *,
    since: int = 0,
    target: str | None = None,
    follow: bool = False,
    poll_sec: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    stopped: Callable[[], bool] = lambda: False,
) -> int:
    """Write each event as one JSON line and return the last id written."""
    raise NotImplementedError
