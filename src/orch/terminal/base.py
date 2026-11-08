"""Terminal adapter boundary and the record of panes orch created."""

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path


class TerminalError(Exception):
    """A terminal call failed or targeted a pane orch does not own."""


class MainPaneStatus(StrEnum):
    """What a check of the recorded main pane found. Only ALIVE is healthy."""

    ALIVE = "alive"
    NO_RECORD = "no_record"
    NO_SESSION = "no_session"
    UNAVAILABLE = "unavailable"
    PANE_GONE = "pane_gone"
    PANE_DEAD = "pane_dead"
    NOT_CLAUDE = "not_claude"


class SessionProbe(StrEnum):
    """Whether a session exists. UNAVAILABLE means tmux could not answer."""

    PRESENT = "present"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class PaneRecord:
    session: str
    main_pane: str
    created: tuple[str, ...] = ()


def record_path(target_root: Path) -> Path:
    return target_root / "terminal.json"


def _valid_pane(pane: object) -> bool:
    return isinstance(pane, str) and re.fullmatch(r"%[0-9]+", pane) is not None


@contextmanager
def _record_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_record(path: Path) -> PaneRecord | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise TerminalError(f"Cannot read terminal record: {exc}") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("session"), str)
        or not _valid_pane(data.get("main_pane"))
        or not isinstance(data.get("created"), list)
        or not all(_valid_pane(pane) for pane in data["created"])
    ):
        raise TerminalError("Invalid terminal record")
    return PaneRecord(data["session"], data["main_pane"], tuple(data["created"]))


def save_record(path: Path, record: PaneRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = stream.name
            json.dump(asdict(record), stream)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
