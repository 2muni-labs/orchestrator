"""Claude Code hook runner and fail-open handlers other than PreToolUse."""

import io
import json
import os
import re
import sqlite3
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orch import config, guard, notify, render, runner, scheduler, store
from orch.models import TaskState

Alive = Callable[[int, str], bool]
RecoverErrorHandler = Callable[[str, Exception], None]

SESSION_ID_PATTERN = r"[A-Za-z0-9_-]{1,128}"
WAIT_MARKER_MAX_AGE = timedelta(days=7)
END_HOOKS: Mapping[str, str] = {
    "post-tool-use": "post_tool_use",
    "post-tool-use-failure": "post_tool_use_failure",
    "user-prompt-submit": "user_prompt_submit",
    "stop": "stop",
    "session-end": "session_end",
}


def process_alive(pid: int, started_at: str) -> bool:
    return bool(started_at) and runner.process_started_at(pid) == started_at


def recover(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    alive: Alive = process_alive,
    on_error: RecoverErrorHandler | None = None,
) -> tuple[str, ...]:
    orphaned: list[str] = []
    for task in store.list_tasks(conn, states=[TaskState.RUNNING]):
        try:
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
        except Exception as exc:
            if on_error is not None:
                on_error(task.id, exc)
            continue
        orphaned.append(task.id)
    try:
        scheduler.expire_reservations(conn, now=now)
    except Exception as exc:
        if on_error is not None:
            on_error("reservations", exc)
    return tuple(orphaned)


def summary(
    conn: sqlite3.Connection,
    *,
    target: str,
    orphaned: Sequence[str],
    now: datetime,
) -> str:
    orphaned = [
        task_id
        for task_id in orphaned
        if (task := store.get_task(conn, task_id)) is not None and task.target == target
    ]
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
    doctor_line = render.doctor_stale_line(
        render.doctor_state(store.list_doctor_checks(conn), now)
    )
    if not tasks and not orphaned and doctor_line is None:
        return ""
    lines = [f"orch 복구 요약 (대상 {target})"]
    for task in tasks:
        line = f"- {task.id}: {task.state.value}"
        if task.pr_url:
            line += f" {task.pr_url}"
        lines.append(line)
    if orphaned:
        lines.append(f"이번에 orphaned로 바꾼 작업: {', '.join(orphaned)}")
    if doctor_line is not None:
        lines.append(doctor_line)
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
        target = environ.get("ORCH_TARGET")
        if target:
            try:
                clean_wait_markers(orch_home, target, now=now)
            except Exception as exc:
                _log(environ, now, "session-start", "waits " + type(exc).__name__)
        path = config.state_db_path(orch_home)
        if not path.exists():
            return ""
        with closing(store.connect(path)) as conn:
            orphaned = recover(
                conn,
                now=now,
                alive=alive,
                on_error=lambda key, exc: _log(
                    environ, now, "session-start", f"{key} {type(exc).__name__}"
                ),
            )
            context = (
                summary(conn, target=target, orphaned=orphaned, now=now)
                if target
                else ""
            )
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
        kind = payload.get("notification_type")
        if kind not in ("permission_prompt", "idle_prompt"):
            return ""
    except Exception as exc:
        _log(environ, now, "notification", type(exc).__name__)
        return ""
    if kind == "permission_prompt":
        try:
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
    try:
        start_wait(environ, payload, note=kind, now=now)
    except Exception as exc:
        _log(environ, now, "notification", "wait " + type(exc).__name__)
    return ""


def waits_dir(orch_home: Path, target: str) -> Path:
    """Return the directory of the target's open wait markers."""
    return config.target_paths(orch_home, target).root / "waits"


