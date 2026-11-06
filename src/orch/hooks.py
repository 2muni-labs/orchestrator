"""Claude Code hooks other than PreToolUse. Every handler fails open."""

import io
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orch import config, notify, runner, scheduler, store
from orch.models import TaskState

Alive = Callable[[int, str], bool]


def process_alive(pid: int, started_at: str) -> bool:
    return bool(started_at) and runner.process_started_at(pid) == started_at


def recover(
    conn: sqlite3.Connection, *, now: datetime, alive: Alive = process_alive
) -> tuple[str, ...]:
    orphaned: list[str] = []
    for task in store.list_tasks(conn, states=[TaskState.RUNNING]):
        run = store.latest_run(conn, task.id)
        if (
            run is not None
            and run.pid is None
            and run.started_at is not None
            and timedelta(0)
            <= now - datetime.fromisoformat(run.started_at)
            <= timedelta(seconds=60)
        ):
            # orch run records the run before it can record its process.
            continue
        if (
            run is not None
            and run.pid
            and run.pid_started_at
            and alive(run.pid, run.pid_started_at)
        ):
            continue
        try:
            store.transition(
                conn,
                task.id,
                TaskState.RUNNING,
                TaskState.ORPHANED,
                actor="orch",
                note="session-start",
                at=now.isoformat(),
            )
        except store.StateMismatchError:
            continue
        orphaned.append(task.id)
    scheduler.expire_reservations(conn, now=now)
    return tuple(orphaned)


def summary(conn: sqlite3.Connection, *, target: str, orphaned: Sequence[str]) -> str:
    tasks = store.list_tasks(
        conn,
        target=target,
        states=[
            TaskState.JUDGING,
            TaskState.BLOCKED,
            TaskState.ORPHANED,
            TaskState.CONFLICT,
            TaskState.PR_OPEN,
        ],
    )
    if not tasks and not orphaned:
        return ""
    lines = [f"orch 복구 요약 (대상 {target})"]
    for task in tasks:
        line = f"- {task.id}: {task.state.value}"
        if task.pr_url:
            line += f" {task.pr_url}"
        lines.append(line)
    if orphaned:
        lines.append(f"이번에 orphaned로 바꾼 작업: {', '.join(orphaned)}")
    return "\n".join(lines)


def _log(
    environ: Mapping[str, str], now: datetime | None, hook: str, detail: str
) -> None:
    """Best-effort logging, including when hook setup itself failed."""
    try:
        if now is None:
            now = datetime.now(UTC)
        path = config.orch_home(environ) / "hooks.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{now.isoformat()} {hook} {detail}\n")
    except Exception:
        pass


def _payload(stdin_text: str) -> dict[str, object]:
    value = json.loads(stdin_text)
    if not isinstance(value, dict):
        raise ValueError("Hook input must be an object")
    return value


def handle_session_start(
    stdin_text: str,
    environ: Mapping[str, str],
    *,
    now: datetime | None = None,
    alive: Alive = process_alive,
) -> str:
    try:
        if now is None:
            now = datetime.now(UTC)
        orch_home = config.orch_home(environ)
        path = config.state_db_path(orch_home)
        if not path.exists():
            return ""
        with closing(store.connect(path)) as conn:
            orphaned = recover(conn, now=now, alive=alive)
            target = environ.get("ORCH_TARGET")
            context = summary(conn, target=target, orphaned=orphaned) if target else ""
        if not context:
            return ""
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        _log(environ, now, "session-start", type(exc).__name__)
        return ""


def handle_subagent_stop(
    stdin_text: str, environ: Mapping[str, str], *, now: datetime | None = None
) -> str:
    try:
        target = environ.get("ORCH_TARGET")
        if not target:
            return ""
        if now is None:
            now = datetime.now(UTC)
        orch_home = config.orch_home(environ)
        payload = _payload(stdin_text)
        agent_type = payload.get("agent_type")
        if not isinstance(agent_type, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]+", agent_type
        ):
            agent_type = "unknown"
        message = payload.get("last_assistant_message")
        if not isinstance(message, str):
            message = ""
        message = re.sub(r"[\x00-\x1f\x7f]", " ", message)[:500]
        path = config.target_paths(orch_home, target).logs / "subagents.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{now.isoformat()} {agent_type} {message}\n")
    except Exception as exc:
        _log(environ, now, "subagent-stop", type(exc).__name__)
    return ""


def handle_notification(
    stdin_text: str,
    environ: Mapping[str, str],
    *,
    now: datetime | None = None,
    transport: notify.Transport = notify.urllib_transport,
) -> str:
    try:
        target = environ.get("ORCH_TARGET")
        if not target:
            return ""
        if now is None:
            now = datetime.now(UTC)
        payload = _payload(stdin_text)
        if payload.get("notification_type") != "permission_prompt":
            return ""
        orch_home = config.orch_home(environ)
        outcome = notify.send(
            "input_wait",
            settings=config.load_config(orch_home),
            orch_home=orch_home,
            home=Path(environ["HOME"]),
            now=now,
            target=target,
            task_id="main",
            transport=transport,
            stderr=io.StringIO(),
        )
        if outcome in (notify.Outcome.FAILED, notify.Outcome.REFUSED_PERMISSIONS):
            _log(environ, now, "notification", outcome.value)
    except Exception as exc:
        _log(environ, now, "notification", type(exc).__name__)
    return ""
