"""ORCH_HOME paths, target registration and the ~/.orch/config file."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

RESERVED_TARGET_NAMES = frozenset({"bin", "secrets", "config", "state.db"})
MARKER_FILE = ".orch-target.toml"


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


def orch_home(env: Mapping[str, str] | None = None) -> Path:
    """Return the base directory for all runtime data."""
    raise NotImplementedError


def state_db_path(home: Path) -> Path:
    """Return the path of the database shared by all targets."""
    raise NotImplementedError


def config_path(home: Path) -> Path:
    """Return the path of the TOML configuration file."""
    raise NotImplementedError


def validate_target_name(name: str) -> None:
    """Raise TargetNameError if the name is reserved or escapes ORCH_HOME."""
    raise NotImplementedError


def target_paths(home: Path, name: str) -> TargetPaths:
    """Compute a target's paths without touching the filesystem."""
    raise NotImplementedError


def ensure_target(home: Path, name: str, github: str) -> TargetPaths:
    """Create a target's directories and marker, or raise on a collision."""
    raise NotImplementedError


def load_targets(path: Path) -> dict[str, Target]:
    """Load and validate targets.toml. A missing file means no targets."""
    raise NotImplementedError


def load_config(home: Path) -> Config:
    """Load and validate the configuration file. A missing file means defaults."""
    raise NotImplementedError
