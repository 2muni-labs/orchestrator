"""What the lanes share: the command, the environment, the prompt and the fallback."""

import re
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
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "LANG",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
    }
    fake = environ.get("ORCH_FAKE_AGENTS") == "1"
    return {
        name: value
        for name, value in environ.items()
        if name in allowed
        or name.startswith("LC_")
        or (fake and name.startswith("ORCH_FAKE_"))
    }


def build_prompt(spec_text: str, *, task_id: str, log_label: str) -> str:
    """Return the task spec followed by the common rules."""
    return (
        spec_text
        + "\n\nCommon rules:\n"
        + f"Write {RESULT_FILE} in the worktree root, conforming to "
        "schemas/result.json.\n"
        + f"Set task_id to {task_id!r} and log_path to {log_label!r}.\n"
        "Do not run git commit. Do not run commands in the background.\n"
        "Do not modify files under tests/.\n"
    )


def resolve_agent(
    name: str, configured: Path | None, environ: Mapping[str, str]
) -> Path:
    """Return the absolute path of an agent CLI. PATH is never searched."""
    if environ.get("ORCH_FAKE_AGENTS") == "1":
        hub = Path(environ.get("ORCH_HUB", ""))
        if not hub.is_absolute():
            raise LaneError("Fake agents require an absolute ORCH_HUB")
        return hub / "tests" / "fake_agents" / name
    if configured is None:
        raise LaneError(f"No configured path for {name}")
    return configured


def matches_limit(text: str, patterns: Iterable[str]) -> bool:
    """Return whether any usage limit pattern matches, ignoring case."""
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def tail_hits_limit(log_text: str, *, spec_text: str, patterns: Iterable[str]) -> bool:
    """Search the end of a log for a limit message, ignoring the echoed task spec."""
    spec_lines = {line.strip() for line in spec_text.splitlines()}
    tail = log_text.splitlines()[-LOG_TAIL_LINES:]
    return matches_limit(
        "\n".join(line for line in tail if line.strip() not in spec_lines), patterns
    )


def patterns_for(config: Config, provider: str) -> tuple[str, ...]:
    """Return a provider's usage limit patterns."""
    return tuple(config.usage_limit_patterns.get(provider, ()))
