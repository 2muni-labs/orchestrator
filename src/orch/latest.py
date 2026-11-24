"""Daily latest version check of the agents and the doctor agent_latest item (4.13)."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from orch.models import CheckLevel

LATEST_FORMAT: Final = 1
CHECK_INTERVAL: Final = timedelta(hours=24)
FETCH_TIMEOUT_SEC: Final = 10
BODY_LIMIT: Final = 65536
AGENTS: Final = ("claude", "codex")
VERSION_PATTERN: Final = r"[0-9]+(\.[0-9]+){1,3}"


class LatestError(Exception):
    """config/pins.toml cannot be read or a latest_url is missing or not https."""


@dataclass(frozen=True, slots=True)
class AgentLatest:
    """The latest version of one agent, or why it is unknown."""

    latest: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class LatestRecord:
    """The contents of ~/.orch/latest.json."""

    checked_at: str
    agents: Mapping[str, AgentLatest]


Fetch = Callable[[str, float], tuple[int, bytes]]


def http_fetch(url: str, timeout: float) -> tuple[int, bytes]:
    """GET url and return its status and at most BODY_LIMIT + 1 body bytes."""
    raise NotImplementedError


def latest_path(orch_home: Path) -> Path:
    """Return the path of the latest version record."""
    raise NotImplementedError


def load_urls(hub: Path) -> dict[str, str]:
    """Return each agent's latest_url from the hub's config/pins.toml."""
    raise NotImplementedError


def parse_latest(agent: str, status: int, body: bytes) -> AgentLatest:
    """Classify one fetched response."""
    raise NotImplementedError


def compare_versions(a: str, b: str) -> int:
    """Return -1, 0 or 1 comparing dotted versions numerically."""
    raise NotImplementedError


def read_record(path: Path) -> LatestRecord | None:
    """Return the stored record, or None when it is missing or malformed."""
    raise NotImplementedError


def write_record(path: Path, record: LatestRecord) -> None:
    """Write the record atomically."""
    raise NotImplementedError


def refresh(
    *, orch_home: Path, hub: Path, now: datetime, fetch: Fetch = http_fetch
) -> LatestRecord:
    """Return a record no older than CHECK_INTERVAL, fetching when needed."""
    raise NotImplementedError


def current_versions(orch_home: Path) -> dict[str, str | None]:
    """Return each agent's current version from agents.json."""
    raise NotImplementedError


def check(
    *, orch_home: Path, hub: Path, now: datetime, fetch: Fetch = http_fetch
) -> tuple[CheckLevel, str | None]:
    """Judge the doctor agent_latest item."""
    raise NotImplementedError
