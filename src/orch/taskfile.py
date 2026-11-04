"""Parse and validate task spec front matter."""

import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from orch.config import TargetNameError, validate_target_name
from orch.models import Effort, Grade, Lane


class TaskFileError(Exception):
    """A task spec is invalid."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskSpec:
    """A parsed task spec."""

    id: str
    title: str
    target: str
    grade: Grade
    lane: Lane
    model: str
    timeout_min: int
    modify: tuple[str, ...]
    read: tuple[str, ...]
    verify: str
    depends_on: tuple[str, ...]
    body: str
    effort: Effort | None = None
    tests: str | None = None
    review: bool = False


def _validate_id(value: object) -> None:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value)
        or ".." in value
        or value.endswith((".", ".lock"))
    ):
        raise TaskFileError("Invalid task id")


def _paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise TaskFileError("Paths must be a nonempty list")
    for path in value:
        if (
            not isinstance(path, str)
            or not path
            or path.startswith(("/", "~"))
            or "\\" in path
            or any(part in ("", "..") for part in path.split("/"))
        ):
            raise TaskFileError("Invalid repository path")
    return tuple(value)


def parse_task(text: str) -> TaskSpec:
    """Parse a task spec and validate everything that needs no target settings."""
    if not isinstance(text, str):
        raise TaskFileError("Task must be text")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "+++":
        raise TaskFileError("Missing front matter")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "+++"), None)
    if end is None:
        raise TaskFileError("Unclosed front matter")
    try:
        data = tomllib.loads("".join(lines[1:end]))
    except ValueError as exc:
        raise TaskFileError("Invalid front matter TOML") from exc
    required = {
        "id",
        "target",
        "grade",
        "lane",
        "model",
        "timeout_min",
        "modify",
        "read",
        "verify",
        "depends_on",
    }
    optional = {"effort", "tests", "review"}
    if not required <= data.keys() or data.keys() - (required | optional):
        raise TaskFileError("Missing or unknown task field")
    review = data.get("review", False)
    if type(review) is not bool:
        raise TaskFileError("review must be a boolean")
    _validate_id(data["id"])
    try:
        validate_target_name(data["target"])
    except TargetNameError as exc:
        raise TaskFileError("Invalid target") from exc
    try:
        grade = Grade(data["grade"])
        lane = Lane(data["lane"])
        effort = Effort(data["effort"]) if "effort" in data else None
    except (ValueError, TypeError) as exc:
        raise TaskFileError("Invalid grade, lane or effort") from exc
    timeout = data["timeout_min"]
    if type(timeout) is not int or timeout <= 0:
        raise TaskFileError("Timeout must be a positive integer")
    verify = data["verify"]
    if not isinstance(verify, str) or not verify.strip():
        raise TaskFileError("verify must be a nonblank string")
    modify = _paths(data["modify"])
    read = _paths(data["read"])
    dependencies = data["depends_on"]
    if not isinstance(dependencies, list):
        raise TaskFileError("depends_on must be a list")
    for dependency in dependencies:
        _validate_id(dependency)
        if dependency == data["id"]:
            raise TaskFileError("Task cannot depend on itself")
    if "tests" in data and (data["tests"] != "none" or grade != Grade.L1):
        raise TaskFileError("tests may only be none on L1")
    codex_efforts = {
        Grade.L1: Effort.LOW,
        Grade.L2: Effort.MEDIUM,
        Grade.L3: Effort.HIGH,
        Grade.L3R: Effort.XHIGH,
    }
    claude = {
        Grade.L1: ("claude-haiku-5-5", None),
        Grade.L2: ("claude-sonnet-5-5", None),
        Grade.L3: ("claude-sonnet-5-5", Effort.HIGH),
        Grade.L3R: ("claude-opus-5-5", None),
    }
    pair = (data["model"], effort)
    codex_match = pair == ("gpt-6.1-sol", codex_efforts[grade])
    claude_match = pair == claude[grade]
    if not (
        (lane in (Lane.B, Lane.D) and codex_match)
        or (lane in (Lane.C, Lane.D) and claude_match)
    ):
        raise TaskFileError("Model and effort do not match grade and lane")
    return TaskSpec(
        id=data["id"],
        target=data["target"],
        grade=grade,
        lane=lane,
        model=data["model"],
        effort=effort,
        timeout_min=timeout,
        modify=modify,
        read=read,
        verify=verify,
        depends_on=tuple(dependencies),
        body="".join(lines[end + 1 :]),
        tests=data.get("tests"),
        review=review,
    )


def load_task(path: Path) -> TaskSpec:
    """Read a task spec file whose stem must equal the spec's id."""
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            text = stream.read()
    except (OSError, ValueError) as exc:
        raise TaskFileError("Cannot read task file") from exc
    spec = parse_task(text)
    if path.stem != spec.id:
        raise TaskFileError("Task filename must match its id")
    return spec


def check_modify(
    modify: Iterable[str],
    shared_paths: Iterable[str],
    test_paths: Iterable[str] = ("tests/",),
) -> None:
    """Raise TaskFileError if a modify glob can reach a shared or test path."""
    protected = [
        path.casefold().removesuffix("/**").removesuffix("/")
        for path in (*shared_paths, *test_paths)
    ]
    for original in modify:
        glob = original.casefold()
        pieces = glob.split("/")
        for name in protected:
            direct = glob == name or glob.startswith(name + "/")
            broad = len(pieces) > 1 and any(c in pieces[0] for c in "*?")
            single = False
            if len(pieces) == 1 and "/" not in name:
                pattern = re.escape(glob).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
                single = re.fullmatch(pattern, name) is not None
            if direct or broad or single:
                raise TaskFileError(f"modify glob reaches a protected path: {original}")
