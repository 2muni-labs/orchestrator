"""Lane C: claude -p."""

from collections.abc import Iterable, Mapping
from pathlib import Path

from orch.config import Config, Target
from orch.lanes.base import (
    LaneCommand,
    LaneError,
    build_prompt,
    child_env,
    load_hub_rules,
    resolve_agent,
    tail_hits_limit,
)
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
    commands = (target.test_command, spec.verify)
    if any(char in command for command in commands for char in "),\n\r"):
        raise LaneError("Command cannot be represented in the allowed tools list")
    tools = "Read,Edit,Write,Grep,Glob," + ",".join(
        f"Bash({command}:*)" for command in commands
    )
    agent = resolve_agent(PROVIDER, config.claude_path, environ)
    argv = (
        str(agent),
        "-p",
        "--model",
        spec.model,
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        tools,
    )
    if spec.effort is not None:
        argv += ("--effort", str(spec.effort))
    return LaneCommand(
        provider=PROVIDER,
        argv=argv,
        cwd=worktree,
        env=child_env(environ),
        stdin_text=build_prompt(
            spec_text,
            task_id=spec.id,
            log_label=log_label,
            hub_rules=load_hub_rules(environ),
        ),
    )


def hit_usage_limit(log_path: Path, *, spec_text: str, patterns: Iterable[str]) -> bool:
    """Search the end of the log for a limit message."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        text = ""
    return tail_hits_limit(text, spec_text=spec_text, patterns=patterns)
