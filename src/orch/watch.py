"""orch watch: a foreground status board that redraws on an interval."""

import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, TextIO

CLEAR_SCREEN = "\x1b[2J\x1b[H"


class Panels(Protocol):
    """The part of the terminal adapter that log panels use."""

    def terminal_split(
        self,
        command: Sequence[str],
        *,
        target: str | None = None,
        vertical: bool = False,
        percent: int | None = None,
    ) -> str: ...

    def close_pane(self, pane: str) -> None: ...

    def live_panes(self) -> frozenset[str]: ...


def current_log(logs: Path, task_id: str) -> Path | None:
    raise NotImplementedError


def load_panel_map(path: Path) -> dict[str, str]:
    raise NotImplementedError


def save_panel_map(path: Path, mapping: Mapping[str, str]) -> None:
    raise NotImplementedError


def sync_panels(
    terminal: Panels,
    *,
    running: Mapping[str, Path],
    state: Path,
    anchor: str | None = None,
) -> dict[str, str]:
    raise NotImplementedError


def watch(
    frame: Callable[[], str],
    *,
    interval_sec: int = 2,
    once: bool = False,
    out: TextIO | None = None,
    sleep: Callable[[float], None] = time.sleep,
    tick: Callable[[], None] | None = None,
) -> int:
    """Draw frames until interrupted and return the exit code."""
    if tick is not None:
        raise NotImplementedError
    try:
        output = sys.stdout if out is None else out
        while True:
            failed = False
            try:
                content = frame()
            except Exception as exc:
                content = f"조회 실패: {exc}"
                failed = True

            if output.isatty():
                output.write(CLEAR_SCREEN)
            output.write(content)
            if not content.endswith("\n"):
                output.write("\n")
            output.flush()

            if once:
                return 70 if failed else 0
            sleep(interval_sec)
    except KeyboardInterrupt:
        return 0
