"""Read task state transitions from the events table as JSON Lines."""

import json
import sqlite3
import time
from collections.abc import Callable


def read_events(
    conn: sqlite3.Connection, *, since: int, target: str | None
) -> list[dict[str, object]]:
    """Return the events after since in id order, for target's tasks or all."""
    query = """SELECT e.id, e.task_id, t.target, e.from_state, e.to_state,
                      e.actor, e.note, e.at
               FROM events AS e JOIN tasks AS t ON t.id = e.task_id
               WHERE e.id > ?"""
    parameters: list[object] = [since]
    if target is not None:
        query += " AND t.target = ?"
        parameters.append(target)
    query += " ORDER BY e.id"
    cursor = conn.execute(query, parameters)
    try:
        rows = cursor.fetchall()
    finally:
        cursor.close()
    keys = ("id", "task_id", "target", "from_state", "to_state", "actor", "note", "at")
    return [{"schema_version": 1, **dict(zip(keys, row, strict=True))} for row in rows]


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
    last = since
    while True:
        for event in read_events(conn, since=last, target=target):
            write(json.dumps(event, ensure_ascii=False) + "\n")
            last = int(event["id"])
        if not follow or stopped():
            return last
        sleep(poll_sec)
        if stopped():
            return last
