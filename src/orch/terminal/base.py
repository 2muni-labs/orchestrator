"""Terminal adapter boundary and the record of panes orch created."""

from dataclasses import dataclass
from pathlib import Path


class TerminalError(Exception):
    """A terminal call failed or targeted a pane orch does not own."""


@dataclass(frozen=True)
class PaneRecord:
    session: str
    main_pane: str
    created: tuple[str, ...] = ()


def record_path(target_root: Path) -> Path:
    raise NotImplementedError


def load_record(path: Path) -> PaneRecord | None:
    raise NotImplementedError


def save_record(path: Path, record: PaneRecord) -> None:
    raise NotImplementedError
