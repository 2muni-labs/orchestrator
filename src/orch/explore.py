"""orch explore: read-only Codex exploration with checked answers (design 4.16)."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from orch.config import Config, Target, TargetPaths

EXPLORE_TIMEOUT_SEC = 900
QUESTION_LIMIT_BYTES = 8192


class ExploreError(Exception):
    """A refused explore: nothing ran, no slot or log was made."""


class ExploreOutcome(StrEnum):
    """How an explore run ended."""

    SAVED = "saved"
    INVALID = "invalid"
    FAILED = "failed"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    BUSY = "busy"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExploreRun:
    """The result of one explore run. It never holds answer text."""

    explore_id: str
    outcome: ExploreOutcome
    exit_code: int | None
    answer_path: Path
    log_path: Path | None
    claims: int
    unresolved: int
    proxy_denials: int


def explore_root(paths: TargetPaths) -> Path:
    """Return the target's runtime explore/ directory."""
    raise NotImplementedError


def next_explore_log(logs: Path, explore_id: str) -> Path:
    """Return logs/explore/<n>.<k>.log for the next run, without creating it."""
    raise NotImplementedError


def read_question(question: Path, paths: TargetPaths) -> tuple[str, str]:
    """Check a question file and return its id and text."""
    raise NotImplementedError


def resolve_dir(dir_arg: Path | None, *, target: Target, paths: TargetPaths) -> Path:
    """Return the real directory to explore: the target or one of its worktrees."""
    raise NotImplementedError


def explore_prompt(explore_id: str, question: str) -> str:
    """Return the English prompt for an explore run."""
    raise NotImplementedError


def check_answer(text: str, *, explore_id: str, root: Path) -> dict[str, object]:
    """Return the parsed answer, or raise ValueError."""
    raise NotImplementedError


def acquire_slot(paths: TargetPaths, explore_id: str, limit: int) -> int | None:
    """Hold <n>.lock and return its descriptor, or None when busy."""
    raise NotImplementedError


def release_slot(paths: TargetPaths, explore_id: str, fd: int) -> None:
    """Remove <n>.lock, then close the descriptor that held it."""
    raise NotImplementedError


def run_explore(
    question: Path,
    *,
    dir_arg: Path | None,
    target: Target,
    config: Config,
    paths: TargetPaths,
    environ: Mapping[str, str],
    timeout_sec: int | None = None,
) -> ExploreRun:
    """Run one explore question under the Codex review sandbox."""
    raise NotImplementedError
