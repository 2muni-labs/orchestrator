"""Per-group plan files built from state.db and the task bodies' priority lines."""

import sqlite3
from datetime import datetime
from pathlib import Path

from orch import config, render, store, tracking


def refresh_plans(
    conn: sqlite3.Connection, orch_home: Path, target: str, now: datetime
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Write open groups' plan files and remove the others.

    Return the written keys and the removed keys.
    """
    if tracking.repository(conn, target) is None:
        return (), ()
    paths = config.target_paths(orch_home, target)
    tasks_by_id = {task.id: task for task in store.list_tasks(conn, target=target)}
    groups: dict[str, list[render.PlanEntry]] = {}
    for record in tracking.list_records(conn, target):
        tasks = tuple(
            sorted(
                (
                    tasks_by_id[task_id]
                    for task_id in tracking.task_ids(conn, target, record.record_id)
                    if task_id in tasks_by_id
                ),
                key=lambda task: (task.updated_at, task.id),
            )
        )
        priority = None
        if tasks:
            try:
                body = (paths.tasks / f"{tasks[-1].id}.md").read_text(encoding="utf-8")
                priority = render.plan_priority(body)
            except OSError, UnicodeDecodeError:
                pass
        state = tracking.get_state(conn, target, record.record_id)
        groups.setdefault(record.project_key, []).append(
            render.PlanEntry(
                record=record,
                tasks=tasks,
                priority=priority,
                issue_closed=(
                    state is not None
                    and state.verified is not None
                    and state.verified.get("issue_state") == "closed"
                ),
            )
        )
    written: list[str] = []
    removed: list[str] = []
    for project in tracking.list_projects(conn, target):
        entries = groups.get(project.key, [])
        path = paths.plans / f"{project.key}.md"
        if any(render.entry_open(entry) for entry in entries):
            render.write_todo(
                path, render.plan_text(project, entries, tasks_by_id, now)
            )
            written.append(project.key)
        elif path.exists():
            path.unlink()
            removed.append(project.key)
    return tuple(written), tuple(removed)
