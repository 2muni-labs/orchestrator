"""ORCH_HOME paths, target registration and the ~/.orch/config file."""

import json
import os
import re
import tempfile
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from orch.redact import RedactionError, redact

RESERVED_TARGET_NAMES = frozenset({"bin", "secrets", "config", "state.db"})
MARKER_FILE = ".orch-target.toml"
NOTIFICATION_TOPIC_PATTERN = r"[A-Za-z0-9_-]{20,64}"


class ConfigError(Exception):
    """A configuration file or value is invalid."""


class TargetNameError(ConfigError):
    """A target name is invalid or collides with another target."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TargetPaths:
    """Runtime paths of one target."""

    root: Path
    tasks: Path
    results: Path
    reviews: Path
    logs: Path
    worktrees: Path
    todo: Path

    def review_path(self, task_id: str, attempt: int | None = None) -> Path:
        """Return the current review, or the one archived for an attempt."""
        validate_task_id(task_id)
        if attempt is None:
            return self.reviews / f"{task_id}.json"
        _positive_integer(attempt)
        return self.reviews / "attempts" / f"{task_id}.{attempt}.json"

    def triage_path(self, task_id: str, attempt: int | None = None) -> Path:
        """Return the current triage file, or the one archived for an attempt."""
        validate_task_id(task_id)
        if attempt is None:
            return self.reviews / f"{task_id}.triage.md"
        _positive_integer(attempt)
        return self.reviews / "attempts" / f"{task_id}.{attempt}.triage.md"


@dataclass(frozen=True, slots=True, kw_only=True)
class Target:
    """One entry of targets.toml."""

    name: str
    path: Path
    github: str
    default_branch: str
    setup_command: str
    test_command: str
    lint_command: str
    shared_paths: tuple[str, ...] = ()
    safety_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Limits:
    """Concurrent run limits."""

    total: int = 3
    lane_b: int = 2
    lane_a: int = 2


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """Contents of ~/.orch/config."""

    terminal_backend: str = "tmux"
    retention_days: int = 30
    reservation_ttl_min: int = 10
    limits: Limits = field(default_factory=Limits)
    notify_method: str = "none"
    notify_topic: str | None = field(default=None, repr=False)
    codex_path: Path | None = None
    claude_path: Path | None = None
    usage_limit_patterns: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def _absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError("Expected an absolute path")
    try:
        path = Path(value).expanduser()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError("Cannot expand path") from exc
    if not path.is_absolute():
        raise ConfigError("Path must be absolute")
    return path


def orch_home(env: Mapping[str, str] | None = None) -> Path:
    """Return the base directory for all runtime data."""
    source = os.environ if env is None else env
    return _absolute_path(source.get("ORCH_HOME") or "~/.orch")


def state_db_path(home: Path) -> Path:
    """Return the path of the database shared by all targets."""
    return home / "state.db"


def config_path(home: Path) -> Path:
    """Return the path of the TOML configuration file."""
    return home / "config"


def notify_state_path(home: Path) -> Path:
    """Return the path of the notification deduplication state."""
    return home / "notify.json"


def validate_target_name(name: str) -> None:
    """Raise TargetNameError if the name is reserved or escapes ORCH_HOME."""
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", name
    ):
        raise TargetNameError("Invalid target name")
    if name.casefold() in RESERVED_TARGET_NAMES:
        raise TargetNameError("Reserved target name")


def validate_task_id(value: object) -> None:
    """Raise ConfigError if the task id is unsafe for filenames or branches."""
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value)
        or ".." in value
        or value.endswith((".", ".lock"))
    ):
        raise ConfigError("Invalid task id")


def target_paths(home: Path, name: str) -> TargetPaths:
    """Compute a target's paths without touching the filesystem."""
    validate_target_name(name)
    root = home / name
    return TargetPaths(
        root=root,
        tasks=root / "tasks",
        results=root / "results",
        reviews=root / "reviews",
        logs=root / "logs",
        worktrees=root / "worktrees",
        todo=root / "TODO.md",
    )


def _validate_github(github: object) -> None:
    if not isinstance(github, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github
    ):
        raise ConfigError("Invalid GitHub repository")


def _check_marker(marker: Path, github: str) -> None:
    try:
        data = tomllib.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TargetNameError("Cannot read target marker") from exc
    existing = data.get("github")
    if not isinstance(existing, str) or existing.casefold() != github.casefold():
        raise TargetNameError("Target marker belongs to another repository")


