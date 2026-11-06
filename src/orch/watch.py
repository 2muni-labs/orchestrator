"""orch watch: a foreground status board that redraws on an interval."""

import json
import os
import re
import sys
import tempfile
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
    """Find the latest numbered run log for exactly this task."""
    pattern = re.compile(rf"{re.escape(task_id)}\.(\d+)\.log")
    latest: Path | None = None
    highest = -1
    try:
        with os.scandir(logs) as entries:
            for entry in entries:
                if (match := pattern.fullmatch(entry.name)) and entry.is_file():
                    number = int(match[1])
                    if number > highest:
                        highest = number
                        latest = logs / entry.name
    except FileNotFoundError:
        pass
    return latest


def load_panel_map(path: Path) -> dict[str, str]:
    """Load valid task-to-pane entries, tolerating absent or damaged state."""
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    if not isinstance(mapping, dict):
        return {}
    return {
        task_id: pane
        for task_id, pane in mapping.items()
        if isinstance(task_id, str) and isinstance(pane, str)
    }


def save_panel_map(path: Path, mapping: Mapping[str, str]) -> None:
    """Replace the panel state atomically with a complete JSON object."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(dict(mapping), stream)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sync_panels(
    terminal: Panels,
    *,
    running: Mapping[str, Path],
    state: Path,
    anchor: str | None = None,
) -> dict[str, str]:
    """Keep one live log panel per running task and persist partial progress."""
    mapping = load_panel_map(state)
    live = terminal.live_panes()
    mapping = {task_id: pane for task_id, pane in mapping.items() if pane in live}
    for task_id in list(mapping):
        if task_id not in running:
            pane = mapping.pop(task_id)
            try:
                terminal.close_pane(pane)
            except Exception:
                pass

    first_error: Exception | None = None
    for task_id in sorted(running):
        if task_id in mapping:
            continue
        try:
            mapping[task_id] = terminal.terminal_split(
                ["tail", "-F", str(running[task_id])], target=anchor, vertical=True
            )
        except Exception as exc:
            if first_error is None:
                first_error = exc

    save_panel_map(state, mapping)
    if first_error is not None:
        raise first_error
    return mapping


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
    try:
        output = sys.stdout if out is None else out
        while True:
            failed = False
            tick_error: str | None = None
            if tick is not None:
                try:
                    tick()
                except Exception as exc:
                    tick_error = f"패널 갱신 실패: {exc}\n"
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
            if tick_error is not None:
                output.write(tick_error)
            output.flush()

            if once:
                return 70 if failed else 0
            sleep(interval_sec)
    except KeyboardInterrupt:
        return 0
