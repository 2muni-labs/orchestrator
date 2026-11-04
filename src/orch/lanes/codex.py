"""Lane B: codex exec, and its structured usage limit errors."""

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from orch.config import Config
from orch.lanes.base import (
    LOG_TAIL_LINES,
    LaneCommand,
    LaneError,
    build_prompt,
    child_env,
    matches_limit,
    resolve_agent,
)
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
    if spec.effort is None:
        raise LaneError("Codex requires an effort")
    agent = resolve_agent(PROVIDER, config.codex_path, environ)
    return LaneCommand(
        provider=PROVIDER,
        argv=(
            str(agent),
            "exec",
            "--json",
            "-C",
            str(worktree),
            "-m",
            spec.model,
            "-c",
            f"model_reasoning_effort={spec.effort}",
            "--sandbox",
            "workspace-write",
            "-",
        ),
        cwd=worktree,
        env=child_env(environ),
        stdin_text=build_prompt(spec_text, task_id=spec.id, log_label=log_label),
    )


def _json_object(line: str) -> dict[str, object] | None:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def thread_id_from_log(log_text: str) -> str | None:
    """Return the thread id of the first valid thread.started event."""
    for line in log_text.splitlines():
        event = _json_object(line)
        if event is None or event.get("type") != "thread.started":
            continue
        thread = event.get("thread_id")
        if isinstance(thread, str) and re.fullmatch(r"[0-9A-Za-z-]+", thread):
            return thread
    return None


def structured_limit(log_text: str, codex_home: Path) -> bool | None:
    """Read the rollout's last turn error. None means the file cannot tell."""
    thread = thread_id_from_log(log_text)
    if thread is None:
        return None
    last_payload = None
    for path in sorted(codex_home.glob(f"sessions/*/*/*/rollout-*-{thread}.jsonl")):
        for line in _read_text(path).splitlines():
            event = _json_object(line)
            if event is None or event.get("type") != "event_msg":
                continue
            payload = event.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "task_complete":
                last_payload = payload
    if last_payload is None:
        return None
    error = last_payload.get("error")
    if not isinstance(error, dict) or "codex_error_info" not in error:
        return None
    info = error["codex_error_info"]
    if info == "usage_limit_exceeded":
        return True
    if info == "other":
        return None
    return False


def hit_usage_limit(
    log_path: Path, *, patterns: Iterable[str], environ: Mapping[str, str]
) -> bool:
    """Judge by the structured error, falling back to error messages in the log."""
    log_text = _read_text(log_path)
    home = environ.get("CODEX_HOME")
    if home is None and "HOME" in environ:
        home = str(Path(environ["HOME"]) / ".codex")
    if home is not None:
        verdict = structured_limit(log_text, Path(home))
        if verdict is not None:
            return verdict
    candidates = []
    lines = log_text.splitlines()
    tail_start = max(0, len(lines) - LOG_TAIL_LINES)
    for index, line in enumerate(lines):
        event = _json_object(line)
        if event is None:
            if index >= tail_start:
                candidates.append(line)
            continue
        message = None
        if event.get("type") == "error":
            message = event.get("message")
        elif event.get("type") == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict):
                message = error.get("message")
        if isinstance(message, str):
            candidates.append(message)
    return matches_limit("\n".join(candidates), patterns)
