"""tmux backend. Every call goes through an injected runner, never a shell."""

import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run_tmux(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    raise NotImplementedError


class TmuxTerminal:
    def __init__(
        self,
        session: str,
        record: Path,
        *,
        runner: Runner = run_tmux,
        tmux: str = "tmux",
    ) -> None:
        raise NotImplementedError

    def has_session(self) -> bool:
        raise NotImplementedError

    def open_session(
        self, *, cwd: Path, env: Mapping[str, str], command: Sequence[str]
    ) -> str:
        raise NotImplementedError

    def attach_argv(self, env: Mapping[str, str]) -> list[str]:
        raise NotImplementedError

    def terminal_split(
        self,
        command: Sequence[str],
        *,
        target: str | None = None,
        vertical: bool = False,
        percent: int | None = None,
    ) -> str:
        raise NotImplementedError

    def terminal_open_tab(self, command: Sequence[str], *, name: str) -> str:
        raise NotImplementedError

    def terminal_send(self, pane: str, text: str) -> None:
        raise NotImplementedError

    def close_pane(self, pane: str) -> None:
        raise NotImplementedError

    def live_panes(self) -> frozenset[str]:
        raise NotImplementedError
