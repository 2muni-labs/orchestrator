"""orch open: assemble the H2 launch plan. Nothing here launches anything."""

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from orch.config import Target

PINNED_CLAUDE_VERSION: Final = "2.1.319"
MAIN_MODEL: Final = "claude-opus-5-5"

VersionRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class LaunchError(Exception):
    """The main session cannot be launched as asked."""


@dataclass(frozen=True)
class LaunchPlan:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    warnings: tuple[str, ...]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    raise NotImplementedError


def parse_claude_version(output: str) -> str | None:
    raise NotImplementedError


def probe_claude_version(claude: Path, *, runner: VersionRunner = _run) -> str | None:
    raise NotImplementedError


def version_warning(version: str | None) -> str | None:
    raise NotImplementedError


def build_plan(
    *,
    target: Target,
    hub: Path,
    orch_home: Path,
    default_orch_home: Path,
    claude: Path,
    claude_version: str | None,
    dirty: bool,
) -> LaunchPlan:
    raise NotImplementedError
