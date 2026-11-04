"""orch run: start a sub-agent, stop it on time, and classify how it ended."""

import sqlite3
import subprocess
from collections.abc import Mapping
from pathlib import Path

from orch.config import Config, Target, TargetPaths
from orch.results import TaskResult

TERM_GRACE_SEC = 10


class RunError(Exception):
    """A task cannot start. Its state has not changed."""


def log_label(log_path: Path, environ: Mapping[str, str]) -> str:
    """Return the log path as a sub-agent reports it, with the home as a tilde."""
    raise NotImplementedError


def process_started_at(pid: int) -> str:
    """Return a process's start time from ps, or an empty string if ps cannot tell."""
    raise NotImplementedError


def descendants(root: int) -> set[int]:
    """Return a process's descendants from ps, or an empty set if ps cannot tell."""
    raise NotImplementedError


def stop_process(process: subprocess.Popen[bytes]) -> None:
    """Stop a process, its group and its descendants: SIGTERM, then SIGKILL."""
    raise NotImplementedError


def run_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    target: Target,
    config: Config,
    paths: TargetPaths,
    environ: Mapping[str, str],
) -> TaskResult:
    """Run a reserved task to its end, record the result, and move it to judging."""
    raise NotImplementedError