def start_wait(
    environ: Mapping[str, str],
    payload: Mapping[str, object],
    *,
    note: str,
    now: datetime,
) -> None:
    """Record a wait start unless the session already has an open wait."""
    target = environ.get("ORCH_TARGET")
    session_id = payload.get("session_id")
    if (
        not target
        or not isinstance(session_id, str)
        or not re.fullmatch(SESSION_ID_PATTERN, session_id)
    ):
        return
    orch_home = config.orch_home(environ)
    path = config.state_db_path(orch_home)
    if not path.exists():
        return
    marker = waits_dir(orch_home, target) / session_id
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    try:
        os.close(descriptor)
        with closing(store.connect(path)) as conn:
            store.record_wait(
                conn,
                kind="wait_start",
                target=target,
                session_id=session_id,
                note=note,
                at=now.isoformat(),
            )
    except Exception:
        marker.unlink(missing_ok=True)
        raise


def end_wait(
    environ: Mapping[str, str],
    payload: Mapping[str, object],
    *,
    note: str,
    now: datetime,
) -> None:
    """Record a wait end when the session has an open wait."""
    target = environ.get("ORCH_TARGET")
    session_id = payload.get("session_id")
    if (
        not target
        or not isinstance(session_id, str)
        or not re.fullmatch(SESSION_ID_PATTERN, session_id)
    ):
        return
    orch_home = config.orch_home(environ)
    marker = waits_dir(orch_home, target) / session_id
    try:
        marker.unlink()
    except FileNotFoundError:
        return
    path = config.state_db_path(orch_home)
    if path.exists():
        with closing(store.connect(path)) as conn:
            store.record_wait(
                conn,
                kind="wait_end",
                target=target,
                session_id=session_id,
                note=note,
                at=now.isoformat(),
            )


def clean_wait_markers(orch_home: Path, target: str, *, now: datetime) -> int:
    """Remove wait markers older than WAIT_MARKER_MAX_AGE and count them."""
    try:
        markers = list(waits_dir(orch_home, target).iterdir())
    except FileNotFoundError:
        return 0
    cutoff = (now - WAIT_MARKER_MAX_AGE).timestamp()
    removed = 0
    for marker in markers:
        try:
            info = marker.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                marker.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def handle_permission_request(
    stdin_text: str, environ: Mapping[str, str], *, now: datetime | None = None
) -> str:
    try:
        if now is None:
            now = datetime.now(UTC)
        start_wait(environ, _payload(stdin_text), note="permission_request", now=now)
    except Exception as exc:
        _log(environ, now, "permission-request", type(exc).__name__)
    return ""


def handle_wait_end(
    stdin_text: str,
    environ: Mapping[str, str],
    *,
    hook: str,
    now: datetime | None = None,
) -> str:
    try:
        if now is None:
            now = datetime.now(UTC)
        end_wait(environ, _payload(stdin_text), note=END_HOOKS[hook], now=now)
    except Exception as exc:
        _log(environ, now, hook, type(exc).__name__)
    return ""


def run_hook(name: str) -> int:
    """Run one Claude Code hook in its own output format (technical design 5.3)."""

    def log_failure(exc: BaseException) -> None:
        if name == "pre-bash":
            return
        try:
            _log(os.environ, None, name, type(exc).__name__)
        except BaseException:
            pass

    try:
        stdin_text = sys.stdin.read()
    except BaseException as exc:
        log_failure(exc)
        stdin_text = ""

    try:
        if name == "pre-bash":
            output = guard.handle(stdin_text, os.environ)
        elif name in END_HOOKS:
            output = handle_wait_end(stdin_text, os.environ, hook=name)
        else:
            handler = {
                "session-start": handle_session_start,
                "subagent-stop": handle_subagent_stop,
                "notification": handle_notification,
                "permission-request": handle_permission_request,
            }[name]
            output = handler(stdin_text, os.environ)
    except BaseException as exc:
        log_failure(exc)
        output = guard._FAILURE_OUTPUT if name == "pre-bash" else ""

    try:
        if output:
            sys.stdout.write(output + "\n")
            sys.stdout.flush()
    except BaseException as exc:
        # Do not let Python retry a failed flush at shutdown and exit with 120.
        sys.stdout = None
        if name == "pre-bash":
            try:
                sys.stderr.write("Bash 훅 출력을 쓸 수 없어 차단합니다.\n")
                sys.stderr.flush()
            except BaseException:
                sys.stderr = None
            return 2
        log_failure(exc)
    return 0
