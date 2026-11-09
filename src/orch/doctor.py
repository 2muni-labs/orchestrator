"""Periodic health checks, dispatch pauses and stuck task kills."""

import json
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from orch import config, gc, lifecycle, notify, service, store
from orch.config import Config, Target
from orch.gc import GcReport
from orch.lifecycle import KillReport
from orch.models import CheckLevel, DoctorCheck, FailureReason, TaskState
from orch.terminal import MainPaneStatus, TmuxTerminal

DEFAULT_DISK_WARN_GB: Final = 10
DEFAULT_DISK_FAIL_GB: Final = 2
TAILSCALE: Final = "/usr/local/bin/tailscale"
STUCK_MARGIN_SEC: Final = 600
EXPIRY_WARN_DAYS: Final = 7
PAUSE_ITEMS: Final = frozenset({"claude_auth", "codex_auth", "disk"})


@dataclass(frozen=True, slots=True, kw_only=True)
class DoctorReport:
    """What one pass found and did."""

    checks: tuple[DoctorCheck, ...] = ()
    killed: tuple[str, ...] = ()
    paused: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    gc: GcReport | None = None


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def run_doctor(
    conn: sqlite3.Connection,
    *,
    settings: Config,
    targets: Mapping[str, Target],
    orch_home: Path,
    home: Path,
    now: datetime,
    environ: Mapping[str, str],
    disk_warn_gb: int = DEFAULT_DISK_WARN_GB,
    disk_fail_gb: int = DEFAULT_DISK_FAIL_GB,
    run: service.Runner = service.run_command,
    free_bytes: Callable[[Path], int] = _free_bytes,
    terminal: service.TerminalFactory = TmuxTerminal,
    send: Callable[..., notify.Outcome] = notify.send,
    collect: Callable[..., GcReport] = gc.collect,
    kill: Callable[..., KillReport] = lifecycle.kill_task,
) -> DoctorReport:
    """Check health independently, pause dispatch as needed and kill stuck runs."""
    checks: list[DoctorCheck] = []
    killed: list[str] = []
    errors: list[str] = []
    gc_report: GcReport | None = None
    at = now.isoformat()
    severity = {CheckLevel.OK: 0, CheckLevel.WARN: 1, CheckLevel.FAIL: 2}

    def inspect_item(item: str, target: str) -> tuple[CheckLevel, str | None] | None:
        nonlocal gc_report
        if item == "main_session":
            record = orch_home / target / "terminal.json"
            if not record.exists():
                return None
            if settings.claude_path is None:
                return CheckLevel.WARN, "not_configured"
            status = terminal(f"orch-{target}", record).main_pane_status(
                settings.claude_path
            )
            if status is MainPaneStatus.ALIVE:
                return CheckLevel.OK, None
            return CheckLevel.WARN, status.value

        if item == "disk":
            result = _disk_check(free_bytes(orch_home), disk_warn_gb, disk_fail_gb)
            if result[0] is not CheckLevel.OK:
                try:
                    gc_report = collect(
                        conn,
                        targets=targets,
                        orch_home=orch_home,
                        now=now,
                        retention_days=settings.retention_days,
                    )
                except Exception as exc:
                    errors.append(f"disk gc {type(exc).__name__}")
                else:
                    result = _disk_check(
                        free_bytes(orch_home), disk_warn_gb, disk_fail_gb
                    )
            return result

        return _command_check(
            item, settings=settings, orch_home=orch_home, now=now, run=run
        )

    def process_item(item: str, target: str = "") -> None:
        try:
            result = inspect_item(item, target)
            if result is None:
                return
            level, detail = result
            checks.append(
                DoctorCheck(target=target, item=item, level=level, detail=detail, at=at)
            )
            previous = next(
                (
                    row.level
                    for row in store.list_doctor_checks(conn)
                    if row.target == target and row.item == item
                ),
                CheckLevel.OK,
            )
            recorded_level = level
            if severity[level] > severity[previous]:
                # Network I/O must finish before taking the database write lock.
                outcome = send(
                    f"doctor_{item}",
                    settings=settings,
                    orch_home=orch_home,
                    home=home,
                    now=now,
                    target=target,
                )
                if outcome in (notify.Outcome.FAILED, notify.Outcome.SUPPRESSED):
                    recorded_level = previous

            with store.transaction(conn):
                store.record_doctor_check(
                    conn,
                    target=target,
                    item=item,
                    level=recorded_level,
                    detail=detail,
                    at=at,
                )
                if item in PAUSE_ITEMS:
                    if level is CheckLevel.FAIL:
                        store.add_pause(conn, item, since=at, note=detail)
                    else:
                        store.remove_pause(conn, item)
        except Exception as exc:
            label = f"{target}/{item}" if item == "main_session" else item
            errors.append(f"{label} {type(exc).__name__}")

    for name in sorted(targets):
        process_item("main_session", name)
    for item in ("claude_auth", "codex_auth", "github_app", "disk", "network"):
        process_item(item)

    for task in store.list_tasks(conn, states=[TaskState.RUNNING]):
        try:
            latest = store.latest_run(conn, task.id)
            if latest is None or latest.started_at is None:
                continue
            elapsed = now - datetime.fromisoformat(latest.started_at)
            if elapsed <= timedelta(seconds=task.timeout_sec + STUCK_MARGIN_SEC):
                continue
            report = kill(
                conn,
                task.id,
                paths=config.target_paths(orch_home, task.target),
                environ=environ,
            )
            if report.failure_reason is FailureReason.ABORTED:
                killed.append(task.id)
                send(
                    "doctor_stuck_task",
                    settings=settings,
                    orch_home=orch_home,
                    home=home,
                    now=now,
                    target=task.target,
                    task_id=task.id,
                    dedup=False,
                )
        except Exception as exc:
            errors.append(f"stuck_task/{task.id} {type(exc).__name__}")

    return DoctorReport(
        checks=tuple(checks),
        killed=tuple(killed),
        paused=tuple(pause.reason for pause in store.list_pauses(conn)),
        errors=tuple(errors),
        gc=gc_report,
    )


