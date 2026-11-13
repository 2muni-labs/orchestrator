"""orch open: assemble the H2 launch plan. Nothing here launches anything."""

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from orch.config import Target, target_paths

PINNED_CLAUDE_VERSION: Final = "2.1.319"
MAIN_MODEL: Final = "claude-opus-5-5"
AGENT_KEYS: Final = frozenset({"name", "description", "model", "tools"})

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


def parse_agent(text: str, *, stem: str) -> dict[str, object]:
    """Convert one agent definition file into its --agents entry."""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise LaunchError(f"Agent {stem}: missing opening metadata delimiter")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise LaunchError(f"Agent {stem}: missing closing metadata delimiter") from exc

    metadata: dict[str, str] = {}
    for line in lines[1:closing]:
        key, separator, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not separator or key not in AGENT_KEYS or key in metadata or not value:
            raise LaunchError(f"Agent {stem}: invalid metadata line")
        metadata[key] = value
    if "name" not in metadata or "description" not in metadata:
        raise LaunchError(f"Agent {stem}: name and description are required")
    if metadata["name"] != stem:
        raise LaunchError(f"Agent {stem}: name must match the file stem")

    prompt = "\n".join(lines[closing + 1 :]).strip()
    if not prompt:
        raise LaunchError(f"Agent {stem}: prompt must not be empty")
    definition: dict[str, object] = {
        "description": metadata["description"],
        "prompt": prompt,
    }
    if "tools" in metadata:
        tools = [tool.strip() for tool in metadata["tools"].split(",")]
        if any(not tool for tool in tools):
            raise LaunchError(f"Agent {stem}: tools must not contain empty items")
        definition["tools"] = tools
    if "model" in metadata:
        definition["model"] = metadata["model"]
    return definition


def load_agents(directory: Path) -> dict[str, dict[str, object]]:
    """Read every *.md agent definition in the directory, by file name."""
    agents: dict[str, dict[str, object]] = {}
    try:
        if not directory.is_dir():
            raise LaunchError(f"Agent definitions directory is missing: {directory}")
        for path in sorted(directory.glob("*.md"), key=lambda path: path.name):
            if not path.is_file():
                raise LaunchError(f"Agent definition must be a regular file: {path}")
            agents[path.stem] = parse_agent(
                path.read_text(encoding="utf-8"), stem=path.stem
            )
    except (OSError, UnicodeError) as exc:
        raise LaunchError(f"Cannot read agent definitions in {directory}") from exc
    if not agents:
        raise LaunchError(f"No agent definitions found in {directory}")
    return agents


def agents_json(agents: Mapping[str, Mapping[str, object]]) -> str:
    """Return the --agents JSON string."""
    return json.dumps(agents, ensure_ascii=False, sort_keys=True)


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

    main = hub / "config" / "main.md"
    try:
        if not main.is_file():
            raise LaunchError("Hub main rules must be a regular file")
        if not main.read_text(encoding="utf-8").strip():
            raise LaunchError("Hub main rules must not be empty")
    except (OSError, UnicodeError) as exc:
        raise LaunchError("Cannot read hub main rules") from exc
    agents = load_agents(hub / "config" / "agents")

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
            "--append-system-prompt-file",
            str(main),
            "--agents",
            agents_json(agents),
            "--add-dir",
            str(target_paths(orch_home, target.name).root),
            "--remote-control",
        ),
        cwd=target.path,
        env=env,
        warnings=tuple(warnings),
    )
