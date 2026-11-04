"""Lane adapters: how to start each sub-agent CLI and read its usage limit errors."""

from collections.abc import Mapping
from pathlib import Path

from orch.config import Config, Target
from orch.lanes import base, claude, codex
from orch.lanes.base import LaneCommand, LaneError
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
    if spec.lane is Lane.B:
        return codex.build_command(
            spec=spec,
            spec_text=spec_text,
            worktree=worktree,
            config=config,
            environ=environ,
            log_label=log_label,
        )
    if spec.lane is Lane.C:
        return claude.build_command(
            spec=spec,
            spec_text=spec_text,
            worktree=worktree,
            target=target,
            config=config,
            environ=environ,
            log_label=log_label,
        )
    raise LaneError(f"Unsupported lane: {spec.lane}")


def hit_usage_limit(
    lane: Lane,
    log_path: Path,
    *,
    spec_text: str,
    config: Config,
    environ: Mapping[str, str],
) -> bool:
    """Judge a run that left no valid result. Lanes other than B and C are refused."""
    if lane is Lane.B:
        return codex.hit_usage_limit(
            log_path,
            patterns=base.patterns_for(config, codex.PROVIDER),
            environ=environ,
        )
    if lane is Lane.C:
        return claude.hit_usage_limit(
            log_path,
            spec_text=spec_text,
            patterns=base.patterns_for(config, claude.PROVIDER),
        )
    raise LaneError(f"Unsupported lane: {lane}")