def _disk_check(free: int, warn_gb: int, fail_gb: int) -> tuple[CheckLevel, str]:
    if free < fail_gb * 10**9:
        level = CheckLevel.FAIL
    elif free < warn_gb * 10**9:
        level = CheckLevel.WARN
    else:
        level = CheckLevel.OK
    return level, f"free_gb={free // 10**9}"


def _json_object(output: str) -> dict[str, object] | None:
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _command_check(
    item: str,
    *,
    settings: Config,
    orch_home: Path,
    now: datetime,
    run: service.Runner,
) -> tuple[CheckLevel, str | None]:
    """Reduce command results to fixed details without retaining private output."""
    failure_level = CheckLevel.FAIL
    if item == "claude_auth":
        if settings.claude_path is None:
            return failure_level, "not_configured"
        argv = [str(settings.claude_path), "auth", "status", "--json"]
    elif item == "codex_auth":
        if settings.codex_path is None:
            return failure_level, "not_configured"
        argv = [str(settings.codex_path), "login", "status"]
    elif item == "github_app":
        argv = [str(orch_home / "bin" / "ghapp-token.sh")]
    else:
        failure_level = CheckLevel.WARN
        tailscale = (
            str(settings.tailscale_path)
            if settings.tailscale_path is not None
            else TAILSCALE
        )
        argv = [tailscale, "status", "--json"]

    try:
        result = run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return failure_level, type(exc).__name__
    if result.returncode != 0:
        return failure_level, f"exit={result.returncode}"

    if item == "codex_auth":
        return CheckLevel.OK, None
    if item == "github_app":
        if not result.stdout.strip():
            return failure_level, "empty"
        return CheckLevel.OK, None

    data = _json_object(result.stdout)
    if data is None:
        return failure_level, "not_json"
    if item == "claude_auth":
        if data.get("loggedIn") is not True:
            return failure_level, "not_logged_in"
        expiry = data.get("loginExpiresAt")
        if expiry is not None:
            if not isinstance(expiry, str):
                return CheckLevel.WARN, "expiry_unreadable"
            try:
                expires_at = datetime.fromisoformat(expiry)
            except ValueError:
                return CheckLevel.WARN, "expiry_unreadable"
            if expires_at.utcoffset() is None:
                return CheckLevel.WARN, "expiry_unreadable"
            if expires_at - now <= timedelta(days=EXPIRY_WARN_DAYS):
                return CheckLevel.WARN, "expires_soon"
    else:
        self_info = data.get("Self")
        if (
            data.get("BackendState") != "Running"
            or not isinstance(self_info, dict)
            or self_info.get("Online") is not True
        ):
            return failure_level, "not_running"
    return CheckLevel.OK, None
