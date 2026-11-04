"""Lane B: codex exec, and its structured usage limit errors."""

from collections.abc import Iterable, Mapping
from pathlib import Path

from orch.config import Config
from orch.lanes.base import LaneCommand
from orch.taskfile import TaskSpec

PROVIDER = "codex"


def build_command(
    *,
    spec: TaskSpec,
    spec_text: str,
    worktree: Path,
    config: Config,
    environ: Mapping[str, str],
    log_label: str,
) -> LaneCommand:
    """Build the codex exec command."""
    raise NotImplementedError


def thread_id_from_log(log_text: str) -> str | None:
    """Return the thread id of the first valid thread.started event."""
    raise NotImplementedError


def structured_limit(log_text: str, codex_home: Path) -> bool | None:
    """Read the rollout's last turn error. None means the file cannot tell."""
    raise NotImplementedError


def hit_usage_limit(
    log_path: Path, *, patterns: Iterable[str], environ: Mapping[str, str]
) -> bool:
    """Judge by the structured error, falling back to error messages in the log."""
    raise NotImplementedError
