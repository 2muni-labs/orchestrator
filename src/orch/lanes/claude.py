"""Lane C: claude -p."""

from collections.abc import Iterable, Mapping
from pathlib import Path

from orch.config import Config, Target
from orch.lanes.base import LaneCommand
from orch.taskfile import TaskSpec

PROVIDER = "claude"


def build_command(
    *,
    spec: TaskSpec,
    spec_text: str,
    worktree: Path,
    target: Target,
    config: Config,
    environ: Mapping[str, str],
    log_label: str,
) -> LaneCommand:
    """Build the claude -p command."""
    raise NotImplementedError


def hit_usage_limit(log_path: Path, *, spec_text: str, patterns: Iterable[str]) -> bool:
    """Search the end of the log for a limit message."""
    raise NotImplementedError
