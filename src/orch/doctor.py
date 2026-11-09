"""Periodic health checks, dispatch pauses and stuck task kills."""

import shutil
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from orch import gc, lifecycle, notify, service
from orch.config import Config, Target
from orch.gc import GcReport
from orch.lifecycle import KillReport
from orch.models import DoctorCheck
from orch.terminal import TmuxTerminal

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
    raise NotImplementedError
