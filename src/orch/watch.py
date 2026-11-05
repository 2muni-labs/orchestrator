"""orch watch: a foreground status board that redraws on an interval."""

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
    raise NotImplementedError
