"""orch versions: installed, verified and latest versions (technical design 4.13)."""

import re
import subprocess
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

from orch import latest, selfcheck, selfcheck_codex
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
    try:
        data = tomllib.loads((hub / "config" / "pins.toml").read_text("utf-8"))
        baselines = {}
        for name in TOOLS:
            version = data[name]["version"]
            if not isinstance(version, str) or not re.fullmatch(
                latest.VERSION_PATTERN, version
            ):
                raise ValueError(f"Invalid {name} version")
            baselines[name] = version
        return baselines
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise VersionsError("Cannot load tool pins") from exc


def installed_version(
    name: str, binary: Path | None, *, run: Run = selfcheck.run_command
) -> str | None:
    """Return the version a binary reports, or None."""
    if name == "macos":
        argv = [SW_VERS, "-productVersion"]
    elif binary is None:
        return None
    else:
        argv = [str(binary), "-V" if name == "tmux" else "--version"]
    try:
        result = run(argv, VERSION_TIMEOUT_SEC)
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    words = result.stdout.split()
    if name == "claude":
        version = words[0] if words else ""
    elif name in {"codex", "tmux"}:
        prefix = "codex-cli" if name == "codex" else "tmux"
        version = words[1] if len(words) == 2 and words[0] == prefix else ""
    elif name == "macos":
        version = result.stdout.strip()
    else:
        return None
    return version if re.fullmatch(latest.VERSION_PATTERN, version) else None


def orch_latest(hub: Path, *, run: Run = selfcheck.run_command) -> str | None:
    """Return the highest v<x.y.z> tag of the hub clone without the v, or None."""
    try:
        result = run([GIT, "-C", str(hub), "tag", "--list", "v*"], VERSION_TIMEOUT_SEC)
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    versions = [
        match.group(1)
        for line in result.stdout.splitlines()
        if (match := re.fullmatch(TAG_PATTERN, line.strip()))
    ]
    return max(
        versions, key=lambda value: tuple(map(int, value.split("."))), default=None
    )


def agent_report(
    agent: str,
    *,
    installed: str | None,
    baseline: str,
    blocking: Sequence[str],
    warn: Sequence[str],
    state: Mapping[str, object] | None,
    record: LatestRecord | None,
    default_model: str | None = None,
) -> dict[str, object]:
    """Return one agent's object of schemas/cli/versions.v1.json."""
    state = state or {}
    current_entry = state.get("current")
    previous_entry = state.get("previous")
    current = current_entry["version"] if isinstance(current_entry, Mapping) else None
    previous = (
        previous_entry["version"] if isinstance(previous_entry, Mapping) else None
    )
    newest = record.agents[agent].latest if record is not None else None
    candidate_entry = state.get("candidate")
    candidate = None
    if isinstance(candidate_entry, Mapping):
        candidate = {
            "version": candidate_entry["version"],
            "status": candidate_entry["status"],
            "at": candidate_entry["at"],
            "checks": [
                {key: check[key] for key in ("id", "result", "detail")}
                for check in candidate_entry["checks"]
            ],
            "warnings": list(candidate_entry["warnings"]),
        }
    models = None
    if agent == "codex":
        model = (
            current_entry.get("model") if isinstance(current_entry, Mapping) else None
        )
        if isinstance(model, str) and model:
            models = {"model": model, "source": "record"}
        elif isinstance(default_model, str):
            models = {"model": default_model, "source": "default"}
    elif agent == "claude" and isinstance(current_entry, Mapping):
        recorded_models = current_entry.get("models")
        if isinstance(recorded_models, dict):
            models = dict(recorded_models)
    return {
        "installed": installed,
        "baseline": baseline,
        "current": current,
        "previous": previous,
        "latest": newest,
        "latest_checked_at": record.checked_at if record is not None else None,
        "behind": (
            latest.compare_versions(newest, current) > 0
            if current is not None and newest is not None
            else None
        ),
        "candidate": candidate,
        "models": models,
        "remeasure": {"blocking": list(blocking), "warn": list(warn)},
        "commands": {
            "apply": (
                f"orch selfcheck --apply {agent}"
                if candidate is not None and candidate["status"] == "ready"
                else None
            ),
            "rollback": (
                f"orch selfcheck --rollback {agent}" if previous is not None else None
            ),
        },
    }


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
    try:
        pins = {
            "claude": selfcheck.load_pins(hub).claude,
            "codex": selfcheck_codex.load_codex_pins(hub),
        }
        baselines = load_tool_baselines(hub)
    except selfcheck.SelfcheckError as exc:
        raise VersionsError(str(exc)) from exc
    try:
        states = selfcheck.load_agents_state(selfcheck.agents_state_path(orch_home))[
            "agents"
        ]
    except selfcheck.SelfcheckError:
        states = {}
    record = latest.read_record(latest.latest_path(orch_home))
    paths = {
        "claude": claude_path,
        "codex": codex_path,
        "tmux": tmux_path,
        "macos": None,
    }
    agents = {
        agent: agent_report(
            agent,
            installed=installed_version(agent, paths[agent], run=run),
            baseline=pins[agent].version,
            blocking=pins[agent].blocking,
            warn=pins[agent].warn,
            state=states.get(agent),
            record=record,
            default_model=pins["codex"].default_model if agent == "codex" else None,
        )
        for agent in AGENTS
    }
    tools = {}
    for name in TOOLS:
        installed = installed_version(name, paths[name], run=run)
        tools[name] = {
            "installed": installed,
            "baseline": baselines[name],
            "differs": installed != baselines[name] if installed is not None else None,
        }
    return {
        "agents": agents,
        "orch": {"installed": orch_version, "latest": orch_latest(hub, run=run)},
        "tools": tools,
    }