def ensure_target(home: Path, name: str, github: str) -> TargetPaths:
    """Create a target's directories and marker, or raise on a collision."""
    validate_target_name(name)
    _validate_github(github)
    paths = target_paths(home, name)
    try:
        home.mkdir(parents=True, exist_ok=True)
        for entry in home.iterdir():
            if entry.name != name and entry.name.casefold() == name.casefold():
                raise TargetNameError("Target name differs only by case")
        if paths.root.is_symlink() or (paths.root.exists() and not paths.root.is_dir()):
            raise TargetNameError("Target root must be a directory, not a symlink")
        paths.root.mkdir(exist_ok=True)
        marker = paths.root / MARKER_FILE
        if marker.exists():
            _check_marker(marker, github)
        else:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=paths.root, delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(f'github = "{github}"\n')
                try:
                    os.link(temporary, marker)
                except FileExistsError:
                    temporary.unlink(missing_ok=True)
                    _check_marker(marker, github)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        for directory in (
            paths.tasks,
            paths.results,
            paths.reviews,
            paths.logs,
            paths.worktrees,
        ):
            directory.mkdir(exist_ok=True)
    except (OSError, ValueError) as exc:
        raise ConfigError("Cannot create target directories") from exc
    return paths


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError("Cannot read configuration TOML") from exc


