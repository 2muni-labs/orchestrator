"""Lane B: codex exec, and its structured usage limit errors."""

import json
import re
import socket
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from orch import proxy, sandbox
from orch.config import Config
from orch.lanes.base import (
    LOG_TAIL_LINES,
    LaneCommand,
    LaneError,
    build_prompt,
    child_env,
    load_hub_rules,
    matches_limit,
    resolve_agent,
)
from orch.models import Effort  # noqa: F401 (used by the implementation)
from orch.taskfile import TaskSpec

PROVIDER = "codex"
# Turn off the user's hooks and plugin MCP servers (measured on codex-cli 0.160.0).
ISOLATION_ARGS: tuple[str, ...] = ("--disable", "hooks", "--disable", "plugins")
FAKE_PROXY_ENV = "ORCH_FAKE_PROXY"
FAKE_PROXY_PORT = 9
FAKE_DENIED_REQUEST = b"CONNECT denied.invalid:443 HTTP/1.1\r\n\r\n"


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexSandbox:
    """The per-run proxy port and TMPDIR of a sandboxed Codex run."""

    port: int
    tmpdir: Path


def codex_home(environ: Mapping[str, str]) -> Path:
    """Return HOME/.codex; CODEX_HOME may only name the same directory."""
    home = environ.get("HOME")
    if not home or not Path(home).is_absolute():
        raise LaneError("Codex requires an absolute HOME")
    expected = Path(home) / ".codex"
    if "CODEX_HOME" in environ:
        if Path(environ["CODEX_HOME"]).resolve() != expected.resolve():
            raise LaneError("CODEX_HOME must name HOME/.codex")
    return expected


def build_command(
    *,
    spec: TaskSpec,
    spec_text: str,
    worktree: Path,
    config: Config,
    environ: Mapping[str, str],
    log_label: str,
    run_sandbox: CodexSandbox,
) -> LaneCommand:
    """Build codex exec under the orch Codex profile, behind the proxy."""
    if spec.effort is None:
        raise LaneError("Codex requires an effort")
    agent = resolve_agent(PROVIDER, config.codex_path, environ)
    home = codex_home(environ)
    argv = (
        str(agent),
        "exec",
        "--json",
        *ISOLATION_ARGS,
        "-C",
        str(worktree),
        "-m",
        spec.model,
        "-c",
        f"model_reasoning_effort={spec.effort}",
        "--sandbox",
        "danger-full-access",
        "-",
    )
    try:
        wrapped = sandbox.wrap_codex(
            argv,
            executable=sandbox.executable(environ),
            worktree=worktree,
            tmpdir=run_sandbox.tmpdir,
            home=Path(environ["HOME"]),
            codex_home=home,
            port=run_sandbox.port,
        )
    except ValueError as exc:
        raise LaneError("Cannot build the Codex sandbox") from exc
    env = child_env(environ)
    env.update(TMPDIR=str(run_sandbox.tmpdir))
    env.update(proxy.proxy_env(run_sandbox.port))
    return LaneCommand(
        provider=PROVIDER,
        argv=tuple(wrapped),
        cwd=worktree,
        env=env,
        stdin_text=build_prompt(
            spec_text,
            task_id=spec.id,
            log_label=log_label,
            hub_rules=load_hub_rules(environ),
        ),
    )


def command_events(log_text: str) -> int:
    """Count item.completed events of command_execution items in a log."""
    count = 0
    for line in log_text.splitlines():
        event = _json_object(line)
        if event is None or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "command_execution":
            count += 1
    return count


class _FakeProxyListener:
    """The listener operations used by AllowlistProxy, without binding."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.timeout: float = 0.05
        self.closed = False
        self.accepted = False
        self.peer: socket.socket | None = None

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", FAKE_PROXY_PORT)

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def close(self) -> None:
        self.closed = True
        if self.peer is not None:
            self.peer.close()

    def accept(self) -> tuple[socket.socket, tuple[str, int]]:
        if self.closed:
            raise OSError("Fake listener is closed")
        if not self.accepted:
            self.accepted = True
            if self.mode == "die":
                time.sleep(0.3)
                raise OSError("Fake listener stopped")
            if self.mode == "deny":
                client, self.peer = socket.socketpair()
                self.peer.sendall(FAKE_DENIED_REQUEST)
                return client, ("127.0.0.1", FAKE_PROXY_PORT)
        time.sleep(self.timeout)
        raise TimeoutError


def fake_proxy_listener(
    environ: Mapping[str, str],
) -> Callable[[], socket.socket] | None:
    """In fake agent mode, a proxy listener factory that never binds."""
    if environ.get("ORCH_FAKE_AGENTS") != "1":
        return None
    mode = environ.get(FAKE_PROXY_ENV, "")

    def factory() -> socket.socket:
        if mode not in ("", "die", "deny"):
            raise proxy.ProxyError("Fake listener cannot start")
        return cast(socket.socket, _FakeProxyListener(mode))

    return factory


def build_review_command(
    *,
    worktree: Path,
    prompt: str,
    output_path: Path,
    model: str,
    effort: str,
    config: Config,
    environ: Mapping[str, str],
    run_sandbox: CodexSandbox,
) -> LaneCommand:
    """codex exec under the review-mode orch profile, behind the proxy."""
    raise NotImplementedError


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
