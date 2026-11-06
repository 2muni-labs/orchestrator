"""Claude Code hooks other than PreToolUse. Every handler fails open."""

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from orch import notify, runner  # noqa: F401  (runner is used by process_alive)

Alive = Callable[[int, str], bool]


def process_alive(pid: int, started_at: str) -> bool:
    raise NotImplementedError


def recover(
    conn: sqlite3.Connection, *, now: datetime, alive: Alive = process_alive
) -> tuple[str, ...]:
    raise NotImplementedError


def summary(conn: sqlite3.Connection, *, target: str, orphaned: Sequence[str]) -> str:
    raise NotImplementedError


def handle_session_start(
    stdin_text: str,
    environ: Mapping[str, str],
    *,
    now: datetime | None = None,
    alive: Alive = process_alive,
) -> str:
    raise NotImplementedError


def handle_subagent_stop(
    stdin_text: str, environ: Mapping[str, str], *, now: datetime | None = None
) -> str:
    raise NotImplementedError


def handle_notification(
    stdin_text: str,
    environ: Mapping[str, str],
    *,
    now: datetime | None = None,
    transport: notify.Transport = notify.urllib_transport,
) -> str:
    raise NotImplementedError