def _table(value: object, allowed: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or value.keys() - allowed:
        raise ConfigError("Invalid table or unknown configuration key")
    return value


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def load_targets(path: Path) -> dict[str, Target]:
    """Load and validate targets.toml. A missing file means no targets."""
    data = _table(_read_toml(path), {"targets"})
    entries = data.get("targets", {})
    if not isinstance(entries, dict):
        raise ConfigError("targets must be a table")
    targets = {}
    seen = set()
    for name, entry in entries.items():
        validate_target_name(name)
        if name.casefold() in seen:
            raise TargetNameError("Target names differ only by case")
        seen.add(name.casefold())
        targets[name] = _target_from_entry(name, entry)
    return targets


def _target_path_list(value: object, *, field: str, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{field} must be a list")
    for path in value:
        if not _nonempty_string(path):
            raise ConfigError(f"Invalid {label} path")
        plain = path[:-3] if path.endswith("/**") else path
        if (
            path.startswith("/")
            or ".." in path.split("/")
            or "*" in plain
            or "?" in plain
        ):
            raise ConfigError(f"Invalid {label} path")
    return tuple(value)


def _target_from_entry(name: str, entry: object) -> Target:
    """Validate a target entry using the same rules for loading and registration."""
    required = {
        "path",
        "github",
        "default_branch",
        "setup_command",
        "test_command",
        "lint_command",
    }
    entry = _table(entry, required | {"shared_paths", "safety_paths"})
    if not required <= entry.keys() or any(
        not _nonempty_string(entry[key]) for key in required
    ):
        raise ConfigError("Missing or invalid target field")
    target_path = _absolute_path(entry["path"])
    _validate_github(entry["github"])
    shared = _target_path_list(
        entry.get("shared_paths", []), field="shared_paths", label="shared"
    )
    safety = _target_path_list(
        entry.get("safety_paths", []), field="safety_paths", label="safety"
    )
    return Target(
        name=name,
        path=target_path,
        github=entry["github"],
        default_branch=entry["default_branch"],
        setup_command=entry["setup_command"],
        test_command=entry["test_command"],
        lint_command=entry["lint_command"],
        shared_paths=shared,
        safety_paths=safety,
    )


def home_relative(path: Path, home: Path) -> str:
    """Return an existing directory under home in the form ~/relative."""
    try:
        resolved_path = path.resolve()
        resolved_home = home.resolve()
        if not resolved_path.is_dir() or resolved_path == resolved_home:
            raise ConfigError("Target path must be a directory below home")
        relative = resolved_path.relative_to(resolved_home)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError("Cannot resolve target path below home") from exc
    return "~/" + relative.as_posix()


def add_target(
    targets_file: Path,
    name: str,
    *,
    path: Path,
    github: str,
    default_branch: str,
    setup_command: str,
    test_command: str,
    lint_command: str,
    shared_paths: Iterable[str] = (),
    home: Path,
    orch_home: Path,
) -> Target:
    """Register a target in targets.toml and create its runtime directories."""
    validate_target_name(name)
    targets = load_targets(targets_file)
    if any(existing.casefold() == name.casefold() for existing in targets):
        raise TargetNameError("Target is already registered")
    stored_path = home_relative(path, home)
    if isinstance(shared_paths, (str, bytes)):
        raise ConfigError("shared_paths must be an iterable of paths")
    entry = {
        "path": stored_path,
        "github": github,
        "default_branch": default_branch,
        "setup_command": setup_command,
        "test_command": test_command,
        "lint_command": lint_command,
        "shared_paths": list(shared_paths),
    }
    target = _target_from_entry(name, entry)
    for value in (
        target.github,
        target.default_branch,
        target.setup_command,
        target.test_command,
        target.lint_command,
        *target.shared_paths,
    ):
        try:
            redacted = redact(value, home=home, orch_home=orch_home)
        except RedactionError as exc:
            raise ConfigError("Target field contains private information") from exc
        if redacted != value:
            raise ConfigError("Target field contains a personal path")
    ensure_target(orch_home, name, github)

    temporary = None
    try:
        try:
            original = targets_file.read_bytes()
        except FileNotFoundError:
            original = b""
        newline = "\r\n" if b"\r\n" in original else "\n"
        prefix = original.decode("utf-8")
        if prefix and not prefix.endswith("\n"):
            prefix += newline
        lines = [f'[targets."{name}"]']
        lines.extend(
            f"{key} = {json.dumps(value, ensure_ascii=False)}"
            for key, value in entry.items()
        )
        text = prefix + newline.join(lines) + newline
        try:
            tomllib.loads(text)
        except ValueError as exc:
            raise ConfigError("Cannot serialize target configuration") from exc
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=targets_file.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text.encode("utf-8"))
        os.replace(temporary, targets_file)
    except (OSError, UnicodeError) as exc:
        raise ConfigError("Cannot write targets file") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                raise ConfigError("Cannot remove temporary targets file") from exc
    return target


def _positive_integer(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ConfigError("Expected a positive integer")
    return value


def load_config(home: Path) -> Config:
    """Load and validate the configuration file. A missing file means defaults."""
    data = _table(
        _read_toml(config_path(home)),
        {
            "terminal_backend",
            "retention_days",
            "reservation_ttl_min",
            "limits",
            "notify",
            "agents",
            "usage_limit_patterns",
        },
    )
    backend = data.get("terminal_backend", "tmux")
    if backend != "tmux":
        raise ConfigError("Unsupported terminal backend")
    limits_data = _table(data.get("limits", {}), {"total", "lane_b", "lane_a"})
    limits = Limits(
        **{
            key: _positive_integer(limits_data.get(key, default))
            for key, default in (("total", 3), ("lane_b", 2), ("lane_a", 2))
        }
    )
    if limits.lane_b > limits.total:
        raise ConfigError("lane_b exceeds total")
    notify = _table(data.get("notify", {}), {"method", "topic"})
    method = notify.get("method", "none")
    if method not in ("none", "ntfy"):
        raise ConfigError("Unsupported notification method")
    if method == "none" and "topic" in notify:
        raise ConfigError("Notification topic requires ntfy")
    if method == "ntfy" and not _nonempty_string(notify.get("topic")):
        raise ConfigError("ntfy requires a topic")
    if method == "ntfy" and not re.fullmatch(
        NOTIFICATION_TOPIC_PATTERN, notify["topic"]
    ):
        raise ConfigError("Invalid notification topic")
    agents = _table(data.get("agents", {}), {"codex", "claude"})
    agent_paths = {key: _absolute_path(value) for key, value in agents.items()}
    patterns_data = _table(data.get("usage_limit_patterns", {}), {"codex", "claude"})
    defaults = (
        "usage limit",
        "rate limit",
        "hit your .*limit",
        "limit reached",
        "quota",
        "out of credits",
    )
    patterns = {"codex": defaults, "claude": defaults}
    for provider, values in patterns_data.items():
        if not isinstance(values, list) or not values:
            raise ConfigError("Usage patterns must be a nonempty list")
        for pattern in values:
            if not _nonempty_string(pattern):
                raise ConfigError("Usage patterns must be nonempty strings")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError("Invalid usage pattern") from exc
        patterns[provider] = tuple(values)
    return Config(
        terminal_backend=backend,
        retention_days=_positive_integer(data.get("retention_days", 30)),
        reservation_ttl_min=_positive_integer(data.get("reservation_ttl_min", 10)),
        limits=limits,
        notify_method=method,
        notify_topic=notify.get("topic"),
        codex_path=agent_paths.get("codex"),
        claude_path=agent_paths.get("claude"),
        usage_limit_patterns=patterns,
    )
