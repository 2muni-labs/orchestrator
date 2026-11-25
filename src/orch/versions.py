"""orch versions: installed, verified and latest versions (technical design 4.13)."""

import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

from orch import selfcheck
from orch.latest import LatestRecord

VERSION_TIMEOUT_SEC: Final = 10
SW_VERS: Final = "/usr/bin/sw_vers"
GIT: Final = "/usr/bin/git"
AGENTS: Final = ("claude", "codex")
TOOLS: Final = ("tmux", "macos")
TAG_PATTERN: Final = r"v([0-9]+\.[0-9]+\.[0-9]+)"

Run = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


class VersionsError(Exception):
    """config/pins.toml cannot be read or is invalid."""


def load_tool_baselines(hub: Path) -> dict[str, str]:
    """Return the [tmux] and [macos] baseline versions of config/pins.toml."""
    raise NotImplementedError


def installed_version(
    name: str, binary: Path | None, *, run: Run = selfcheck.run_command
) -> str | None:
    """Return the version a binary reports, or None."""
    raise NotImplementedError


def orch_latest(hub: Path, *, run: Run = selfcheck.run_command) -> str | None:
    """Return the highest v<x.y.z> tag of the hub clone without the v, or None."""
    raise NotImplementedError


def agent_report(
    agent: str,
    *,
    installed: str | None,
    baseline: str,
    blocking: Sequence[str],
    warn: Sequence[str],
    state: Mapping[str, object] | None,
    record: LatestRecord | None,
) -> dict[str, object]:
    """Return one agent's object of schemas/cli/versions.v1.json."""
    raise NotImplementedError


def build_report(
    *,
    orch_home: Path,
    hub: Path,
    claude_path: Path | None,
    codex_path: Path | None,
    tmux_path: Path | None,
    orch_version: str,
    run: Run = selfcheck.run_command,
) -> dict[str, object]:
    """Return the agents, orch and tools objects of orch versions."""
    raise NotImplementedError
