"""launchd agents: install, uninstall and restore of missing main sessions."""

import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from orch.terminal import TmuxTerminal

LABELS: Final = ("orch.session", "orch.doctor", "orch.gc")
LAUNCHCTL: Final = "/bin/launchctl"
PLUTIL: Final = "/usr/bin/plutil"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
TerminalFactory = Callable[[str, Path], TmuxTerminal]


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceReport:
    """Labels handled by install or uninstall. ok is true when nothing failed."""

    ok: bool
    done: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class RestoreReport:
    """What one restore pass did for each target that has a terminal record."""

    opened: tuple[str, ...] = ()
    already_open: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    raise NotImplementedError


def agent_path(*, orch: Path, tmux: Path) -> str:
    raise NotImplementedError


def agent_plist(
    label: str,
    *,
    orch: Path,
    tmux: Path,
    hub: Path,
    orch_home: Path,
    custom_orch_home: bool = False,
) -> dict[str, object]:
    raise NotImplementedError


def install(
    *,
    orch: Path,
    tmux: Path,
    hub: Path,
    home: Path,
    orch_home: Path,
    uid: int,
    custom_orch_home: bool = False,
    run: Runner = run_command,
) -> ServiceReport:
    raise NotImplementedError


def uninstall(*, home: Path, uid: int, run: Runner = run_command) -> ServiceReport:
    raise NotImplementedError


def restore(
    targets: Iterable[str],
    *,
    orch: Path,
    orch_home: Path,
    run: Runner = run_command,
    terminal: TerminalFactory = TmuxTerminal,
) -> RestoreReport:
    raise NotImplementedError
