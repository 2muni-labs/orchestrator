"""Lane adapters: how to start each sub-agent CLI and read its usage limit errors."""

from collections.abc import Mapping
from pathlib import Path

from orch.config import Config, Target
from orch.lanes.base import LaneCommand
from orch.models import Lane
from orch.taskfile import TaskSpec


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
    """Build the command of the spec's lane. Lanes other than B and C are refused."""
    raise NotImplementedError


def hit_usage_limit(
    lane: Lane,
    log_path: Path,
    *,
    spec_text: str,
    config: Config,
    environ: Mapping[str, str],
) -> bool:
    """Judge a run that left no valid result. Lanes other than B and C are refused."""
    raise NotImplementedError
