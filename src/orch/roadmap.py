"""orch roadmap: validate docs/roadmap.toml and report progress (4.13)."""

import re
import sqlite3
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from orch import store
from orch.models import Task, TaskState

ROADMAP_PATH: Final = "docs/roadmap.toml"
ROADMAP_FORMAT: Final = 1
ID_PATTERN: Final = r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}"
STATUSES: Final = ("done", "current", "planned")
GIT: Final = "/usr/bin/git"

Run = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True, slots=True)
class Problem:
    """One validation problem: where it is and a fixed reason word."""

    where: str
    reason: str


@dataclass(frozen=True, slots=True)
class Milestone:
    id: str
    title: str


@dataclass(frozen=True, slots=True)
class Phase:
    id: str
    title: str
    status: str
    exit: tuple[str, ...]
    milestones: tuple[Milestone, ...]


@dataclass(frozen=True, slots=True)
class Roadmap:
    phases: tuple[Phase, ...]


class RoadmapError(Exception):
    """The roadmap cannot be read or is invalid."""

    def __init__(
        self, error: str, message: str, problems: tuple[Problem, ...] = ()
    ) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.problems = problems


def parse_roadmap(text: str) -> Roadmap:
    """Parse and validate the roadmap, reporting every problem at once."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RoadmapError(
            "invalid_roadmap", "Invalid roadmap TOML", (Problem("", "syntax"),)
        ) from exc

    problems: list[Problem] = []
    seen: set[str] = set()
    current_seen = False

    def unknown(table: dict[str, object], keys: set[str], prefix: str) -> None:
        for key in table:
            if key not in keys:
                problems.append(Problem(f"{prefix}{key}", "unknown_key"))

    def field(table: dict[str, object], key: str, prefix: str) -> None:
        nonlocal current_seen
        reason = None
        value = table.get(key)
        if key not in table:
            reason = "missing"
        elif key == "exit":
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                reason = "type"
            elif not value or any(not item.strip() for item in value):
                reason = "empty"
        elif not isinstance(value, str):
            reason = "type"
        elif key == "id":
            if re.fullmatch(ID_PATTERN, value) is None:
                reason = "id_format"
            elif value in seen:
                reason = "duplicate_id"
            else:
                seen.add(value)
        elif key == "title" and not value.strip():
            reason = "empty"
        elif key == "status":
            if value not in STATUSES:
                reason = "status"
            elif value == "current":
                if current_seen:
                    reason = "multiple_current"
                current_seen = True
        if reason is not None:
            problems.append(Problem(f"{prefix}{key}", reason))

    def tables(value: object) -> bool:
        return isinstance(value, list) and all(isinstance(item, dict) for item in value)

    if type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
        problems.append(Problem("schema_version", "schema_version"))
    unknown(data, {"schema_version", "phase"}, "")
    phases = data.get("phase")
    if "phase" not in data:
        problems.append(Problem("phase", "missing"))
    elif not tables(phases):
        problems.append(Problem("phase", "type"))
    elif not phases:
        problems.append(Problem("phase", "empty"))
    else:
        for i, phase in enumerate(phases):
            prefix = f"phase[{i}]."
            unknown(phase, {"id", "title", "status", "exit", "milestone"}, prefix)
            for key in ("id", "title", "status", "exit"):
                field(phase, key, prefix)
            milestones = phase.get("milestone", [])
            if not tables(milestones):
                problems.append(Problem(f"{prefix}milestone", "type"))
                continue
            for j, milestone in enumerate(milestones):
                milestone_prefix = f"{prefix}milestone[{j}]."
                unknown(milestone, {"id", "title"}, milestone_prefix)
                for key in ("id", "title"):
                    field(milestone, key, milestone_prefix)
    if problems:
        raise RoadmapError("invalid_roadmap", "Invalid roadmap", tuple(problems))
    return Roadmap(
        tuple(
            Phase(
                phase["id"],
                phase["title"],
                phase["status"],
                tuple(phase["exit"]),
                tuple(
                    Milestone(m["id"], m["title"]) for m in phase.get("milestone", [])
                ),
            )
            for phase in phases
        )
    )


def read_roadmap(
    repo: Path, default_branch: str, *, run: Run = subprocess.run
) -> tuple[str, str, str]:
    """Return (ref, commit, text) of the roadmap on origin/<default branch>."""
    ref = f"origin/{default_branch}"
    resolved = run(
        [
            GIT,
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/remotes/{ref}^{{commit}}",
        ],
        capture_output=True,
        text=True,
    )
    if resolved.returncode != 0:
        raise RoadmapError("no_ref", f"Remote tracking ref is missing: {ref}")
    commit = resolved.stdout.strip()
    result = run(
        [GIT, "-C", str(repo), "show", f"{commit}:{ROADMAP_PATH}"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RoadmapError("no_roadmap", f"Roadmap is missing at {ref}:{ROADMAP_PATH}")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RoadmapError(
            "invalid_roadmap", "Roadmap is not UTF-8", (Problem("", "syntax"),)
        ) from exc
    return ref, commit, text


def chains(tasks: Sequence[Task]) -> list[tuple[Task, ...]]:
    """Group tasks linked by replaces, oldest first."""
    ordered = sorted(tasks, key=lambda task: (task.created_at, task.id))
    by_id = {task.id: task for task in ordered}
    successors: dict[str, Task] = {}
    for task in ordered:
        if task.replaces is not None:
            successors.setdefault(task.replaces, task)
    visited: set[str] = set()
    result: list[tuple[Task, ...]] = []
    for task in ordered:
        if task.replaces in by_id or task.id in visited:
            continue
        chain: list[Task] = []
        while task.id not in visited:
            chain.append(task)
            visited.add(task.id)
            successor = successors.get(task.id)
            if successor is None:
                break
            task = successor
        result.append(tuple(chain))
    result.extend((task,) for task in ordered if task.id not in visited)
    return sorted(result, key=lambda chain: (chain[0].created_at, chain[0].id))


def progress_report(roadmap: Roadmap, tasks: Sequence[Task]) -> dict[str, object]:
    """Return phases with per-milestone progress, unknown milestones and the rest."""
    grouped: dict[str, list[dict[str, object]]] = {}
    no_milestone = 0
    for chain in chains(tasks):
        last = chain[-1]
        if last.milestone is None:
            no_milestone += 1
            continue
        grouped.setdefault(last.milestone, []).append(
            {"tasks": [task.id for task in chain], "state": last.state.value}
        )

    def milestone_report(milestone: Milestone) -> dict[str, object]:
        entries = grouped.pop(milestone.id, [])
        total = sum(entry["state"] != TaskState.DISCARDED.value for entry in entries)
        done = sum(entry["state"] == TaskState.CLOSED.value for entry in entries)
        return {
            "id": milestone.id,
            "title": milestone.title,
            "total": total,
            "done": done,
            "progress": round(done / total, 4) if total else None,
            "chains": entries,
        }

    phases = [
        {
            "id": phase.id,
            "title": phase.title,
            "status": phase.status,
            "exit": list(phase.exit),
            "milestones": [milestone_report(m) for m in phase.milestones],
        }
        for phase in roadmap.phases
    ]
    return {
        "phases": phases,
        "unknown_milestones": [
            {"milestone": key, "chains": grouped[key]} for key in sorted(grouped)
        ],
        "no_milestone": no_milestone,
    }


def build_report(
    conn: sqlite3.Connection,
    *,
    target: str,
    repo: Path,
    default_branch: str,
    run: Run = subprocess.run,
) -> dict[str, object]:
    """Return the orch roadmap report of one target."""
    ref, commit, text = read_roadmap(repo, default_branch, run=run)
    roadmap = parse_roadmap(text)
    tasks = store.list_tasks(conn, target=target)
    return {
        "target": target,
        "ref": ref,
        "commit": commit,
        **progress_report(roadmap, tasks),
    }
