"""orch open: assemble the H2 launch plan. Nothing here launches anything."""

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from orch.config import Target, target_paths

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
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=10)


def parse_claude_version(output: str) -> str | None:
    words = output.strip().split()
    if words and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", words[0]):
        return words[0]
    return None


def probe_claude_version(claude: Path, *, runner: VersionRunner = _run) -> str | None:
    try:
        result = runner([str(claude), "--version"])
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return parse_claude_version(result.stdout)


def version_warning(version: str | None) -> str | None:
    if version == PINNED_CLAUDE_VERSION:
        return None
    if version is None:
        return (
            "Claude Code 버전을 확인하지 못했습니다. "
            f"고정 버전은 {PINNED_CLAUDE_VERSION}입니다."
        )
    return (
        f"Claude Code 실제 버전 {version}이 "
        f"고정 버전 {PINNED_CLAUDE_VERSION}과 다릅니다."
    )


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
    if not claude.is_absolute():
        raise LaunchError("Claude Code path must be absolute")
    settings = hub / "config" / "settings.json"
    if not settings.is_file():
        raise LaunchError("Hub hook settings file is missing")
    if not target.path.is_dir():
        raise LaunchError("Target path must be an existing directory")

    env = {"ORCH_TARGET": target.name, "ORCH_HUB": str(hub)}
    if orch_home != default_orch_home:
        env["ORCH_HOME"] = str(orch_home)

    warnings: list[str] = []
    warning = version_warning(claude_version)
    if warning is not None:
        warnings.append(warning)
    if dirty:
        warnings.append("대상 작업 트리에 미커밋 변경이 있습니다.")

    return LaunchPlan(
        argv=(
            str(claude),
            "--model",
            MAIN_MODEL,
            "--settings",
            str(settings),
            "--add-dir",
            str(target_paths(orch_home, target.name).root),
            "--remote-control",
        ),
        cwd=target.path,
        env=env,
        warnings=tuple(warnings),
    )
