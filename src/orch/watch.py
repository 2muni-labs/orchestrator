"""orch watch: a foreground status board that redraws on an interval."""

import sys
import time
from collections.abc import Callable
from typing import TextIO

CLEAR_SCREEN = "\x1b[2J\x1b[H"


def watch(
    frame: Callable[[], str],
    *,
    interval_sec: int = 2,
    once: bool = False,
    out: TextIO | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Draw frames until interrupted and return the exit code."""
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
