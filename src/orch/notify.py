"""ntfy notifications: sending, deduplication and the config permission check."""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from orch.config import Config

TIMEOUT_SEC = 5
DEDUP_WINDOW = timedelta(minutes=30)
NTFY_URL = "https://ntfy.sh/"

Transport = Callable[[str, bytes, float], None]


class Outcome(StrEnum):
    """Result of one notification attempt."""

    SENT = "sent"
    DISABLED = "disabled"
    REFUSED_PERMISSIONS = "refused_permissions"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


def validate_event(event: str) -> None:
    """Raise ValueError if event is not a known event kind."""
    raise NotImplementedError


def dedup_key(event: str, *, target: str = "", task_id: str = "") -> str:
    """Return the deduplication key of an event."""
    raise NotImplementedError


def message(event: str, *, target: str = "", task_id: str = "") -> str:
    """Return the notification body before redaction."""
    raise NotImplementedError


def permissions_ok(path: Path) -> bool:
    """Return True if no group or other permission bit is set on path."""
    raise NotImplementedError


def load_state(path: Path) -> dict[str, str]:
    """Return the last send time per key, or an empty dict."""
    raise NotImplementedError


def save_state(path: Path, state: Mapping[str, str]) -> None:
    """Replace the state file atomically."""
    raise NotImplementedError


def should_send(state: Mapping[str, str], key: str, now: datetime) -> bool:
    """Return False if key was sent within DEDUP_WINDOW before now."""
    raise NotImplementedError


def urllib_transport(url: str, body: bytes, timeout: float) -> None:
    """POST body to url with the standard library."""
    raise NotImplementedError


def send(
    event: str,
    *,
    settings: Config,
    orch_home: Path,
    home: Path,
    now: datetime,
    target: str = "",
    task_id: str = "",
    transport: Transport = urllib_transport,
    stderr: TextIO | None = None,
    dedup: bool = True,
) -> Outcome:
    """Send one notification. Never raises except for an invalid event."""
    raise NotImplementedError
