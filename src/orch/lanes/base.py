"""What the lanes share: the command, the environment, the prompt and the fallback."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from orch.config import Config

RESULT_FILE = ".orch-result.json"
LOG_TAIL_LINES = 20


class LaneError(Exception):
    """A lane cannot build a command for this task."""


@dataclass(frozen=True, slots=True, kw_only=True)
class LaneCommand:
    """How to start one sub-agent."""

    provider: str
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    stdin_text: str


def child_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Return only the environment variables that a sub-agent may see."""
    raise NotImplementedError


def build_prompt(spec_text: str, *, task_id: str, log_label: str) -> str:
    """Return the task spec followed by the common rules."""
    raise NotImplementedError


def resolve_agent(
    name: str, configured: Path | None, environ: Mapping[str, str]
) -> Path:
    """Return the absolute path of an agent CLI. PATH is never searched."""
    raise NotImplementedError


def matches_limit(text: str, patterns: Iterable[str]) -> bool:
    """Return whether any usage limit pattern matches, ignoring case."""
    raise NotImplementedError


def tail_hits_limit(log_text: str, *, spec_text: str, patterns: Iterable[str]) -> bool:
    """Search the end of a log for a limit message, ignoring the echoed task spec."""
    raise NotImplementedError


def patterns_for(config: Config, provider: str) -> tuple[str, ...]:
    """Return a provider's usage limit patterns."""
    return tuple(config.usage_limit_patterns.get(provider, ()))
